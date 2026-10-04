"""Small utilities shared by the test modules."""

import time
from collections.abc import Callable


def wait_until(
    predicate: Callable[[], object], timeout: float = 5.0, interval: float = 0.005
) -> bool:
    """Poll ``predicate`` until it is truthy; False if ``timeout`` seconds pass first."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True
