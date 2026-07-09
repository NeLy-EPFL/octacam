"""Full node-map introspection for the standalone ``genicam.genapi`` binding.

Used by the Harvesters backend (and any GenTL consumer that exposes a
``genicam.genapi`` node map) to build the Camera tab's parameter browser: it
walks the device node map once, classifying each feature by its GenApi interface
type into the widget kinds :class:`~octacam.cameras.base.FeatureInfo` describes
(int/float/bool/enum/string/command), reading bounds and enum entries, and
grouping by category.

Everything is best-effort and node-local: any node whose introspection raises is
skipped (logged at debug), so one quirky feature never aborts the whole walk.
The Basler backend implements the same shape against ``pypylon.genicam`` (whose
API is PascalCase) rather than sharing this module.
"""

import logging

from octacam.cameras.base import BackendError, FeatureInfo

log = logging.getLogger("octacam")


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _type_name(genapi, itype) -> str | None:
    """Map an ``EInterfaceType`` to a FeatureInfo widget kind (None = skip)."""
    et = genapi.EInterfaceType
    return {
        et.intfIInteger: "int",
        et.intfIFloat: "float",
        et.intfIBoolean: "bool",
        et.intfIEnumeration: "enum",
        et.intfIString: "string",
        et.intfICommand: "command",
        et.intfICategory: "category",
    }.get(itype)


def _visibility_name(genapi, node) -> str:
    ev = genapi.EVisibility
    try:
        return {
            ev.Beginner: "beginner",
            ev.Expert: "expert",
            ev.Guru: "guru",
            ev.Invisible: "invisible",
        }.get(node.visibility, "beginner")
    except Exception:
        return "beginner"


def _is_included(genapi, node) -> bool:
    """Whether a node should appear in the browser (available, not Invisible)."""
    try:
        if not genapi.is_available(node):
            return False
    except Exception:
        return False
    vis = _visibility_name(genapi, node)
    return vis in ("beginner", "expert", "guru")


def _category_of(genapi, node) -> str:
    """The node's grouping category (first category-typed parent's label)."""
    et = genapi.EInterfaceType
    try:
        for parent in node.parents:
            try:
                if parent.principal_interface_type == et.intfICategory:
                    return parent.display_name or parent.name
            except Exception:
                continue
    except Exception:
        pass
    return "Other"


def _num_attr(node, attr):
    try:
        return getattr(node, attr)
    except Exception:
        return None


def _read_value(genapi, node, itype):
    """Current value of a value-bearing node, or None if unreadable."""
    try:
        if not genapi.is_readable(node):
            return None
        return node.value
    except Exception:
        return None


def _enum_entries(genapi, node) -> list[dict] | None:
    try:
        out = []
        for entry in node.entries:
            try:
                symbolic = entry.symbolic
            except Exception:
                continue
            if not symbolic:
                continue
            try:
                available = genapi.is_available(entry.node)
            except Exception:
                available = True
            out.append({"value": symbolic, "display": symbolic, "available": available})
        return out or None
    except Exception:
        return None


def _access(genapi, node) -> tuple[bool, bool]:
    """(readable, writable) from the node's access mode."""
    try:
        readable = bool(genapi.is_readable(node))
    except Exception:
        readable = False
    try:
        writable = bool(genapi.is_writable(node))
    except Exception:
        writable = False
    return readable, writable


def build_feature(genapi, node_map, node) -> FeatureInfo | None:
    """Build one :class:`FeatureInfo` from a generic ``INode`` (None = skip)."""
    try:
        itype = node.principal_interface_type
    except Exception:
        return None
    kind = _type_name(genapi, itype)
    if kind is None or kind == "category":
        return None
    name = node.name
    typed = getattr(node_map, name, None)  # typed node (value/bounds/entries)
    if typed is None:
        return None
    readable, writable = _access(genapi, node)
    try:
        display = node.display_name or name
    except Exception:
        display = name
    try:
        tooltip = node.tooltip or node.description or None
    except Exception:
        tooltip = None
    feature = FeatureInfo(
        name=name,
        display_name=display,
        type=kind,
        category=_category_of(genapi, node),
        readable=readable,
        writable=writable,
        visibility=_visibility_name(genapi, node),
        tooltip=tooltip,
    )
    if kind in ("int", "float"):
        feature.value = _read_value(genapi, typed, itype)
        feature.min = _num_attr(typed, "min")
        feature.max = _num_attr(typed, "max")
        feature.inc = _num_attr(typed, "inc")
        feature.unit = _num_attr(typed, "unit") or None
    elif kind == "bool":
        feature.value = _read_value(genapi, typed, itype)
    elif kind == "enum":
        feature.value = _read_value(genapi, typed, itype)
        feature.entries = _enum_entries(genapi, typed)
    elif kind == "string":
        feature.value = _read_value(genapi, typed, itype)
    # command: no value
    return feature


def walk_features(genapi, node_map) -> list[FeatureInfo]:
    """Every included feature in ``node_map``, grouped-order not guaranteed.

    Iterates the flat node list (each node's category is resolved from its
    parents), skipping category containers, Guru/hidden nodes, and anything that
    raises. The caller/UI groups by ``FeatureInfo.category``."""
    out: list[FeatureInfo] = []
    seen: set[str] = set()
    try:
        nodes = node_map.nodes
    except Exception as e:  # pragma: no cover - defensive
        raise BackendError(f"cannot enumerate node map: {e}") from e
    for node in nodes:
        try:
            if not node.is_feature or not _is_included(genapi, node):
                continue
            name = node.name
            if name in seen:
                continue
            seen.add(name)
            feature = build_feature(genapi, node_map, node)
            if feature is not None:
                out.append(feature)
        except Exception as e:  # one bad node must not abort the walk
            log.debug("Skipping node during feature walk: %s", e)
    return out


def read_feature(genapi, node_map, name: str) -> FeatureInfo:
    """Re-read one node into a FeatureInfo (raises BackendError if absent)."""
    try:
        node = node_map.get_node(name)
    except Exception as e:
        raise BackendError(f"no such node: {name}") from e
    if node is None:
        raise BackendError(f"no such node: {name}")
    feature = build_feature(genapi, node_map, node)
    if feature is None:
        raise BackendError(f"node {name} is not an editable feature")
    return feature


def _snap(value: float, node) -> float:
    """Clamp to [min, max] and round to inc for a numeric node (best-effort)."""
    lo = _num_attr(node, "min")
    hi = _num_attr(node, "max")
    inc = _num_attr(node, "inc")
    if inc:
        base = lo if lo is not None else 0.0
        value = base + round((value - base) / inc) * inc
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def write_feature(genapi, node_map, name: str, value: object) -> None:
    """Write one node, coercing ``value`` to the node's GenApi type."""
    try:
        node = node_map.get_node(name)
        itype = node.principal_interface_type
    except Exception as e:
        raise BackendError(f"no such node: {name}") from e
    kind = _type_name(genapi, itype)
    typed = getattr(node_map, name, None)
    if typed is None:
        raise BackendError(f"no such node: {name}")
    try:
        if kind == "int":
            typed.value = int(round(_snap(float(value), typed)))
        elif kind == "float":
            typed.value = float(_snap(float(value), typed))
        elif kind == "bool":
            typed.value = _to_bool(value)
        elif kind in ("enum", "string"):
            typed.value = str(value)
        else:
            raise BackendError(f"node {name} is not writable ({kind})")
    except BackendError:
        raise
    except Exception as e:
        raise BackendError(str(e)) from e


def execute_command(genapi, node_map, name: str) -> None:
    """Execute a command node."""
    try:
        node = node_map.get_node(name)
        itype = node.principal_interface_type
    except Exception as e:
        raise BackendError(f"no such node: {name}") from e
    if _type_name(genapi, itype) != "command":
        raise BackendError(f"node {name} is not a command")
    try:
        getattr(node_map, name).execute()
    except Exception as e:
        raise BackendError(str(e)) from e
