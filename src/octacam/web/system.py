"""Routes about the session and the host: the descriptor, state, serial ports,
NVENC and shutdown."""

from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from octacam.web.state import AppState
from octacam.ffmpeg import nvenc_max_sessions
from octacam.writer import NVENC_H264_PARAMS


class ShutdownRequest(BaseModel):
    process_after: bool = False


def router(state: AppState, shutdown_callback: Callable[[], None]) -> APIRouter:
    api = APIRouter()
    controller = state.controller

    @api.get("/api/system")
    def get_system():
        return state.system_descriptor()

    @api.get("/api/state")
    def get_state():
        return controller.snapshot()

    @api.get("/api/serial/ports")
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

    @api.get("/api/nvenc/capabilities")
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

    @api.post("/api/shutdown")
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

    return api
