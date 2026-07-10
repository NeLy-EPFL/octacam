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

# The save-method dropdown is populated from these (mirrors writer.FORMATS as the
# server serializes it); "nvenc" is added by the nvenc-specific test module.
DEFAULT_FORMATS = [
    {"save_method": "ffmpeg", "label": "x264 mkv (ffmpeg)"},
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


def make_tab(page: Page, formats=DEFAULT_FORMATS) -> None:
    """Construct the real RecordTab against the loaded DOM, exposed as
    ``window.__tab``. Uses no-op plugin/notify deps so no network is touched."""
    page.evaluate(
        """async (formats) => {
            const m = await import('./js/record.js');
            window.__tab = new m.RecordTab({
                formats,
                getPluginParams: () => ({}),
                notify: () => {},
            });
        }""",
        formats,
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


def test_ffmpeg_params_disabled_for_raw_enabled_for_ffmpeg(page):
    make_tab(page)
    page.evaluate("() => window.__tab.applySettings({ save_method: 'raw' })")
    assert prop(page, "#ffmpeg-params", "e => e.disabled") is True
    page.evaluate("() => window.__tab.applySettings({ save_method: 'ffmpeg' })")
    assert prop(page, "#ffmpeg-params", "e => e.disabled") is False


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
