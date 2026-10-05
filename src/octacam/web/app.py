"""FastAPI backend for the octacam web GUI.

One process serves the SPA, a REST control plane and one WebSocket carrying
preview JPEGs (binary), telemetry/state/event JSON and plugin messages such as
the flywheel jog. One socket works through a plain `ssh -L` forward and stays
under the browser's per-host connection limit. Preview frames follow the display
refresh rate, go newest-only to each client, and are encoded only for a client.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import signal
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import octacam
from octacam import config_writer, updates
from octacam.config import OctacamConfig, safe_segment
from octacam.controller import RecordingController, StartResult
from octacam.web.hub import EVENT_BACKLOG_REPLAY, Client, Hub, to_json
from octacam.web.preview import parse_views, preview_loop
from octacam.ffmpeg import nvenc_max_sessions
from octacam.writer import FORMATS, NVENC_H264_PARAMS

log = logging.getLogger("octacam")

STATIC_DIR = Path(__file__).parent / "static"
TELEMETRY_INTERVAL_S = 0.5


class _NoCacheStaticFiles(StaticFiles):
    """Static files with ``Cache-Control: no-cache``: the assets are unversioned,
    so a reload must revalidate (ETags still make an unchanged file a cheap 304)."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


class SaveDirValidateRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value


class BrowseRequest(BaseModel):
    """A directory to list for the save-dir picker; blank means the save dir."""

    model_config = ConfigDict(extra="forbid")

    path: str = ""


class RecordingStartRequest(BaseModel):
    confirm_overwrite: bool = False
    plugin_params: dict | None = None


class ShutdownRequest(BaseModel):
    process_after: bool = False


class DiagnosticRunRequest(BaseModel):
    """Parameters for a Benchmark run (octacam.diagnostics)."""

    model_config = ConfigDict(extra="forbid")

    # None targets the current Record-tab fps; a value overrides it for this run.
    target_fps: float | None = None
    duration_s: float = 5.0
    find_max: bool = True
    sink: Literal["config", "null"] = "config"

    @field_validator("duration_s")
    @classmethod
    def _duration_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("duration_s must be > 0")
        return value

    @field_validator("target_fps")
    @classmethod
    def _fps_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("target_fps must be > 0")
        return value


class CameraFeaturePatch(BaseModel):
    """Set one node-map feature by GenApi name; the backend coerces ``value`` to
    the node's type (an enum sends its symbol)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value: Any
    scope: Literal["selected", "all"] = "selected"


class CameraFeatureReset(BaseModel):
    """Reset one node-map feature to its config value (else factory default)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    scope: Literal["selected", "all"] = "selected"


class CameraCommandRequest(BaseModel):
    """Execute one command node (e.g. TimestampLatch) on the selected camera."""

    model_config = ConfigDict(extra="forbid")

    name: str


class CameraCenterPatch(BaseModel):
    """Toggle ROI auto-centering on an axis for one camera or all."""

    model_config = ConfigDict(extra="forbid")

    axis: Literal["x", "y"]
    enabled: bool
    scope: Literal["selected", "all"] = "selected"


class CameraNamePatch(BaseModel):
    """Rename one camera (validated and applied by the controller)."""

    model_config = ConfigDict(extra="forbid")

    name: str


class CameraTransformPatch(BaseModel):
    """One camera's live display transform (negative scale = flip), sent on every
    rotate/flip so a "display"-form recording bakes in what the operator sees."""

    model_config = ConfigDict(extra="forbid")

    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0


class CameraDisplayParams(BaseModel):
    """A camera's display state to save; defaults mirror CameraConfig (-1: unset)."""

    model_config = ConfigDict(extra="forbid")

    serial: str
    name: str | None = None
    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0
    window_x: float = -1.0
    window_y: float = -1.0
    window_width: float = -1.0
    window_height: float = -1.0
    center_x: bool = False
    center_y: bool = False

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str | None) -> str | None:
        # A video filename stem: the live rename's rules apply.
        return safe_segment(value, "camera name") if value is not None else None


class SaveConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["active", "new"] = "active"
    name: str | None = None  # required when target == "new"
    overwrite: bool = False  # only meaningful for target == "new"
    save_sensor: bool = True  # write <serial>.pfs files
    save_display: bool = True  # write octacam_config.toml
    cameras: list[CameraDisplayParams] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "SaveConfigRequest":
        # Two cameras sharing a name would write to the same video file.
        names = [c.name for c in self.cameras if c.name]
        if len(names) != len(set(names)):
            raise ValueError("Camera names must be unique")
        return self


class _AppState:
    def __init__(
        self,
        controller: RecordingController,
        config: OctacamConfig,
        config_dir: str = "",
    ):
        self.controller = controller
        # `config` is live (a save replaces it); `raw_config` is the parsed TOML a
        # save patches, so [gui], [[plugins]] and the save-dir template survive it.
        self.config = config
        self.config_dir = config_dir
        self.raw_config = (
            config_writer.load_raw_config(config_dir) if config_dir else {}
        )
        self.plugins = controller.plugins
        # {name: assets dir} of the plugins with a web UI, set once by create_app.
        self.plugin_web: dict[str, Path] = {}
        # Set by POST /api/shutdown ("Shut down & process"); cli.gui's teardown then
        # starts a detached processing job.
        self.process_after = False
        self.hub = Hub()
        # The update banner's notice; None until the background check returns.
        self._update_notice: updates.UpdateNotice | None = None

    def refresh_update_notice(self) -> None:
        """Populate _update_notice from a PyPI check. Fail-soft; never raises."""
        try:
            self._update_notice = updates.check()
        except Exception:
            log.debug("update check failed", exc_info=True)

    def update_status(self) -> dict | None:
        """The update notice for /api/system, or None if not (yet) known."""
        return self._update_notice.as_dict() if self._update_notice else None

    def system_descriptor(self) -> dict:
        """The /api/system payload, also pushed as the WS ``system`` message.

        ``ready`` is False (``cameras: []``) while the init thread opens the
        cameras; it pushes a fresh descriptor once they are attached."""
        controller = self.controller
        config_by_serial = {c.serial_number: c for c in self.config.cameras}
        cameras = []
        for index, camera in enumerate(controller.camera_system):
            camera_config = config_by_serial.get(camera.serial_number)
            cameras.append(
                {
                    "index": index,
                    "serial": camera.serial_number,
                    "name": camera.name,
                    "width": camera.width,
                    "height": camera.height,
                    "layout": {
                        key: getattr(camera_config, key) if camera_config else -1.0
                        for key in (
                            "window_x",
                            "window_y",
                            "window_width",
                            "window_height",
                        )
                    },
                    "transform": {
                        key: getattr(camera_config, key) if camera_config else default
                        for key, default in (
                            ("scale_x", 1.0),
                            ("scale_y", 1.0),
                            ("rotation_deg", 0.0),
                        )
                    },
                    # So a save covers cameras whose Camera tab was never opened.
                    "center_x": camera.center_x,
                    "center_y": camera.center_y,
                }
            )
        # Where app.js imports each plugin's UI bundle from.
        plugins_status = self.plugins.status()
        for name, adir in self.plugin_web.items():
            entry = plugins_status.get(name)
            if entry is None:
                continue
            web = {"module": f"/plugins/{name}/{name}.js"}
            if (adir / f"{name}.css").is_file():
                web["css"] = f"/plugins/{name}/{name}.css"
            entry["web"] = web
        return {
            "version": octacam.__version__,
            "update": self.update_status(),
            "ready": controller.ready,
            # Why the init opened no camera, instead of an endless placeholder.
            "init_error": controller.init_error,
            # Configured cameras that did not open: a 7-of-8 rig must not look
            # like a healthy one with a smaller grid.
            "missing_cameras": [
                {"serial": serial, "reason": reason}
                for serial, reason in sorted(controller.camera_system.missing.items())
            ],
            "config_dir": self.config_dir,
            "plugins": plugins_status,
            # A loaded plugin can drive the trigger: offer "managed".
            "managed_trigger_available": controller.managed_trigger_available,
            "display_refresh_interval_ms": (
                self.config.gui.display_refresh_interval_ms
            ),
            "theme": self.config.gui.theme,
            "formats": [
                {"save_method": save_method, "label": video_format.label}
                for save_method, video_format in FORMATS.items()
            ],
            "cameras": cameras,
        }

    def broadcast_system(self) -> None:
        """Push a fresh descriptor to every browser (the init thread, once the
        cameras are attached)."""
        self.hub.publish("system", self.system_descriptor())

    # ----------------------------------------------------- background tasks

    async def telemetry_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(TELEMETRY_INTERVAL_S)
            if not self.hub.clients:
                continue
            snapshot = await loop.run_in_executor(None, self.controller.snapshot)
            self.hub.broadcast("telemetry", snapshot)


def _default_shutdown() -> None:
    """SIGINT ourselves: uvicorn shuts down gracefully, then cli.gui's ``finally``
    releases the hardware."""
    os.kill(os.getpid(), signal.SIGINT)


@contextlib.contextmanager
def _http_errors():
    """Map a controller error to HTTP: no such camera 404, busy 409, bad value 422.
    A route with a narrower contract maps only its own errors, so an unexpected one
    stays a logged 500."""
    try:
        yield
    except IndexError as e:
        raise HTTPException(404, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    except (ValueError, TypeError) as e:
        raise HTTPException(422, str(e)) from None


def create_app(
    controller: RecordingController,
    config: OctacamConfig,
    config_dir: str = "",
    shutdown_callback: Callable[[], None] = _default_shutdown,
) -> FastAPI:
    """The GUI's app for ``controller``, serving the plugins it was built with."""
    state = _AppState(controller, config, config_dir)
    plugins = state.plugins
    hub = state.hub
    plugins.attach(broadcast=hub.publish)

    # Resolved once, so the mounts and /api/system agree on which plugins have a
    # UI; a missing dir means none.
    plugin_web: dict[str, Path] = {}
    for plugin in plugins.plugins:
        adir, name = plugin.web_dir, plugin.name
        if adir is None:
            continue
        if not adir.is_dir():
            log.warning("Plugin %r: web_dir %s does not exist; skipping", name, adir)
            continue
        plugin_web[name] = adir
    state.plugin_web = plugin_web

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        hub.loop = asyncio.get_running_loop()
        controller.add_listener(hub.publish)
        interval = max(state.config.gui.display_refresh_interval_ms, 10) / 1000
        tasks = [
            asyncio.create_task(preview_loop(hub, controller, interval)),
            asyncio.create_task(state.telemetry_loop()),
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

    def _require_config_dir() -> Path:
        if not config_dir:
            raise HTTPException(400, "No config directory is set for this session")
        return Path(config_dir)

    # Handlers are sync `def`: FastAPI runs them in its thread pool, so blocking
    # SDK, serial and filesystem calls never stall the preview WebSocket's loop.

    @app.get("/api/system")
    def get_system():
        return state.system_descriptor()

    @app.get("/api/serial/ports")
    def get_serial_ports():
        """Serial ports for the plugin tabs' picker; never opens one, so it is safe
        while a board is armed."""
        from octacam import serial_ports

        return {
            "ports": [
                {
                    "device": p.device,
                    "board_name": p.board_name,
                    "vid_pid": p.vid_pid,
                    "serial_number": p.serial_number,
                    "likely_arduino": p.likely_arduino,
                    "likely_microcontroller": p.likely_microcontroller,
                }
                for p in serial_ports.list_serial_ports()
            ]
        }

    @app.get("/api/state")
    def get_state():
        return controller.snapshot()

    @app.get("/api/settings")
    def get_settings():
        return dataclasses.asdict(controller.get_settings())

    @app.put("/api/settings")
    def put_settings(patch: dict[str, Any]):
        """Apply the fields sent. An unknown key or a value that fails
        validation answers 422 with the controller's message
        (RecordingSettings.updated), in any state; a RuntimeError (a valid
        change while recording or starting) answers 409."""
        try:
            updated = controller.update_settings(**patch)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        settings = dataclasses.asdict(updated)
        hub.publish("settings", settings)
        return settings

    @app.get("/api/nvenc/capabilities")
    def get_nvenc_capabilities():
        """NVENC capability and the detected session cap (what ``auto`` resolves
        to). The first call runs the cached probe, which loads the GPU, so the
        client asks only once nvenc is selected."""
        detected = nvenc_max_sessions()
        return {
            "available": detected is not None and detected > 0,
            "max_sessions": detected,
            "encoder": "h264_nvenc",
            "default_params": NVENC_H264_PARAMS,
        }

    @app.put("/api/cameras/{index}/name")
    def put_camera_name(index: int, patch: CameraNamePatch):
        with _http_errors():
            result = controller.set_camera_name(index, patch.name)
        hub.publish("camera_name", result, key=result["index"])
        return result

    @app.put("/api/cameras/{index}/transform")
    def put_camera_transform(index: int, patch: CameraTransformPatch):
        try:
            return controller.set_camera_transform(
                index, patch.scale_x, patch.scale_y, patch.rotation_deg
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    # ---------------------------------------------- full device node map (tab)

    def _broadcast_features_dirty(result: dict) -> None:
        """Tell clients to re-GET a changed camera's features (too large to push)."""
        for entry in result["updated"]:
            index = entry["index"]
            hub.publish(
                "camera_features_dirty",
                {"index": index, "center_x": entry["center_x"], "center_y": entry["center_y"]},
                key=index,
            )

    @app.get("/api/cameras/{index}/features")
    def get_camera_features(index: int):
        try:
            return controller.read_camera_features(index)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None

    @app.put("/api/cameras/{index}/features")
    def put_camera_feature(index: int, patch: CameraFeaturePatch):
        with _http_errors():
            result = controller.set_camera_feature(
                index, patch.name, patch.value, patch.scope
            )
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/features/reset")
    def reset_camera_feature(index: int, payload: CameraFeatureReset):
        _require_config_dir()
        with _http_errors():
            result = controller.reset_camera_feature(index, payload.name, payload.scope)
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/cameras/{index}/commands")
    def run_camera_command(index: int, payload: CameraCommandRequest):
        with _http_errors():
            result = controller.execute_camera_command(index, payload.name)
        _broadcast_features_dirty(result)
        return result

    @app.put("/api/cameras/{index}/center")
    def put_camera_center(index: int, patch: CameraCenterPatch):
        with _http_errors():
            result = controller.set_camera_center(
                index, patch.axis, patch.enabled, patch.scope
            )
        _broadcast_features_dirty(result)
        return result

    @app.post("/api/config/save")
    def save_config(req: SaveConfigRequest):
        active = _require_config_dir()
        if not req.save_sensor and not req.save_display:
            raise HTTPException(422, "Nothing to save: enable sensor and/or display")
        # A node-map snapshot would contend with the record grab loops.
        if controller.recording_active:
            raise HTTPException(409, "Cannot save the config while recording")
        try:
            saved = config_writer.save_rig_config(
                controller,
                active,
                state.raw_config,
                [c.model_dump() for c in req.cameras],
                new_name=(req.name or "") if req.target == "new" else None,
                overwrite=req.overwrite,
                sensor=req.save_sensor,
                display=req.save_display,
            )
        except RuntimeError as e:  # recording started between the check and save
            raise HTTPException(409, str(e)) from None
        except ValueError as e:  # invalid new-config name
            raise HTTPException(422, str(e)) from None
        except FileExistsError as e:
            raise HTTPException(
                409, f"Config already exists: {e}. Resend with overwrite=true."
            ) from None
        except PermissionError as e:
            raise HTTPException(403, f"Config directory is not writable: {e}") from None
        except OSError as e:
            raise HTTPException(500, f"Failed to write config: {e}") from None
        if saved.raw is not None:
            state.raw_config = saved.raw
        if saved.config is not None:
            state.config = saved.config
        return {
            "status": "ok",
            "config_dir": str(saved.directory),
            "target": req.target,
            "cameras_written": saved.cameras_written,
        }

    @app.post("/api/save-dir/validate")
    def validate_save_dir(payload: SaveDirValidateRequest):
        return controller.validate_save_dir(payload.path)

    @app.post("/api/browse")
    def browse(payload: BrowseRequest | None = None):
        payload = payload or BrowseRequest()
        return controller.browse_directory(payload.path)

    @app.post("/api/recording/start")
    def start_recording(payload: RecordingStartRequest | None = None):
        payload = payload or RecordingStartRequest()
        result = controller.start_recording(
            confirm_overwrite=payload.confirm_overwrite,
            plugin_params=payload.plugin_params,
        )
        body = {"status": result.status, "message": result.message}
        if result.ok:
            return JSONResponse(body, status_code=202)
        if result.status in (StartResult.NEEDS_CONFIRM, StartResult.BUSY):
            return JSONResponse(body, status_code=409)
        return JSONResponse(body, status_code=500)

    @app.post("/api/recording/stop")
    def stop_recording():
        controller.stop_recording(abort=False)
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/recording/abort")
    def abort_recording():
        controller.stop_recording(abort=True)
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/diagnostics/run")
    def run_diagnostic(payload: DiagnosticRunRequest | None = None):
        # The report arrives later as a WS "diagnostics" message.
        payload = payload or DiagnosticRunRequest()
        result = controller.run_diagnostic(
            target_fps=payload.target_fps,
            duration_s=payload.duration_s,
            find_max=payload.find_max,
            sink=payload.sink,
        )
        body = {"status": result.status, "message": result.message}
        return JSONResponse(body, status_code=202 if result.ok else 409)

    @app.post("/api/diagnostics/cancel")
    def cancel_diagnostic():
        controller.cancel_diagnostic()
        return JSONResponse({"status": "ok"}, status_code=202)

    @app.post("/api/shutdown")
    def shutdown(background_tasks: BackgroundTasks, body: ShutdownRequest | None = None):
        # Closing would abort the take. The callback runs after the 202 is sent, so
        # the client learns the request was accepted before the server dies.
        if controller.recording_active or controller.diagnosing:
            raise HTTPException(
                409,
                "Stop the recording or benchmark before shutting down the server",
            )
        state.process_after = bool(body and body.process_after)
        background_tasks.add_task(shutdown_callback)
        return JSONResponse({"status": "shutting_down"}, status_code=202)

    @app.websocket("/api/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws.accept()
        client = Client(ws)
        hub.connect(client)
        sender = asyncio.create_task(client.sender())
        loop = asyncio.get_running_loop()
        try:
            # The descriptor is built and queued with no await between, so an init
            # that finishes later reaches this client after it, never before.
            client.queue("system", to_json("system", state.system_descriptor()))
            snapshot = await loop.run_in_executor(None, controller.snapshot)
            client.queue("state", to_json("state", snapshot))
            settings = dataclasses.asdict(controller.get_settings())
            client.queue("settings", to_json("settings", settings))
            # Replay the last benchmark report and recent events.
            last_diag = controller.get_last_diagnostic()
            if last_diag:
                client.queue("diagnostics", to_json("diagnostics", last_diag))
            for event in list(controller.events)[-EVENT_BACKLOG_REPLAY:]:
                client.queue("event", to_json("event", event))
            while True:
                text = await ws.receive_text()
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                # "view" is the core's: cheap dict work, handled inline, never
                # offered to plugins.
                if isinstance(message, dict) and message.get("type") == "view":
                    client.views.update(parse_views(message))
                    continue
                # In the executor: a plugin's hook may block on I/O. A raising
                # hook is logged, so a bad message cannot kill the socket.
                await loop.run_in_executor(
                    None, state.plugins.on_ws_message, message, client.id
                )
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(client)
            # E.g. the flywheel stops a jog this client owned, so a dropped socket
            # cannot leave the motor spinning. In the executor: it may block.
            await loop.run_in_executor(
                None, state.plugins.on_ws_disconnect, client.id
            )
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender

    # Plugin REST routes, registered before the "/" catch-all.
    for plugin in plugins.plugins:
        router = plugin.api_router()
        if router is not None:
            app.include_router(router)

    # Before the "/" catch-all, whose html=True fallback would answer a /plugins/
    # path with index.html (not runnable as a module). No html=True here, so a
    # missing plugin asset 404s.
    for name, adir in plugin_web.items():
        app.mount(
            f"/plugins/{name}",
            _NoCacheStaticFiles(directory=adir),
            name=f"plugin-{name}",
        )

    if STATIC_DIR.is_dir():
        app.mount("/", _NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app
