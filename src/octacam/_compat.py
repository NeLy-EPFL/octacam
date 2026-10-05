"""Standard-library features from Python 3.11 that octacam also needs on 3.10:
``tomllib`` (the ``tomli`` backport on 3.10) and ``StrEnum``."""

import sys

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

# A version_info guard, not try/except, so a type checker targeting 3.10 sees
# the backport (typeshed version-gates enum.StrEnum).
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised only on 3.10
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of :class:`enum.StrEnum` (typer's choice options)."""


__all__ = ["StrEnum", "tomllib"]
