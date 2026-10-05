"""Video writers and the offline transcodes of ``octacam process``.

A capture writer runs its sink on a thread behind a bounded queue, so write()
never blocks the grab loop: it refuses a frame when the queue is full or the sink
has failed. FfmpegVideoWriter pipes Mono8 frames into an ffmpeg child, which
encodes outside the GIL; RawVideoWriter dumps them for ``octacam process`` to
transcode (the geometry is in the recording summary).
"""

# The sink handles (_queue/_proc/_file) exist only between open() and close(),
# which pyright cannot follow across methods.
# pyright: reportOptionalMemberAccess=false

import contextlib
import errno
import glob
import logging
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only; see _partial_is_live
    fcntl = None  # type: ignore[assignment]

from octacam.ffmpeg import (
    DEFAULT_PIX_FMT,
    find_ffmpeg,
    nvenc_encoder,
    nvenc_max_sessions,
    output_args,
    pix_fmt_of,
    quiet_argv,
    rawvideo_input_args,
    split_opts,
)

log = logging.getLogger("octacam")

_SENTINEL = None
FINALIZE_TIMEOUT_S = 120  # max wait for ffmpeg to flush after stdin closes

# Capture: near-visually-lossless at a preset fast enough to keep up (on the rig,
# 8 parallel ultrafast encoders sustain >1200 fps in total at 1080p).
DEFAULT_CRF = 18
DEFAULT_PRESET = "ultrafast"

# The config's ffmpeg_params: encoder output args, spliced verbatim after the
# derived rawvideo input args. The offline transcode re-encodes harder.
DEFAULT_FFMPEG_PARAMS = f"-c:v libx264 -preset {DEFAULT_PRESET} -crf {DEFAULT_CRF} -pix_fmt {DEFAULT_PIX_FMT}"
DEFAULT_TRANSCODE_FFMPEG_PARAMS = (
    f"-c:v libx264 -preset veryslow -crf 20 -pix_fmt {DEFAULT_PIX_FMT}"
)

# GPU capture (record.save_method = "nvenc", or any *_nvenc encoder). NVENC
# rejects 4:0:0, so yuv420p, which output_args keeps at 0-255 luma. NVENC
# ignores -crf, and -cq 16 matches libx264's -crf 18; -bf 0 buffers no B-frames.
# Cameras past the GPU's session limit fall back to libx264
# (resolve_capture_formats).
NVENC_H264_PARAMS = "-c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq 16 -bf 0 -pix_fmt yuv420p"

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


def _write_all(file, frame) -> None:
    """Write a frame to an unbuffered file, handling partial pipe writes."""
    view = memoryview(frame).cast("B")
    while view.nbytes:
        n = file.write(view)
        if n is None or n == view.nbytes:
            return
        view = view[n:]


class AsyncFrameWriter:
    """Bounded-queue writer; subclasses implement _open_sink/_write_frame/_close_sink.

    write() takes ownership of the frame: the caller must not mutate it. Fills
    keep one video frame per trigger pulse: ``write(frame, fill_before=n)``
    first repeats the previous frame n times (*frame* itself if none was
    written), and ``close(fill_after=n)`` appends n repeats. A fill rides on the
    next queued item, never in a slot of its own, so it cannot be dropped alone
    and leave the video short. ``profile`` (off for recordings, which then pay
    nothing) times each sink write and tracks the queue's high-water depth for
    the benchmark's short trials.
    """

    def __init__(self, max_queue_size: int = 20, *, profile: bool = False):
        self._max_queue_size = max_queue_size
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._profile = profile
        # True once the sink has died; later writes are refused.
        self.failed = False
        # Frames accepted (fills included) and frames handed to the sink; each
        # counter has a single writer thread, so their difference is race-free.
        self._accepted = 0
        self.frames_written = 0
        # With ``profile``: each frame's sink write time (ns) and the queue's
        # high-water depth.
        self.encode_ns_samples: list[int] = []
        self.max_queue_depth = 0

    @property
    def max_queue_size(self) -> int:
        """How many items the queue holds before :meth:`write` refuses one."""
        return self._max_queue_size

    @property
    def backlog(self) -> int:
        """Frames accepted but not yet handed to the sink, fills included (the
        queue's item count hides the fills that ride on each item)."""
        return max(0, self._accepted - self.frames_written)

    def open(self, filename: str, fps: float, frame_size: tuple[int, int]) -> bool:
        """Open `filename` for writing. frame_size is (width, height)."""
        self.close()
        self.failed = False
        self._accepted = 0
        self.frames_written = 0
        self.encode_ns_samples = []
        self.max_queue_depth = 0
        try:
            self._open_sink(str(filename), fps, frame_size)
            self._queue = queue.Queue(maxsize=self._max_queue_size)
            self._thread = threading.Thread(target=self._writer_loop, daemon=True)
            self._thread.start()
        except BaseException as e:
            # close() skips a writer whose thread never started, so the sink
            # (an ffmpeg child, a file) is released here, even on Ctrl-C.
            self._thread = self._queue = None
            self._abort_sink()
            if not isinstance(e, Exception):
                raise
            log.error("Failed to open writer for %s: %s", filename, e)
            return False
        self._running = True
        return True

    def write(self, frame, fill_before: int = 0) -> bool:
        """Enqueue a frame, preceded by ``fill_before`` repeats of the previous
        one; returns False if it was dropped (and with it, the fill)."""
        if not self._running or self.failed:
            return False
        if self._profile:
            self.max_queue_depth = max(self.max_queue_depth, self._queue.qsize())
        try:
            self._queue.put_nowait((frame, fill_before))
        except queue.Full:
            return False
        self._accepted += fill_before + 1
        return True

    def close(self, fill_after: int = 0) -> None:
        """Stop accepting frames, drain the queue, append ``fill_after`` repeats
        of the last frame, and finalize the file."""
        if self._thread is None:
            return
        self._running = False
        if fill_after > 0:
            self._queue.put((None, fill_after))  # blocking: a fill is never dropped
        self._queue.put(_SENTINEL)  # queued frames are written first
        self._thread.join()
        self._thread = None
        self._queue = None
        try:
            self._close_sink()
        except Exception as e:
            log.error("Failed to finalize video: %s", e)

    def _writer_loop(self) -> None:
        last = None
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                break
            frame, fill = item
            if self.failed:
                continue  # keep draining: close() waits for the sentinel
            # A fill repeats the previous frame; a leading one (nothing written
            # yet) repeats the frame it precedes.
            filler = last if last is not None else frame
            try:
                for _ in range(fill if filler is not None else 0):
                    self._write_frame(filler)
                    self.frames_written += 1
                if frame is not None:
                    t0 = time.perf_counter_ns() if self._profile else 0
                    self._write_frame(frame)
                    if self._profile:
                        self.encode_ns_samples.append(time.perf_counter_ns() - t0)
                    self.frames_written += 1
                    last = frame
            except Exception as e:
                self.failed = True
                self._on_sink_failure(e)

    def _open_sink(self, filename: str, fps: float, frame_size) -> None:
        raise NotImplementedError

    def _write_frame(self, frame) -> None:
        raise NotImplementedError

    def _close_sink(self) -> None:
        raise NotImplementedError

    def _abort_sink(self) -> None:
        """Release a sink that never received a frame."""
        self._close_sink()

    def _on_sink_failure(self, exc: Exception) -> None:
        log.error("Writer failed (%s); subsequent frames will be dropped", exc)


class FfmpegVideoWriter(AsyncFrameWriter):
    """Pipes Mono8 frames into an ffmpeg child. While ffmpeg falls behind the
    pipe write blocks and the queue absorbs it; if the child dies, writes are
    refused from then on and its stderr tail is logged (an MKV stays playable).
    """

    def __init__(
        self,
        ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS,
        max_queue_size: int = 20,
        *,
        profile: bool = False,
    ):
        super().__init__(max_queue_size, profile=profile)
        self.ffmpeg_params = ffmpeg_params
        self._proc: subprocess.Popen | None = None
        self._filename: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    @property
    def error_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def _open_sink(self, filename, fps, frame_size):
        width, height = frame_size
        # No -nostdin (see octacam.ffmpeg): stdin is the frame pipe.
        args = [
            find_ffmpeg(require_encoder=nvenc_encoder(self.ffmpeg_params)),
            "-hide_banner",
            "-loglevel",
            "warning",
            *rawvideo_input_args(width, height, fps),
            *output_args(self.ffmpeg_params, (width, height)),
            "-y",
            filename,
        ]
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

    def _abort_sink(self):
        # Killed, not finalized: a graceful close would leave an empty video.
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.stdin.close()
            proc.kill()
            proc.wait()

    def _close_sink(self):
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # ffmpeg only flushes its buffers now, but a slow preset can take a
        # while, and an early kill would truncate the file.
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
            self.failed = True
            log.error(
                "ffmpeg exited with code %d for %s%s",
                returncode,
                self._filename,
                ("\nffmpeg output:\n" + self.error_tail) if self._stderr_tail else "",
            )


class RawVideoWriter(AsyncFrameWriter):
    """Dumps raw Mono8 frames for ``octacam process``, which reads their
    geometry from the recording summary."""

    def __init__(self, max_queue_size: int = 20, *, profile: bool = False):
        super().__init__(max_queue_size, profile=profile)
        self._file = None

    def _open_sink(self, filename, fps, frame_size):
        self._file = open(Path(filename), "wb", buffering=0)

    def _write_frame(self, frame):
        _write_all(self._file, frame)

    def _close_sink(self):
        if self._file is not None:
            self._file.close()
            self._file = None


@dataclass(frozen=True)
class VideoFormat:
    """A recording method selectable from the GUI/CLI."""

    save_method: str  # "ffmpeg" | "raw"
    extension: str
    label: str
    ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS

    def create_writer(
        self, max_queue_size: int = 20, *, profile: bool = False
    ) -> AsyncFrameWriter:
        if self.save_method == "ffmpeg":
            return FfmpegVideoWriter(
                ffmpeg_params=self.ffmpeg_params,
                max_queue_size=max_queue_size,
                profile=profile,
            )
        if self.save_method == "raw":
            return RawVideoWriter(max_queue_size, profile=profile)
        raise ValueError(f"Unknown save method: {self.save_method}")


# Keyed by record.save_method. "nvenc" is an FfmpegVideoWriter with GPU params
# (see resolve_capture_formats); "raw" dumps Mono8 for an offline transcode.
FORMATS: dict[str, VideoFormat] = {
    "ffmpeg": VideoFormat("ffmpeg", "mkv", "x264 mkv (ffmpeg)"),
    "nvenc": VideoFormat(
        "ffmpeg", "mkv", "H.264 NVENC GPU (ffmpeg)", ffmpeg_params=NVENC_H264_PARAMS
    ),
    "raw": VideoFormat("raw", "raw", "raw Mono8 (transcode later)"),
}


def cpu_fallback_format(base: VideoFormat) -> VideoFormat:
    """A libx264 VideoFormat for a camera that gets no NVENC session, keeping
    *base*'s container and ``-pix_fmt`` so a mixed GPU+CPU take is uniform."""
    pix_fmt = pix_fmt_of(base.ffmpeg_params) or DEFAULT_PIX_FMT
    return VideoFormat(
        save_method="ffmpeg",
        extension=base.extension,
        label="x264 mkv (CPU fallback)",
        ffmpeg_params=(
            f"-c:v libx264 -preset {DEFAULT_PRESET} -crf {DEFAULT_CRF} "
            f"-pix_fmt {pix_fmt}"
        ),
    )


def resolve_capture_formats(
    base: VideoFormat, num_cameras: int, max_nvenc_sessions: int | None = None
) -> tuple[list[VideoFormat], list[str]]:
    """Per-camera formats for *base* within the GPU's NVENC session limit.

    For NVENC, every camera falls back to libx264 when no ffmpeg can run it, and
    otherwise those past the cap do: an over-cap session fails its init and
    records nothing. *max_nvenc_sessions* is the cap, None to detect it (cached).
    Returns (formats, warnings for the operator).
    """
    if num_cameras <= 0:
        return [], []
    encoder = nvenc_encoder(base.ffmpeg_params)
    if encoder is None:
        return [base] * num_cameras, []
    warnings: list[str] = []
    try:
        find_ffmpeg(require_encoder=encoder)
    except RuntimeError as e:
        warnings.append(
            f"GPU encoding ({encoder}) unavailable — {e} "
            f"Recording all {num_cameras} camera(s) on CPU (libx264) instead."
        )
        return [cpu_fallback_format(base)] * num_cameras, warnings
    if max_nvenc_sessions is None:
        # An NVENC ffmpeg exists, so an undetected cap lets every camera try.
        detected = nvenc_max_sessions(encoder)
        cap = num_cameras if detected is None else detected
    else:
        cap = max(0, max_nvenc_sessions)
    n_gpu = min(cap, num_cameras)
    formats = [base] * n_gpu + [cpu_fallback_format(base)] * (num_cameras - n_gpu)
    if n_gpu < num_cameras:
        warnings.append(
            f"{num_cameras} cameras exceed the NVENC session limit "
            f"(max_nvenc_sessions={cap}): the first {n_gpu} encode on GPU "
            f"({encoder}); the remaining {num_cameras - n_gpu} fall back to CPU "
            "(libx264). Raise record.max_nvenc_sessions if your GPU/driver "
            "allows more (see `octacam doctor`)."
        )
    return formats, warnings


# Tags a transcode's temp (see _partial_path), which folder scans skip.
PARTIAL_INFIX = ".octacam-part"


# Without flock, a temp idle this long is an orphan. Generous: deleting a live
# temp is worse than keeping a dead one, and a live ffmpeg touches it every few
# seconds.
_PARTIAL_IDLE_S = 6 * 3600


def _partial_path(output: Path) -> Path:
    """A hidden temp beside *output*: the same directory (an atomic rename), the
    real extension last (ffmpeg picks the muxer from it), and pid+uuid so two
    runs over one folder never share a temp or rename the other's."""
    return output.with_name(
        f".{output.stem}{PARTIAL_INFIX}.{os.getpid()}.{uuid.uuid4().hex}"
        f"{output.suffix}"
    )


def _partial_glob(output: Path) -> str:
    """Glob for every temp of *output*, the pid-less ``.<stem>.octacam-part<ext>``
    included. Escaped: unescaped, a camera ``cam[1]`` would match (and sweep)
    ``cam1``'s temp."""
    return f".{glob.escape(output.stem)}{PARTIAL_INFIX}*{glob.escape(output.suffix)}"


def is_partial_transcode(path: Path) -> bool:
    """True for a transcode temp (see :func:`_partial_path`), which a hard kill
    can leave behind."""
    return PARTIAL_INFIX in path.name


def _partial_is_live(path: Path) -> bool:
    """Whether a process still writes *path*: :func:`_atomic_output` holds a
    flock on its temp while it owns it, so a lockable temp is an orphan. Without
    flock (some network mounts), an idle-mtime test. Anything uninspectable is
    live, so the sweep only ever errs toward keeping."""
    if fcntl is None:  # pragma: no cover - POSIX only
        return _partial_is_recent(path)
    try:
        handle = open(path, "r+")
    except OSError:
        return True
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True  # a live _atomic_output holds it
            return _partial_is_recent(path)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        with contextlib.suppress(OSError):
            handle.close()


def _partial_is_recent(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < _PARTIAL_IDLE_S
    except OSError:
        return True


def _sweep_orphan_partials(output: Path, keep: Path) -> None:
    """Delete *output*'s temps whose writer is gone, never a live one or *keep*."""
    try:
        stale_paths = list(output.parent.glob(_partial_glob(output)))
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
def _atomic_output(output: Path):
    """Yield a temp to encode into, renamed onto *output* only on success.

    Any exception, Ctrl-C included, deletes the temp, so a partial encode never
    appears at *output* or replaces it. The temp is created and flock-ed before
    ffmpeg runs (every caller passes ``-y``), so a concurrent run can tell it
    from an orphan.
    """
    tmp = _partial_path(output)
    try:
        lock = open(tmp, "w")
    except OSError:
        # Let the encode fail with the real error (read-only dir, ENOSPC).
        lock = None
    if lock is not None and fcntl is not None:
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


def transcode_raw(
    raw_path: Path,
    output: Path | None = None,
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
    """Encode a .raw dump to *output* (default ``<raw>.mkv``).

    The stream has no geometry: *width*/*height*/*fps* come from the recording
    summary and are required. *frames* (else the file size) sizes the bar.
    """
    raw_path = Path(raw_path)
    output = Path(output) if output else raw_path.with_suffix(".mkv")
    if width is None or height is None or fps is None:
        raise FileNotFoundError(
            f"no recording_summary.json geometry for {raw_path}; cannot "
            "determine width/height/fps to transcode the raw stream"
        )
    if pixel_format not in _RAW_PIXEL_FORMATS:
        raise ValueError(f"cannot transcode {pixel_format} raw video from {raw_path}")
    input_pix_fmt, bytes_per_pixel = _RAW_PIXEL_FORMATS[pixel_format]
    total_frames = frames
    if total_frames is None and width and height:
        total_frames = raw_path.stat().st_size // (width * height * bytes_per_pixel)
    with _atomic_output(output) as tmp:
        args = [
            find_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "warning",
            *rawvideo_input_args(width, height, fps, str(raw_path), input_pix_fmt),
            *output_args(ffmpeg_params, (width, height)),
            "-y",
            str(tmp),
        ]
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
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    *,
    width: int | None = None,
    height: int | None = None,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Re-encode an mkv/mp4 to *output*, never stream-copying: captures use a
    fast preset, and this offline pass is where a slow one pays off.

    *width*/*height* let a gray output become 4:2:0 (see
    :func:`octacam.ffmpeg.output_args`); *total_frames* sizes the bar.
    """
    src = Path(src)
    output = Path(output)
    ffmpeg = find_ffmpeg()
    frame_size = (width, height) if width and height else None
    with _atomic_output(output) as tmp:
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(src),
            *output_args(ffmpeg_params, frame_size),
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
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    *,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    pixel_format: str = "Mono8",
    frames: int | None = None,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Transcode one ``.raw``/``.mkv``/``.mp4`` to *output* (its extension picks
    the container). A ``.raw`` takes geometry and *frames* from the summary; an
    encoded input uses *width*/*height* only for its output pixel format and
    *total_frames* for the bar."""
    input_path = Path(input_path)
    if input_path.suffix == ".raw":
        return transcode_raw(
            input_path,
            output=output,
            ffmpeg_params=ffmpeg_params,
            width=width,
            height=height,
            fps=fps,
            pixel_format=pixel_format,
            frames=frames,
            on_progress=on_progress,
            raw_output=raw_output,
        )
    return transcode_encoded(
        input_path,
        output,
        ffmpeg_params=ffmpeg_params,
        width=width,
        height=height,
        total_frames=total_frames,
        on_progress=on_progress,
        raw_output=raw_output,
    )


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


def _run_ffmpeg(
    args: list[str],
    src: Path,
    *,
    on_progress: ProgressCallback | None = None,
    total_frames: int | None = None,
    raw_output: bool = False,
) -> None:
    """Run an ffmpeg transcode of *src*, raising RuntimeError on failure. With
    *raw_output* ffmpeg paints the terminal itself; otherwise its progress feeds
    *on_progress* and its stderr is shown only on failure."""
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
