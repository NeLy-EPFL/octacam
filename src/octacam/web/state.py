"""What the GUI's routers share: the session state and the strict request base."""

import dataclasses
import logging
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

import octacam
from octacam import updates
from octacam.config import ConfigError, OctacamConfig, find_config_file, parse_config
from octacam.controller import RecordingController
from octacam.web.hub import Hub
from octacam.writer import FORMATS

log = logging.getLogger("octacam")


class StrictModel(BaseModel):
    """A request body that rejects unknown fields (422)."""

    model_config = ConfigDict(extra="forbid")


def require_config_dir(config_dir: str | Path | None) -> Path:
    """``config_dir`` as a Path; 400 when the session has none."""
    if not config_dir:
        raise HTTPException(400, "No config directory is set for this session")
    return Path(config_dir)


@dataclasses.dataclass(eq=False)
class AppState:
    """One GUI session; cli.gui reads it as ``app.state.app_state``.

    ``config`` is live (a save of the active config replaces it); ``raw_config``
    is the parsed TOML a save patches, so [gui], [[plugins]] and the save-dir
    template survive it. ``plugin_web`` maps each plugin with a web UI to its
    assets dir."""

    controller: RecordingController
    hub: Hub
    config: OctacamConfig
    config_dir: str
    raw_config: dict
    plugin_web: dict[str, Path]
    # The update banner's notice; None until the background check returns.
    update_notice: updates.UpdateNotice | None = None
    # Set by POST /api/shutdown ("Shut down & process"); cli.gui's teardown then
    # starts a detached processing job.
    process_after: bool = False

    def adopt_config(self, raw: dict) -> None:
        """Make the active config just saved from ``raw`` live: the next save
        patches it, and the cameras get its display transforms (which recordings
        bake in) and its ROI centering."""
        self.raw_config = raw
        try:
            self.config = parse_config(find_config_file(self.config_dir))
        except ConfigError as e:
            # Unexpected (written from a validated document); the save succeeded.
            log.error("Saved config did not parse back; keeping the live one: %s", e)
            return
        self.controller.camera_system.apply_display_config(self.config.cameras)

    def refresh_update_notice(self) -> None:
        """Set ``update_notice`` from a PyPI check. Fail-soft; never raises."""
        try:
            self.update_notice = updates.check()
        except Exception:
            log.debug("update check failed", exc_info=True)

    def system_descriptor(self) -> dict:
        """The /api/system payload and WS ``system`` message; ``ready`` is False
        (no cameras) until the init thread attaches them and pushes a fresh one."""
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
        plugins_status = controller.plugins.status()
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
            "update": self.update_notice.as_dict() if self.update_notice else None,
            "ready": controller.ready,
            # Why the init opened no camera, instead of an endless placeholder.
            "init_error": controller.init_error,
            # Configured cameras that did not open: an incomplete rig must say so.
            "missing_cameras": [
                {"serial": serial, "reason": reason}
                for serial, reason in sorted(controller.camera_system.missing.items())
            ],
            "config_dir": self.config_dir,
            "plugins": plugins_status,
            # A loaded plugin can drive the trigger: offer "managed".
            "managed_trigger_available": controller.managed_trigger_available,
            "display_refresh_interval_ms": self.config.gui.display_refresh_interval_ms,
            "theme": self.config.gui.theme,
            "formats": [
                {"save_method": save_method, "label": video_format.label}
                for save_method, video_format in FORMATS.items()
            ],
            "cameras": cameras,
        }

    def broadcast_system(self) -> None:
        """Push a fresh descriptor to every browser."""
        self.hub.publish("system", self.system_descriptor())
