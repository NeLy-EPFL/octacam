"""`octacam benchmark`: an instrumented dry run (no video kept) for the
achievable and maximum fps and the limiting stage. It opens the cameras, like
`record`.
"""

import contextlib
import json
import logging
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

import typer

from octacam.cli._common import (
    Verbose,
    command,
    open_rig,
    resolve_config_arg,
    stderr_console,
    warn_if_transcoding,
)

log = logging.getLogger("octacam")


class BenchmarkSink(StrEnum):
    config = "config"
    null = "null"


class RecordForm(StrEnum):
    display = "display"
    sensor = "sensor"


class _BenchmarkProgressBar:
    """Benchmark progress bar over diagnose's seconds budget: a ticker advances
    it in real time toward the current step's end and never moves it back.
    """

    def __init__(self) -> None:
        from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn

        self._progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TextColumn("[progress.remaining]{task.fields[left]}"),
            console=stderr_console(),
            transient=True,
        )
        self._task = self._progress.add_task(
            "Benchmarking\N{HORIZONTAL ELLIPSIS}", total=None, left=""
        )
        self._lock = threading.Lock()
        self._shown = 0.0  # budget seconds the bar shows
        self._base = 0.0  # where the bar was when the step began
        self._end = 0.0  # the step's end
        self._total = 0.0
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._ticker = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Self:
        self._progress.start()
        self._ticker.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._ticker.join(timeout=1.0)
        self._progress.update(self._task, total=1, completed=1)
        self._progress.stop()

    def update(self, p) -> None:
        """Start a step (an `octacam.diagnostics.Progress`)."""
        with self._lock:
            self._total = p.total_s
            self._base = max(self._shown, p.elapsed_s)
            self._end = max(self._base, p.elapsed_s + p.step_s)
            self._t0 = time.monotonic()
        desc = f"{p.phase} {p.detail}" if p.detail else p.phase
        self._progress.update(self._task, description=desc, total=p.total_s)

    def _run(self) -> None:
        while not self._stop.wait(0.05):
            with self._lock:
                elapsed = time.monotonic() - self._t0
                self._shown = min(self._end, self._base + elapsed)
                shown, total = self._shown, self._total
            left = f"~{total - shown:.0f}s left" if total else ""
            self._progress.update(self._task, completed=shown, left=left)


def _fps(value) -> str:
    """Format an fps/ceiling for the report ("-" for None/inf)."""
    import math

    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "\N{EN DASH}"
    return f"{value:.0f}"


def _render_benchmark(report) -> None:
    """Render a DiagnosticReport on stdout: key results, then the limiting stage,
    then per-camera detail.
    """
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from octacam import diagnostics as diag

    console = Console()
    r = report
    c = r.ceilings
    console.print()
    console.print(
        Text(
            f"octacam benchmark \N{EM DASH} {r.n_cameras} camera(s) via {r.backend}",
            style="bold",
        )
    )
    encoder = r.save_method + (f" ({r.ffmpeg_params})" if r.ffmpeg_params else "")
    console.print(
        f"target {r.target_fps:g} fps \N{MIDDLE DOT} {r.trigger_source} trigger "
        f"\N{MIDDLE DOT} sink={encoder}"
    )

    console.print()
    console.print(Text("KEY RESULTS", style="bold"))
    if r.measured_max_fps is not None:
        confidence = "confirmed" if r.max_confirmed else "safety margin"
        console.print(
            f"  Synchronized (software) max:  {_fps(r.measured_max_fps)} fps/cam "
            f"({confidence})"
        )
    else:
        console.print(
            f"  Synchronized (software) max:  {_fps(r.predicted_max_fps)} fps/cam "
            "(predicted)"
        )
    if r.hardware_max_fps is not None:
        measured = " (measured)" if r.freerun_trials else ""
        console.print(
            f"  Free-run / hardware max:      {_fps(r.hardware_max_fps)} fps/cam"
            f"{measured}"
        )
    if r.achievable:
        console.print(
            Text(
                f"  \N{CHECK MARK} {r.target_fps:g} fps is ACHIEVABLE",
                style="bold green",
            )
        )
    else:
        console.print(
            Text(
                f"  \N{BALLOT X} {r.target_fps:g} fps is NOT achievable \N{EM DASH} "
                f"limited by {r.bottleneck_label}",
                style="bold red",
            )
        )

    if c is not None:
        console.print()
        console.print(
            Text("BY STAGE", style="bold"),
            Text("  (system ceiling = slowest camera)", style="dim"),
        )

        def mark(name):
            return (
                Text("  \N{LEFTWARDS ARROW} limits", style="red")
                if r.bottleneck == name
                else Text("")
            )

        console.print(
            Text(f"  acquisition  {_fps(c.grab_min):>4} fps/cam  "),
            Text("(software; exposure+transfer serial)", style="dim"),
            mark(diag.ACQUISITION),
        )
        if r.throughput_mbps_total:
            per_cam = r.throughput_mbps_total / r.n_cameras if r.n_cameras else 0.0
            solo = (
                f" \N{MIDDLE DOT} alone {_fps(c.grab_solo_min)} fps/cam"
                if c.grab_solo_fps
                else ""
            )
            console.print(
                Text(
                    f"  transfer     {per_cam:>4.0f} MB/s/cam \N{MIDDLE DOT} "
                    f"{r.throughput_mbps_total:.0f} MB/s total"
                ),
                Text(
                    f"(derived from frame size \N{MULTIPLICATION SIGN} fps{solo})",
                    style="dim",
                ),
                mark(diag.TRANSFER),
            )
        if c.encode_fps:
            console.print(f"  encode       {_fps(c.encode_min):>4} fps/cam")
        else:
            console.print(
                Text(
                    "  encode        n/a  (null sink \N{EM DASH} encoder not measured)",
                    style="dim",
                )
            )

    console.print()
    console.print(Text("BY CAMERA", style="bold"))
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("camera")
    table.add_column("size", justify="right")
    table.add_column("acq", justify="right")
    table.add_column("free", justify="right")
    table.add_column("enc", justify="right")
    table.add_column("fps", justify="right")
    table.add_column("drop%", justify="right")
    table.add_column("queue peak", justify="right")
    table.add_column("acquire p50/p99 ms", justify="right")
    table.add_column("encode p50/p99 ms", justify="right")
    for t in r.trials:
        acq = t.stages.get("acquire")
        enc = t.stages.get("encode")
        s = t.serial
        table.add_row(
            t.name,
            f"{t.width}\N{MULTIPLICATION SIGN}{t.height}",
            _fps(c.grab_fps.get(s)) if c else "\N{EN DASH}",
            _fps(c.freerun_fps.get(s)) if c and c.freerun_fps else "\N{EN DASH}",
            _fps(c.encode_fps.get(s)) if c and c.encode_fps else "\N{EN DASH}",
            f"{t.achieved_fps:.1f}",
            f"{100 * t.drop_rate:.2f}",
            f"{t.max_queue_depth} of {r.writer_queue_size}",
            f"{acq.p50_ms:.1f}/{acq.p99_ms:.1f}" if acq else "\N{EN DASH}",
            f"{enc.p50_ms:.2f}/{enc.p99_ms:.2f}"
            if enc and enc.samples
            else "\N{EN DASH}",
        )
    console.print(table)
    console.print(
        Text(
            "  acq/free/enc = per-camera ceilings (concurrent / free-run / encode); "
            "fps/drop%/queue = the end-to-end trial at the target. drop% counts only "
            "frames the encoder queue refused (host couldn't keep up), not camera "
            "transport gaps.",
            style="dim",
        )
    )

    if r.freerun_trials:
        console.print()
        console.print(
            Text("  Free-run trial", style="bold"),
            Text("(real free-run pipeline, encoder in the loop)", style="dim"),
        )
        ft = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        ft.add_column("  camera")
        ft.add_column("fps", justify="right")
        ft.add_column("drop%", justify="right")
        ft.add_column("queue peak", justify="right")
        for t in r.freerun_trials:
            ft.add_row(
                f"  {t.name}",
                f"{t.achieved_fps:.1f}",
                f"{100 * t.drop_rate:.2f}",
                f"{t.max_queue_depth} of {r.writer_queue_size}",
            )
        console.print(ft)

    extras = []
    if r.system_cpu_percent is not None:
        extras.append(f"machine load (pre-run) {r.system_cpu_percent:.0f}% cpu")
    if r.cpu_percent is not None:
        extras.append(f"benchmark cpu {r.cpu_percent:.0f}%")
    if r.jitter_p99_ms is not None:
        extras.append(f"scheduler jitter p99 {r.jitter_p99_ms:.2f} ms")
    if extras:
        console.print("  " + " \N{MIDDLE DOT} ".join(extras))

    if r.recommendations:
        console.print()
        for rec in r.recommendations:
            console.print(Text(f"  \N{RIGHTWARDS ARROW} {rec}", style="yellow"))
    for note in r.notes:
        console.print(Text(f"  \N{BULLET} {note}", style="dim"))
    console.print()


@command
def benchmark(
    config_dir: Annotated[
        Path,
        typer.Argument(
            exists=True,
            file_okay=False,
            dir_okay=True,
            help="The rig's config directory (`octacam_config.toml` and its camera "
            "files), or a recording folder, whose config snapshot it uses.",
        ),
    ] = Path("."),
    fps: Annotated[
        float | None,
        typer.Option(
            "--fps", "-f", help="Target fps to test (default: from the config)."
        ),
    ] = None,
    duration: Annotated[
        float,
        typer.Option(
            "--duration", "-d", help="Seconds per measurement window (each scenario)."
        ),
    ] = 5.0,
    find_max: Annotated[
        bool,
        typer.Option(
            "--find-max/--no-find-max",
            help="Search for the maximum *stable* fps (software trigger only).",
        ),
    ] = True,
    freerun: Annotated[
        bool,
        typer.Option(
            "--freerun/--no-freerun",
            help="Also measure the free-run (external-trigger-equivalent) ceiling.",
        ),
    ] = True,
    sink: Annotated[
        BenchmarkSink,
        typer.Option(
            "--sink",
            help="What to write through: 'config' (the rig's real save_method, so "
            "the encode cost is measured) or 'null' (discard frames \N{EM DASH} "
            "isolate "
            "acquisition, skip the encoder).",
        ),
    ] = BenchmarkSink.config,
    backend: Annotated[
        str | None,
        typer.Option("--backend", help="Override the config's camera backend."),
    ] = None,
    record_form: Annotated[
        RecordForm | None,
        typer.Option(
            "--record-form",
            help="'display' (bake the transform) or 'sensor' "
            "(default: from the config).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON instead of the table."),
    ] = False,
    verbose: Verbose = False,
) -> None:
    """Benchmark a rig: is the target fps achievable, what is the max, and what limits
    it.

    Runs a short, instrumented dry-run against the cameras in `config_dir` -- an
    acquisition-ceiling sweep, an encoder-ceiling sweep, and an end-to-end trial
    at the target fps -- then reports the achievable rate, the maximum, and the
    per-stage throughput so you can see the bottleneck. No video is kept. It opens
    the cameras (like `record`), so it cannot run at the same time as a live GUI or
    recording on the same rig.

    Exits nonzero when the target fps is not achievable, so it is usable as a
    pre-flight check in scripts.
    """
    from octacam import diagnostics as diag
    from octacam.config import RecordingSettings, load_config_dir

    config_dir = resolve_config_arg(config_dir)
    config = load_config_dir(config_dir)
    settings = RecordingSettings.from_config(config, fps=fps)
    if record_form is not None:
        settings.record_form = record_form.value

    warn_if_transcoding()

    system = open_rig(config, config_dir, backend)
    try:
        log.info(
            "Benchmarking %d camera(s) at %g fps (%s trigger, "
            "sink=%s)\N{HORIZONTAL ELLIPSIS}",
            len(system),
            settings.fps,
            settings.trigger_source,
            sink.value,
        )
        # No live bar for a machine-readable --json run.
        bar = None if json_output else _BenchmarkProgressBar()
        with bar or contextlib.nullcontext():
            report = diag.diagnose(
                system,
                settings,
                duration_s=duration,
                find_max=find_max,
                measure_freerun=freerun,
                sink=sink.value,
                progress_cb=bar.update if bar else None,
            )
    finally:
        system.close()

    if json_output:
        typer.echo(json.dumps(report.to_dict(), indent=2))
    else:
        _render_benchmark(report)

    # The verdict comes from a software-trigger trial. Hardware-triggered cameras
    # overlap exposure and transfer, so on an external rig it is only a lower
    # bound, not a failure.
    if not report.achievable and settings.trigger_source != "external":
        raise typer.Exit(1)
