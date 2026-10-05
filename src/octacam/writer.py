"""Capture writers: the per-camera video sinks a recording writes into.

A writer runs its sink on a thread behind a bounded queue, so write() never
blocks the grab loop: it refuses a frame when the queue is full or the sink has
failed. FfmpegVideoWriter pipes Mono8 frames into an ffmpeg child, which encodes
outside the GIL; RawVideoWriter dumps them for ``octacam process`` to transcode
(the geometry is in the recording summary).
"""

# The sink handles (_queue/_proc/_file) exist only between open() and close(),
# which pyright cannot follow across methods.
# pyright: reportOptionalMemberAccess=false

import logging
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from octacam.ffmpeg import (
    DEFAULT_PIX_FMT,
    find_ffmpeg,
    nvenc_encoder,
    nvenc_max_sessions,
    output_args,
    pix_fmt_of,
    rawvideo_input_args,
)

log = logging.getLogger("octacam")

_SENTINEL = None
FINALIZE_TIMEOUT_S = 120  # max wait for ffmpeg to flush after stdin closes

# Capture: near-visually-lossless at a preset fast enough to keep up (on the rig,
# 8 parallel ultrafast encoders sustain >1200 fps in total at 1080p).
DEFAULT_CRF = 18
DEFAULT_PRESET = "ultrafast"

# The config's ffmpeg_params: the encoder's output args, run through
# ffmpeg.output_args (the 4:2:0/full-range policy) after the derived rawvideo
# input args.
DEFAULT_FFMPEG_PARAMS = f"-c:v libx264 -preset {DEFAULT_PRESET} -crf {DEFAULT_CRF} -pix_fmt {DEFAULT_PIX_FMT}"

# GPU capture (record.save_method = "nvenc", or any *_nvenc encoder). NVENC
# rejects 4:0:0, so yuv420p, which output_args keeps at 0-255 luma. NVENC
# ignores -crf, and -cq 16 matches libx264's -crf 18; -bf 0 buffers no B-frames.
# Cameras past the GPU's session limit fall back to libx264
# (resolve_capture_formats).
NVENC_H264_PARAMS = "-c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq 16 -bf 0 -pix_fmt yuv420p"


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
