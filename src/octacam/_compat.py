"""Small standard-library shims so octacam runs on Python 3.10.

octacam targets Python ≥3.10 (its lowest supported interpreter). Two stdlib
features octacam uses only arrived in 3.11, so they are polyfilled here and
imported from this one module instead of the stdlib:

* ``tomllib`` — the TOML *reader* (3.11+). On 3.10 the pip-installable ``tomli``
  package is a drop-in (same ``loads`` / ``TOMLDecodeError``), pulled in via the
  ``tomli; python_version < "3.11"`` dependency.
* ``StrEnum`` — ``enum.StrEnum`` (3.11+); the ``str``-mixin enum typer uses for
  its choice options. The pre-3.11 equivalent is a plain ``class StrEnum(str,
  Enum)``.
"""

import sys

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[no-redef]

# A version_info guard (rather than try/except ImportError) so a static checker
# targeting 3.10 statically selects the backport branch — enum.StrEnum is
# version-gated in typeshed, so a from-import would otherwise read as Unknown.
if sys.version_info >= (3, 11):
    from enum import StrEnum
else:  # pragma: no cover - exercised only on 3.10
    from enum import Enum

    class StrEnum(str, Enum):
        """Backport of :class:`enum.StrEnum` for Python 3.10.

        A str-mixin enum whose members are usable directly as strings (what
        typer's choice options rely on)."""


__all__ = ["StrEnum", "tomllib"]
