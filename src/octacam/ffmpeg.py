"""The ffmpeg toolchain: finding the binaries, probing what they can encode, and
the argument policy every encode shares.

Every ffmpeg launch but the capture pipe is built with :func:`quiet_argv`
(``-nostdin``) and run with ``stdin=subprocess.DEVNULL``. With a terminal on
stdin ffmpeg turns echo off to read keys and restores it only on a clean exit,
so a killed ffmpeg, or concurrent ones racing to restore it, would leave the
user's shell echo-off. The capture pipe is exempt: its stdin carries the frames.
"""

import contextlib
import functools
import logging
import os
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

log = logging.getLogger("octacam")

# Not "gray": 4:0:0 H.264 decodes as flat gray on NVIDIA hardware decoders
# (see _playable_pix_fmt).
DEFAULT_PIX_FMT = "yuv420p"

_BUNDLED = "bundled imageio-ffmpeg"


def quiet_argv(exe: str, *args: str) -> list[str]:
    """``[exe, "-nostdin", *args]``; launch it with ``stdin=subprocess.DEVNULL``."""
    return [exe, "-nostdin", *args]


# --- discovery ---------------------------------------------------------------


def _ffmpeg_candidates() -> Iterator[tuple[str, str]]:
    """(executable, origin) of every ffmpeg, most preferred first, realpath-deduped:
    $OCTACAM_FFMPEG, the bundled imageio binary, then $PATH's (where a working
    NVENC lives). Lazy: taking the first never runs the bundled binary's
    validation behind an override."""

    def found() -> Iterator[tuple[str, str]]:
        env = os.environ.get("OCTACAM_FFMPEG")
        if env:
            yield env, "OCTACAM_FFMPEG override"
        try:
            import imageio_ffmpeg

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:  # pragma: no cover - depends on environment
            log.debug("imageio-ffmpeg unavailable: %s", e)
        else:
            yield bundled, _BUNDLED
        for directory in os.get_exec_path():  # os.defpath without $PATH
            exe = shutil.which("ffmpeg", path=directory) if directory else None
            if exe:
                yield exe, "system PATH"

    seen: set[str] = set()
    for exe, origin in found():
        key = _realpath(exe)
        if key not in seen:
            seen.add(key)
            yield exe, origin


def _realpath(exe: str) -> str:
    try:
        return os.path.realpath(exe)
    except OSError:
        return exe


def find_ffmpeg(require_encoder: str | None = None) -> str:
    """The preferred ffmpeg (see :func:`_ffmpeg_candidates`), or with
    *require_encoder* the first that can run it (:func:`ffmpeg_encoder_works`).
    Raises RuntimeError when none qualifies, so a caller can fall back to the CPU.
    """
    if require_encoder is not None:
        return _ffmpeg_for_encoder(require_encoder)
    first = next(iter(_ffmpeg_candidates()), None)
    if first is None:
        raise RuntimeError(
            "No ffmpeg executable found: install the imageio-ffmpeg package or "
            "a system ffmpeg, or set OCTACAM_FFMPEG."
        )
    return first[0]


@functools.cache
def _ffmpeg_for_encoder(encoder: str) -> str:
    """The first candidate ffmpeg that runs ``encoder`` (cached); a failure
    (RuntimeError) is not cached, so a later call searches again."""
    for exe, _origin in _ffmpeg_candidates():
        if ffmpeg_encoder_works(exe, encoder):
            return exe
    raise RuntimeError(
        f"no ffmpeg with a working {encoder} encoder was found "
        f"(install a system ffmpeg built with {encoder} and a matching "
        "NVIDIA driver, or set OCTACAM_FFMPEG to one)."
    )


def find_ffprobe() -> str:
    """$OCTACAM_FFPROBE, else the ffprobe beside :func:`find_ffmpeg`'s (so the
    probe and the encode share a build), else $PATH's.

    imageio-ffmpeg bundles no ffprobe, so without a system ffmpeg this raises
    RuntimeError; a caller that can do without (the grid) catches it.
    """
    exe = os.environ.get("OCTACAM_FFPROBE")
    if exe:
        return exe
    try:
        sibling = Path(find_ffmpeg()).with_name(
            "ffprobe.exe" if os.name == "nt" else "ffprobe"
        )
    except RuntimeError:
        sibling = None
    if sibling is not None and os.path.isfile(sibling) and os.access(sibling, os.X_OK):
        return str(sibling)
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    raise RuntimeError(
        "No ffprobe executable found: install a system ffmpeg (the bundled "
        "imageio-ffmpeg binary ships ffmpeg only, without ffprobe), or set "
        "OCTACAM_FFPROBE."
    )


def ffmpeg_source(exe: str) -> str:
    """Where *exe* sits in :func:`find_ffmpeg`'s search, for ``octacam doctor``."""
    key = _realpath(exe)
    origin = next(
        (o for candidate, o in _ffmpeg_candidates() if _realpath(candidate) == key),
        "system PATH",
    )
    if origin == _BUNDLED:
        with contextlib.suppress(PackageNotFoundError):
            return f"{origin} {version('imageio-ffmpeg')}"
    return origin


# --- probes --------------------------------------------------------------------


def ffmpeg_query(exe: str, *args: str) -> str:
    """The combined output of a fast, read-only ffmpeg query ("" on error)."""
    try:
        out = subprocess.run(
            quiet_argv(exe, *args), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "") + (out.stderr or "")


def ffmpeg_version(exe: str) -> str:
    """The version token from ``ffmpeg -version`` (e.g. "7.0.2"), or ""."""
    for line in ffmpeg_query(exe, "-hide_banner", "-version").splitlines():
        line = line.strip()
        if line.startswith("ffmpeg version"):
            toks = line.split()
            return toks[2] if len(toks) >= 3 else line
    return ""


@functools.cache
def ffmpeg_encoder_works(exe: str, encoder: str) -> bool:
    """Whether *exe* can run *encoder* on this machine (cached): a real one-frame
    encode, since an ffmpeg newer than the NVIDIA driver lists h264_nvenc yet
    fails to run it."""
    ok = False
    try:
        proc = subprocess.run(
            quiet_argv(
                exe, "-hide_banner", "-loglevel", "error",
                # NVENC fails to init below a minimum frame size.
                "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5",
                "-frames:v", "1", "-c:v", encoder, "-f", "null", "-",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )  # fmt: skip
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("encoder probe failed for %s / %s: %s", exe, encoder, e)
    return ok


def probe_nvenc_max_sessions(
    encoder: str = "h264_nvenc", ceiling: int = 12
) -> int | None:
    """How many concurrent NVENC sessions this GPU allows, or None without an
    NVENC-capable ffmpeg.

    Counts which of *ceiling* overlapping encodes initialize (*ceiling* means at
    least that many; GeForce drivers allow 8 to 12). It loads the GPU briefly,
    and under-counts, but never disturbs, the sessions a live recording holds
    (``octacam doctor`` runs it).
    """
    try:
        exe = find_ffmpeg(require_encoder=encoder)
    except RuntimeError:
        return None
    procs: list[subprocess.Popen] = []
    for _ in range(max(1, ceiling)):
        try:
            proc = subprocess.Popen(
                quiet_argv(
                    exe, "-hide_banner", "-loglevel", "error", "-re",
                    "-f", "lavfi", "-i", "testsrc=size=256x256:rate=10",
                    "-t", "2", "-c:v", encoder, "-f", "null", "-",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # fmt: skip
        except OSError:
            break
        procs.append(proc)
    ok = 0
    for proc in procs:
        try:
            if proc.wait(timeout=30) == 0:
                ok += 1
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return ok


def nvenc_max_sessions(encoder: str = "h264_nvenc") -> int | None:
    """:func:`probe_nvenc_max_sessions`, once per process. Call it before any
    record session is open (the controller's off-lock warm-up does)."""
    return _nvenc_session_cap(encoder)


# Keyed by the resolved encoder: functools.cache would key nvenc_max_sessions()
# and nvenc_max_sessions("h264_nvenc") apart and probe the GPU twice.
@functools.cache
def _nvenc_session_cap(encoder: str) -> int | None:
    return probe_nvenc_max_sessions(encoder)


# --- options -------------------------------------------------------------------

_PIX_FMT_OPTS = ("-pix_fmt", "-pixel_format")
_VF_OPTS = ("-vf", "-filter:v")
# The video-codec spellings encoder_of reads, and those the 4:0:0 rule reads.
_VIDEO_CODEC_FLAGS = ("-c:v", "-codec:v", "-vcodec", "-c:v:0")
_VIDEO_CODEC_OPTS = ("-c:v", "-codec:v", "-vcodec", "-c", "-codec")


def split_opts(
    tokens: list[str], names: tuple[str, ...], flags: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Split ``tokens`` into the rest and the values of the ``names`` options.

    ``flags`` (options that take no value) are dropped too.
    """
    rest: list[str] = []
    values: list[str] = []
    it = iter(tokens)
    for tok in it:
        if tok in names:
            value = next(it, None)
            if value is not None:
                values.append(value)
        elif tok not in flags:
            rest.append(tok)
    return rest, values


def _first_value(tokens: list[str], names: tuple[str, ...]) -> str | None:
    values = split_opts(tokens, names)[1]
    return values[0] if values else None


def _param_value(ffmpeg_params: str, names: tuple[str, ...]) -> str | None:
    try:
        return _first_value(shlex.split(ffmpeg_params), names)
    except ValueError:  # bad quoting
        return None


def encoder_of(ffmpeg_params: str) -> str | None:
    """The video encoder named by an ffmpeg_params string (after -c:v), or None."""
    return _param_value(ffmpeg_params, _VIDEO_CODEC_FLAGS)


def pix_fmt_of(ffmpeg_params: str) -> str | None:
    """The output pixel format an ffmpeg_params string names, or None."""
    return _param_value(ffmpeg_params, _PIX_FMT_OPTS)


def nvenc_encoder(ffmpeg_params: str) -> str | None:
    """The ``*_nvenc`` encoder *ffmpeg_params* names, or None. Only these need
    :func:`find_ffmpeg`'s capability search: the bundled ffmpeg lacks them and a
    stale driver can fail to run them."""
    encoder = encoder_of(ffmpeg_params)
    return encoder if encoder and encoder.endswith("_nvenc") else None


# --- pixel format and color range ----------------------------------------------

# Software encoders whose monochrome (4:0:0) output hardware decoders mishandle.
_MONO_UNSAFE_ENCODERS = ("libx264", "libx265")
_warned_pix_fmt: set[str] = set()


def is_limited_range_yuv(pix_fmt: str) -> bool:
    """True for YUV formats that default to limited range (16-235 luma), which
    would irreversibly squeeze full-range camera frames; ``gray`` and ``yuvj*``
    are full range."""
    return pix_fmt.startswith("yuv") and not pix_fmt.startswith("yuvj")


def color_range_args(pix_fmt: str) -> list[str]:
    """``-color_range pc`` for limited-range YUV: only a tag (ffmpeg 7 converts no
    pixels on it), so :func:`output_args` also adds ``scale=out_range=full``."""
    return ["-color_range", "pc"] if is_limited_range_yuv(pix_fmt) else []


def _full_range_vf(pix_fmt: str, vf: str = "") -> str:
    """*vf* plus ``scale=out_range=full`` for limited-range YUV, so the gray→YUV
    conversion keeps 0-255 luma."""
    if not is_limited_range_yuv(pix_fmt):
        return vf
    frag = "scale=out_range=full"
    return f"{vf},{frag}" if vf else frag


def _playable_pix_fmt(
    tokens: list[str], frame_size: tuple[int, int] | None
) -> list[str]:
    """Swap a libx264/libx265 4:0:0 (``gray``) output for full-range 4:2:0.

    NVIDIA's hardware decoder (VLC's default there) shows 4:0:0 H.264 as flat
    gray; full-range yuv420p decodes to the same pixels everywhere. This seam
    fixes every config and snapshot still saying ``gray``. 4:2:0 cannot code an
    odd side, so such a frame stays (or falls back to) 4:0:0; an unknown size is
    left as configured.
    """
    if _first_value(tokens, _VIDEO_CODEC_OPTS) not in _MONO_UNSAFE_ENCODERS:
        return tokens
    pix_fmt = _first_value(tokens, _PIX_FMT_OPTS)
    if frame_size is None or pix_fmt not in ("gray", "yuv420p"):
        return tokens
    even = frame_size[0] % 2 == 0 and frame_size[1] % 2 == 0
    target = "yuv420p" if even else "gray"
    if pix_fmt == target:
        return tokens
    if target not in _warned_pix_fmt:
        _warned_pix_fmt.add(target)
        if even:
            log.info(
                "Writing -pix_fmt gray as full-range yuv420p: monochrome (4:0:0) "
                "H.264 shows as flat gray frames in hardware decoders (VLC on "
                "NVIDIA); the pixel values are unchanged"
            )
        else:
            log.warning(
                "Writing %dx%d frames as -pix_fmt gray (4:0:0): yuv420p cannot code "
                "an odd width or height. Hardware decoders (VLC on NVIDIA) may show "
                "these videos as flat gray; disable hardware decoding to view them",
                *frame_size,
            )
    i = next(i for i, tok in enumerate(tokens) if tok in _PIX_FMT_OPTS)
    return [*tokens[: i + 1], target, *tokens[i + 2 :]]


def output_args(
    ffmpeg_params: str, frame_size: tuple[int, int] | None = None
) -> list[str]:
    """The output args every encode runs *ffmpeg_params* as: its pixel format
    made playable (:func:`_playable_pix_fmt`) and full range, with the user's
    -vf merged ahead of the range conversion (ffmpeg takes only the last -vf).

    ffprobe reports 4:0:0 and 4:2:0 H.264 alike (yuvj420p); x264's log says
    which was written.
    """
    tokens = _playable_pix_fmt(shlex.split(ffmpeg_params), frame_size)
    out_pix_fmt = _first_value(tokens, _PIX_FMT_OPTS) or ""
    tokens, user_vfs = split_opts(tokens, _VF_OPTS)
    vf = _full_range_vf(out_pix_fmt, user_vfs[-1] if user_vfs else "")
    return [*(["-vf", vf] if vf else []), *tokens, *color_range_args(out_pix_fmt)]


def rawvideo_input_args(
    width: int, height: int, fps: float, source: str = "pipe:0", pix_fmt: str = "gray"
) -> list[str]:
    """Input args for a headerless rawvideo stream: the geometry it lacks."""
    return [
        "-f", "rawvideo", "-pixel_format", pix_fmt,
        "-video_size", f"{width}x{height}", "-framerate", f"{fps:g}",
        "-i", source,
    ]  # fmt: skip
