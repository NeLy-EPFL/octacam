"""octacam: preview, record, and save synchronized video from many scientific
cameras (Basler, FLIR, and any GenICam USB3-Vision camera)."""

from importlib.metadata import PackageNotFoundError, version

try:
    # The installed distribution metadata is the single source of truth for the
    # version; it is set from pyproject.toml's [project].version at build/install
    # time. Bump it there (and tag) — never hard-code a second copy here.
    __version__ = version("octacam")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
