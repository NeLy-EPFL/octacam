"""opto plugin: the LED board's channels, its (re)configuration and switching."""

import pytest
from helpers import wait_until
from test_triggerbox_plugin import (
    DEVICE,
    OPTO_CONFIG_MAGIC,
    OPTO_GATE_MAGIC,
    FakeLink,
    _gates,
    _opto_records,
    _written,
)

from octacam.plugins.opto import OptoPlugin
from octacam.plugins.triggerbox.plugin import TriggerboxPlugin
from octacam.plugins.triggerbox.protocol import PIN_LABELS, PROTOCOL_VERSION

OPTIONS = {
    "device": DEVICE,
    "channels": [
        {"pin": "D7"},
        {"pin": "D8"},
        {"pin": "D9", "freq_hz": 20, "pulse_ms": 10},
    ],
}


def _plugin(**options) -> tuple[OptoPlugin, FakeLink]:
    plugin = OptoPlugin.from_options({**OPTIONS, **options})
    link = FakeLink(plugin)
    plugin._link = link
    return plugin, link


@pytest.fixture
def resolves(monkeypatch):
    monkeypatch.setattr(
        "octacam.serial_ports.resolve_device", lambda configured: (DEVICE, "")
    )


def test_channels_and_the_shared_sketch():
    plugin, _ = _plugin()
    assert [(c.pin, c.freq_hz) for c in plugin.channels] == [
        ("D7", 0),
        ("D8", 0),
        ("D9", 20),
    ]
    assert plugin.drives_opto and not plugin.generates_trigger
    # The triggerbox sketch, so one build serves both boards.
    assert plugin.firmware is TriggerboxPlugin.firmware


@pytest.mark.parametrize(
    "channels, error",
    [
        ([{"pin": "D8"}, {"pin": "D8"}], "already a channel"),
        ([{"pin": "D3"}], "not an output"),
        ([{"pin": "Q1"}], "not an output"),
        (["D8"], "must be a table"),
        ([{"pin": "D5"}] * 7, "at most"),
    ],
)
def test_a_bad_channel_fails_the_plugin_load(channels, error):
    # Channel k is the k-th entry: skipping a bad one would renumber the rest.
    with pytest.raises((ValueError, TypeError), match=error):
        OptoPlugin.from_options({"channels": channels})


def test_open_sends_the_channels(resolves):
    plugin, link = _plugin()
    link.banner = f"TRIGGERBOX {PROTOCOL_VERSION} {plugin.needed_build}"
    assert plugin._open() is None
    (config,) = _written(link, OPTO_CONFIG_MAGIC)
    assert _opto_records(config) == [
        (PIN_LABELS.index("D7"), 0, 0),
        (PIN_LABELS.index("D8"), 0, 0),
        (PIN_LABELS.index("D9"), 10_000, 50_000),
    ]
    assert link.opto_configured


@pytest.mark.parametrize("banner", [None, "TRIGGERBOX 2 oldbuild", "FLYWHEEL 1 x"])
def test_a_board_that_does_not_speak_the_protocol_is_never_sent_anything(
    resolves, banner
):
    # An unknown sketch would take the packets' bytes as its own commands.
    plugin, link = _plugin()
    link.banner = banner
    plugin._open(recover=False)
    assert not plugin.firmware_ok
    assert plugin.set_opto(1, 1.0) is False
    assert _written(link, OPTO_CONFIG_MAGIC) == _written(link, OPTO_GATE_MAGIC) == []


def test_set_opto_configures_once_then_switches():
    plugin, link = _plugin()
    assert plugin.set_opto(2, 1.5) is True
    assert plugin.set_opto(2, 0) is True
    assert len(_written(link, OPTO_CONFIG_MAGIC)) == 1
    assert [_gates(g) for g in _written(link, OPTO_GATE_MAGIC)] == [
        [(1, 1500)],
        [(1, 0)],
    ]


def test_set_opto_refuses_unknown_channels_and_caps_the_window():
    plugin, link = _plugin()
    assert plugin.set_opto(0, 1.0) is False
    assert plugin.set_opto(4, 1.0) is False
    assert plugin.set_opto(1, 1e6) is True
    (gate,) = _written(link, OPTO_GATE_MAGIC)
    assert _gates(gate) == [(0, 600_000)]


def test_a_refused_config_is_reported():
    plugin, link = _plugin()
    link.opto_reject_code = "p"
    assert plugin.set_opto(1, 1.0) is False
    assert _written(link, OPTO_GATE_MAGIC) == []
    assert "unknown pin id" in (plugin.last_error or "")


def test_a_refused_gate_makes_the_next_one_reconfigure(caplog):
    plugin, link = _plugin()
    assert plugin.set_opto(1, 1.0)
    link._feed(b"gp\n")  # the board lost its config (e.g. it rebooted)
    assert "no such opto channel" in caplog.text
    assert plugin.set_opto(1, 1.0)
    assert len(_written(link, OPTO_CONFIG_MAGIC)) == 2


def test_teardown_switches_every_channel_off():
    plugin, link = _plugin()
    plugin.set_opto(3, 5.0)
    plugin.teardown()
    assert _gates(_written(link, OPTO_GATE_MAGIC)[-1]) == [(0, 0), (1, 0), (2, 0)]
    assert not link.is_open


def test_status_lists_the_channels():
    plugin, _ = _plugin()
    assert [c["pin"] for c in plugin.status()["channels"]] == ["D7", "D8", "D9"]


# --------------------------------------------------------------- override


def test_a_forced_channel_stays_on_whatever_else_asks(monkeypatch):
    plugin, link = _plugin()
    assert plugin.force(2, True)
    assert plugin.opto_forced(2) and not plugin.opto_forced(1)
    assert _gates(_written(link, OPTO_GATE_MAGIC)[-1]) == [(1, 1000)]
    sent = len(_written(link, OPTO_GATE_MAGIC))
    # The closed loop switching it off (or on) changes nothing.
    assert plugin.set_opto(2, 0.0) is True and plugin.set_opto(2, 5.0) is True
    assert len(_written(link, OPTO_GATE_MAGIC)) == sent
    assert plugin.status()["forced"] == [2]
    assert plugin.force(2, False)
    assert _gates(_written(link, OPTO_GATE_MAGIC)[-1]) == [(1, 0)]
    assert not plugin.opto_forced(2) and plugin.status()["forced"] == []


def test_a_forced_channel_is_renewed_until_released(monkeypatch):
    from octacam.plugins import opto

    monkeypatch.setattr(opto, "OVERRIDE_STEP_S", 0.01)
    monkeypatch.setattr(
        "octacam.serial_ports.resolve_device", lambda configured: (DEVICE, "")
    )
    plugin, link = _plugin()
    link.banner = f"TRIGGERBOX {PROTOCOL_VERSION} {plugin.needed_build}"
    plugin.setup()
    try:
        plugin.force(3, True)
        assert wait_until(lambda: len(_written(link, OPTO_GATE_MAGIC)) >= 4, timeout=2)
        assert all(_gates(g) == [(2, 1000)] for g in _written(link, OPTO_GATE_MAGIC))
    finally:
        plugin.teardown()
    # Teardown releases it: every channel off.
    assert _gates(_written(link, OPTO_GATE_MAGIC)[-1]) == [(0, 0), (1, 0), (2, 0)]
    assert not plugin.opto_forced(3)


def test_override_route():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    plugin, _ = _plugin()
    app = FastAPI()
    app.include_router(plugin.api_router())
    with TestClient(app) as client:
        assert client.post(
            "/api/opto/override", json={"channel": 1, "on": True}
        ).json() == {
            "ok": True,
            "forced": [1],
        }
        assert (
            client.post(
                "/api/opto/override", json={"channel": 4, "on": True}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/opto/override", json={"channel": 1, "on": False}).json()[
                "forced"
            ]
            == []
        )
