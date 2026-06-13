# octacam: web GUI + ffmpeg/x264 recording — Python first, Rust as fallback

*Plan drafted 2026-06-12. Research reports backing every claim here are in
[web-gui-research/](web-gui-research/) (six dimensions + adversarial verification of
14 load-bearing claims against primary sources).*

## Context

octacam records 8× Basler acA1920-150um USB3 cameras (1920×1080 Mono8, software-triggered up to 150 fps each ≈ 1200 fps, ~2.4 GB/s raw) for fly behavior trials (default 20 s). Current implementation: Python (pypylon + PySide6 + OpenCV), each camera MJPEG-encoded via `cv2.VideoWriter` to AVI. Goals: (1) a **web-based GUI** (cross-platform, remote via `ssh -L`), (2) **faster-than-MJPEG saving to one container per camera** with good compression/quality, (3) speed-first: OK to write a fast intermediate format and convert later.

**Direction (user-decided):** do it **in Python first** — keep the `uv tool install` one-command deployment — and validate speed on the rig. The fully-researched Rust rewrite (see Fallback section) is the contingency if Python can't keep up.

### Why Python-first is credible (research + benchmarks)

- `benchmarks/README.md` (Phase 0): grabbing 8×150 fps in Python threads is **not GIL-limited** (GIL ~14–19% with zero-copy); the bottleneck was **in-process MJPEG encode capacity** — which this plan moves into ffmpeg child processes, entirely outside Python.
- Validated **on the rig itself** (i7-12700KF) with real camera footage: 8 parallel `ffmpeg libx264 -preset ultrafast -pix_fmt gray` (true 4:0:0) 1080p encoders sustain **1201 fps aggregate with ~50% headroom**; CRF 15–18 → SSIM-Y ≈ 0.997 at 25–100× compression. `superfast` does NOT fit (13.9 ms/frame). System ffmpeg 4.4.2 supports `gray` for libx264 (verified). See [web-gui-research/dim_encoding.md](web-gui-research/dim_encoding.md).
- Rig GPU is a T400: NVENC exists (1× Volta-gen engine, contrary to folklore) but ~366–700 fps ceiling and ~3 concurrent sessions in 2 GB VRAM → **not** used. The 12700KF is "KF" → no iGPU → no QuickSync. CPU x264 is the design basis.
- Web preview: browsers cap HTTP/1.1 at ~6 connections/host (kills 8× MJPEG `<img>` streams); WebRTC doesn't traverse `ssh -L`. → **one binary WebSocket multiplexing JPEG previews** (~480×270 gray q75, ≤15 Hz, newest-frame-only backpressure ≈ 10–25 Mbps total). See [web-gui-research/dim_web-ui.md](web-gui-research/dim_web-ui.md).
- Container: **MKV** (crash-safe mid-recording; moov-at-end MP4 loses everything on crash). MKV with gray 4:0:0 H.264 plays in VLC/mpv/ffmpeg/OpenCV/DLC/SLEAP immediately; browsers would need a remux+yuv420p — acceptable ("don't necessarily need to view right after").

## Decisions

- **Encoder default**: ffmpeg subprocess per camera, `-f rawvideo -pix_fmt gray` stdin → `libx264 -preset ultrafast -crf 16 -pix_fmt gray` → `<name>.mkv`. Configurable: crf, preset, pix_fmt (`gray` | `yuv420p` compat), optional auto-remux to MP4 at trial end.
- **Raw mode** (speed-first option): Mono8 frames streamed to `<name>.raw` + JSON sidecar (width/height/fps), `octacam transcode` converts offline. Preview works identically (taps live frames, not files).
- **ffmpeg binary**: `imageio-ffmpeg` dependency (bundles a static GPL ffmpeg with libx264 → `uv tool install` stays one-command), fall back to system `ffmpeg` if present.
- **Web stack**: FastAPI + uvicorn, static frontend shipped as package data. Vanilla JS modules (no node/npm build step). Bind `127.0.0.1` by default; SSH tunnel is the auth layer.
- **Qt GUI stays untouched** for now; the web GUI is a new `octacam serve` command. Retire Qt only after rig validation. _(Update: the web GUI reached parity and the Qt GUI (`gui/`, the `octacam gui` command, the `pyside6` dependency) has since been removed; the web GUI is the only frontend. Arduino/serial control was also extracted into an opt-in plugin under `octacam.plugins.*` — see the README "Plugins" section.)_
- **Timestamps**: keep the per-camera CSV sidecar (`frame_index,timestamp,dropped`) exactly as today.
- **Repo**: no reorg needed (that was for the Rust layout). New code lives in `src/octacam/`.

## Plan

### M1 — ffmpeg writer + the "is Python fast enough" gate

1. **`src/octacam/writer.py`**: add `FfmpegVideoWriter` with the same contract as `AsyncVideoWriter` (`open/write/close`, bounded queue 20, `put_nowait` drop-on-full — reuse the existing queue/thread skeleton; consider extracting a shared base):
   - `open(filename, fps, (w,h), options)` spawns ffmpeg (`-f rawvideo -pixel_format gray -video_size WxH -framerate FPS -i - -c:v libx264 -preset ultrafast -crf 16 -pix_fmt gray -y out.mkv`), writer thread does `proc.stdin.write(frame)` (numpy buffer protocol, no `.tobytes()` copy), releases the GIL.
   - stderr drained on a small thread into a `deque` (surfaced on failure); `close()` = sentinel → drain → close stdin → `proc.wait(timeout=10)` → kill fallback → optional `-c copy` remux to .mp4.
   - Child death mid-trial: `BrokenPipeError` on write → mark failed, keep draining (drop accounting semantics preserved), log + report.
2. **`RawVideoWriter`**: same contract; preallocates file, sequential writes, JSON sidecar.
3. Wire into `Camera.start_record`/`CameraSystem.start_record` (`camera.py`): writer selection becomes a small factory keyed by format string; CLI `--codec` gains `x264` (new default) and `raw`; GUI video-writer combo gains the new entries (config `video_writer_default_index` still applies).
4. **`octacam transcode <dir-or-files>`** CLI subcommand: .raw → x264 MKV using the same ffmpeg invocation.
5. **Benchmark gate**: extend `benchmarks/bench_pipeline.py` with an `ffmpeg-x264-gray` writer option; run on the rig: `--cameras 8 --fps 150 --writer ffmpeg-x264 --trigger software` (60 s). **Pass**: drops ≤ Phase 0 MJPEG software-trigger baseline (~0.3%); **fail** → Rust fallback.

**Verify**: `PYLON_CAMEMU=8 uv run octacam record configs/emulate_8_cameras --fps 150 --duration 10 --codec x264` → 8 MKVs; `ffprobe` shows h264 High 4:0:0 `gray`, ~1500 frames; CSV rows == grabbed frames. SIGINT mid-recording leaves playable MKVs. Raw mode + transcode roundtrip. Then the rig benchmark gate above.

### M2 — recording controller extraction (shared by web + CLI + Qt)

The record/stop/abort orchestration currently lives in `gui/main_window.py` (Qt timers) and is partially duplicated in `cli.py record`. Extract into **`src/octacam/controller.py`**: a `RecordingController` owning the state machine

`preview → waiting_for_trigger → recording(deadline) → finishing → preview`

with: save-dir validation (exists → needs-confirm flag, mkdir), settings (fps live-updates `PreciseTimer`, duration value+unit, trigger source software|external, format), started-poll (replaces `check_record_started_timer`, fires the armed Arduino command exactly when all cameras started), deadline monitor thread (+0.5 s grace as in `cli.py`), stop ordering preserved (stop trigger → grab loops exit → writers drain → CSVs → restore trigger source → restart preview → auto-increment save dir — move `DirectoryEdit.increment()`'s regex into a plain function). `cli.py record` is rewritten on top of it.

**Verify**: emulator: scripted start→countdown→auto-stop, abort path, dir increments `001→002`; existing `uv run pytest` (incl. offscreen Qt test) stays green.

### M3 — `octacam serve`: FastAPI backend

New `src/octacam/web/` (server) + `src/octacam/web/static/` (frontend, package data).

- **REST** (all under `/api`):
  - `GET /api/system` — cameras (index, serial, name, w/h, yaml layout fractions + scale/rotation), serial availability, version, config dir.
  - `GET/PUT /api/settings` — fps, duration {value, unit}, save_dir, trigger_source, format {mode, crf, preset, pix_fmt, remux_mp4}; PUT fps while previewing live-updates the trigger; 409 while recording.
  - `POST /api/save-dir/validate` — {path} → {resolved, exists, creatable, free_bytes}.
  - `POST /api/recording/start` — {confirm_overwrite, arduino_command?} → 202 | 409 exists/busy.
  - `POST /api/recording/stop` | `/abort` — 202.
  - `POST /api/serial/command` — loop-program execute (same 5 fields as `serial_link.Command`); 503 if port absent.
- **WS `/api/ws`** (single socket, multiplexes everything):
  - binary server→client: 24-byte header (version, kind, camera_index, flags, frame_number u32, hw_timestamp u64, fps f32, dropped u32) + JPEG payload.
  - text server→client: `{"type":"telemetry", state, remaining_ms, disk_free, cameras:[{index,fps,frames,dropped,writer_alive}]}` (2 Hz + on state change); `{"type":"event", level, message}` (ffmpeg death, serial errors); `{"type":"settings",...}` echo for multi-tab sync.
  - text client→server: `{"type":"jog","n_steps":1|-1|0}` (stepper press-and-hold; too chatty for REST).
- **Preview producer**: asyncio task at ≤15 Hz pulls `camera.frame_for_display.pop()` (the existing `LatestFrame` pull model maps directly), downscales 1920×1080→480×270 (`frame[::4, ::4]` then `np.ascontiguousarray`), `cv2.imencode('.jpg', q=75)` in a thread executor (releases GIL, <1 ms each, ~120/s total), broadcasts newest-only per client (drop-stale: skip a client whose previous send is still pending). Zero work with no clients.
- pypylon/serial calls from handlers via `run_in_executor`.
- `octacam serve <config_dir> [--port 8000] [--host 127.0.0.1] [--serial-port /dev/ttyACM0]`; `--host 0.0.0.0` opt-in for LAN.

**Verify**: `PYLON_CAMEMU=8 uv run octacam serve configs/emulate_8_cameras` + pytest integration test using `httpx`/`websockets`: REST settings roundtrip, one binary frame per camera arrives, telemetry transitions during a scripted 5 s recording.

### M4 — web frontend (feature parity with the Qt GUI)

Vanilla JS modules in `web/static/` (no build step): `app.js, ws.js, grid.js, record.js, arduino.js, view.js`.

- **Camera grid**: one `<canvas>` per camera positioned from yaml fractional geometry (window_x/y/width/height × container size; auto-tile fallback when unset), camera-name title + live fps label, JPEG frames blitted via `createImageBitmap`.
- **Transforms**: config `scale_x/scale_y/rotation_deg` + runtime rotate ±90°/flip H/V/reset, applied-to selected|all — all pure CSS transforms on the canvas; lime crosshair overlay toggle.
- **Record panel**: duration (value + s/min/h), fps spinner (PUTs settings live), save dir (validate → overwrite-confirm dialog; shows auto-incremented path after each trial), trigger source, format selector, Start/Abort/Stop button driven by the state machine, countdown + status line, disk-free display.
- **Arduino panel** (hidden when serial absent): loop program (direction radio, steps 2–32767, interval 800–65535 µs, rest ms, repeats 1–255, initial wait s) with computed total-duration/RPM label (port `_update_step_info` math: `(steps·interval + rest)·repeats·2 + init_wait − rest`), Execute, "start with recording" checkbox (sends `arduino_command` with recording start); jog buttons via `pointerdown/pointerup` → WS jog messages at the chosen interval, release sends `n_steps: 0`.

**Verify**: browser checklist through `ssh -L 8000:127.0.0.1:8000` from a laptop: 8 live tiles ≥10 Hz, full recording from the browser, overwrite dialog, transforms/crosshair, multi-tab consistency, kill an ffmpeg child mid-trial → error event visible.

### M5 — rig validation & sign-off (hardware)

With `configs/tl_scape_fly_facing_left` on the real 8 cameras: (a) 60 s soak `--fps 150 --codec x264` — drops ≈ 0 (≤ Phase 0 baseline), CSV timestamps monotonic ~6.67 ms; (b) lab-default 20 s/100 fps trial from the browser; (c) raw-mode soak at 2.4 GB/s + transcode + spot-check vs direct x264 trial; (d) external trigger path; (e) fd census `/proc/<pid>/fd` < limit; (f) `uv tool install .` from a clean state and run via the installed tool. Update README. Then decide Qt retirement separately.

### Dependencies added

`fastapi`, `uvicorn`, `imageio-ffmpeg` (+ existing click/numpy/cv2/pypylon/pyserial/pyyaml). All wheels → `uv tool install` unchanged.

### Files touched (core)

- `src/octacam/writer.py` — FfmpegVideoWriter, RawVideoWriter (reuse AsyncVideoWriter skeleton)
- `src/octacam/camera.py` — writer factory, pass format options
- `src/octacam/controller.py` — NEW: extracted recording state machine
- `src/octacam/cli.py` — `--codec x264|raw`, `serve`, `transcode` commands
- `src/octacam/web/` — NEW: FastAPI app + static frontend
- `benchmarks/bench_pipeline.py` — ffmpeg-x264 writer option (the M1 gate)
- `gui/main_window.py` — minimal/no change (keep Qt risk at zero; controller extraction kept additive)

## Fallback: Rust service (if the M1 gate or M5 soak fails)

Fully researched and designed; key facts preserved so nothing needs re-research:

- **Stack**: single Rust process, 8 grab std::threads, axum + tokio, single binary with embedded SPA. ~4–7k LOC, 2 crates (`octacam-core` lib + `octacam` bin). Repo reorg: Python project moves to `python/`, Rust workspace at root.
- **Camera binding**: `pylon-shimload` v0.1.1 (strawlab; runtime-dlopens the pylon SDK — 7.3.0 is installed at `/opt/pylon7.3.0`; it's what strand-braid uses in production today) with `pylon-cxx` 0.4.4 as fallback. Neither bridges `ExecuteSoftwareTrigger` — use the `TriggerSoftware` GenICam command node; `WaitForFrameTriggerReady` → poll `AcquisitionStatusSelector=FrameTriggerWait`/`AcquisitionStatus` once at record start.
- **Encoding**: same per-camera ffmpeg-subprocess design (or `rsmpeg` in-process later). Same preview/WS/REST design as M3/M4 — the frontend transfers verbatim.
- **Prior art**: strawlab/strand-braid (MIT OR Apache-2.0, very active, 1.0.0-rc.2) is the reference implementation — mine its `mp4-writer`, JPEG-firehose backpressure, axum patterns; do NOT fork wholesale (one-process-per-camera; Braid is a 3D-tracking coordinator with a hardware-triggerbox assumption). GStreamer rejected (official `gst-plugin-pylon` cannot execute software triggers — issue #58, open since 2023). Aravis rejected (USB3 throughput risk). `less-avc` is lossless-only I_PCM (no compression); openh264 too slow (~830 fps aggregate best case).
- Detailed Rust milestone/threading/API design exists in the planning transcript; the research reports in [web-gui-research/](web-gui-research/) contain everything load-bearing.

## Verification (end-to-end)

1. `uv run pytest` green at every milestone (existing emulator GUI test included).
2. M1 rig gate: `uv run benchmarks/bench_pipeline.py --cameras 8 --fps 150 --writer ffmpeg-x264 --trigger software` → drops ≤0.3%.
3. Emulator smoke: headless record (x264 + raw), ffprobe checks, CSV parity, SIGINT crash-safety.
4. Browser-through-tunnel checklist (M4) and 8-camera hardware sign-off (M5).
