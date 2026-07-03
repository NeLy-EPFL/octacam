"""Priority claiming in the backend cascade (CameraSystem._enumerate).

Mocks several backends enumerating overlapping serials and asserts that each
camera is claimed by the highest-priority tier that sees it, and never twice — so
a camera served by a vendor SDK is not also opened by harvesters/pycameleon.
"""

import octacam.cameras.system as system_mod
from octacam.cameras.system import CameraSystem


def _fake_backends(monkeypatch, layout: dict[str, list[str]]):
    """Wire system's resolve/select to fake tiers.

    ``layout`` maps backend name -> the serials that tier enumerates, in the
    intended cascade priority order (dict insertion order). Each tier gets a
    distinct sentinel factory so the claiming tier is identifiable by identity.
    """
    order = list(layout)
    factories = {name: (lambda h, _n=name: _n) for name in order}

    def fake_resolve(_selector):
        return order

    def fake_select(name):
        serials = layout[name]

        def enumerate_fn(requested):
            pairs = [(s, s) for s in serials]
            if requested:
                by = dict(pairs)
                return [(s, by[s]) for s in requested if s in by]
            return pairs

        return enumerate_fn, factories[name], "json"

    monkeypatch.setattr(system_mod, "resolve_backend_names", fake_resolve)
    monkeypatch.setattr(system_mod, "select_backend", fake_select)
    return factories


def _enumerate(selector="auto", requested=None):
    # Drive _enumerate without opening cameras (no __init__).
    obj = CameraSystem.__new__(CameraSystem)
    return obj._enumerate(selector, requested)


def test_highest_tier_claims_each_serial(monkeypatch):
    factories = _fake_backends(
        monkeypatch,
        {
            "vendor": ["A", "B"],
            "producer": ["B", "C"],
            "floor": ["A", "C", "D"],
        },
    )
    entries = _enumerate()
    claimed = {serial: factory for serial, _handle, factory in entries}
    # A/B belong to the vendor tier, C to the producer tier, D falls through to
    # the floor — each claimed by the first (highest-priority) tier that saw it.
    assert claimed["A"] is factories["vendor"]
    assert claimed["B"] is factories["vendor"]
    assert claimed["C"] is factories["producer"]
    assert claimed["D"] is factories["floor"]


def test_no_serial_is_claimed_twice(monkeypatch):
    _fake_backends(
        monkeypatch,
        {"vendor": ["A", "B"], "producer": ["A", "B"], "floor": ["A", "B"]},
    )
    entries = _enumerate()
    serials = [serial for serial, _handle, _factory in entries]
    assert serials == ["A", "B"]  # each appears once despite three tiers seeing it


def test_requested_order_and_dedup(monkeypatch):
    factories = _fake_backends(
        monkeypatch,
        {"vendor": ["A", "B"], "producer": ["B", "C"], "floor": ["C", "D"]},
    )
    entries = _enumerate(requested=["D", "A", "C"])
    assert [serial for serial, _h, _f in entries] == ["D", "A", "C"]
    claimed = {serial: factory for serial, _h, factory in entries}
    assert claimed["A"] is factories["vendor"]
    assert claimed["C"] is factories["producer"]
    assert claimed["D"] is factories["floor"]


def test_single_tier_passes_requested_straight_through(monkeypatch):
    # With one active backend, requested serials go straight to its enumeration
    # (preserving its own ordering / not-found warnings).
    _fake_backends(monkeypatch, {"solo": ["X", "Y", "Z"]})
    entries = _enumerate(requested=["Z", "X"])
    assert [serial for serial, _h, _f in entries] == ["Z", "X"]
