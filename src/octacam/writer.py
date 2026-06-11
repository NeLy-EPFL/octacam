"""Asynchronous video writer. Port of cpp/src/video_writer.{hpp,cpp}.

cv2.VideoWriter on a background thread behind a bounded queue: write() never
blocks the grab loop and drops the frame when the queue is full. Phase 0
benchmarks picked OpenCV's MJPG over PyAV's mjpeg (faster, and encoding
releases the GIL); see benchmarks/README.md.
"""

import logging
import queue
import threading

import cv2
import numpy as np

log = logging.getLogger("octacam")

_SENTINEL = None


class AsyncVideoWriter:
    """Bounded-queue writer matching OpencvVideoWriter's contract.

    write() takes ownership of the frame array (the caller must not mutate it
    afterwards); the C++ version cloned instead, but callers here always pass
    a freshly owned copy from GrabResult.Array.
    """

    def __init__(self, max_queue_size: int = 20):
        self._max_queue_size = max_queue_size
        self._queue: queue.Queue | None = None
        self._writer: cv2.VideoWriter | None = None
        self._thread: threading.Thread | None = None
        self._running = False

    def open(
        self,
        filename: str,
        fourcc: str,
        fps: float,
        frame_size: tuple[int, int],
        is_color: bool = False,
    ) -> bool:
        """Open `filename` for writing. frame_size is (width, height)."""
        self.close()

        writer = cv2.VideoWriter(
            str(filename),
            cv2.VideoWriter_fourcc(*fourcc),
            fps,
            frame_size,
            is_color,
        )
        if not writer.isOpened():
            writer.release()
            return False

        self._writer = writer
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        return True

    def write(self, frame: np.ndarray) -> bool:
        """Enqueue a frame; returns False if it was dropped (queue full)."""
        if not self._running:
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
        self._writer.release()
        self._writer = None

    def _writer_loop(self) -> None:
        while True:
            frame = self._queue.get()
            if frame is _SENTINEL:
                break
            self._writer.write(frame)
