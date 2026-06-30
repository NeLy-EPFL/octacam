"""Asynchronous video writers.

Every writer runs a sink on a background thread behind a bounded queue:
write() never blocks the grab loop and drops the frame when the queue is
full (or once the sink has failed). Available sinks:

- FfmpegVideoWriter (default): pipes raw GRAY8 frames to an ffmpeg child
  encoding H.264 with libx264 in true monochrome 4:0:0. Encoding happens
  entirely in the child process, outside the GIL. Validated on the rig:
  8 parallel ultrafast encoders sustain >1200 fps aggregate at 1080p
  (see docs/web-gui-plan.md).
- RawVideoWriter: raw Mono8 dump + JSON sidecar, for `octacam transcode`.
"""

# The sink handles (_queue/_proc/_writer) follow an open -> use -> close
# lifecycle; they are only touched while the writer thread is running, an
# invariant pyright can't track across methods.
# pyright: reportOptionalMemberAccess=false

import contextlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

_SENTINEL = None
FINALIZE_TIMEOUT_S = 120  # max wait for ffmpeg to flush after stdin closes

# Default libx264 encoder parameters. The single source of truth shared by the
# writer, the RecordingSettings dataclass, the config (gui.*_default), and the
# CLI, so the default can never drift between layers. CRF 18 is the capture
# default (near-visually-lossless; offline transcoding can re-encode harder).
DEFAULT_CRF = 18
DEFAULT_PRESET = "ultrafast"
DEFAULT_PIX_FMT = "gray"
# Extra libx264 options passed verbatim to ffmpeg's -x264-params (e.g.
# "keyint=30:scenecut=0"); empty means the flag is omitted entirely.
DEFAULT_X264_PARAMS = ""


def _is_limited_range_yuv(pix_fmt: str) -> bool:
    """True for YUV formats that default to limited/"TV" range (16-235 luma).

    Those squeeze our full-range (0-255) camera frames into 16-235 — an
    irreversible ~3.6% loss that happens even with lossless encoding. ``gray``
    (4:0:0) and the ``yuvj*`` aliases are already full range, so they're exempt.
    """
    return pix_fmt.startswith("yuv") and not pix_fmt.startswith("yuvj")


def _color_range_args(pix_fmt: str) -> list[str]:
    """ffmpeg output args tagging the stream FULL range for limited-range YUV.

    ``-color_range pc`` writes the H.264 VUI full_range_flag / container tag so
    decoders expand luma back to 0-255 instead of rendering washed-out. This is
    only the *tag*: on its own (notably on ffmpeg 7.x) it does NOT change the
    pixel data, which is why callers must also run :func:`_full_range_vf` to
    force the conversion itself. The two together are the single source of truth
    for full-range handling, shared by the capture writer, the offline
    transcoders, and the grid compositor.
    """
    return ["-color_range", "pc"] if _is_limited_range_yuv(pix_fmt) else []


def _full_range_vf(pix_fmt: str, vf: str = "") -> str:
    """Append a full-range conversion filter to *vf* for limited-range YUV.

    ``scale=out_range=full`` forces the gray→YUV conversion to keep 0-255 luma
    instead of compressing to 16-235. Unlike the ``-color_range`` *flag*, a
    filter reliably converts the data on every ffmpeg version we ship. A no-op
    (returns *vf* unchanged) for ``gray``/``yuvj*`` outputs, which are already
    full range. Pair with :func:`_color_range_args` so the result is also tagged.
    """
    if not _is_limited_range_yuv(pix_fmt):
        return vf
    frag = "scale=out_range=full"
    return f"{vf},{frag}" if vf else frag


@dataclass(frozen=True)
class TranscodeProgress:
    """One progress sample parsed from ffmpeg's ``-progress pipe:1`` stream.

    ``total_frames`` is the encode's known frame count when derivable (always
    for ``.raw`` inputs, and from the recording summary for encoded ones), else
    None for an indeterminate bar. ``fps``/``speed`` are 0.0 until ffmpeg has
    measured them. ``done`` is True on the terminal ``progress=end`` block."""

    frame: int
    fps: float
    out_time_s: float
    speed: float
    total_frames: int | None
    done: bool


# Called once per ffmpeg progress block during a transcode. Lives in the writer
# as a plain callback so the UI layer (the CLI's rich progress bar) owns all
# rendering and the writer stays free of presentation concerns.
ProgressCallback = Callable[[TranscodeProgress], None]


def find_ffmpeg() -> str:
    """Locate an ffmpeg executable: $OCTACAM_FFMPEG, imageio-ffmpeg, $PATH."""
    exe = os.environ.get("OCTACAM_FFMPEG")
    if exe:
        return exe
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover - depends on environment
        log.debug("imageio-ffmpeg unavailable: %s", e)
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    raise RuntimeError(
        "No ffmpeg executable found: install the imageio-ffmpeg package or "
        "a system ffmpeg, or set OCTACAM_FFMPEG."
    )


def build_x264_args(
    ffmpeg: str,
    output: str,
    fps: float,
    width: int,
    height: int,
    crf: int,
    preset: str,
    pix_fmt: str,
    source: str = "pipe:0",
    x264_params: str = "",
    vf: str = "",
) -> list[str]:
    """ffmpeg argv encoding rawvideo GRAY8 (from `source`) to x264.

    pix_fmt "gray" produces true monochrome 4:0:0 H.264 (decodes in all
    ffmpeg-based tools; browsers would need yuv420p). Note: ffprobe shows
    such streams as yuvj420p because the H.264 decoder synthesizes neutral
    chroma; the x264 encoder log ("4:0:0, 8-bit") is the source of truth.

    ``x264_params``, when non-empty, is passed verbatim as ffmpeg's
    ``-x264-params`` (a single ``opt=val:opt2=val2`` token) so any libx264
    knob beyond crf/preset can be set from config without a dedicated flag.

    ``vf``, when non-empty, is a single ffmpeg ``-vf`` filter chain (e.g.
    ``transpose=1,hflip``) applied before encoding — used by `octacam
    transcode --as-displayed` to bake a display orientation in. For a
    limited-range YUV ``pix_fmt`` a full-range conversion filter is appended so
    0-255 luma survives (see :func:`_full_range_vf`).
    """
    vf = _full_range_vf(pix_fmt, vf)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pixel_format",
        "gray",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        f"{fps:g}",
        "-i",
        source,
        *(["-vf", vf] if vf else []),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        *(["-x264-params", x264_params] if x264_params else []),
        "-pix_fmt",
        pix_fmt,
        *_color_range_args(pix_fmt),
        "-y",
        str(output),
    ]


def _write_all(file, frame) -> None:
    """Write a frame to an unbuffered file, handling partial pipe writes."""
    view = memoryview(frame).cast("B")
    while view.nbytes:
        n = file.write(view)
        if n is None or n == view.nbytes:
            return
        view = view[n:]


class AsyncFrameWriter:
    """Bounded-queue writer base (the original AsyncVideoWriter skeleton).

    write() takes ownership of the frame array (the caller must not mutate
    it afterwards); callers pass a freshly owned copy from GrabResult.Array.
    Subclasses implement _open_sink/_write_frame/_close_sink.
    """

    def __init__(self, max_queue_size: int = 20):
        self._max_queue_size = max_queue_size
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._failed = False
        self._written = 0

    @property
    def failed(self) -> bool:
        """True once the sink has died; subsequent writes are dropped."""
        return self._failed

    @property
    def frames_written(self) -> int:
        """Frames actually handed to the sink (excludes any discarded after
        a sink failure). The grab loop reconciles this against the queue to
        keep the CSV's per-frame `dropped` column accurate."""
        return self._written

    def open(self, filename: str, fps: float, frame_size: tuple[int, int]) -> bool:
        """Open `filename` for writing. frame_size is (width, height)."""
        self.close()
        try:
            self._open_sink(str(filename), fps, frame_size)
        except Exception as e:
            log.error("Failed to open writer for %s: %s", filename, e)
            return False
        self._failed = False
        self._written = 0
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        return True

    def write(self, frame) -> bool:
        """Enqueue a frame; returns False if it was dropped."""
        if not self._running or self._failed:
            return False
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        """Stop accepting frames, drain the queue, and finalize the file."""
        if self._thread is None:
            return
        self._running = False
        self._queue.put(_SENTINEL)  # queued frames are written first
        self._thread.join()
        self._thread = None
        self._queue = None
        try:
            self._close_sink()
        except Exception as e:
            log.error("Failed to finalize video: %s", e)

    def _writer_loop(self) -> None:
        while True:
            frame = self._queue.get()
            if frame is _SENTINEL:
                break
            if self._failed:
                continue  # keep draining so close() semantics are unchanged
            try:
                self._write_frame(frame)
                self._written += 1
            except Exception as e:
                self._failed = True
                self._on_sink_failure(e)

    # -- subclass hooks ----------------------------------------------------

    def _open_sink(self, filename: str, fps: float, frame_size) -> None:
        raise NotImplementedError

    def _write_frame(self, frame) -> None:
        raise NotImplementedError

    def _close_sink(self) -> None:
        raise NotImplementedError

    def _on_sink_failure(self, exc: Exception) -> None:
        log.error("Writer failed (%s); subsequent frames will be dropped", exc)


class FfmpegVideoWriter(AsyncFrameWriter):
    """Pipes raw GRAY8 frames into an ffmpeg child encoding H.264 (libx264).

    The pipe write blocks when ffmpeg falls behind; the bounded queue absorbs
    that and drops on full, preserving the drop-accounting contract. If the
    child dies mid-recording, write() returns False from then on and the
    stderr tail is logged (MKV output stays playable up to that point).
    """

    def __init__(
        self,
        crf: int = DEFAULT_CRF,
        preset: str = DEFAULT_PRESET,
        pix_fmt: str = DEFAULT_PIX_FMT,
        remux_mp4: bool = False,
        max_queue_size: int = 20,
        x264_params: str = DEFAULT_X264_PARAMS,
    ):
        super().__init__(max_queue_size)
        self.crf = crf
        self.preset = preset
        self.pix_fmt = pix_fmt
        self.remux_mp4 = remux_mp4
        self.x264_params = x264_params
        self._proc: subprocess.Popen | None = None
        self._filename: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    @property
    def error_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def _open_sink(self, filename, fps, frame_size):
        width, height = frame_size
        args = build_x264_args(
            find_ffmpeg(),
            filename,
            fps,
            width,
            height,
            self.crf,
            self.preset,
            self.pix_fmt,
            x264_params=self.x264_params,
        )
        self._filename = filename
        self._stderr_tail.clear()
        # bufsize=0: frames go straight to the pipe (no Python-side
        # double-buffering) and the write syscall releases the GIL.
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(self._proc,), daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self, proc):
        with proc.stderr:
            for line in proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)

    def _write_frame(self, frame):
        _write_all(self._proc.stdin, frame)

    def _on_sink_failure(self, exc):
        tail = self.error_tail
        log.error(
            "ffmpeg writer for %s failed (%s); subsequent frames will be dropped%s",
            self._filename,
            exc,
            ("\nffmpeg output:\n" + tail) if tail else "",
        )

    def _close_sink(self):
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # Generous finalize window: after stdin closes ffmpeg only has to
        # flush frames already queued in its own buffers. ultrafast is
        # near-instant, but a slow preset on a long trial can take a while -
        # killing it early would truncate the file and wrongly flag failure.
        try:
            returncode = proc.wait(timeout=FINALIZE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.error(
                "ffmpeg still running %d s after stdin close; killing it",
                FINALIZE_TIMEOUT_S,
            )
            proc.kill()
            returncode = proc.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        if returncode != 0:
            self._failed = True
            log.error(
                "ffmpeg exited with code %d for %s%s",
                returncode,
                self._filename,
                ("\nffmpeg output:\n" + self.error_tail) if self._stderr_tail else "",
            )
        elif self.remux_mp4:
            self._remux()

    def _remux(self):
        source = Path(self._filename)  # pyright: ignore[reportArgumentType]
        target = source.with_suffix(".mp4")
        result = subprocess.run(
            [
                find_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-i",
                str(source),
                "-c",
                "copy",
                str(target),
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            source.unlink()
            log.info("Remuxed %s -> %s", source, target)
        else:
            log.error(
                "Remux of %s failed (kept the MKV): %s",
                source,
                result.stderr.decode(errors="replace").strip(),
            )


class RawVideoWriter(AsyncFrameWriter):
    """Dumps raw Mono8 frames + a JSON sidecar for `octacam transcode`."""

    def __init__(self, max_queue_size: int = 20):
        super().__init__(max_queue_size)
        self._file = None

    def _open_sink(self, filename, fps, frame_size):
        width, height = frame_size
        path = Path(filename)
        path.with_suffix(".json").write_text(
            json.dumps(
                {
                    "width": width,
                    "height": height,
                    "pixel_format": "Mono8",
                    "fps": fps,
                }
            )
            + "\n"
        )
        self._file = open(path, "wb", buffering=0)

    def _write_frame(self, frame):
        _write_all(self._file, frame)

    def _close_sink(self):
        if self._file is not None:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoFormat:
    """A recording format selectable from the GUI/CLI."""

    codec: str  # "x264" | "raw"
    extension: str
    label: str
    crf: int = DEFAULT_CRF
    preset: str = DEFAULT_PRESET
    pix_fmt: str = DEFAULT_PIX_FMT
    remux_mp4: bool = False
    x264_params: str = DEFAULT_X264_PARAMS

    def create_writer(self, max_queue_size: int = 20) -> AsyncFrameWriter:
        if self.codec == "x264":
            return FfmpegVideoWriter(
                crf=self.crf,
                preset=self.preset,
                pix_fmt=self.pix_fmt,
                remux_mp4=self.remux_mp4,
                max_queue_size=max_queue_size,
                x264_params=self.x264_params,
            )
        if self.codec == "raw":
            return RawVideoWriter(max_queue_size)
        raise ValueError(f"Unknown codec: {self.codec}")


# Insertion order defines the GUI combo order; index 0 is the default
# (config key video_writer_default_index).
FORMATS: dict[str, VideoFormat] = {
    "x264": VideoFormat("x264", "mkv", "x264 mkv (ffmpeg)"),
    "raw": VideoFormat("raw", "raw", "raw Mono8 (transcode later)"),
}


def default_codec(gui_config) -> str:
    """Resolve the default codec key from a GuiConfig.

    Prefers the explicit named `video_writer_default` (stable across changes
    to the writer list); falls back to the positional
    `video_writer_default_index`; else "x264".
    """
    named = getattr(gui_config, "video_writer_default", "")
    if named:
        if named in FORMATS:
            return named
        log.warning("Unknown video_writer_default %r; using x264", named)
        return "x264"
    codecs = list(FORMATS)
    index = getattr(gui_config, "video_writer_default_index", 0)
    if 0 <= index < len(codecs):
        return codecs[index]
    return "x264"


# Infix tagging an in-progress transcode's temp file (see _partial_path). Kept
# greppable and stable so the folder scanner (cli._transcode_jobs) can skip any
# such file a hard kill left behind.
PARTIAL_INFIX = ".octacam-part"


def _partial_path(output: Path) -> Path:
    """Sibling temp path an in-progress encode of ``output`` writes to.

    Lives in ``output``'s own directory (so the final rename is an atomic,
    same-filesystem ``os.replace``) and is hidden + tagged with
    :data:`PARTIAL_INFIX`, yet keeps ``output``'s real extension last so ffmpeg
    still infers the container muxer from the filename."""
    return output.with_name(f".{output.stem}{PARTIAL_INFIX}{output.suffix}")


def is_partial_transcode(path: Path) -> bool:
    """True for a transcode temp file (see :func:`_partial_path`).

    Lets a folder scan skip a partial output a crash/SIGKILL orphaned before
    its cleanup could run — a Ctrl-C or any caught failure removes it itself."""
    return PARTIAL_INFIX in path.name


@contextlib.contextmanager
def _atomic_output(output: Path):
    """Yield a temp path to encode into, swapped onto ``output`` only on success.

    The whole point of graceful interruption: ffmpeg writes a sibling
    :func:`_partial_path`, which is atomically renamed onto ``output`` when the
    body returns normally and deleted on *any* exception — a re-encode failure,
    or a Ctrl-C (KeyboardInterrupt) / kill that propagates out mid-encode. So a
    partial encode never appears at ``output``, and an interrupted run never
    clobbers an existing ``output`` (the rename happens only once the new file
    is whole)."""
    tmp = _partial_path(output)
    tmp.unlink(missing_ok=True)  # clear any orphan a prior hard kill left
    try:
        yield tmp
        # Swap in only once the encode is whole. Inside the try so a failed
        # rename cleans up too, never stranding the temp.
        os.replace(tmp, output)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def transcode_raw(
    raw_path: Path,
    crf: int = 20,
    preset: str = "veryslow",
    pix_fmt: str = DEFAULT_PIX_FMT,
    output: Path | None = None,
    x264_params: str = DEFAULT_X264_PARAMS,
    vf: str = "",
    *,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Transcode a .raw Mono8 dump (with its .json sidecar) to x264.

    ``output`` defaults to ``<raw>.mkv``; pass an explicit path to choose the
    container. ``vf`` bakes in a display orientation (see build_x264_args).
    ``on_progress``/``raw_output`` control progress reporting (see
    :func:`_run_ffmpeg`); the exact frame total is derived from the Mono8 file
    size so the bar is always determinate."""
    raw_path = Path(raw_path)
    sidecar = raw_path.with_suffix(".json")
    if not sidecar.exists():
        raise FileNotFoundError(f"missing JSON sidecar for {raw_path}: {sidecar}")
    meta = json.loads(sidecar.read_text())
    output = Path(output) if output else raw_path.with_suffix(".mkv")
    width, height = meta["width"], meta["height"]
    # Mono8 is exactly 1 byte/pixel, so the file size pins the frame count.
    total_frames = (
        raw_path.stat().st_size // (width * height) if width and height else None
    )
    with _atomic_output(output) as tmp:
        args = build_x264_args(
            find_ffmpeg(),
            str(tmp),
            meta["fps"],
            width,
            height,
            crf,
            preset,
            pix_fmt,
            source=str(raw_path),
            x264_params=x264_params,
            vf=vf,
        )
        _run_ffmpeg(
            args,
            raw_path,
            on_progress=on_progress,
            total_frames=total_frames,
            raw_output=raw_output,
        )
    return output


def transcode_encoded(
    src: Path,
    output: Path,
    crf: int = 20,
    preset: str = "veryslow",
    pix_fmt: str = DEFAULT_PIX_FMT,
    x264_params: str = DEFAULT_X264_PARAMS,
    vf: str = "",
    *,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Re-encode an already-encoded video (mkv/mp4) to ``output`` with libx264.

    Always re-encodes with the given libx264 ``preset``/``crf``/``pix_fmt``
    rather than stream-copying the source: captures are written with a fast
    preset to keep up with the cameras, so this offline pass is where the slow
    preset earns its compression. ``vf``, when non-empty, additionally bakes a
    display transform in (see build_x264_args).

    ``total_frames`` (e.g. from the recording summary) makes the progress bar
    determinate; without it the bar is indeterminate. ``on_progress``/
    ``raw_output`` control progress reporting (see :func:`_run_ffmpeg`).
    """
    src = Path(src)
    output = Path(output)
    ffmpeg = find_ffmpeg()
    vf = _full_range_vf(pix_fmt, vf)
    with _atomic_output(output) as tmp:
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(src),
            *(["-vf", vf] if vf else []),
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            *(["-x264-params", x264_params] if x264_params else []),
            "-pix_fmt",
            pix_fmt,
            *_color_range_args(pix_fmt),
            str(tmp),
        ]
        _run_ffmpeg(
            args,
            src,
            on_progress=on_progress,
            total_frames=total_frames,
            raw_output=raw_output,
        )
    return output


def transcode_file(
    input_path: Path,
    output: Path,
    crf: int = 20,
    preset: str = "veryslow",
    pix_fmt: str = DEFAULT_PIX_FMT,
    x264_params: str = DEFAULT_X264_PARAMS,
    vf: str = "",
    *,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Transcode one ``.raw``/``.mkv``/``.mp4`` file to ``output``.

    Dispatches on the input suffix; ``vf`` (if any) bakes a display transform
    in. The caller picks ``output`` (extension = desired container).
    ``total_frames``/``on_progress``/``raw_output`` are forwarded to the
    progress reporting (a ``.raw`` input derives its own exact total, so the
    hint there is ignored)."""
    input_path = Path(input_path)
    if input_path.suffix == ".raw":
        return transcode_raw(
            input_path,
            crf=crf,
            preset=preset,
            pix_fmt=pix_fmt,
            output=output,
            x264_params=x264_params,
            vf=vf,
            on_progress=on_progress,
            raw_output=raw_output,
        )
    return transcode_encoded(
        input_path,
        output,
        crf=crf,
        preset=preset,
        pix_fmt=pix_fmt,
        x264_params=x264_params,
        vf=vf,
        total_frames=total_frames,
        on_progress=on_progress,
        raw_output=raw_output,
    )


def _reporting_args(args: list[str], raw_output: bool) -> list[str]:
    """Re-set ffmpeg's verbosity/progress flags for the chosen output mode.

    Strips whatever ``-hide_banner``/``-loglevel``/``-stats``/``-nostats``/
    ``-progress`` flags the arg builders baked in, then re-inserts the pair the
    mode needs: the octacam bar wants a quiet ffmpeg emitting a machine-readable
    ``-progress`` stream, while raw mode wants ffmpeg's native ``-stats`` line
    at info level streamed straight to the terminal."""
    exe, rest = args[0], args[1:]
    cleaned: list[str] = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-loglevel", "-progress"):
            skip_next = True  # also drop the value token that follows
            continue
        if tok in ("-hide_banner", "-stats", "-nostats"):
            continue
        cleaned.append(tok)
    if raw_output:
        flags = ["-hide_banner", "-loglevel", "info", "-stats"]
    else:
        flags = [
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostats",
            "-progress",
            "pipe:1",
        ]
    return [exe, *flags, *cleaned]


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:  # ffmpeg prints "N/A" before the first measurement
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def _parse_progress(
    stream, on_progress: ProgressCallback, total_frames: int | None
) -> None:
    """Parse ffmpeg ``-progress pipe:1`` blocks, emitting one sample per block.

    ffmpeg writes one ``key=value`` per line and closes each block with a
    ``progress=continue`` (or final ``progress=end``) line; we snapshot the
    latest frame/fps/time/speed at every block boundary. Unmeasured fields
    arrive as ``N/A`` and keep their prior value."""
    frame = 0
    fps = 0.0
    out_time_s = 0.0
    speed = 0.0
    for line in stream:
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        value = value.strip()
        if key == "frame":
            frame = _to_int(value, frame)
        elif key == "fps":
            fps = _to_float(value, fps)
        elif key == "out_time_us":
            out_time_s = _to_float(value, out_time_s * 1e6) / 1e6
        elif key == "speed":
            speed = _to_float(value.rstrip("x"), speed)
        elif key == "progress":
            on_progress(
                TranscodeProgress(
                    frame, fps, out_time_s, speed, total_frames, value == "end"
                )
            )


def _drain_into(stream, sink: deque[str]) -> None:
    with stream:
        for line in stream:
            text = line.rstrip()
            if text:
                sink.append(text)


def _run_ffmpeg(
    args: list[str],
    src: Path,
    *,
    on_progress: ProgressCallback | None = None,
    total_frames: int | None = None,
    raw_output: bool = False,
) -> None:
    """Run an ffmpeg transcode of ``src``, raising RuntimeError on failure.

    With ``raw_output`` true, ffmpeg's native output streams straight to the
    terminal (the user opted into the raw ffmpeg view). Otherwise ffmpeg runs
    quietly with ``-progress pipe:1``: each block is parsed and forwarded to
    ``on_progress`` (if any) to drive a progress bar, while stderr is captured
    and surfaced only when the encode fails."""
    args = _reporting_args(args, raw_output)
    if raw_output:
        # Inherit stdout/stderr so ffmpeg's stats/log paint the terminal live.
        returncode = subprocess.run(args).returncode
        if returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {src} (exit code {returncode})")
        return

    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None and proc.stderr is not None  # PIPE => set
    stderr_tail: deque[str] = deque(maxlen=40)
    # stderr is drained on its own thread (and closed there via `with stream`)
    # so a chatty ffmpeg can never fill the pipe and stall while we read stdout.
    stderr_thread = threading.Thread(
        target=_drain_into, args=(proc.stderr, stderr_tail), daemon=True
    )
    stderr_thread.start()
    try:
        if on_progress is not None:
            _parse_progress(proc.stdout, on_progress, total_frames)
        else:
            for _ in proc.stdout:  # drain so a full pipe never stalls ffmpeg
                pass
    except BaseException:
        # A Ctrl-C (or a raising progress callback) must take ffmpeg down with
        # us, not leave it encoding a partial file after we stop reading it.
        proc.kill()
        raise
    finally:
        # Always reap the child (the old subprocess.run did); after a kill the
        # wait returns at once. stderr is left for its own thread to close.
        proc.stdout.close()
        returncode = proc.wait()
        stderr_thread.join(timeout=2)
    if returncode != 0:
        tail = "\n".join(stderr_tail).strip()
        raise RuntimeError(f"ffmpeg failed for {src}: {tail}")
