"""Browser-driven tests for the vanilla-JS Record tab.

Loads the *real* ES modules (``web/static/js/record.js`` and its imports) in a
headless Chromium against the *real* ``index.html`` DOM, so the GUI wiring —
which fields exist, when they enable/disable, applySettings round-trips — is
exercised the way a browser actually runs it. This is the automated counterpart
to the manual "GUI headless render" recipe in CLAUDE.md, and it is what catches
the class of bug where a backend setting ships without a GUI control.

Opt-in: the ``playwright`` package lives in the ``frontend`` dependency group,
so the default ``uv run pytest`` skips this whole module (importorskip). Run it
with::

    uv run --group frontend pytest tests/test_frontend.py

The first time also needs a browser: ``uv run --group frontend playwright
install chromium``. If no usable Chromium is present the tests skip rather than
fail, so CI without the browser stays green.
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import (  # noqa: E402  (after importorskip)
    Error as PlaywrightError,
)
from playwright.sync_api import (
    Page,
    sync_playwright,
)

STATIC = Path(__file__).resolve().parents[1] / "src" / "octacam" / "web" / "static"
# Plugin UI bundles live in each plugin's own folder, served at /plugins/<name>/;
# the static server above only covers web/static, so a test that loads one routes
# it in from here (see the triggerbox timing-plot test).
TRIGGERBOX_JS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "octacam"
    / "plugins"
    / "triggerbox"
    / "web"
    / "triggerbox.js"
)

# The save-method dropdown is populated from these (mirrors writer.FORMATS as the
# server serializes it). NVENC_FORMATS adds the GPU method for the nvenc tests.
DEFAULT_FORMATS = [
    {"save_method": "ffmpeg", "label": "x264 mkv (ffmpeg)"},
    {"save_method": "raw", "label": "raw Mono8 (transcode later)"},
]
NVENC_FORMATS = [
    {"save_method": "ffmpeg", "label": "x264 mkv (ffmpeg)"},
    {"save_method": "nvenc", "label": "H.264 NVENC GPU (ffmpeg)"},
    {"save_method": "raw", "label": "raw Mono8 (transcode later)"},
]


@pytest.fixture(scope="session")
def static_server():
    """Serve web/static over HTTP so the browser can load ES modules (file://
    origins can't ``import``)."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(STATIC)
    )
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True)
        except PlaywrightError as e:  # no/incompatible browser installed
            pytest.skip(f"Playwright Chromium unavailable: {e}")
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(static_server, browser):
    page = browser.new_page()
    # Stub the app.js entry so the SPA never boots its WebSocket/plugin loader;
    # we only want the DOM plus the record.js module under test.
    page.route(
        "**/js/app.js",
        lambda route: route.fulfill(
            status=200, content_type="text/javascript", body=""
        ),
    )
    page.goto(f"{static_server}/index.html", wait_until="domcontentloaded")
    try:
        yield page
    finally:
        page.close()


def make_tab(page: Page, formats=DEFAULT_FORMATS, transfer_users=None, active_user=None) -> None:
    """Construct the real RecordTab against the loaded DOM, exposed as
    ``window.__tab``. Uses no-op plugin/notify deps so no network is touched."""
    page.evaluate(
        """async ({formats, transferUsers, activeUser}) => {
            const m = await import('./js/record.js');
            window.__tab = new m.RecordTab({
                formats,
                getPluginParams: () => ({}),
                notify: () => {},
                transferUsers,
                activeUser,
            });
        }""",
        {"formats": formats, "transferUsers": transfer_users, "activeUser": active_user},
    )


def prop(page: Page, selector: str, expr: str):
    return page.eval_on_selector(selector, expr)


# --------------------------------------------------------------------------- #


def test_save_method_dropdown_populated_from_formats(page):
    make_tab(page)
    values = page.eval_on_selector_all(
        "#format option", "els => els.map(e => e.value)"
    )
    assert values == ["ffmpeg", "raw"]


def test_ffmpeg_params_shown_for_ffmpeg_hidden_for_raw(page):
    # Even without nvenc in the dropdown, the CPU box shows for ffmpeg and hides
    # for raw (the encoder-params blocks swap by save method).
    make_tab(page)
    page.evaluate("() => window.__tab.applySettings({ save_method: 'ffmpeg' })")
    assert prop(page, "#ffmpeg-params-row", "e => e.hidden") is False
    page.evaluate("() => window.__tab.applySettings({ save_method: 'raw' })")
    assert prop(page, "#ffmpeg-params-row", "e => e.hidden") is True


def test_writer_queue_size_round_trips_from_settings(page):
    make_tab(page)
    page.evaluate("() => window.__tab.applySettings({ writer_queue_size: 128 })")
    assert prop(page, "#writer-queue-size", "e => e.value") == "128"


def test_writer_queue_size_change_rounds_to_int(page):
    """A typed non-integer must be rounded before the PUT (the server rejects a
    float writer_queue_size), and the rounded value written back to the input."""
    make_tab(page)
    body = page.evaluate(
        """async () => {
            let sent = null;
            window.fetch = async (url, opts) => {
                sent = JSON.parse(opts.body);
                return { ok: true, status: 200, json: async () => ({}) };
            };
            const el = document.getElementById('writer-queue-size');
            el.value = '64.7';
            el.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            await new Promise((r) => setTimeout(r, 0));
            return sent;
        }"""
    )
    assert body == {"writer_queue_size": 65}
    assert prop(page, "#writer-queue-size", "e => e.value") == "65"


# --- nvenc-specific GUI wiring --------------------------------------------- #


def _rows(page):
    return page.evaluate(
        """() => ({
            ffmpeg: document.getElementById('ffmpeg-params-row').hidden,
            nvenc: document.getElementById('nvenc-params-row').hidden,
            sessions: document.getElementById('nvenc-sessions-row').hidden,
        })"""
    )


def test_save_method_swaps_param_boxes(page):
    """The core fix: exactly the selected method's encoder box shows — the CPU
    box for ffmpeg, the GPU box + session cap for nvenc, neither for raw."""
    make_tab(page, formats=NVENC_FORMATS)
    page.evaluate("() => window.__tab.applySettings({ save_method: 'ffmpeg' })")
    assert _rows(page) == {"ffmpeg": False, "nvenc": True, "sessions": True}
    page.evaluate("() => window.__tab.applySettings({ save_method: 'nvenc' })")
    assert _rows(page) == {"ffmpeg": True, "nvenc": False, "sessions": False}
    page.evaluate("() => window.__tab.applySettings({ save_method: 'raw' })")
    assert _rows(page) == {"ffmpeg": True, "nvenc": True, "sessions": True}


def test_nvenc_params_round_trips_from_settings(page):
    make_tab(page, formats=NVENC_FORMATS)
    page.evaluate(
        "() => window.__tab.applySettings("
        "{ save_method: 'nvenc', nvenc_params: '-c:v h264_nvenc -cq 20' })"
    )
    assert prop(page, "#nvenc-params", "e => e.value") == "-c:v h264_nvenc -cq 20"


def test_max_nvenc_sessions_auto_and_override(page):
    make_tab(page, formats=NVENC_FORMATS)
    # null => auto-detect: checkbox on, manual box disabled.
    page.evaluate(
        "() => window.__tab.applySettings("
        "{ save_method: 'nvenc', max_nvenc_sessions: null })"
    )
    assert prop(page, "#nvenc-auto", "e => e.checked") is True
    assert prop(page, "#max-nvenc-sessions", "e => e.disabled") is True
    # an int => manual override: checkbox off, box enabled and showing the value.
    page.evaluate(
        "() => window.__tab.applySettings("
        "{ save_method: 'nvenc', max_nvenc_sessions: 4 })"
    )
    assert prop(page, "#nvenc-auto", "e => e.checked") is False
    assert prop(page, "#max-nvenc-sessions", "e => e.disabled") is False
    assert prop(page, "#max-nvenc-sessions", "e => e.value") == "4"


def test_nvenc_capabilities_displayed_and_default_filled(page):
    """Selecting nvenc lazily fetches the detected GPU cap, shows it, and (in auto
    mode) mirrors it into the disabled session box."""
    make_tab(page, formats=NVENC_FORMATS)
    result = page.evaluate(
        """async () => {
            window.fetch = async () => ({
                ok: true,
                status: 200,
                json: async () => ({
                    available: true, max_sessions: 8,
                    encoder: 'h264_nvenc', default_params: 'x',
                }),
            });
            window.__tab.applySettings({ save_method: 'nvenc', max_nvenc_sessions: null });
            await new Promise((r) => setTimeout(r, 0));
            await new Promise((r) => setTimeout(r, 0));
            return {
                text: document.getElementById('nvenc-detected').textContent,
                sessions: document.getElementById('max-nvenc-sessions').value,
            };
        }"""
    )
    assert "8" in result["text"]
    assert result["sessions"] == "8"


def test_nvenc_auto_toggle_change_sends_null_then_number(page):
    """The #nvenc-auto change handler: checking sends max_nvenc_sessions:null and
    disables the box; unchecking with a blank box seeds the detected cap (never a
    silent 0) and PUTs it."""
    make_tab(page, formats=NVENC_FORMATS)
    result = page.evaluate(
        """async () => {
            const sent = [];
            window.fetch = async (url, opts) => {
                sent.push(JSON.parse(opts.body));
                return { ok: true, status: 200, json: async () => ({}) };
            };
            const auto = document.getElementById('nvenc-auto');
            const num = document.getElementById('max-nvenc-sessions');
            window.__tab._nvencCaps = { available: true, max_sessions: 8 };
            auto.checked = true;
            num.value = '';
            auto.checked = false;                 // turn auto OFF, box blank
            auto.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            const afterUncheck = { disabled: num.disabled, value: num.value };
            auto.checked = true;                  // turn auto back ON
            auto.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            return { sent, afterUncheck, disabledWhenAuto: num.disabled };
        }"""
    )
    assert result["afterUncheck"] == {"disabled": False, "value": "8"}
    assert result["disabledWhenAuto"] is True
    assert result["sent"] == [
        {"max_nvenc_sessions": 8},
        {"max_nvenc_sessions": None},
    ]


def test_max_nvenc_sessions_change_rounds_to_int(page):
    make_tab(page, formats=NVENC_FORMATS)
    body = page.evaluate(
        """async () => {
            let sent = null;
            window.fetch = async (url, opts) => {
                sent = JSON.parse(opts.body);
                return { ok: true, status: 200, json: async () => ({}) };
            };
            const el = document.getElementById('max-nvenc-sessions');
            el.value = '3.7';
            el.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            await new Promise((r) => setTimeout(r, 0));
            return sent;
        }"""
    )
    assert body == {"max_nvenc_sessions": 4}


# --- per-user transfer profile dropdown ------------------------------------ #


def test_transfer_user_row_hidden_with_no_profiles(page):
    make_tab(page)  # no transfer_users given
    assert prop(page, "#transfer-user-row", "e => e.hidden") is True


def test_transfer_user_row_populated_and_preselected(page):
    make_tab(
        page,
        transfer_users={
            "MD": {"directory": "/mnt/store/MD/BallPushing_Imaging", "twophoton_source": None},
            "MA": {"directory": "/mnt/store/MA/octacam_2P", "twophoton_source": "/mnt/share/MA"},
        },
        active_user="MA",
    )
    assert prop(page, "#transfer-user-row", "e => e.hidden") is False
    values = page.eval_on_selector_all(
        "#transfer-user option", "els => els.map(e => e.value)"
    )
    # blank + sorted initials + the self-service option, in that order
    assert values == ["", "MA", "MD", "__add__"]
    assert prop(page, "#transfer-user", "e => e.value") == "MA"


def test_selecting_a_profile_patches_directory_and_twophoton_source(page):
    make_tab(
        page,
        transfer_users={
            "MA": {"directory": "/mnt/store/MA/octacam_2P", "twophoton_source": "/mnt/share/MA"},
        },
    )
    sent = page.evaluate(
        """async () => {
            let sent = null;
            window.fetch = async (url, opts) => {
                sent = { url, body: JSON.parse(opts.body) };
                return {
                    ok: true, status: 200,
                    json: async () => ({ transfer_directory: '/mnt/store/MA/octacam_2P' }),
                };
            };
            const sel = document.getElementById('transfer-user');
            sel.value = 'MA';
            sel.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            return sent;
        }"""
    )
    assert sent["url"] == "/api/settings"
    assert sent["body"] == {
        "transfer_directory": "/mnt/store/MA/octacam_2P",
        "transfer_twophoton_source": "/mnt/share/MA",
    }
    # applySettings (via _put) updates the visible field from the response.
    assert prop(page, "#transfer-dir", "e => e.value") == "/mnt/store/MA/octacam_2P"


def test_add_yourself_flow_posts_and_refreshes_dropdown(page):
    make_tab(page, transfer_users={"MD": {"directory": "/mnt/store/MD", "twophoton_source": None}})
    result = page.evaluate(
        """async () => {
            const posted = [];
            window.prompt = () => 'MA';
            window.fetch = async (url, opts) => {
                posted.push(JSON.parse(opts.body));
                return {
                    ok: true, status: 200,
                    json: async () => ({
                        status: 'ok', initials: 'MA', directory: '/mnt/store/MA/octacam_2P',
                        transfer_users: {
                            MD: { directory: '/mnt/store/MD', twophoton_source: null },
                            MA: { directory: '/mnt/store/MA/octacam_2P', twophoton_source: null },
                        },
                    }),
                };
            };
            const sel = document.getElementById('transfer-user');
            sel.value = '__add__';
            sel.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            await new Promise((r) => setTimeout(r, 0));
            return {
                posted,
                selected: sel.value,
                options: Array.from(sel.options).map((o) => o.value),
            };
        }"""
    )
    # Adding also immediately applies the new profile (same _onTransferUserChange
    # path a plain selection uses), so a settings PUT follows the users POST.
    assert result["posted"] == [
        {"initials": "MA"},
        {"transfer_directory": "/mnt/store/MA/octacam_2P"},
    ]
    assert result["selected"] == "MA"
    assert set(result["options"]) == {"", "MD", "MA", "__add__"}


def test_add_yourself_cancelled_prompt_reverts_selection(page):
    make_tab(page, transfer_users={"MD": {"directory": "/mnt/store/MD", "twophoton_source": None}})
    selected = page.evaluate(
        """async () => {
            window.prompt = () => null;  // user cancelled
            window.fetch = async () => { throw new Error('must not be called'); };
            const sel = document.getElementById('transfer-user');
            sel.value = '__add__';
            sel.dispatchEvent(new Event('change'));
            await new Promise((r) => setTimeout(r, 0));
            return sel.value;
        }"""
    )
    assert selected == ""


# --- advanced-options toggle ----------------------------------------------- #


def _flip_advanced(page, on: bool) -> None:
    page.evaluate(
        """(on) => {
            const t = document.getElementById('record-advanced-toggle');
            t.checked = on;
            t.dispatchEvent(new Event('change'));
        }""",
        on,
    )


# The exact essentials/advanced partition from the spec. Parametrized below so a
# regression that pushes an essential into the advanced block (or leaves an
# advanced knob among the essentials) fails a named case — not just the two-field
# spot-check the toggle test would otherwise give.
ESSENTIAL_IDS = [
    "duration-value", "fps", "record-dir", "relative-dir", "transfer-user", "transfer-dir",
]
ADVANCED_IDS = [
    "trigger-source",
    "preview-trigger-source",
    "format",
    "ffmpeg-params",
    "nvenc-params",
    "nvenc-auto",
    "max-nvenc-sessions",
    "record-form",
    "save-frame-timestamps",
    "writer-queue-size",
    "transcode-ffmpeg-params",
    "transfer-checksum",
]


def test_advanced_section_hidden_by_default(page):
    """The advanced knobs start collapsed and the switch is off."""
    make_tab(page)
    assert prop(page, "#record-advanced-toggle", "e => e.checked") is False
    assert prop(page, "#record-advanced", "e => e.hidden") is True


@pytest.mark.parametrize("field_id", ESSENTIAL_IDS)
def test_essential_fields_are_outside_advanced(page, field_id):
    """Each always-visible essential must live outside the advanced block."""
    make_tab(page)
    inside = prop(page, f"#{field_id}", "e => !!e.closest('#record-advanced')")
    assert inside is False


@pytest.mark.parametrize("field_id", ADVANCED_IDS)
def test_advanced_fields_are_inside_advanced(page, field_id):
    """Every non-essential knob must be nested under the advanced toggle."""
    make_tab(page)
    inside = prop(page, f"#{field_id}", "e => !!e.closest('#record-advanced')")
    assert inside is True


# --- keyboard shortcuts ---------------------------------------------------- #


def init_shortcuts(page: Page) -> None:
    """Install the real shortcut layer against the loaded DOM. ``window.__calls``
    records every method the (fake) grid receives, as ``[name, ...args]``."""
    page.evaluate(
        """async () => {
            const m = await import('./js/shortcuts.js');
            window.__calls = [];
            const grid = new Proxy({}, {
                get: (_, k) => (...args) => { window.__calls.push([k, ...args]); },
            });
            window.__sc = m.initShortcuts({ grid });
        }"""
    )


def key(page: Page, k: str, *, target: str | None = None, **mods) -> None:
    """Dispatch a keydown. With ``target``, the event originates on that element
    (so the typing-suppression guard sees it); otherwise on ``document``."""
    page.evaluate(
        """({ k, target, mods }) => {
            const el = target ? document.querySelector(target) : document;
            el.dispatchEvent(new KeyboardEvent('keydown', {
                key: k, bubbles: true, cancelable: true,
                ctrlKey: !!mods.ctrl, shiftKey: !!mods.shift, metaKey: !!mods.meta,
            }));
        }""",
        {"k": k, "target": target, "mods": mods},
    )


def _hidden(page: Page, selector: str) -> bool:
    return prop(page, selector, "e => e.classList.contains('hidden')")


def test_bare_key_shortcut_fires_and_is_suppressed_while_typing(page):
    """``t`` toggles the theme via the real button — but never while a text
    field holds focus (the edit-heavy GUI must not let bare keys eat input)."""
    init_shortcuts(page)
    page.evaluate(
        """() => {
            window.__c = 0;
            document.getElementById('theme-toggle')
                .addEventListener('click', () => window.__c++);
        }"""
    )
    key(page, "t")
    assert page.evaluate("() => window.__c") == 1
    # Same key, but originating from an input: suppressed.
    key(page, "t", target="#fps")
    assert page.evaluate("() => window.__c") == 1


def test_record_shortcut_respects_disabled(page):
    """Ctrl+Enter drives #record-button, and is a no-op while it's disabled — so
    it inherits the app's connected/finishing/pending gating for free."""
    init_shortcuts(page)
    disabled = page.evaluate(
        """() => {
            const btn = document.getElementById('record-button');
            window.__n = 0;
            btn.addEventListener('click', () => window.__n++);
            return btn.disabled;  // starts disabled in index.html
        }"""
    )
    assert disabled is True
    key(page, "Enter", ctrl=True)
    assert page.evaluate("() => window.__n") == 0
    page.evaluate("() => (document.getElementById('record-button').disabled = false)")
    key(page, "Enter", ctrl=True)
    assert page.evaluate("() => window.__n") == 1


def test_global_preview_shortcuts_call_grid(page):
    """[, ], f, 0 drive the grid's selection/zoom regardless of the active tab."""
    init_shortcuts(page)
    for k in ("[", "]", "f", "0"):
        key(page, k)
    names = page.evaluate("() => window.__calls.map(c => c[0])")
    assert names == [
        "selectPrev",
        "selectNext",
        "toggleMaximizeSelected",
        "resetZoomSelected",
    ]


def test_view_shortcut_scoped_to_active_tab(page):
    """``r`` rotates only while the View tab is active; on another tab it's inert
    (so the same letter is free to mean other things per tab)."""
    init_shortcuts(page)
    key(page, "r")  # Record tab is active by default
    assert page.evaluate("() => window.__calls.length") == 0
    page.evaluate(
        """() => document.querySelectorAll('#tabs button[data-tab]').forEach(
            b => b.classList.toggle('active', b.dataset.tab === 'view'))"""
    )
    key(page, "r")
    assert page.evaluate("() => window.__calls") == [
        ["applyView", {"rotateDelta": 90}, "selected"]
    ]


def test_digit_switches_tab_by_fixed_order(page):
    """Digit 3 clicks the View tab button (3rd in the fixed order), independent
    of overflow-menu packing."""
    init_shortcuts(page)
    clicks = page.evaluate(
        """() => {
            const clicks = {};
            for (const b of document.querySelectorAll('#tabs button[data-tab]')) {
                b.addEventListener('click', () => {
                    clicks[b.dataset.tab] = (clicks[b.dataset.tab] || 0) + 1;
                });
            }
            document.dispatchEvent(new KeyboardEvent('keydown',
                { key: '3', bubbles: true, cancelable: true }));
            return clicks;
        }"""
    )
    assert clicks == {"view": 1}


def test_help_overlay_toggles_and_lists_bindings(page):
    """``?`` opens the help overlay (rendered from the binding table), Escape
    closes it; it lists the sections and real keycaps."""
    init_shortcuts(page)
    assert _hidden(page, "#shortcuts-overlay") is True
    key(page, "?")
    assert _hidden(page, "#shortcuts-overlay") is False
    info = page.evaluate(
        """() => {
            const c = document.querySelector('#shortcuts-overlay .shortcuts-card');
            return {
                groups: [...c.querySelectorAll('.shortcuts-group-head')].map(e => e.textContent),
                kbds: [...c.querySelectorAll('kbd')].map(k => k.textContent),
                text: c.textContent,
            };
        }"""
    )
    assert "Recording" in info["groups"]
    assert "Enter" in info["kbds"]
    assert "Switch tab" in info["text"]
    key(page, "Escape")
    assert _hidden(page, "#shortcuts-overlay") is True


def test_record_button_gets_shortcut_title_hint(page):
    """The layer appends the shortcut to anchor buttons' tooltips for discovery."""
    init_shortcuts(page)
    title = prop(page, "#record-button", "e => e.title")
    assert "(Ctrl+Enter)" in title


def test_grid_keyboard_helpers_drive_selection_and_zoom(page):
    """The new CameraGrid helpers the shortcuts call: selectNext/Prev wrap the
    selection, zoomSelected/resetZoomSelected move zoom (and notify the server),
    and toggleMaximizeSelected maximizes the current tile."""
    result = page.evaluate(
        """async () => {
            const m = await import('./js/grid.js');
            const cams = [0, 1, 2].map((i) => ({
                serial: 'S' + i, name: 'C' + i, width: 640, height: 480,
                transform: {}, layout: {},
            }));
            let views = 0;
            const grid = new m.CameraGrid(
                document.getElementById('grid'), cams, { onViewChange: () => views++ });
            grid.select(0);
            grid.selectNext();                    // 0 -> 1
            const afterNext = grid.selected;
            grid.selectPrev(); grid.selectPrev(); // 1 -> 0 -> 2 (wrap)
            const afterWrap = grid.selected;
            grid.zoomSelected(2);
            const zoomed = grid.tiles[grid.selected].zoom;
            grid.resetZoomSelected();
            const reset = grid.tiles[grid.selected].zoom;
            grid.toggleMaximizeSelected();
            const maxed = grid.tiles[grid.selected].maximized;
            return { afterNext, afterWrap, zoomed, reset, maxed, views };
        }"""
    )
    assert result["afterNext"] == 1
    assert result["afterWrap"] == 2  # wrapped past 0
    assert result["zoomed"] > 1
    assert result["reset"] == 1
    assert result["maxed"] is True
    assert result["views"] > 0  # zoom/maximize pushed a fresh view spec


def test_advanced_toggle_reveals_and_hides_and_persists(page):
    """Flipping the switch shows/hides the advanced block and remembers the
    choice in localStorage."""
    make_tab(page)
    _flip_advanced(page, True)
    assert prop(page, "#record-advanced", "e => e.hidden") is False
    assert page.evaluate("() => localStorage.getItem('octacam.record.advanced')") == "1"
    _flip_advanced(page, False)
    assert prop(page, "#record-advanced", "e => e.hidden") is True
    assert page.evaluate("() => localStorage.getItem('octacam.record.advanced')") == "0"


def test_advanced_state_restored_from_storage(page):
    """A remembered "open" choice is restored when the tab is (re)constructed."""
    page.evaluate("() => localStorage.setItem('octacam.record.advanced', '1')")
    make_tab(page)
    assert prop(page, "#record-advanced-toggle", "e => e.checked") is True
    assert prop(page, "#record-advanced", "e => e.hidden") is False


# --- update-available banner (update.js) ----------------------------------- #

UPDATE_AVAILABLE = {
    "available": True,
    "current": "0.3.0",
    "latest": "0.9.0",
    "command": "uv tool upgrade octacam",
}


def _init_banner(page, payload):
    """Call the real initUpdateBanner against the loaded DOM; returns its bool."""
    return page.evaluate(
        """async (payload) => {
            const m = await import('./js/update.js');
            return m.initUpdateBanner(payload);
        }""",
        payload,
    )


def test_update_banner_shows_command_when_available(page):
    assert _init_banner(page, UPDATE_AVAILABLE) is True
    assert prop(page, "#update-banner", "e => e.classList.contains('hidden')") is False
    assert "0.9.0" in prop(page, "#update-banner", "e => e.textContent")
    assert (
        prop(page, "#update-banner .update-cmd", "e => e.textContent")
        == "uv tool upgrade octacam"
    )


def test_update_banner_hidden_when_no_update(page):
    payload = {"available": False, "current": "0.3.0", "latest": None, "command": ""}
    assert _init_banner(page, payload) is False
    assert prop(page, "#update-banner", "e => e.classList.contains('hidden')") is True


def test_update_banner_dismiss_persists_until_newer(page):
    page.evaluate("() => localStorage.removeItem('octacam.updateDismissed')")
    assert _init_banner(page, UPDATE_AVAILABLE) is True
    page.click("#update-dismiss-btn")
    assert prop(page, "#update-banner", "e => e.classList.contains('hidden')") is True
    assert (
        page.evaluate("() => localStorage.getItem('octacam.updateDismissed')")
        == "0.9.0"
    )
    # Same version stays dismissed; a newer release re-shows the banner.
    assert _init_banner(page, UPDATE_AVAILABLE) is False
    assert _init_banner(page, {**UPDATE_AVAILABLE, "latest": "1.0.0"}) is True


# --- shutdown dialog (shut down & process) --------------------------------- #


def _make_shutdown(page: Page) -> None:
    """Construct the real ShutdownDialog against the loaded DOM as window.__sd."""
    page.evaluate(
        """async () => {
            const m = await import('./js/shutdown.js');
            window.__sd = new m.ShutdownDialog();
        }"""
    )


def test_shutdown_offers_three_way_choice_when_work_exists(page):
    _make_shutdown(page)
    result = page.evaluate(
        """async () => {
            const p = window.__sd.confirm({ recordingActive: false, hasWork: true, peerCount: 1 });
            const visible = !document.getElementById('shutdown-dialog').classList.contains('hidden');
            const btns = ['shutdown-cancel', 'shutdown-plain', 'shutdown-process']
                .every(id => document.getElementById(id) !== null);
            document.getElementById('shutdown-process').click();
            return { visible, btns, choice: await p };
        }"""
    )
    assert result == {"visible": True, "btns": True, "choice": "process"}


def test_shutdown_plain_button_resolves_shutdown(page):
    _make_shutdown(page)
    choice = page.evaluate(
        """async () => {
            const p = window.__sd.confirm({ recordingActive: false, hasWork: true, peerCount: 1 });
            document.getElementById('shutdown-plain').click();
            return await p;
        }"""
    )
    assert choice == "shutdown"


def test_shutdown_no_modal_when_nothing_recorded(page):
    _make_shutdown(page)
    result = page.evaluate(
        """async () => {
            const choice = await window.__sd.confirm({ recordingActive: false, hasWork: false });
            const hidden = document.getElementById('shutdown-dialog').classList.contains('hidden');
            return { choice, hidden };
        }"""
    )
    assert result == {"choice": "shutdown", "hidden": True}


def test_shutdown_while_recording_uses_binary_confirm(page):
    _make_shutdown(page)
    result = page.evaluate(
        """async () => {
            window.confirm = () => true;  // operator accepts the speed-bump
            const modalShown = [];
            const choice = await window.__sd.confirm({ recordingActive: true, hasWork: false, peerCount: 2 });
            const hidden = document.getElementById('shutdown-dialog').classList.contains('hidden');
            return { choice, hidden };  // no 3-way modal while recording
        }"""
    )
    assert result == {"choice": "shutdown", "hidden": True}


def test_shutdown_cancel_button_resolves_cancel(page):
    _make_shutdown(page)
    choice = page.evaluate(
        """async () => {
            const p = window.__sd.confirm({ recordingActive: false, hasWork: true, peerCount: 1 });
            document.getElementById('shutdown-cancel').click();
            return await p;
        }"""
    )
    assert choice == "cancel"


# --- deferred startup: shell renders first, grid fills in later ------------- #

# Replaces window.WebSocket before app.js boots so main()'s socket never hits a
# real server; window.__pushWs(obj) delivers a JSON message to the live socket.
_WS_STUB = """
window.__wsSockets = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.binaryType = "blob";
    window.__wsSockets.push(this);
    setTimeout(() => this.onopen && this.onopen({}), 0);
  }
  send() {}
  close() { this.readyState = 3; this.onclose && this.onclose({}); }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
window.WebSocket = FakeWebSocket;
window.__pushWs = (obj) => {
  const ws = window.__wsSockets[window.__wsSockets.length - 1];
  if (ws && ws.onmessage) ws.onmessage({ data: JSON.stringify(obj) });
};
"""


def _system_payload(*, ready, plugins=None):
    payload = {
        "version": "test",
        "update": None,
        "ready": ready,
        "init_error": None,
        "config_dir": "/x",
        "plugins": plugins or {},
        "managed_trigger_available": False,
        "display_refresh_interval_ms": 100,
        "theme": "dark",
        "formats": [
            {"save_method": "ffmpeg", "label": "H.264"},
            {"save_method": "raw", "label": "Raw"},
        ],
        "cameras": [],
    }
    if ready:
        payload["cameras"] = [
            {
                "index": 0,
                "serial": "S0",
                "name": "cam0",
                "width": 640,
                "height": 480,
                "params": {},
                "layout": {
                    "window_x": -1.0,
                    "window_y": -1.0,
                    "window_width": -1.0,
                    "window_height": -1.0,
                },
                "transform": {"scale_x": 1.0, "scale_y": 1.0, "rotation_deg": 0.0},
                "center_x": False,
                "center_y": False,
            }
        ]
    return payload


def _state_payload(*, ready):
    return {
        "state": "idle",
        "ready": ready,
        "init_error": None,
        "remaining_ms": None,
        "recording_id": None,
        "recordings_made": 0,
        "save_dir": "/x",
        "disk_free_bytes": 0,
        "settings": {"fps": 50.0, "duration_s": 20.0, "save_dir": "/x"},
        "cameras": [],
    }


def test_grid_placeholder_shows_then_fills_on_system_push(static_server, browser):
    """The real main() serves the shell immediately against a not-ready system
    (loading placeholder, no tiles), then builds the grid when a ready `system`
    message arrives over the socket — the whole point of deferred startup."""
    page = browser.new_page()
    ready = {"v": False}

    def json_route(builder):
        return lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(builder()),
        )

    # Catch-all first so specific routes (registered after) win; anything else
    # the SPA fetches at load (e.g. nvenc capabilities) just gets {}.
    page.route("**/api/**", json_route(dict))
    page.route("**/api/system", json_route(lambda: _system_payload(ready=ready["v"])))
    page.route("**/api/state", json_route(lambda: _state_payload(ready=ready["v"])))
    page.add_init_script(_WS_STUB)

    try:
        page.goto(f"{static_server}/index.html", wait_until="domcontentloaded")

        # Shell served against the not-ready system: a placeholder, zero tiles.
        page.wait_for_selector(".grid-placeholder", timeout=5000)
        assert page.eval_on_selector_all("#grid .tile", "els => els.length") == 0
        assert "Connecting" in page.eval_on_selector(
            ".grid-placeholder", "el => el.textContent"
        )

        # Cameras finished opening on the server: push the ready `system` message.
        ready["v"] = True
        page.evaluate(
            "(sys) => window.__pushWs(sys)",
            {"type": "system", **_system_payload(ready=True)},
        )

        # The grid fills in and the placeholder is gone — no reload.
        page.wait_for_selector("#grid .tile", timeout=5000)
        assert page.eval_on_selector_all("#grid .tile", "els => els.length") == 1
        assert page.eval_on_selector_all(".grid-placeholder", "els => els.length") == 0
    finally:
        page.close()


# The triggerbox tab's status slice: one Auto (cover exposure) strobe on ch1, so
# the timing plot has a light row whose on-time comes from the live exposures.
_TRIGGERBOX_STATUS = {
    "ready": True,
    "device": "/dev/ttyACM0",
    "arduino_state": "idle",
    "firmware": "TRIGGERBOX 2 abc1234",
    "firmware_ok": True,
    "firmware_state": "current",
    "needs_flash": False,
    "error": None,
    "guard_us": 100,
    "cameras": [{"pin": "D13", "pulse_us": 500, "delay_us": 0}],
    "lights": [
        {
            "channel": 1,
            "pin": "D5",
            "mode": "strobe",
            "duty_mode": "auto",
            "duty_percent": 20,
            "delay_us": 0,
        }
    ],
    "web": {"module": "/plugins/triggerbox/triggerbox.js"},
}


def test_triggerbox_auto_strobe_updates_when_the_camera_system_attaches(
    static_server, browser
):
    """An Auto strobe's on-time reaches the timing plot after deferred startup.

    Serve-first startup means the triggerbox tab is constructed — and reads
    /api/triggerbox/exposures — while the server still holds the hardware-free
    placeholder camera system, so it sees zero exposures and every Auto (cover
    exposure) strobe falls back to its manual duty percent. The `system` push
    that fills the grid in must also make the tab re-read the exposures, or the
    plot disagrees with the on-time the board is actually armed with for the
    whole session."""
    page = browser.new_page()
    ready = {"v": False}
    exposure_reads = {"n": 0}

    def json_route(builder):
        return lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(builder()),
        )

    def exposures():
        exposure_reads["n"] += 1
        # Before the cameras open the endpoint reports no cameras at all (it
        # iterates controller.camera_system, which is CameraSystem.pending()).
        cameras = (
            [{"index": 0, "name": "cam0", "exposure_us": 5000.0,
              "trigger_delay_us": 0.0}]
            if ready["v"]
            else []
        )
        return {"guard_us": 100, "duty_auto_default": False, "cameras": cameras}

    plugins = {"triggerbox": _TRIGGERBOX_STATUS}
    # Catch-all first so the specific routes registered after it win.
    page.route("**/api/**", json_route(dict))
    page.route(
        "**/api/system",
        json_route(lambda: _system_payload(ready=ready["v"], plugins=plugins)),
    )
    page.route("**/api/state", json_route(lambda: _state_payload(ready=ready["v"])))
    page.route("**/api/triggerbox/exposures", json_route(exposures))
    # The plugin's UI bundle lives outside web/static; serve the real file.
    page.route(
        "**/plugins/triggerbox/triggerbox.js",
        lambda route: route.fulfill(
            status=200,
            content_type="text/javascript",
            body=TRIGGERBOX_JS.read_text(),
        ),
    )
    page.add_init_script(_WS_STUB)

    try:
        page.goto(f"{static_server}/index.html", wait_until="domcontentloaded")

        # No exposures yet: the Auto strobe is drawn at its manual duty (20% of
        # the 20 ms period = 4 ms) and the summary says so.
        page.wait_for_function(
            """() => document.getElementById('triggerbox-timing-summary')
                     ?.textContent.includes('no camera exposures yet')""",
            timeout=5000,
        )
        assert (
            page.eval_on_selector(
                "#triggerbox-timing-viz rect.tb-led title", "el => el.textContent"
            )
            == "on 4.00 ms"
        )

        # Cameras finished opening on the server: push the ready `system`, which
        # carries each plugin's status slice through to its tab.
        ready["v"] = True
        page.evaluate(
            "(sys) => window.__pushWs(sys)",
            {"type": "system", **_system_payload(ready=True, plugins=plugins)},
        )

        # The Auto strobe now brackets the longest exposure + the guard band.
        page.wait_for_function(
            """() => document.getElementById('triggerbox-timing-summary')
                     ?.textContent.includes('longest exposure 5.00 ms + 100 µs guard')""",
            timeout=5000,
        )
        assert (
            page.eval_on_selector(
                "#triggerbox-timing-viz rect.tb-led title", "el => el.textContent"
            )
            == "on 5.10 ms"  # 5 ms exposure + 100 µs guard
        )
        # …and the guard band is drawn past the exposure it covers.
        assert (
            page.eval_on_selector_all(
                "#triggerbox-timing-viz rect.tb-guard", "els => els.length"
            )
            == 1
        )
        # The reads are debounced: a page load must not turn into a fetch storm.
        assert 2 <= exposure_reads["n"] <= 4, exposure_reads["n"]
    finally:
        page.close()
