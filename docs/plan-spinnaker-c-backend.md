# Plan: Spinnaker C-API FLIR backend (`spinnaker` tier)

**Status:** IMPLEMENTED and hardware-verified (2026-07-04). Branch
`feat/harvesters-backend`. See "## Outcome" at the bottom.
**Why:** give FLIR cameras a fast, watermark-free, Python-3.14 path that does not
starve co-recorded cameras. The two shipped fixes below make FLIR *work* on
py3.14 without a watermark; this plan makes it *fast*.

## Background — what is already done (shipped on this branch)

Two problems the user reported were fixed and hardware-verified:

1. **BALLUFF watermark.** The auto cascade used to fall the FLIRs through to the
   `harvesters` tier, whose only freely-installable GenTL producer is Balluff
   **mvIMPACT**, which stamps a "BALLUFF … evaluation period ended" rectangle onto
   the frames after ~8 s and is EULA-restricted for third-party cameras.
   **Fix:** `registry.CASCADE` is now `("basler", "flir", "pycameleon")` —
   `harvesters` is removed from auto-selection and is opt-in only
   (`backend = "harvesters"`). FLIRs now land on the always-free `pycameleon`
   floor. No producer, no watermark, no EULA.

2. **Shared trigger thread throttled the Basler.** `CameraSystem._trigger_all`
   calls `trigger_once()` on every camera serially on one thread; the Basler and
   harvesters/FLIR backends did the device software-trigger *there* (a native,
   GIL-holding call), so a slow camera delayed the fast one.
   **Fix:** new `cameras/_trigger_handoff.py::SoftwareTriggerHandoff` mixin —
   `trigger_once()` only bumps a `_pending` counter (cap `PENDING_MAX=2`,
   drop-newest with a rate-limited warning); each camera's own `retrieve()` fires
   the device trigger and fetches one frame. Adopted by fake, pycameleon, basler,
   harvesters, flir. Keeps the invariant *one frame recorded per trigger fired*.

Full suite 406 pass, ruff clean, pyright 0 new errors (21→17 on `cameras/`).

## The remaining problem this plan solves

**`pycameleon.receive()` holds the Python GIL for the whole exposure wait.**
Measured on the rig (2× FLIR Grasshopper3 GS3-U3-41C6NIR @ 1920×1200,
auto-exposure ≈ 44 ms; 1× Basler acA1920-150um):

| scenario | Basler fps | FLIR fps (each) |
|---|---|---|
| Basler alone (no writer) | ~69 (USB/exposure ceiling at full ROI) | — |
| Basler + 2 FLIR via **pycameleon** | **~5** (starved) | ~4–5 |
| 2 FLIR only via pycameleon | — | ~4 (they starve each other) |
| Basler + 2 FLIR via **mvIMPACT** (old, watermarked) | ~30 | ~16 |

The GIL hold was measured directly: a 1 ms ticker thread saw **57 ms** max stalls
during `receive()`. mvIMPACT is faster *only* because `HarvestersBackend.retrieve`
polls with `try_fetch(2 ms)` + `time.sleep(3 ms)` and `time.sleep` **releases the
GIL**. pycameleon's `receive()` has no timeout parameter, so the same
poll-and-sleep trick cannot be applied; its `receive_async` needs a running
asyncio loop (calls `get_running_loop()` at call time), which does not fit
octacam's threaded grab-loop model without a larger rewrite.

**Decision (user):** build a thin ctypes binding to the Spinnaker **C API** first
(this plan); fork pycameleon to release the GIL later as a fallback/secondary win.
The C API path is the only one that is simultaneously fast, watermark-free,
Python-3.14-capable, and clean-closing (we control DeInit/ReleaseInstance
ordering directly, sidestepping the GenTL producer's GIL-holding `DevClose`
deadlock — see [[harvesters-migration]]).

## Design

New module `src/octacam/cameras/spinnaker_c.py` implementing the `CameraBackend`
protocol (`cameras/base.py`) over `libSpinnaker_C.so` via `ctypes`. Mirror the
structure of `flir.py` (PySpin) — same SFNC node names, same `PARAM_NODES`, same
`extension = "json"`, same `teardown()` for the session-wide `spinSystem`
singleton — but call the flat C ABI instead of PySpin.

Reuse `SoftwareTriggerHandoff`: `trigger_once()` → `_bump_trigger()`; the device
`TriggerSoftware` execute + `spinCameraGetNextImageEx` happen in `retrieve()` on
the grab thread. **The GIL win:** `ctypes` releases the GIL around every foreign
call by default, so `spinCameraGetNextImageEx(timeout)` blocking for the exposure
does NOT hold the GIL — the Basler thread keeps running. (This is the whole point
vs pycameleon; must be verified, see below.)

### Registry / cascade

- Add `"spinnaker"` to `registry.BACKENDS` and to `CASCADE` **above** `pycameleon`
  and (probably) at the FLIR-vendor position: `("basler", "flir", "spinnaker",
  "pycameleon")`. Rationale: `flir` (PySpin) stays highest for the cp310 rigs that
  have it; on py3.14 `flir` drops out and `spinnaker` (C API) claims the FLIRs
  before the `pycameleon` floor.
- `select_backend("spinnaker")` imports `cameras.spinnaker_c` defensively and
  returns `(enumerate_spinnaker, SpinnakerBackend, "json")`; raises
  `BackendUnavailable("spinnaker", "the Spinnaker SDK (libSpinnaker_C.so) is not
  installed")` when `ctypes.CDLL("libSpinnaker_C.so")` fails — so it self-disables
  on boxes without the SDK, exactly like `flir`.
- Add `"spinnaker": "octacam.cameras.spinnaker_c"` to `_TEARDOWN_MODULES` (the
  `spinSystem` handle must be released once after all cameras close).

### Minimal C call sequence (verified names from the research pass)

All are `spinError`-returning functions on opaque handles; wrap each in a helper
that raises `BackendError` on non-`SPINNAKER_ERR_SUCCESS`.

```
enumerate:  spinSystemGetInstance(&hSystem)
            spinCameraListCreateEmpty(&hCamList)
            spinSystemGetCameras(hSystem, hCamList)
            spinCameraListGetSize(hCamList, &n)
            spinCameraListGet(hCamList, i, &hCam)
            -> read DeviceSerialNumber node for each (TL device nodemap)
open:       spinCameraInit(hCam)
            spinCameraGetNodeMap(hCam, &hNodeMap)
            set PixelFormat=Mono8, TriggerSelector=FrameStart,
                TriggerMode=On, TriggerSource=Software
              (spinNodeMapGetNode + spinEnumerationGetEntryByName +
               spinEnumerationEntryGetEnumValue + spinEnumerationSetEnumValue)
grab start: spinCameraBeginAcquisition(hCam)
trigger:    node TriggerSoftware -> spinCommandExecute
retrieve:   spinCameraGetNextImageEx(hCam, timeout_ms, &hImage)  # GIL-free
            spinImageIsIncomplete(hImage, &incomplete)
            spinImageGetWidth/Height/GetData/GetTimeStamp
            spinImageRelease(hImage)   # MUST release every image
stop:       spinCameraEndAcquisition(hCam)
close:      spinCameraDeInit(hCam) ; drop hCam
teardown:   spinCameraListClear(hCamList); spinCameraListDestroy(hCamList)
            spinSystemReleaseInstance(hSystem)   # exactly once, after all DeInit
```

Node read/write (bounds + values, for `read_node`/`write_node`): use the GenApi C
functions from `SpinnakerGenApiC.h` — `spinNodeMapGetNode`,
`spinFloatGetValue/SetValue/GetMin/GetMax`, `spinIntegerGetValue/...`,
`spinNodeGetAccessMode` for writability. This gives the **bounds + HW timestamps**
pycameleon lacks (a bonus over the current floor).

References for exact signatures/ctypes prototypes (no complete open-source ctypes
binding exists — we write the first):
- Spinnaker.jl (Julia `ccall`, v1.2.0) — complete call signatures.
- bytedeco/javacpp-presets `spinnaker` (JNI) — full API surface mapping.
- FLIR's bundled C examples (`Acquisition_C`, `Enumeration_C`, `Trigger_C`).

## Effort & risks

- ~1–2 days: minimal enumerate→Mono8→software-trigger→grab→close is ~25–30
  distinct C functions; parameter-complete (bounds) is the longer tail.
- **Own the binding** — no upstream to lean on. Struct layouts (`spinImage`
  handle is opaque, fine) and enum values must match `SpinnakerDefsC.h`.
- Error-code discipline: every call returns `spinError`; a missed check on
  `GetNextImageEx`/`ImageRelease` leaks image buffers and stalls acquisition.
- Clean close: replicate the PySpin ordering (EndAcquisition → DeInit → drop →
  ReleaseInstance once) that already closes cleanly for the FLIR C++ SDK.

## Environment gotchas discovered

- The Spinnaker C runtime is registered in `ldconfig` as
  `libSpinnaker_C.so → /opt/spinnaker/lib/libSpinnaker_C.so` (SDK **4.3.0.189**),
  but `/opt/spinnaker` is **not directly accessible from the sandboxed Bash tool**
  in this environment — `ls`/`find`/`head` and even `ctypes.CDLL` failed there.
  Build/test this backend **unsandboxed** (or add `/opt/spinnaker` to the sandbox
  allowlist). Camera enumeration via mvIMPACT/pycameleon worked because those libs
  live elsewhere on the ldconfig path.
- Dev headers are dpkg-registered under `/opt/spinnaker/include/spinc/`
  (`SpinnakerC.h`, `SpinnakerGenApiC.h`, `SpinnakerDefsC.h`, …) but were not
  readable here for the same sandbox reason — read them when building for the
  authoritative prototypes/enum values.
- **Do NOT** revive the Spinnaker *GenTL producer* (`Spinnaker_GenTL.cti`) via
  Harvesters: it enumerated 0 of the 3 USB3 cameras here and its `DevClose`
  deadlocks while holding the GIL (rig-confirmed; see [[harvesters-migration]]).
  This plan uses the *SDK C API*, a different, deadlock-free code path.

## Verification checklist (on the rig, unsandboxed)

1. `spinnaker` backend enumerates the 2 FLIR serials (17475185, 17475187).
2. FLIR via `spinnaker`: open → Mono8 → software-trigger → grab N frames →
   **clean close** (no hang). No BALLUFF watermark in the saved mkv.
3. GIL test: 1 ms ticker sees < ~10 ms stalls during `GetNextImageEx` (proves
   ctypes releases the GIL — the pycameleon failure mode is gone).
4. `test_basler_and_flir` config: Basler holds ~69 fps (its ceiling) with 2 FLIR
   co-recording via `spinnaker`; FLIR ≥ ~16 fps each. i.e. no cross-starvation.
5. `read_node` returns real min/max/inc/unit for exposure/gain/width/height.
6. Full suite + ruff + pyright green; add `tests/test_spinnaker_backend.py` with a
   ctypes-mocked lib (mirror `test_harvesters_backend.py`'s fake-handle approach).

## Follow-up (deferred, user-approved order: after the above)

- Fork `pycameleon` to wrap its blocking `receive()` in `py.allow_threads` so the
  Rust core releases the GIL during the wait. Restores the clean MIT/libusb/no-SDK
  deploy as a GIL-friendly floor and could add the missing node bounds + per-frame
  HW timestamp (the Rust core already has both). Needs cargo+maturin to build
  wheels; publish or upstream. See [[harvesters-migration]] gaps list.

## Outcome (2026-07-04, implemented + verified)

Built `src/octacam/cameras/spinnaker_c.py`: a thin `ctypes` binding (`_Spinnaker`
façade that sets every function's argtypes/restype — mandatory on 64-bit or the
opaque handles truncate — and raises `BackendError` on any non-success
`spinError`) plus `SpinnakerBackend`, structurally identical to `FlirBackend`.
Registry wired: `BACKENDS` gains `spinnaker`; `CASCADE = ("basler","flir",
"spinnaker","pycameleon")`; `select_backend("spinnaker")` loads
`libSpinnaker_C.so` lazily and self-disables via `BackendUnavailable` when absent
(exactly like FLIR/PySpin); `_TEARDOWN_MODULES` gains it (System released once
after all cameras close). Unit test `tests/test_spinnaker_backend.py` fakes the
façade (17 tests); `tests/test_backends.py` updated for the new cascade.

**On-rig (2× FLIR GS3-U3-41C6NIR, 2048×2048 Mono8, exposure forced 40 ms):**
- Enumerate → `17475185`, `17475187`. Open → Mono8 → software-trigger → grab →
  **clean close** (~1.0 s/cam DeInit+Release, teardown 2.7 s; NO hang — this is
  the SDK C API, not the deadlocking Spinnaker *GenTL producer*). Process exits
  code 0.
- **GIL PROBE PASSED — the whole point.** A 1 ms ticker thread ran while BOTH
  FLIRs grabbed concurrently on their own threads (each blocking ~40 ms in
  `spinCameraGetNextImageEx`): ticker p50=1.05 ms, **p99=1.06 ms, max=2.38 ms**.
  So `ctypes` (a `CDLL`, not `PyDLL`) releases the GIL around the blocking grab —
  the pycameleon failure mode (57 ms stalls) is gone. The ticker stands in for the
  Basler thread; it never stalled ⇒ no cross-starvation by construction.
- Both FLIRs sustained **18.7 / 18.8 fps** each (exposure-limited; ≥ the ~16 fps
  target), with no watermark over a 12 s run (> the 8 s mvIMPACT eval window; this
  path loads no GenTL producer, so there is no watermark mechanism).
- `read_node` returns real bounds: width 32/2048/32, height 2/2048/2, exposure
  16.1–351828 µs, gain 0–9.83 dB. C-API quirk (verified, documented in code):
  integer nodes expose no unit and float nodes no increment
  (`SpinnakerGenApiC.h` has no `spinIntegerGetUnit` / `spinFloatGetInc`);
  `snap_value` tolerates both.
- Full suite **425 pass** (was 406; +17 spinnaker + 2 selection tests), ruff
  clean, pyright 0 new on `cameras/` (stays at the 17-error baseline).

NOT run end-to-end: the literal "Basler + 2 FLIR co-record, Basler holds ~69 fps"
mixed-rig test (checklist #4) — the Basler needs pypylon + a udev rule and wasn't
wired here. The GIL ticker probe is the direct mechanistic proof of the same
claim (a real Basler grab thread keeps running exactly as the ticker did).

## Post-verification perf fix (2026-07-04): `TriggerOverlap=ReadOut`

On the mixed rig the user saw the FLIRs at ~6–11 fps (and, historically, the Basler
starved). Diagnosis: the Basler starvation was already fixed (spinnaker releases the
GIL — mixed rig Basler = 74 fps), but the FLIRs were throttled by the FLIR default
`TriggerOverlap=Off`: a FrameStart software trigger fired during the previous frame's
readout is **silently ignored**, so the camera accepts only ~every other trigger →
half rate + a full 200 ms grab-timeout stall on each dropped one.

Single-FLIR bench @ 4 ms exposure, 2048² Mono8:
| mode | frames | fps | getnext p50 / max |
|---|---|---|---|
| SW trigger, TriggerOverlap=Off (before) | 76/150 | 4.7 | 15 / **200** ms |
| SW trigger, TriggerOverlap=ReadOut (after) | 150/150 | **64** | 15 / 15 ms |
| Continuous free-run (hexaview-style) | 100/100 | **78** | 12 / 66 ms |

Fix: `SpinnakerBackend._enable_trigger_overlap()` sets `TriggerOverlap=ReadOut`
(best-effort) in `enable_frame_trigger` + `begin_software_trigger_preview`; mirrored
into `flir.py`. Mixed rig after fix @ 4 ms / 80 Hz: Basler 73.6, FLIR 62.4 / 62.6 fps.

The remaining 62→80 fps gap is not a bug: full-2048² readout ceiling (~64 fps for SW
trigger) + SW-trigger per-frame overhead. To go faster: smaller ROI (hexaview
fast.yaml uses 1664×1120), a shorter exposure (octacam configs set none, so fps is
capped by whatever the camera currently holds — set it via the GUI slider, then
`octacam config` snapshots it to a `{serial}.json`), or `trigger_source=external`
(hexaview's Arduino-PWM hardware-trigger path) for tight multi-cam sync at the strobe
rate.

Still deferred (unchanged): the pycameleon GIL fork in the follow-up above.
