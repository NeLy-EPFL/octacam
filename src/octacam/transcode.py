"""Offline transcodes for ``octacam process``: one recording video re-encoded
into an atomic partial output, with ffmpeg's progress reported as it runs."""

import contextlib
import fcntl
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from octacam.ffmpeg import (
    DEFAULT_PIX_FMT,
    find_ffmpeg,
    output_args,
    quiet_argv,
    rawvideo_input_args,
    split_opts,
)
from octacam.files import flock_held, partial_glob, partial_path

# The offline pass re-encodes harder than capture (writer.DEFAULT_FFMPEG_PARAMS).
DEFAULT_TRANSCODE_FFMPEG_PARAMS = (
    f"-c:v libx264 -preset veryslow -crf 20 -pix_fmt {DEFAULT_PIX_FMT}"
)

# Recorded pixel format -> (ffmpeg rawvideo pixel format, bytes per pixel).
# Every backend records Mono8.
_RAW_PIXEL_FORMATS = {"Mono8": ("gray", 1)}


@dataclass(frozen=True)
class TranscodeProgress:
    """One block of ffmpeg's ``-progress`` stream. ``total_frames`` is None when
    unknown (an indeterminate bar); ``fps``/``speed`` are 0.0 until measured."""

    frame: int
    fps: float
    out_time_s: float
    speed: float
    total_frames: int | None
    done: bool


ProgressCallback = Callable[[TranscodeProgress], None]


def transcode_file(
    src: Path,
    output: Path,
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    *,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    pixel_format: str = "Mono8",
    frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Re-encode one ``.raw``/``.mkv``/``.mp4`` to *output* (its extension picks
    the container), never stream-copying: captures use a fast preset, and this
    offline pass is where a slow one pays off.

    A ``.raw`` has no geometry: *width*/*height*/*fps* (from the recording
    summary) are required, and *frames* (else the file size) sizes the bar. An
    encoded input uses *width*/*height* only to make a gray output 4:2:0, and
    *frames* only for the bar.
    """
    src, output = Path(src), Path(output)
    total_frames = frames
    if src.suffix == ".raw":
        if width is None or height is None or fps is None:
            raise FileNotFoundError(
                f"no recording_summary.json geometry for {src}; cannot "
                "determine width/height/fps to transcode the raw stream"
            )
        if pixel_format not in _RAW_PIXEL_FORMATS:
            raise ValueError(f"cannot transcode {pixel_format} raw video from {src}")
        input_pix_fmt, bytes_per_pixel = _RAW_PIXEL_FORMATS[pixel_format]
        if total_frames is None and width and height:
            total_frames = src.stat().st_size // (width * height * bytes_per_pixel)
        input_args = rawvideo_input_args(width, height, fps, str(src), input_pix_fmt)
    else:
        input_args = ["-i", str(src)]
    frame_size = (width, height) if width and height else None
    # Built before the temp exists: no ffmpeg or bad quoting leaves the folder alone.
    cmd = [find_ffmpeg(), *input_args, *output_args(ffmpeg_params, frame_size)]
    with atomic_output(output) as tmp:
        run_ffmpeg(
            [*cmd, "-y", str(tmp)],
            src,
            on_progress=on_progress,
            total_frames=total_frames,
            raw_output=raw_output,
        )
    return output


# --- atomic partial outputs ------------------------------------------------------

# Without flock, a temp idle this long is an orphan. Generous: deleting a live
# temp is worse than keeping a dead one, and a live ffmpeg touches it every few
# seconds.
_PARTIAL_IDLE_S = 6 * 3600


def _partial_is_live(path: Path) -> bool:
    """Whether a process still writes *path*: :func:`atomic_output` holds a
    flock on its temp while it owns it, so a lockable temp is an orphan. Where
    flock cannot tell (some network mounts), an idle-mtime test. Anything
    uninspectable is live, so the sweep only ever errs toward keeping."""
    try:
        held = flock_held(path, "r+")
    except OSError:
        return True
    return _partial_is_recent(path) if held is None else held


def _partial_is_recent(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < _PARTIAL_IDLE_S
    except OSError:
        return True


def _sweep_orphan_partials(output: Path, keep: Path) -> None:
    """Delete *output*'s temps whose writer is gone, never a live one or *keep*."""
    try:
        stale_paths = list(output.parent.glob(partial_glob(output, extension_last=True)))
    except OSError:
        return
    for stale in stale_paths:
        if stale == keep:
            continue
        try:
            if not _partial_is_live(stale):
                stale.unlink(missing_ok=True)
        except OSError:
            pass


@contextlib.contextmanager
def atomic_output(output: Path):
    """Yield a temp to encode into, renamed onto *output* only on success.

    Any exception, Ctrl-C included, deletes the temp, so a partial encode never
    appears at *output* or replaces it. The temp is created and flock-ed before
    ffmpeg runs (every caller passes ``-y``), so a concurrent run can tell it
    from an orphan.
    """
    tmp = partial_path(output, extension_last=True)
    try:
        lock = open(tmp, "w")
    except OSError:
        # Let the encode fail with the real error (read-only dir, ENOSPC).
        lock = None
    if lock is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        _sweep_orphan_partials(output, keep=tmp)
        try:
            yield tmp
            os.replace(tmp, output)  # inside the try: a failed rename cleans up
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    finally:
        if lock is not None:
            with contextlib.suppress(OSError):
                lock.close()


# --- running ffmpeg ----------------------------------------------------------------


def run_ffmpeg(
    args: list[str],
    src: Path,
    *,
    on_progress: ProgressCallback | None = None,
    total_frames: int | None = None,
    raw_output: bool = False,
) -> None:
    """Run the ffmpeg argv *args* (executable first) for *src*, raising
    RuntimeError on failure. With *raw_output* ffmpeg paints the terminal
    itself; otherwise its progress feeds *on_progress* and its stderr is shown
    only on failure.

    It owns the reporting flags (-nostdin, -hide_banner, -loglevel, -progress,
    -stats, -nostats) and drops any in *args*: pass only inputs and outputs."""
    args = _reporting_args(args, raw_output)
    if raw_output:
        returncode = subprocess.run(args, stdin=subprocess.DEVNULL).returncode
        if returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {src} (exit code {returncode})")
        return

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None  # PIPE => set
    stderr_tail: deque[str] = deque(maxlen=40)
    # Drained on its own thread, so a chatty ffmpeg never stalls on a full pipe.
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
        # A Ctrl-C or a raising callback takes ffmpeg down too.
        proc.kill()
        raise
    finally:
        proc.stdout.close()
        returncode = proc.wait()
        stderr_thread.join(timeout=2)
    if returncode != 0:
        tail = "\n".join(stderr_tail).strip()
        raise RuntimeError(f"ffmpeg failed for {src}: {tail}")


def _reporting_args(args: list[str], raw_output: bool) -> list[str]:
    """*args* with its reporting flags replaced by the output mode's: a quiet
    ffmpeg writing ``-progress`` for octacam's bar, or with *raw_output*
    ffmpeg's own ``-stats`` at info level. Both run off the tty (raw mode only
    loses ffmpeg's 'q' key)."""
    cleaned, _ = split_opts(
        args[1:],
        ("-loglevel", "-progress"),
        flags=("-nostdin", "-hide_banner", "-stats", "-nostats"),
    )
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
    return quiet_argv(args[0], *flags, *cleaned)


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
    """Emit one TranscodeProgress per ``-progress`` block (closed by a
    ``progress=`` line); a field ffmpeg reports as ``N/A`` keeps its value."""
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
