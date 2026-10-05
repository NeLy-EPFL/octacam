"""View- and Camera-tab routes: a camera's name and transform, and its node map."""

import contextlib
from typing import Any, Literal

from fastapi import APIRouter, HTTPException

from octacam.web.state import AppState, StrictModel, require_config_dir


class CameraNamePatch(StrictModel):
    """Rename one camera (validated and applied by the controller)."""

    name: str


class CameraTransformPatch(StrictModel):
    """One camera's live display transform (negative scale = flip), sent on every
    rotate/flip so a "display"-form recording bakes in what the operator sees."""

    scale_x: float = 1.0
    scale_y: float = 1.0
    rotation_deg: float = 0.0


class CameraFeaturePatch(StrictModel):
    """Set one node-map feature by GenApi name; the backend coerces ``value`` to
    the node's type (an enum sends its symbol)."""

    name: str
    value: Any
    scope: Literal["selected", "all"] = "selected"


class CameraFeatureReset(StrictModel):
    """Reset one node-map feature to its config value (else factory default)."""

    name: str
    scope: Literal["selected", "all"] = "selected"


class CameraCommandRequest(StrictModel):
    """Execute one command node (e.g. TimestampLatch) on the selected camera."""

    name: str


class CameraCenterPatch(StrictModel):
    """Toggle ROI auto-centering on an axis for one camera or all."""

    axis: Literal["x", "y"]
    enabled: bool
    scope: Literal["selected", "all"] = "selected"


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


def router(state: AppState) -> APIRouter:
    api = APIRouter()
    controller, hub = state.controller, state.hub

    def _publish_features_dirty(result: dict) -> None:
        """Tell clients to re-GET a changed camera's features (too large to push)."""
        for entry in result["updated"]:
            index = entry["index"]
            hub.publish(
                "camera_features_dirty",
                {"index": index, "center_x": entry["center_x"], "center_y": entry["center_y"]},
                key=index,
            )

    @api.put("/api/cameras/{index}/name")
    def put_camera_name(index: int, patch: CameraNamePatch):
        with _http_errors():
            result = controller.set_camera_name(index, patch.name)
        hub.publish("camera_name", result, key=result["index"])
        return result

    @api.put("/api/cameras/{index}/transform")
    def put_camera_transform(index: int, patch: CameraTransformPatch):
        try:
            return controller.set_camera_transform(
                index, patch.scale_x, patch.scale_y, patch.rotation_deg
            )
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from None

    @api.get("/api/cameras/{index}/features")
    def get_camera_features(index: int):
        try:
            return controller.read_camera_features(index)
        except IndexError:
            raise HTTPException(404, f"No camera at index {index}") from None

    @api.put("/api/cameras/{index}/features")
    def put_camera_feature(index: int, patch: CameraFeaturePatch):
        with _http_errors():
            result = controller.set_camera_feature(
                index, patch.name, patch.value, patch.scope
            )
        _publish_features_dirty(result)
        return result

    @api.post("/api/cameras/{index}/features/reset")
    def reset_camera_feature(index: int, payload: CameraFeatureReset):
        # The saved values come from the controller's config dir.
        require_config_dir(controller.config_dir)
        with _http_errors():
            result = controller.reset_camera_feature(index, payload.name, payload.scope)
        _publish_features_dirty(result)
        return result

    @api.post("/api/cameras/{index}/commands")
    def run_camera_command(index: int, payload: CameraCommandRequest):
        with _http_errors():
            result = controller.execute_camera_command(index, payload.name)
        _publish_features_dirty(result)
        return result

    @api.put("/api/cameras/{index}/center")
    def put_camera_center(index: int, patch: CameraCenterPatch):
        with _http_errors():
            result = controller.set_camera_center(
                index, patch.axis, patch.enabled, patch.scope
            )
        _publish_features_dirty(result)
        return result

    return api
