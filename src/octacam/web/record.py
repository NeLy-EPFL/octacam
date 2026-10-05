"""Record-tab and Benchmark routes: settings, the save dir, takes and benchmarks."""

import dataclasses
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from octacam.controller import StartResult
from octacam.web.state import AppState, StrictModel


class SaveDirValidateRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value


class BrowseRequest(StrictModel):
    """A directory to list for the save-dir picker; blank means the save dir."""

    path: str = ""


class RecordingStartRequest(BaseModel):
    confirm_overwrite: bool = False
    plugin_params: dict | None = None


class DiagnosticRunRequest(StrictModel):
    """Parameters for a Benchmark run (octacam.diagnostics)."""

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


def router(state: AppState) -> APIRouter:
    api = APIRouter()
    controller = state.controller

    @api.get("/api/settings")
    def get_settings():
        return dataclasses.asdict(controller.get_settings())

    @api.put("/api/settings")
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
        state.hub.publish("settings", settings)
        return settings

    @api.post("/api/save-dir/validate")
    def validate_save_dir(payload: SaveDirValidateRequest):
        return controller.validate_save_dir(payload.path)

    @api.post("/api/browse")
    def browse(payload: BrowseRequest | None = None):
        payload = payload or BrowseRequest()
        return controller.browse_directory(payload.path)

    @api.post("/api/recording/start")
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

    @api.post("/api/recording/stop")
    def stop_recording():
        controller.stop_recording(abort=False)
        return JSONResponse({"status": "ok"}, status_code=202)

    @api.post("/api/recording/abort")
    def abort_recording():
        controller.stop_recording(abort=True)
        return JSONResponse({"status": "ok"}, status_code=202)

    @api.post("/api/diagnostics/run")
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

    @api.post("/api/diagnostics/cancel")
    def cancel_diagnostic():
        controller.cancel_diagnostic()
        return JSONResponse({"status": "ok"}, status_code=202)

    return api
