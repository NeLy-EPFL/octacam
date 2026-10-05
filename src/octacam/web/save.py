"""The Save dialog's route: write the rig config (config_writer.save_rig_config)."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator, model_validator

from octacam import config_writer
from octacam.config import safe_segment
from octacam.web.state import AppState, StrictModel


class CameraDisplayParams(StrictModel):
    """A camera's display state to save; defaults mirror CameraConfig (-1: unset)."""

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


class SaveConfigRequest(StrictModel):
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


def router(state: AppState) -> APIRouter:
    api = APIRouter()
    controller = state.controller

    @api.post("/api/config/save")
    def save_config(req: SaveConfigRequest):
        active = state.require_config_dir()
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

    return api
