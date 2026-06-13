"""Precise periodic callback timer."""

import threading
import time


class PreciseTimer:
    """Calls `callback` at a fixed frequency on a dedicated thread.

    Like the C++ PreciseTimer, there is no catch-up protection: if a tick is
    late, subsequent ticks fire immediately until the schedule is caught up,
    keeping the average rate (and thus total frame count) at the target.
    """

    def __init__(self, callback):
        self._callback = callback
        self._interval = 0.01  # seconds
        self._running = False
        self._thread: threading.Thread | None = None

    def set_frequency(self, hz: float) -> None:
        if hz <= 0.0:
            return
        self._interval = 1.0 / hz

    def start(self, duration: float | None = None) -> None:
        """Start firing; if `duration` (seconds) is given, stop after it."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, args=(duration,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    def _run(self, duration: float | None) -> None:
        next_time = time.monotonic()
        end_time = None if duration is None else next_time + duration
        while self._running and (end_time is None or next_time < end_time):
            next_time += self._interval
            self._callback()
            delay = next_time - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self._running = False
