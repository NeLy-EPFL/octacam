"""Asynchronous video writers.

Every writer runs a sink on a background thread behind a bounded queue:
write() never blocks the grab loop and drops the frame when the queue is
full (or once the sink has failed). Available sinks:

- FfmpegVideoWriter (default): pipes raw GRAY8 frames to an ffmpeg child
  encoding H.264 with libx264 in true monochrome 4:0:0. Encoding happens
  entirely in the child process, outside the GIL. Validated on the rig:
  8 parallel ultrafast encoders sustain >1200 fps aggregate at 1080p
  (see docs/web-gui-plan.md).
- OpencvVideoWriter: the original cv2.VideoWriter path (MJPG avi, avc1 mp4).
- RawVideoWriter: raw Mono8 dump + JSON sidecar, for `octacam transcode`.
"""

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

_SENTINEL = None


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
) -> list[str]:
    """ffmpeg argv encoding rawvideo GRAY8 (from `source`) to x264.

    pix_fmt "gray" produces true monochrome 4:0:0 H.264 (decodes in all
    ffmpeg-based tools; browsers would need yuv420p). Note: ffprobe shows
    such streams as yuvj420p because the H.264 decoder synthesizes neutral
    chroma; the x264 encoder log ("4:0:0, 8-bit") is the source of truth.
    """
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "rawvideo",
        "-pixel_format", "gray",
        "-video_size", f"{width}x{height}",
        "-framerate", f"{fps:g}",
        "-i", source,
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", str(crf),
        "-pix_fmt", pix_fmt,
        "-y", str(output),
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

    @property
    def failed(self) -> bool:
        """True once the sink has died; subsequent writes are dropped."""
        return self._failed

    def open(self, filename: str, fps: float, frame_size: tuple[int, int]) -> bool:
        """Open `filename` for writing. frame_size is (width, height)."""
        self.close()
        try:
            self._open_sink(str(filename), fps, frame_size)
        except Exception as e:
            log.error("Failed to open writer for %s: %s", filename, e)
            return False
        self._failed = False
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


class OpencvVideoWriter(AsyncFrameWriter):
    """cv2.VideoWriter sink (the original octacam writer)."""

    def __init__(
        self, fourcc: str, max_queue_size: int = 20, is_color: bool = False
    ):
        super().__init__(max_queue_size)
        self._fourcc = fourcc
        self._is_color = is_color
        self._writer = None

    def _open_sink(self, filename, fps, frame_size):
        import cv2

        writer = cv2.VideoWriter(
            filename,
            cv2.VideoWriter_fourcc(*self._fourcc),
            fps,
            frame_size,
            self._is_color,
        )
        if not writer.isOpened():
            writer.release()
            raise RuntimeError("cv2.VideoWriter could not open the file")
        self._writer = writer

    def _write_frame(self, frame):
        self._writer.write(frame)

    def _close_sink(self):
        if self._writer is not None:
            self._writer.release()
            self._writer = None


class FfmpegVideoWriter(AsyncFrameWriter):
    """Pipes raw GRAY8 frames into an ffmpeg child encoding H.264 (libx264).

    The pipe write blocks when ffmpeg falls behind; the bounded queue absorbs
    that and drops on full, preserving the drop-accounting contract. If the
    child dies mid-recording, write() returns False from then on and the
    stderr tail is logged (MKV output stays playable up to that point).
    """

    def __init__(
        self,
        crf: int = 16,
        preset: str = "ultrafast",
        pix_fmt: str = "gray",
        remux_mp4: bool = False,
        max_queue_size: int = 20,
    ):
        super().__init__(max_queue_size)
        self.crf = crf
        self.preset = preset
        self.pix_fmt = pix_fmt
        self.remux_mp4 = remux_mp4
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
            find_ffmpeg(), filename, fps, width, height,
            self.crf, self.preset, self.pix_fmt,
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
            "ffmpeg writer for %s failed (%s); subsequent frames will be "
            "dropped%s",
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
        try:
            returncode = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg did not exit after stdin close; killing it")
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
                ("\nffmpeg output:\n" + self.error_tail)
                if self._stderr_tail
                else "",
            )
        elif self.remux_mp4:
            self._remux()

    def _remux(self):
        source = Path(self._filename)
        target = source.with_suffix(".mp4")
        result = subprocess.run(
            [
                find_ffmpeg(), "-hide_banner", "-loglevel", "warning",
                "-y", "-i", str(source), "-c", "copy", str(target),
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

_OPENCV_FOURCC = {"mjpg": "MJPG", "h264": "avc1"}


@dataclass(frozen=True)
class VideoFormat:
    """A recording format selectable from the GUI/CLI."""

    codec: str  # "x264" | "raw" | "mjpg" | "h264"
    extension: str
    label: str
    crf: int = 16
    preset: str = "ultrafast"
    pix_fmt: str = "gray"
    remux_mp4: bool = False

    def create_writer(self, max_queue_size: int = 20) -> AsyncFrameWriter:
        if self.codec == "x264":
            return FfmpegVideoWriter(
                crf=self.crf,
                preset=self.preset,
                pix_fmt=self.pix_fmt,
                remux_mp4=self.remux_mp4,
                max_queue_size=max_queue_size,
            )
        if self.codec == "raw":
            return RawVideoWriter(max_queue_size)
        if self.codec in _OPENCV_FOURCC:
            return OpencvVideoWriter(_OPENCV_FOURCC[self.codec], max_queue_size)
        raise ValueError(f"Unknown codec: {self.codec}")


# Insertion order defines the GUI combo order; index 0 is the default
# (config key video_writer_default_index).
FORMATS: dict[str, VideoFormat] = {
    "x264": VideoFormat("x264", "mkv", "x264 mkv (ffmpeg)"),
    "raw": VideoFormat("raw", "raw", "raw Mono8 (transcode later)"),
    "mjpg": VideoFormat("mjpg", "avi", "MJPG avi (opencv)"),
    "h264": VideoFormat("h264", "mp4", "avc1 mp4 (opencv)"),
}


def transcode_raw(
    raw_path: Path,
    crf: int = 16,
    preset: str = "ultrafast",
    pix_fmt: str = "gray",
    output: Path | None = None,
) -> Path:
    """Transcode a .raw Mono8 dump (with its .json sidecar) to x264 MKV."""
    raw_path = Path(raw_path)
    meta = json.loads(raw_path.with_suffix(".json").read_text())
    output = Path(output) if output else raw_path.with_suffix(".mkv")
    args = build_x264_args(
        find_ffmpeg(),
        str(output),
        meta["fps"],
        meta["width"],
        meta["height"],
        crf,
        preset,
        pix_fmt,
        source=str(raw_path),
    )
    result = subprocess.run(args, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {raw_path}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return output
