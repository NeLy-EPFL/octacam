"""Asynchronous video writers.

Every writer runs a sink on a background thread behind a bounded queue:
write() never blocks the grab loop and drops the frame when the queue is
full (or once the sink has failed). Available sinks:

- FfmpegVideoWriter (default): pipes raw GRAY8 frames to an ffmpeg child
  encoding H.264 with libx264 in true monochrome 4:0:0. Encoding happens
  entirely in the child process, outside the GIL. Validated on the rig:
  8 parallel ultrafast encoders sustain >1200 fps aggregate at 1080p.
- RawVideoWriter: raw Mono8 dump, transcoded later by `octacam process`
  (its geometry lives in the recording's recording_summary.json).
"""

# The sink handles (_queue/_proc/_writer) follow an open -> use -> close
# lifecycle; they are only touched while the writer thread is running, an
# invariant pyright can't track across methods.
# pyright: reportOptionalMemberAccess=false

import contextlib
import logging
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("octacam")

_SENTINEL = None
FINALIZE_TIMEOUT_S = 120  # max wait for ffmpeg to flush after stdin closes

# Building blocks for the default ffmpeg_params strings below. CRF 18 is the
# capture default (near-visually-lossless; offline transcoding re-encodes
# harder at a slower preset).
DEFAULT_CRF = 18
DEFAULT_PRESET = "ultrafast"
DEFAULT_PIX_FMT = "gray"
# Extra libx264 options passed verbatim to ffmpeg's -x264-params (e.g.
# "keyint=30:scenecut=0"); empty means the flag is omitted entirely.
DEFAULT_X264_PARAMS = ""

# Encoder output args as a single ffmpeg string — the config's single source of
# truth (record.ffmpeg_params / transcode.ffmpeg_params), shlex-split and
# spliced verbatim after the derived input args (see build_encode_args). The
# capture default uses a fast preset to keep up with the cameras; the transcode
# default re-encodes harder offline.
DEFAULT_FFMPEG_PARAMS = f"-c:v libx264 -preset {DEFAULT_PRESET} -crf {DEFAULT_CRF} -pix_fmt {DEFAULT_PIX_FMT}"
DEFAULT_TRANSCODE_FFMPEG_PARAMS = "-c:v libx264 -preset veryslow -crf 20 -pix_fmt gray"

# GPU capture params, opt-in via ``record.save_method = "nvenc"`` (or by naming
# any ``*_nvenc`` encoder in ``ffmpeg_params``). NVENC rejects gray/4:0:0, so we
# pack Mono8 into yuv420p — the full-range helpers below (_full_range_vf /
# _color_range_args fire for yuv420p) keep 0-255 luma. NVENC ignores ``-crf``
# (libx264-only); quality is set with ``-cq`` (constant-quality VBR). ``-cq 16``
# targets the same near-visually-lossless level as the libx264 ``-crf 18``
# default (NVENC is a touch less bit-efficient, so the number runs a little
# lower). ``-bf 0`` keeps the encoder from buffering B-frames. The encoder is
# resolved per camera at record time; a rig with more cameras than the GPU's
# NVENC session limit falls the overflow back to libx264 (resolve_capture_formats).
NVENC_H264_PARAMS = "-c:v h264_nvenc -preset p5 -tune hq -rc vbr -cq 16 -bf 0 -pix_fmt yuv420p"

# Recorded pixel format -> ffmpeg rawvideo input pixel format / bytes-per-pixel.
# Mono8 is the invariant every backend records today; the maps give a single
# seam to extend if a backend ever records Mono10/RGB.
_INPUT_PIX_FMT = {"Mono8": "gray"}
_BYTES_PER_PIXEL = {"Mono8": 1}


def _input_pix_fmt(pixel_format: str) -> str:
    return _INPUT_PIX_FMT.get(pixel_format, "gray")


def _bytes_per_pixel(pixel_format: str) -> int:
    return _BYTES_PER_PIXEL.get(pixel_format, 1)


def _is_limited_range_yuv(pix_fmt: str) -> bool:
    """True for YUV formats that default to limited/"TV" range (16-235 luma).

    Those squeeze our full-range (0-255) camera frames into 16-235 — an
    irreversible ~3.6% loss that happens even with lossless encoding. ``gray``
    (4:0:0) and the ``yuvj*`` aliases are already full range, so they're exempt.
    """
    return pix_fmt.startswith("yuv") and not pix_fmt.startswith("yuvj")


def _color_range_args(pix_fmt: str) -> list[str]:
    """ffmpeg output args tagging the stream FULL range for limited-range YUV.

    ``-color_range pc`` writes the H.264 VUI full_range_flag / container tag so
    decoders expand luma back to 0-255 instead of rendering washed-out. This is
    only the *tag*: on its own (notably on ffmpeg 7.x) it does NOT change the
    pixel data, which is why callers must also run :func:`_full_range_vf` to
    force the conversion itself. The two together are the single source of truth
    for full-range handling, shared by the capture writer, the offline
    transcoders, and the grid compositor.
    """
    return ["-color_range", "pc"] if _is_limited_range_yuv(pix_fmt) else []


def _full_range_vf(pix_fmt: str, vf: str = "") -> str:
    """Append a full-range conversion filter to *vf* for limited-range YUV.

    ``scale=out_range=full`` forces the gray→YUV conversion to keep 0-255 luma
    instead of compressing to 16-235. Unlike the ``-color_range`` *flag*, a
    filter reliably converts the data on every ffmpeg version we ship. A no-op
    (returns *vf* unchanged) for ``gray``/``yuvj*`` outputs, which are already
    full range. Pair with :func:`_color_range_args` so the result is also tagged.
    """
    if not _is_limited_range_yuv(pix_fmt):
        return vf
    frag = "scale=out_range=full"
    return f"{vf},{frag}" if vf else frag


@dataclass(frozen=True)
class TranscodeProgress:
    """One progress sample parsed from ffmpeg's ``-progress pipe:1`` stream.

    ``total_frames`` is the encode's known frame count when derivable (always
    for ``.raw`` inputs, and from the recording summary for encoded ones), else
    None for an indeterminate bar. ``fps``/``speed`` are 0.0 until ffmpeg has
    measured them. ``done`` is True on the terminal ``progress=end`` block."""

    frame: int
    fps: float
    out_time_s: float
    speed: float
    total_frames: int | None
    done: bool


# Called once per ffmpeg progress block during a transcode. Lives in the writer
# as a plain callback so the UI layer (the CLI's rich progress bar) owns all
# rendering and the writer stays free of presentation concerns.
ProgressCallback = Callable[[TranscodeProgress], None]


# Which ffmpeg option names carry the video encoder choice. `-c:v`/`-vcodec`
# are the common ones; the stream-qualified forms appear in hand-written params.
_VIDEO_CODEC_FLAGS = ("-c:v", "-codec:v", "-vcodec", "-c:v:0")


def encoder_of(ffmpeg_params: str) -> str | None:
    """The video encoder named by an ffmpeg_params string (after -c:v), or None."""
    try:
        tokens = shlex.split(ffmpeg_params)
    except ValueError:
        return None
    return _extract_opt(tokens, _VIDEO_CODEC_FLAGS)


def is_nvenc_params(ffmpeg_params: str) -> bool:
    """True when ffmpeg_params selects an NVIDIA NVENC encoder (``*_nvenc``)."""
    enc = encoder_of(ffmpeg_params)
    return bool(enc) and enc.endswith("_nvenc")


def _required_encoder(ffmpeg_params: str) -> str | None:
    """The encoder :func:`find_ffmpeg` must guarantee for these params.

    Only GPU encoders (which the bundled imageio ffmpeg lacks, and which a stale
    driver can fail to run) need a capability search; libx264/CPU work runs on
    any ffmpeg, so return None there to keep the historical fast-path binary."""
    enc = encoder_of(ffmpeg_params)
    return enc if enc and enc.endswith("_nvenc") else None


def _which_all(name: str) -> list[str]:
    """Every executable ``name`` on $PATH, in PATH order (shutil.which is first-only)."""
    found: list[str] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found.append(candidate)
    return found


def _ffmpeg_candidates() -> list[str]:
    """Ordered, realpath-deduped ffmpeg executables to probe, most-preferred first.

    $OCTACAM_FFMPEG, then the bundled imageio binary, then every ffmpeg on $PATH
    — a system build is where a working NVENC almost always lives (the bundled
    imageio binary ships without it)."""
    cands: list[str] = []
    env = os.environ.get("OCTACAM_FFMPEG")
    if env:
        cands.append(env)
    try:
        import imageio_ffmpeg

        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as e:  # pragma: no cover - depends on environment
        log.debug("imageio-ffmpeg unavailable: %s", e)
    cands.extend(_which_all("ffmpeg"))
    seen: set[str] = set()
    ordered: list[str] = []
    for exe in cands:
        try:
            key = os.path.realpath(exe)
        except OSError:
            key = exe
        if key not in seen:
            seen.add(key)
            ordered.append(exe)
    return ordered


# Capability-probe caches. A probe runs a real one-frame encode (below), so this
# is not just a version check: it is the ground truth for "can this binary run
# this encoder on THIS machine right now". Cleared only by process restart.
_ENCODER_OK: dict[tuple[str, str], bool] = {}
_FFMPEG_FOR_ENCODER: dict[str, str] = {}
# Detected NVENC session cap per encoder, memoized for the process. The probe
# loads the GPU briefly, so we run it at most once and reuse the count; keyed by
# encoder so h264 and hevc could differ. A missing key means "not yet probed".
_NVENC_MAX_SESSIONS: dict[str, int | None] = {}


def ffmpeg_encoder_works(exe: str, encoder: str) -> bool:
    """Whether *exe* can actually initialise *encoder* on this machine (cached).

    Runs a real one-frame encode to the null muxer. This is stronger than parsing
    ``-encoders``: a too-new ffmpeg lists ``h264_nvenc`` yet fails at runtime when
    its NVENC API is newer than the installed NVIDIA driver supports, and a broken
    GPU/driver fails here too. The result is cached per (realpath, encoder)."""
    real = os.path.realpath(exe) if os.path.exists(exe) else exe
    key = (real, encoder)
    cached = _ENCODER_OK.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        proc = subprocess.run(
            [
                # -nostdin (+ stdin=DEVNULL below): never let ffmpeg touch the
                # controlling tty. With a terminal on stdin ffmpeg switches it to
                # no-echo/cbreak to read keypresses and only restores on a clean
                # exit — the timeout kill below (hung GPU/driver) would leave the
                # terminal with echo off, wedging the user's shell.
                exe, "-nostdin", "-hide_banner", "-loglevel", "error",
                # 256x256: comfortably above NVENC's minimum frame dimensions
                # (a smaller probe frame fails init on its own).
                "-f", "lavfi", "-i", "color=c=black:s=256x256:r=5",
                "-frames:v", "1", "-c:v", encoder, "-f", "null", "-",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
        )  # fmt: skip
        ok = proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.debug("encoder probe failed for %s / %s: %s", exe, encoder, e)
    _ENCODER_OK[key] = ok
    return ok


def probe_nvenc_max_sessions(
    encoder: str = "h264_nvenc", ceiling: int = 12
) -> int | None:
    """Empirically detect how many concurrent NVENC sessions this GPU allows.

    Launches up to *ceiling* short, overlapping NVENC encodes and counts how many
    initialise successfully — the excess fail their encoder init with the driver's
    session-limit error. Returns the count (≤ *ceiling*), or None when no
    NVENC-capable ffmpeg exists. Consumer GeForce cards cap this at a handful (8 on
    driver 570; 12 on late-2025 drivers); a return equal to *ceiling* means "at
    least this many". Used by ``octacam doctor``; it briefly loads the GPU, so it
    is not on any record path.

    Note: run during a live NVENC recording it under-counts (that recording holds
    its sessions), but it cannot disturb those held sessions."""
    try:
        exe = find_ffmpeg(require_encoder=encoder)
    except RuntimeError:
        return None
    procs: list[subprocess.Popen] = []
    for _ in range(max(1, ceiling)):
        try:
            proc = subprocess.Popen(
                [
                    # -nostdin (+ stdin=DEVNULL): these encodes overlap, and any
                    # ffmpeg holding the tty flips it to no-echo. With N of them
                    # racing on save/restore one restores the already-off state,
                    # leaving the terminal echo-off after doctor exits. Keep them
                    # off the tty entirely.
                    exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-re",
                    "-f", "lavfi", "-i", "testsrc=size=256x256:rate=10",
                    "-t", "2", "-c:v", encoder, "-f", "null", "-",
                ],
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
    """Detected NVENC session cap for *encoder*, memoized for the process.

    Wraps :func:`probe_nvenc_max_sessions` with a one-shot cache so the GPU probe
    runs at most once per process. Returns the count, or None when no
    NVENC-capable ffmpeg exists. Call it *before* opening record sessions (the
    probe under-counts while sessions are held) — :meth:`resolve_capture_formats`
    does exactly that via the controller's off-lock warm-up.
    """
    if encoder not in _NVENC_MAX_SESSIONS:
        _NVENC_MAX_SESSIONS[encoder] = probe_nvenc_max_sessions(encoder)
    return _NVENC_MAX_SESSIONS[encoder]


def find_ffmpeg(require_encoder: str | None = None) -> str:
    """Locate an ffmpeg executable.

    Without ``require_encoder``: $OCTACAM_FFMPEG, then imageio-ffmpeg, then $PATH
    (the historical order — the bundled binary is fine for libx264/CPU work).

    With ``require_encoder`` (e.g. ``"h264_nvenc"``): return the first candidate
    ffmpeg that can actually *run* that encoder — a real probe, not just listing
    it (see :func:`ffmpeg_encoder_works`) — searching $OCTACAM_FFMPEG, the bundled
    binary, then every ffmpeg on $PATH. The bundled imageio build has no NVENC, so
    this is how a GPU recording reaches a system ffmpeg. Raises RuntimeError if
    none qualifies (the caller can then fall back to CPU)."""
    if require_encoder is None:
        exe = os.environ.get("OCTACAM_FFMPEG")
        if exe:
            return exe
        try:
            import imageio_ffmpeg

            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception as e:  # pragma: no cover - depends on environment
            log.debug("imageio-ffmpeg unavailable: %s", e)
        exe = shutil.which("ffmpeg")
        if exe:
            return exe
        raise RuntimeError(
            "No ffmpeg executable found: install the imageio-ffmpeg package or "
            "a system ffmpeg, or set OCTACAM_FFMPEG."
        )
    cached = _FFMPEG_FOR_ENCODER.get(require_encoder)
    if cached is not None:
        return cached
    for exe in _ffmpeg_candidates():
        if ffmpeg_encoder_works(exe, require_encoder):
            _FFMPEG_FOR_ENCODER[require_encoder] = exe
            return exe
    raise RuntimeError(
        f"no ffmpeg with a working {require_encoder} encoder was found "
        f"(install a system ffmpeg built with {require_encoder} and a matching "
        "NVIDIA driver, or set OCTACAM_FFMPEG to one)."
    )


def _extract_opt(tokens: list[str], names: tuple[str, ...]) -> str | None:
    """Return the value token following the first of ``names`` in ``tokens``."""
    for i, tok in enumerate(tokens):
        if tok in names and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _strip_opts(tokens: list[str], names: tuple[str, ...]) -> list[str]:
    """Return ``tokens`` with each ``name`` option and its value token removed.

    Used by the grid compositor, which owns its own ``-pix_fmt``/filter handling
    and must drop those from a config ``ffmpeg_params`` string while keeping the
    encoder choice (``-c:v``/``-preset``/``-crf``)."""
    cleaned: list[str] = []
    skip = False
    for tok in tokens:
        if skip:
            skip = False
            continue
        if tok in names:
            skip = True
            continue
        cleaned.append(tok)
    return cleaned


def _merge_vf(transform_vf: str, tokens: list[str]) -> tuple[list[str], str]:
    """Pull any ``-vf``/``-filter:v`` out of ``tokens`` and merge with the
    caller-owned transform filter.

    ffmpeg accepts only one ``-vf``; the display-transform filter is octacam's
    to inject, so a user filter inside ``ffmpeg_params`` must be merged rather
    than left to silently override it. The transform runs first (rotate/flip),
    the user's filter after — matching the numpy transform ordering. Returns the
    tokens with ``-vf`` removed and the single merged filter chain ("" if none).
    """
    cleaned: list[str] = []
    user_vf = ""
    skip = False
    for i, tok in enumerate(tokens):
        if skip:
            skip = False
            continue
        if tok in ("-vf", "-filter:v"):
            if i + 1 < len(tokens):
                user_vf = tokens[i + 1]
                skip = True
            continue
        cleaned.append(tok)
    return cleaned, ",".join(p for p in (transform_vf, user_vf) if p)


def _output_args(ffmpeg_params: str, vf: str) -> tuple[list[str], str, list[str]]:
    """Split a config ``ffmpeg_params`` string into the pieces ffmpeg needs.

    Returns ``(encoder_tokens, merged_vf, color_range_args)``: the verbatim
    encoder tokens (with any user ``-vf`` removed), the single merged filter
    chain (transform + user filter + full-range conversion for limited-range
    YUV), and the ``-color_range`` tag args. Shared by the raw-input encoder
    (:func:`build_encode_args`) and :func:`transcode_encoded` so both apply the
    ``-vf`` merge and full-range handling identically.
    """
    tokens = shlex.split(ffmpeg_params)
    out_pix_fmt = _extract_opt(tokens, ("-pix_fmt", "-pixel_format")) or ""
    tokens, merged_vf = _merge_vf(vf, tokens)
    merged_vf = _full_range_vf(out_pix_fmt, merged_vf)
    return tokens, merged_vf, _color_range_args(out_pix_fmt)


def build_encode_args(
    ffmpeg: str,
    output: str,
    fps: float,
    width: int,
    height: int,
    ffmpeg_params: str,
    *,
    source: str = "pipe:0",
    vf: str = "",
    input_pix_fmt: str = "gray",
) -> list[str]:
    """ffmpeg argv encoding a rawvideo stream (from `source`) with `ffmpeg_params`.

    The **input** args (``-f rawvideo -pixel_format … -video_size … -framerate
    …``) are derived from the frame geometry; the **output/encoder** args come
    verbatim from ``ffmpeg_params`` (shlex-split), e.g. ``-c:v libx264 -preset
    ultrafast -crf 18 -pix_fmt gray``. Any ``-vf`` inside ``ffmpeg_params`` is
    merged with the caller-owned ``vf`` (display transform) into one filter
    chain (see :func:`_merge_vf`). For a limited-range YUV ``-pix_fmt`` a
    full-range conversion filter + tag is injected so 0-255 luma survives (see
    :func:`_full_range_vf`).

    ``-pix_fmt gray`` produces true monochrome 4:0:0 H.264 (decodes in all
    ffmpeg-based tools; browsers would need yuv420p). Note: ffprobe shows such
    streams as yuvj420p because the H.264 decoder synthesizes neutral chroma;
    the x264 encoder log ("4:0:0, 8-bit") is the source of truth.
    """
    tokens, merged_vf, color_args = _output_args(ffmpeg_params, vf)
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-f",
        "rawvideo",
        "-pixel_format",
        input_pix_fmt,
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        f"{fps:g}",
        "-i",
        source,
        *(["-vf", merged_vf] if merged_vf else []),
        *tokens,
        *color_args,
        "-y",
        str(output),
    ]


def _write_all(file, frame) -> None:
    """Write a frame to an unbuffered file, handling partial pipe writes."""
    view = memoryview(frame).cast("B")
    while view.nbytes:
        n = file.write(view)
        if n is None or n == view.nbytes:
            return
        view = view[n:]


class AsyncFrameWriter:
    """Bounded-queue writer base (the original AsyncVideoWriter skeleton).

    write() takes ownership of the frame array (the caller must not mutate
    it afterwards); callers pass a freshly owned copy from GrabResult.Array.
    Subclasses implement _open_sink/_write_frame/_close_sink.

    ``profile`` (off by default, so a normal recording pays nothing) turns on
    lightweight per-frame instrumentation for the diagnostics engine: the writer
    thread times each ``_write_frame`` (the real encode/disk cost) into
    :attr:`encode_ns_samples`, and :meth:`write` tracks the high-water queue
    depth in :attr:`max_queue_depth`. Both are read after a short diagnostic
    trial, so the sample list stays bounded.
    """

    def __init__(self, max_queue_size: int = 20, *, profile: bool = False):
        self._max_queue_size = max_queue_size
        self._queue: queue.Queue | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._failed = False
        self._written = 0
        self._profile = profile
        self._encode_ns_samples: list[int] = []
        self._max_queue_depth = 0

    @property
    def failed(self) -> bool:
        """True once the sink has died; subsequent writes are dropped."""
        return self._failed

    @property
    def frames_written(self) -> int:
        """Frames actually handed to the sink (excludes any discarded after
        a sink failure). The grab loop reconciles this against the queue to
        keep the CSV's per-frame `dropped` column accurate."""
        return self._written

    @property
    def encode_ns_samples(self) -> list[int]:
        """Per-frame ``_write_frame`` durations (ns), when ``profile`` is on.

        Empty unless the writer was constructed with ``profile=True``. This is
        the real encode (ffmpeg pipe write) / disk-write cost the diagnostics
        engine turns into percentiles."""
        return self._encode_ns_samples

    @property
    def max_queue_depth(self) -> int:
        """High-water queue occupancy seen by :meth:`write`, when profiling.

        A queue that repeatedly fills (approaching ``max_queue_size``) is the
        signature of an encoder that cannot keep up with the grab rate."""
        return self._max_queue_depth

    def open(self, filename: str, fps: float, frame_size: tuple[int, int]) -> bool:
        """Open `filename` for writing. frame_size is (width, height)."""
        self.close()
        try:
            self._open_sink(str(filename), fps, frame_size)
        except Exception as e:
            log.error("Failed to open writer for %s: %s", filename, e)
            return False
        self._failed = False
        self._written = 0
        self._encode_ns_samples = []
        self._max_queue_depth = 0
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self._running = True
        self._thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._thread.start()
        return True

    def write(self, frame) -> bool:
        """Enqueue a frame; returns False if it was dropped."""
        if not self._running or self._failed:
            return False
        if self._profile:
            depth = self._queue.qsize()
            if depth > self._max_queue_depth:
                self._max_queue_depth = depth
        try:
            self._queue.put_nowait(frame)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        """Stop accepting frames, drain the queue, and finalize the file."""
        if self._thread is None:
            return
        self._running = False
        self._queue.put(_SENTINEL)  # queued frames are written first
        self._thread.join()
        self._thread = None
        self._queue = None
        try:
            self._close_sink()
        except Exception as e:
            log.error("Failed to finalize video: %s", e)

    def _writer_loop(self) -> None:
        while True:
            frame = self._queue.get()
            if frame is _SENTINEL:
                break
            if self._failed:
                continue  # keep draining so close() semantics are unchanged
            try:
                if self._profile:
                    t0 = time.perf_counter_ns()
                    self._write_frame(frame)
                    self._encode_ns_samples.append(time.perf_counter_ns() - t0)
                else:
                    self._write_frame(frame)
                self._written += 1
            except Exception as e:
                self._failed = True
                self._on_sink_failure(e)

    # -- subclass hooks ----------------------------------------------------

    def _open_sink(self, filename: str, fps: float, frame_size) -> None:
        raise NotImplementedError

    def _write_frame(self, frame) -> None:
        raise NotImplementedError

    def _close_sink(self) -> None:
        raise NotImplementedError

    def _on_sink_failure(self, exc: Exception) -> None:
        log.error("Writer failed (%s); subsequent frames will be dropped", exc)


class FfmpegVideoWriter(AsyncFrameWriter):
    """Pipes raw GRAY8 frames into an ffmpeg child encoding H.264 (libx264).

    The pipe write blocks when ffmpeg falls behind; the bounded queue absorbs
    that and drops on full, preserving the drop-accounting contract. If the
    child dies mid-recording, write() returns False from then on and the
    stderr tail is logged (MKV output stays playable up to that point).
    """

    def __init__(
        self,
        ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS,
        remux_mp4: bool = False,
        max_queue_size: int = 20,
        *,
        profile: bool = False,
    ):
        super().__init__(max_queue_size, profile=profile)
        self.ffmpeg_params = ffmpeg_params
        self.remux_mp4 = remux_mp4
        self._proc: subprocess.Popen | None = None
        self._filename: str | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    @property
    def error_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    def _open_sink(self, filename, fps, frame_size):
        width, height = frame_size
        args = build_encode_args(
            find_ffmpeg(require_encoder=_required_encoder(self.ffmpeg_params)),
            filename,
            fps,
            width,
            height,
            self.ffmpeg_params,
        )
        self._filename = filename
        self._stderr_tail.clear()
        # bufsize=0: frames go straight to the pipe (no Python-side
        # double-buffering) and the write syscall releases the GIL.
        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Reap the child if anything after Popen fails (e.g. thread exhaustion
        # raising from .start()) — otherwise AsyncFrameWriter.open catches, returns
        # False, and close() short-circuits on _thread is None, orphaning ffmpeg.
        # BaseException so no post-Popen failure can leak the process/pipes.
        try:
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(self._proc,), daemon=True
            )
            self._stderr_thread.start()
        except BaseException:
            try:
                self._proc.stdin.close()
                self._proc.kill()
                self._proc.wait()
            finally:
                self._proc = None
                self._stderr_thread = None
            raise

    def _drain_stderr(self, proc):
        with proc.stderr:
            for line in proc.stderr:
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)

    def _write_frame(self, frame):
        _write_all(self._proc.stdin, frame)

    def _on_sink_failure(self, exc):
        tail = self.error_tail
        log.error(
            "ffmpeg writer for %s failed (%s); subsequent frames will be dropped%s",
            self._filename,
            exc,
            ("\nffmpeg output:\n" + tail) if tail else "",
        )

    def _close_sink(self):
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        # Generous finalize window: after stdin closes ffmpeg only has to
        # flush frames already queued in its own buffers. ultrafast is
        # near-instant, but a slow preset on a long trial can take a while -
        # killing it early would truncate the file and wrongly flag failure.
        try:
            returncode = proc.wait(timeout=FINALIZE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log.error(
                "ffmpeg still running %d s after stdin close; killing it",
                FINALIZE_TIMEOUT_S,
            )
            proc.kill()
            returncode = proc.wait()
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
        if returncode != 0:
            self._failed = True
            log.error(
                "ffmpeg exited with code %d for %s%s",
                returncode,
                self._filename,
                ("\nffmpeg output:\n" + self.error_tail) if self._stderr_tail else "",
            )
        elif self.remux_mp4:
            self._remux()

    def _remux(self):
        source = Path(self._filename)  # pyright: ignore[reportArgumentType]
        target = source.with_suffix(".mp4")
        result = subprocess.run(
            [
                find_ffmpeg(),
                # -nostdin (+ stdin=DEVNULL): keep ffmpeg off the controlling
                # tty so it can never leave the terminal in no-echo mode.
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-i",
                str(source),
                "-c",
                "copy",
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        if result.returncode == 0:
            source.unlink()
            log.info("Remuxed %s -> %s", source, target)
        else:
            log.error(
                "Remux of %s failed (kept the MKV): %s",
                source,
                result.stderr.decode(errors="replace").strip(),
            )


class RawVideoWriter(AsyncFrameWriter):
    """Dumps raw Mono8 frames for later transcoding by `octacam process`.

    The stream carries no geometry of its own; width/height/pixel_format/fps
    for the transcode come from the recording's recording_summary.json.
    """

    def __init__(self, max_queue_size: int = 20, *, profile: bool = False):
        super().__init__(max_queue_size, profile=profile)
        self._file = None

    def _open_sink(self, filename, fps, frame_size):
        self._file = open(Path(filename), "wb", buffering=0)

    def _write_frame(self, frame):
        _write_all(self._file, frame)

    def _close_sink(self):
        if self._file is not None:
            self._file.close()
            self._file = None


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VideoFormat:
    """A recording method selectable from the GUI/CLI."""

    save_method: str  # "ffmpeg" | "raw"
    extension: str
    label: str
    ffmpeg_params: str = DEFAULT_FFMPEG_PARAMS
    remux_mp4: bool = False

    def create_writer(
        self, max_queue_size: int = 20, *, profile: bool = False
    ) -> AsyncFrameWriter:
        if self.save_method == "ffmpeg":
            return FfmpegVideoWriter(
                ffmpeg_params=self.ffmpeg_params,
                remux_mp4=self.remux_mp4,
                max_queue_size=max_queue_size,
                profile=profile,
            )
        if self.save_method == "raw":
            return RawVideoWriter(max_queue_size, profile=profile)
        raise ValueError(f"Unknown save method: {self.save_method}")


# Keyed by config `record.save_method`. "ffmpeg" encodes during capture on the
# CPU (libx264); "nvenc" encodes on an NVIDIA GPU (falling back to libx264 for
# cameras beyond the GPU's session limit — see resolve_capture_formats); "raw"
# dumps Mono8 for offline transcoding. "nvenc"'s writer class is still "ffmpeg"
# (an FfmpegVideoWriter); only its params/encoder differ.
FORMATS: dict[str, VideoFormat] = {
    "ffmpeg": VideoFormat("ffmpeg", "mkv", "x264 mkv (ffmpeg)"),
    "nvenc": VideoFormat(
        "ffmpeg", "mkv", "H.264 NVENC GPU (ffmpeg)", ffmpeg_params=NVENC_H264_PARAMS
    ),
    "raw": VideoFormat("raw", "raw", "raw Mono8 (transcode later)"),
}


def cpu_fallback_format(base: VideoFormat) -> VideoFormat:
    """A libx264/CPU VideoFormat mirroring *base*'s container/remux/pixel format.

    Used for cameras that can't get a GPU NVENC session (see
    :func:`resolve_capture_formats`). It keeps *base*'s ``-pix_fmt`` (NVENC uses
    yuv420p) so a mixed GPU+CPU recording is uniform — e.g. every file stays
    yuv420p (browser-playable after an mp4 remux) instead of the fallback cameras
    emitting monochrome 4:0:0. The full-range filter/flag still applies to
    yuv420p via :func:`build_encode_args`, so 0-255 luma survives on both paths."""
    try:
        tokens = shlex.split(base.ffmpeg_params)
    except ValueError:
        tokens = []
    pix_fmt = _extract_opt(tokens, ("-pix_fmt", "-pixel_format")) or DEFAULT_PIX_FMT
    return VideoFormat(
        save_method="ffmpeg",
        extension=base.extension,
        label="x264 mkv (CPU fallback)",
        ffmpeg_params=(
            f"-c:v libx264 -preset {DEFAULT_PRESET} -crf {DEFAULT_CRF} "
            f"-pix_fmt {pix_fmt}"
        ),
        remux_mp4=base.remux_mp4,
    )


def resolve_capture_formats(
    base: VideoFormat, num_cameras: int, max_nvenc_sessions: int | None = None
) -> tuple[list[VideoFormat], list[str]]:
    """Per-camera capture formats, honouring the GPU's NVENC session limit.

    For a non-NVENC ``base`` every camera uses it unchanged. For an NVENC
    ``base``:

    - if no ffmpeg can actually run NVENC on this machine, *all* cameras fall
      back to libx264 (so a misconfigured GPU never kills the whole recording);
    - otherwise the first ``cap`` cameras use NVENC and any beyond that fall back
      to libx264 — one consumer GeForce allows only a handful of concurrent NVENC
      sessions, and the (N+1)-th would otherwise fail its encoder init and
      silently record nothing.

    ``max_nvenc_sessions`` is the cap: ``None`` (the default) auto-detects the
    GPU/driver limit via :func:`nvenc_max_sessions`; an int caps it explicitly
    (e.g. to reserve GPU headroom). Returns ``(formats, warnings)``; the caller
    surfaces each warning to the operator (GUI event / CLI log). Both the NVENC
    capability probe and the session-count probe are cached, so calling this per
    recording is cheap after the first."""
    if num_cameras <= 0:
        return [], []
    if not is_nvenc_params(base.ffmpeg_params):
        return [base] * num_cameras, []
    encoder = encoder_of(base.ffmpeg_params) or "h264_nvenc"
    warnings: list[str] = []
    try:
        find_ffmpeg(require_encoder=encoder)
    except RuntimeError as e:
        warnings.append(
            f"GPU encoding ({encoder}) unavailable — {e} "
            f"Recording all {num_cameras} camera(s) on CPU (libx264) instead."
        )
        return [cpu_fallback_format(base)] * num_cameras, warnings
    if max_nvenc_sessions is None:
        # Auto: use the empirically detected GPU cap. The probe found NVENC-capable
        # ffmpeg above, so a None here would be surprising — treat it as "don't
        # cap" (let every camera try) rather than forcing all-CPU.
        detected = nvenc_max_sessions(encoder)
        cap = num_cameras if detected is None else detected
    else:
        cap = max(0, max_nvenc_sessions)
    n_gpu = min(cap, num_cameras)
    formats = [base] * n_gpu + [cpu_fallback_format(base)] * (num_cameras - n_gpu)
    if n_gpu < num_cameras:
        warnings.append(
            f"{num_cameras} cameras exceed the NVENC session limit "
            f"(max_nvenc_sessions={cap}): the first {n_gpu} encode on GPU "
            f"({encoder}); the remaining {num_cameras - n_gpu} fall back to CPU "
            "(libx264). Raise record.max_nvenc_sessions if your GPU/driver "
            "allows more (see `octacam doctor`)."
        )
    return formats, warnings


def default_save_method(record_config) -> str:
    """Resolve the recording save method key from a RecordConfig.

    Falls back to "ffmpeg" for an unknown value so a stray config can never
    stop a recording.
    """
    method = getattr(record_config, "save_method", "") or "ffmpeg"
    if method in FORMATS:
        return method
    log.warning("Unknown save_method %r; using ffmpeg", method)
    return "ffmpeg"


# Infix tagging an in-progress transcode's temp file (see _partial_path). Kept
# greppable and stable so the folder scanner (cli._transcode_jobs) can skip any
# such file a hard kill left behind.
PARTIAL_INFIX = ".octacam-part"


def _partial_path(output: Path) -> Path:
    """Sibling temp path an in-progress encode of ``output`` writes to.

    Lives in ``output``'s own directory (so the final rename is an atomic,
    same-filesystem ``os.replace``) and is hidden + tagged with
    :data:`PARTIAL_INFIX`, yet keeps ``output``'s real extension last so ffmpeg
    still infers the container muxer from the filename."""
    return output.with_name(f".{output.stem}{PARTIAL_INFIX}{output.suffix}")


def is_partial_transcode(path: Path) -> bool:
    """True for a transcode temp file (see :func:`_partial_path`).

    Lets a folder scan skip a partial output a crash/SIGKILL orphaned before
    its cleanup could run — a Ctrl-C or any caught failure removes it itself."""
    return PARTIAL_INFIX in path.name


@contextlib.contextmanager
def _atomic_output(output: Path):
    """Yield a temp path to encode into, swapped onto ``output`` only on success.

    The whole point of graceful interruption: ffmpeg writes a sibling
    :func:`_partial_path`, which is atomically renamed onto ``output`` when the
    body returns normally and deleted on *any* exception — a re-encode failure,
    or a Ctrl-C (KeyboardInterrupt) / kill that propagates out mid-encode. So a
    partial encode never appears at ``output``, and an interrupted run never
    clobbers an existing ``output`` (the rename happens only once the new file
    is whole)."""
    tmp = _partial_path(output)
    tmp.unlink(missing_ok=True)  # clear any orphan a prior hard kill left
    try:
        yield tmp
        # Swap in only once the encode is whole. Inside the try so a failed
        # rename cleans up too, never stranding the temp.
        os.replace(tmp, output)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def transcode_raw(
    raw_path: Path,
    output: Path | None = None,
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    vf: str = "",
    *,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    pixel_format: str = "Mono8",
    frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Transcode a .raw Mono8 dump to a compressed video with ``ffmpeg_params``.

    The raw stream carries no geometry, so ``width``/``height``/``fps`` (from the
    recording's recording_summary.json) are required; without them the frame
    layout is unknown and a clear error is raised. ``frames`` (the summary's
    exact count) makes the progress bar determinate; it falls back to the file
    size / (w*h*bytes-per-pixel) when absent. ``output`` defaults to
    ``<raw>.mkv``; ``vf`` bakes a display orientation (see build_encode_args).
    """
    raw_path = Path(raw_path)
    output = Path(output) if output else raw_path.with_suffix(".mkv")
    if width is None or height is None or fps is None:
        raise FileNotFoundError(
            f"no recording_summary.json geometry for {raw_path}; cannot "
            "determine width/height/fps to transcode the raw stream"
        )
    total_frames = frames
    if total_frames is None and width and height:
        bpp = _bytes_per_pixel(pixel_format)
        total_frames = raw_path.stat().st_size // (width * height * bpp)
    with _atomic_output(output) as tmp:
        args = build_encode_args(
            find_ffmpeg(),
            str(tmp),
            fps,
            width,
            height,
            ffmpeg_params,
            source=str(raw_path),
            vf=vf,
            input_pix_fmt=_input_pix_fmt(pixel_format),
        )
        _run_ffmpeg(
            args,
            raw_path,
            on_progress=on_progress,
            total_frames=total_frames,
            raw_output=raw_output,
        )
    return output


def transcode_encoded(
    src: Path,
    output: Path,
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    vf: str = "",
    *,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Re-encode an already-encoded video (mkv/mp4) to ``output`` with ``ffmpeg_params``.

    Always re-encodes with the given ``ffmpeg_params`` rather than
    stream-copying the source: captures are written with a fast preset to keep
    up with the cameras, so this offline pass is where a slow preset earns its
    compression. ``vf``, when non-empty, additionally bakes a display transform
    in (merged with any user ``-vf`` — see :func:`_output_args`).

    ``total_frames`` (e.g. from the recording summary) makes the progress bar
    determinate; without it the bar is indeterminate. ``on_progress``/
    ``raw_output`` control progress reporting (see :func:`_run_ffmpeg`).
    """
    src = Path(src)
    output = Path(output)
    ffmpeg = find_ffmpeg()
    tokens, merged_vf, color_args = _output_args(ffmpeg_params, vf)
    with _atomic_output(output) as tmp:
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(src),
            *(["-vf", merged_vf] if merged_vf else []),
            *tokens,
            *color_args,
            str(tmp),
        ]
        _run_ffmpeg(
            args,
            src,
            on_progress=on_progress,
            total_frames=total_frames,
            raw_output=raw_output,
        )
    return output


def transcode_file(
    input_path: Path,
    output: Path,
    ffmpeg_params: str = DEFAULT_TRANSCODE_FFMPEG_PARAMS,
    vf: str = "",
    *,
    width: int | None = None,
    height: int | None = None,
    fps: float | None = None,
    pixel_format: str = "Mono8",
    frames: int | None = None,
    total_frames: int | None = None,
    on_progress: ProgressCallback | None = None,
    raw_output: bool = False,
) -> Path:
    """Transcode one ``.raw``/``.mkv``/``.mp4`` file to ``output``.

    Dispatches on the input suffix; ``vf`` (if any) bakes a display transform
    in. The caller picks ``output`` (extension = desired container). A ``.raw``
    input needs ``width``/``height``/``fps``/``pixel_format``/``frames`` from the
    recording summary; encoded inputs read their own geometry so those are
    ignored there and ``total_frames`` drives the bar instead."""
    input_path = Path(input_path)
    if input_path.suffix == ".raw":
        return transcode_raw(
            input_path,
            output=output,
            ffmpeg_params=ffmpeg_params,
            vf=vf,
            width=width,
            height=height,
            fps=fps,
            pixel_format=pixel_format,
            frames=frames,
            on_progress=on_progress,
            raw_output=raw_output,
        )
    return transcode_encoded(
        input_path,
        output,
        ffmpeg_params=ffmpeg_params,
        vf=vf,
        total_frames=total_frames,
        on_progress=on_progress,
        raw_output=raw_output,
    )


def _reporting_args(args: list[str], raw_output: bool) -> list[str]:
    """Re-set ffmpeg's verbosity/progress flags for the chosen output mode.

    Strips whatever ``-hide_banner``/``-loglevel``/``-stats``/``-nostats``/
    ``-progress`` flags the arg builders baked in, then re-inserts the pair the
    mode needs: the octacam bar wants a quiet ffmpeg emitting a machine-readable
    ``-progress`` stream, while raw mode wants ffmpeg's native ``-stats`` line
    at info level streamed straight to the terminal."""
    exe, rest = args[0], args[1:]
    cleaned: list[str] = []
    skip_next = False
    for tok in rest:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-loglevel", "-progress"):
            skip_next = True  # also drop the value token that follows
            continue
        if tok in ("-hide_banner", "-stats", "-nostats"):
            continue
        cleaned.append(tok)
    # -nostdin in both modes (+ stdin=DEVNULL at the launch): a transcode reads
    # from -i, never the tty, so ffmpeg has no reason to grab the terminal — and
    # if it does, a Ctrl-C mid-encode kills it before it restores echo, wedging
    # the shell. Raw mode still streams ffmpeg's native stats via inherited
    # stdout/stderr; it only loses the interactive 'q' key (use Ctrl-C).
    if raw_output:
        flags = ["-nostdin", "-hide_banner", "-loglevel", "info", "-stats"]
    else:
        flags = [
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostats",
            "-progress",
            "pipe:1",
        ]
    return [exe, *flags, *cleaned]


def _to_int(value: str, default: int) -> int:
    try:
        return int(value)
    except ValueError:  # ffmpeg prints "N/A" before the first measurement
        return default


def _to_float(value: str, default: float) -> float:
    try:
        return float(value)
    except ValueError:
        return default


def _parse_progress(
    stream, on_progress: ProgressCallback, total_frames: int | None
) -> None:
    """Parse ffmpeg ``-progress pipe:1`` blocks, emitting one sample per block.

    ffmpeg writes one ``key=value`` per line and closes each block with a
    ``progress=continue`` (or final ``progress=end``) line; we snapshot the
    latest frame/fps/time/speed at every block boundary. Unmeasured fields
    arrive as ``N/A`` and keep their prior value."""
    frame = 0
    fps = 0.0
    out_time_s = 0.0
    speed = 0.0
    for line in stream:
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        value = value.strip()
        if key == "frame":
            frame = _to_int(value, frame)
        elif key == "fps":
            fps = _to_float(value, fps)
        elif key == "out_time_us":
            out_time_s = _to_float(value, out_time_s * 1e6) / 1e6
        elif key == "speed":
            speed = _to_float(value.rstrip("x"), speed)
        elif key == "progress":
            on_progress(
                TranscodeProgress(
                    frame, fps, out_time_s, speed, total_frames, value == "end"
                )
            )


def _drain_into(stream, sink: deque[str]) -> None:
    with stream:
        for line in stream:
            text = line.rstrip()
            if text:
                sink.append(text)


def _run_ffmpeg(
    args: list[str],
    src: Path,
    *,
    on_progress: ProgressCallback | None = None,
    total_frames: int | None = None,
    raw_output: bool = False,
) -> None:
    """Run an ffmpeg transcode of ``src``, raising RuntimeError on failure.

    With ``raw_output`` true, ffmpeg's native output streams straight to the
    terminal (the user opted into the raw ffmpeg view). Otherwise ffmpeg runs
    quietly with ``-progress pipe:1``: each block is parsed and forwarded to
    ``on_progress`` (if any) to drive a progress bar, while stderr is captured
    and surfaced only when the encode fails."""
    args = _reporting_args(args, raw_output)
    if raw_output:
        # Inherit stdout/stderr so ffmpeg's stats/log paint the terminal live,
        # but keep stdin off the tty (see _reporting_args) so a killed encode
        # can't leave the terminal in no-echo mode.
        returncode = subprocess.run(args, stdin=subprocess.DEVNULL).returncode
        if returncode != 0:
            raise RuntimeError(f"ffmpeg failed for {src} (exit code {returncode})")
        return

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None  # PIPE => set
    stderr_tail: deque[str] = deque(maxlen=40)
    # stderr is drained on its own thread (and closed there via `with stream`)
    # so a chatty ffmpeg can never fill the pipe and stall while we read stdout.
    stderr_thread = threading.Thread(
        target=_drain_into, args=(proc.stderr, stderr_tail), daemon=True
    )
    stderr_thread.start()
    try:
        if on_progress is not None:
            _parse_progress(proc.stdout, on_progress, total_frames)
        else:
            for _ in proc.stdout:  # drain so a full pipe never stalls ffmpeg
                pass
    except BaseException:
        # A Ctrl-C (or a raising progress callback) must take ffmpeg down with
        # us, not leave it encoding a partial file after we stop reading it.
        proc.kill()
        raise
    finally:
        # Always reap the child (the old subprocess.run did); after a kill the
        # wait returns at once. stderr is left for its own thread to close.
        proc.stdout.close()
        returncode = proc.wait()
        stderr_thread.join(timeout=2)
    if returncode != 0:
        tail = "\n".join(stderr_tail).strip()
        raise RuntimeError(f"ffmpeg failed for {src}: {tail}")
