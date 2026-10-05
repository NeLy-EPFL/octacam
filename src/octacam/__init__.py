"""octacam: preview, record, and save synchronized video from many scientific
cameras (Basler, FLIR, and any GenICam USB3-Vision camera)."""

from importlib.metadata import PackageNotFoundError, version

try:  # pyproject.toml's [project].version is the only copy of the version
    __version__ = version("octacam")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
