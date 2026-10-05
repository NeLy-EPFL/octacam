"""FastAPI backend for the octacam web GUI.

One process serves the SPA, a REST control plane and one WebSocket carrying
preview JPEGs (binary), telemetry/state/event JSON and plugin messages such as
the flywheel jog. One socket works through a plain `ssh -L` forward and stays
under the browser's per-host connection limit. Preview frames follow the display
refresh rate, go newest-only to each client, and are encoded only for a client.

The routes live in per-area routers (system, record, cameras, save, ws). Their
HTTP handlers are sync ``def``: FastAPI runs them in its thread pool, so
blocking SDK, serial and filesystem calls never stall the WebSocket's loop.
"""

import asyncio
import contextlib
import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import octacam
from octacam import config_writer
from octacam.config import OctacamConfig
from octacam.controller import RecordingController
from octacam.web import cameras, record, save, system, ws
from octacam.web.hub import Hub
from octacam.web.preview import preview_loop
from octacam.web.state import AppState

STATIC_DIR = Path(__file__).parent / "static"


class _NoCacheStaticFiles(StaticFiles):
    """Static files with ``Cache-Control: no-cache``: the assets are unversioned,
    so a reload must revalidate (ETags still make an unchanged file a cheap 304)."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _default_shutdown() -> None:
    """SIGINT ourselves: uvicorn shuts down gracefully, then cli.gui's ``finally``
    releases the hardware."""
    os.kill(os.getpid(), signal.SIGINT)


def create_app(
    controller: RecordingController,
    config: OctacamConfig,
    config_dir: str = "",
    shutdown_callback: Callable[[], None] = _default_shutdown,
) -> FastAPI:
    """The GUI's app for ``controller``, serving the plugins it was built with."""
    hub = Hub()
    plugins = controller.plugins
    plugins.attach(broadcast=hub.publish)
    state = AppState(
        controller,
        hub,
        config,
        config_dir,
        raw_config=config_writer.load_raw_config(config_dir) if config_dir else {},
        # Resolved once, so the mounts and /api/system agree on which plugins
        # have a UI; a missing dir means none.
        plugin_web={
            p.name: p.web_dir
            for p in plugins.plugins
            if p.web_dir is not None and p.web_dir.is_dir()
        },
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        controller.add_listener(hub.publish)
        interval = max(state.config.gui.display_refresh_interval_ms, 10) / 1000
        tasks = [
            asyncio.create_task(preview_loop(hub, controller, interval)),
            asyncio.create_task(ws.telemetry_loop(hub, controller)),
        ]
        yield
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks)

    app = FastAPI(title="octacam", version=octacam.__version__, lifespan=lifespan)
    app.state.app_state = state  # read by cli.gui
    # GUI load never waits on the PyPI check.
    threading.Thread(
        target=state.refresh_update_notice,
        name="octacam-update-check",
        daemon=True,
    ).start()

    app.include_router(system.router(state, shutdown_callback))
    app.include_router(record.router(state))
    app.include_router(cameras.router(state))
    app.include_router(save.router(state))
    app.include_router(ws.router(state))
    # Plugin REST routes, registered before the "/" catch-all.
    for plugin in plugins.plugins:
        router = plugin.api_router()
        if router is not None:
            app.include_router(router)

    # Before the "/" catch-all, whose html=True fallback would answer a /plugins/
    # path with index.html (not runnable as a module). No html=True here, so a
    # missing plugin asset 404s.
    for name, adir in state.plugin_web.items():
        app.mount(
            f"/plugins/{name}",
            _NoCacheStaticFiles(directory=adir),
            name=f"plugin-{name}",
        )

    if STATIC_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
