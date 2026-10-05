"""What the commands share: the stderr console, the plugin and config-dir
options, opening the rig, and the camera enumeration doctor and the wizard read."""

import functools
import logging
import os
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from rich.console import Console

    from octacam.cameras import CameraSystem
    from octacam.config import OctacamConfig
    from octacam.plugins.serial import SerialPlugin

log = logging.getLogger("octacam")


@functools.cache
def stderr_console() -> "Console":
    """The one stderr Console, shared by the logger and every progress bar so log
    lines render above a live bar instead of corrupting it."""
    from rich.console import Console

    return Console(stderr=True)


EnabledPlugins = Annotated[
    list[str] | None,
    typer.Option(
        "--plugin",
        help="Enable a plugin (repeatable); adds to the config's `plugins` "
        "(e.g. --plugin flywheel). See `octacam doctor`.",
    ),
]


NoPlugins = Annotated[
    bool,
    typer.Option(
        "--no-plugins",
        help="Disable all plugins for this launch, ignoring the config.",
    ),
]


def resolve_enabled(enabled_plugins, no_plugins):
    """build_plugins' ``enabled``: None to use the config, [] for --no-plugins,
    else the --plugin names to add to the config's selection."""
    if no_plugins:
        return []
    return list(enabled_plugins) if enabled_plugins else None


def resolve_config_arg(config_dir: Path) -> Path:
    """The config dir CONFIG_DIR names: a recording folder resolves to its config
    snapshot, so `octacam gui <recording>` relaunches what it ran with. Every
    command that opens a rig calls this once; a redirect is logged."""
    from octacam.config import resolve_config_dir

    resolved = resolve_config_dir(config_dir)
    if resolved != config_dir:
        log.info(
            "%s is a recording folder; using its config snapshot in %s",
            config_dir,
            resolved,
        )
    return resolved


def in_ssh_session() -> bool:
    """True when this shell was started over SSH."""
    return any(
        os.environ.get(var) for var in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )


def browser_skip_reason(no_browser: bool) -> str | None:
    """Why auto-opening the browser should be skipped, or None to open it.

    Over SSH the browser would open on the rig, not the user's machine; the
    no-display check catches SSH setups that strip the SSH_* variables."""
    if no_browser:
        return "--no-browser was passed"
    if in_ssh_session():
        return "running over SSH — open the GUI on your local machine instead"
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return "no local display detected (headless session)"
    return None


def port_available(host: str, port: int) -> bool:
    """Return False if a server is already bound to ``host:port``.

    gui checks it before touching hardware: its init thread opens the cameras
    and arms the plugins before uvicorn binds. SO_REUSEADDR mirrors uvicorn, so
    a socket lingering in TIME_WAIT (which uvicorn could rebind) is not reported
    as in use; a listening server is."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def warn_if_transcoding() -> None:
    """Warn (best-effort) when an `octacam process` is transcoding on this
    machine: it competes with live capture for the CPU."""
    from octacam import session_cache

    try:
        count = session_cache.transcode_running()
    except Exception:
        log.debug("Could not check for running transcodes", exc_info=True)
        return
    if count:
        log.warning(
            "%d octacam process run%s transcoding on this machine. They pause at "
            "their next file/folder boundary while these cameras are in use — "
            "detached and foreground alike — and resume when they are free. A file "
            "already being transcoded finishes first, so capture may be slowed "
            "briefly. Use `octacam process --ignore-capture` to opt out.",
            count,
            " is" if count == 1 else "s are",
        )


def camera_open_error(e: Exception) -> str:
    """The operator's message for a rig whose cameras did not open (*e* is a
    BackendError or BackendUnavailable)."""
    from octacam.cameras import BackendUnavailable

    if isinstance(e, BackendUnavailable):
        return str(e)
    return (
        f"Could not open the cameras: {e}. They may already be in use by another "
        "octacam instance on this rig, or disconnected — only one process can open "
        "them at a time."
    )


def open_rig(
    config: "OctacamConfig", config_dir: Path, backend: str | None = None
) -> "CameraSystem":
    """:meth:`CameraSystem.for_config`, exiting with the reason when the cameras
    do not open."""
    from octacam.cameras import BackendError, BackendUnavailable, CameraSystem

    try:
        return CameraSystem.for_config(config, config_dir, backend)
    except (BackendError, BackendUnavailable) as e:
        sys.exit(camera_open_error(e))


def enumerate_backend(name: str) -> list[tuple[str, str | None]]:
    """``[(serial, model|None), ...]`` for a backend without opening any camera.

    ``"auto"`` (or an empty selector) sweeps the available backend cascade and
    returns each camera once, claimed by the highest-priority tier that sees it
    (so a Basler served by the vendor tier is not also listed under the pycameleon
    floor) — mirroring how :class:`CameraSystem` opens them. Basler goes through
    the pylon TL factory directly so model names come along; every other backend
    labels its cameras through its spec's ``read_model`` (None without one).
    Enumeration never opens/grabs a device, so this is safe alongside a live
    session."""
    from octacam.cameras import select_backend
    from octacam.cameras.registry import is_auto

    if is_auto(name):
        return [(serial, model) for serial, _backend, model in cascade_assignment()]
    if name.strip().lower() == "basler":
        from octacam.cameras.basler import tl_factory

        devices = tl_factory().EnumerateDevices()
        return [(str(d.GetSerialNumber()), str(d.GetModelName())) for d in devices]
    spec = select_backend(name)
    read_model = spec.read_model

    def _model(handle) -> str | None:
        if read_model is None:
            return None
        try:
            return read_model(handle)
        except Exception:  # a model read must never fail enumeration
            return None

    return [(str(serial), _model(handle)) for serial, handle in spec.enumerate(None)]


def cascade_assignment() -> list[tuple[str, str, str | None]]:
    """``[(serial, backend, model|None), ...]``: the tier :class:`CameraSystem`
    would open each camera through under ``auto``. A failing tier is skipped."""
    from octacam.cameras.registry import available_backends

    claimed: dict[str, tuple[str, str | None]] = {}
    order: list[str] = []
    for backend in available_backends():
        try:
            cams = enumerate_backend(backend)
        except Exception:
            continue
        for serial, model in cams:
            if serial in claimed:
                continue
            claimed[serial] = (backend, model)
            order.append(serial)
    return [(serial, claimed[serial][0], claimed[serial][1]) for serial in order]


def serial_plugin(name: str) -> "type[SerialPlugin] | None":
    """The class of the serial-hardware plugin *name* (one with board firmware),
    or None for any other name and for a module that fails to import."""
    from octacam.plugins import plugin_class
    from octacam.plugins.serial import SerialPlugin

    try:
        cls = plugin_class(name)
    except Exception:
        return None
    return cls if issubclass(cls, SerialPlugin) else None
