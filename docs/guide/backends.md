# Camera backends

octacam drives every camera through one vendor-neutral core (`Camera` /
`CameraSystem`). Each vendor is a thin *backend* that implements a small,
SDK-specific seam and nothing else, so the fragile grab-loop and recording
logic is written exactly once. Three backends ship:

| `backend` | Driver | Install | Parameter file | Notes |
| --- | --- | --- | --- | --- |
| `basler` | pypylon | bundled (core dependency) | `<serial>.pfs` | **default** |
| `flir` | Spinnaker SDK + PySpin | installed separately (not on PyPI) | `<serial>.json` | runs the sensor in Mono8 |
| `fake` | none (in-memory synthetic) | bundled | `<serial>.fake` | synthetic frames for tests / CI |

## Selecting a backend

A rig uses **one** backend, chosen once by the top-level `backend` key in its
`octacam_config.toml`. Every camera in that rig is opened through it — there is
no per-camera auto-detection.

```toml
backend = "basler"   # default — omit the key and Basler is assumed
# backend = "flir"   # FLIR / Teledyne (Spinnaker / PySpin)
# backend = "fake"   # in-memory synthetic cameras (tests / CI)
```

!!! note "Parsing is tolerant"
    The config loader never raises. If `backend` is missing, is not a string,
    or names anything other than `basler`, `flir`, or `fake`, octacam logs a
    warning and falls back to `basler`.

If a backend is selected but its SDK is not installed (for example `flir`
without PySpin), octacam surfaces a clear "backend unavailable" message that
names the missing dependency, rather than a raw `ImportError` traceback.

## Basler (pypylon) — default

The pypylon runtime is bundled with octacam, so a Basler rig works out of the
box with no separate SDK install. Per-camera sensor parameters persist as
Basler's native `.pfs` feature-stream files (`<serial>.pfs`), written by the web
GUI's save dialog.

To run without hardware, use Basler's built-in camera emulator (this uses the
real `basler` backend against emulated devices — it is not the `fake` backend):

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

!!! tip "\"Insufficient system resources\" on start"
    If a Basler camera fails to start streaming with an *insufficient system
    resources* error, raise the open-file limit (`ulimit -n`; pylon needs
    roughly 150 file descriptors per camera) and check the kernel's
    `usbfs_memory_mb`. octacam logs this hint, naming the affected camera, when
    a start fails.

## FLIR / Teledyne (Spinnaker / PySpin)

FLIR / Teledyne cameras use the Spinnaker SDK's **PySpin** wheel, which is **not
on PyPI** — it ships with the Spinnaker SDK installer. Install it separately:

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) record the intent — installs nothing on its own:
pip install "octacam[flir]"
```

!!! warning "The `flir` extra installs nothing"
    `octacam[flir]` is an empty marker that documents intent. FLIR support
    requires the manually-installed Spinnaker SDK and PySpin wheel above; the
    extra does not pull them in.

At open time the backend forces `PixelFormat = Mono8` so the sensor delivers a
2-D `uint8` frame matching octacam's grayscale video writer; a non-mono frame is
warned about and skipped. Per-camera parameters persist as JSON
(`<serial>.json`) holding the editable node values plus the trigger state — the
same scheme the `fake` backend uses.

The Spinnaker `System` singleton is held for the whole session and released
exactly once, after every camera has been closed. octacam handles this
teardown automatically when the camera system shuts down.

## Fake (in-memory)

The `fake` backend produces synthetic mono `uint8` frames entirely in memory —
no hardware and no SDK — so the backend-selection layer, the parameter
persistence, and the shared controller/web logic can be exercised in tests and
CI. It is software-trigger driven exactly like the real cameras: a frame is only
returned once a trigger has fired.

Its serials come from the `OCTACAM_FAKE_CAMERAS` environment variable
(comma-separated; default `FAKE-0,FAKE-1`), mirroring how `PYLON_CAMEMU=N`
summons emulated Basler cameras. Parameters persist as JSON in `<serial>.fake`
files.

## What every backend provides

All backends implement the same `CameraBackend` seam, so preview, recording
(monochrome H.264 or raw), the software trigger, the per-camera parameter
controls, the recording summary, the optional per-frame timestamp CSV, and the
web GUI behave identically regardless of which backend is active.

### Editable sensor parameters

The GUI's Camera tab exposes a fixed set of parameters, mapped from octacam's
snake_case names to their standard GenICam (SFNC) node names. Basler and FLIR
share these node names:

| octacam name | GenICam node | Writable while grabbing? |
| --- | --- | --- |
| `width` | `Width` | no (geometry) |
| `height` | `Height` | no (geometry) |
| `exposure` | `ExposureTime` | yes |
| `gain` | `Gain` | yes |
| `offset_x` | `OffsetX` | yes |
| `offset_y` | `OffsetY` | yes |

`Width` and `Height` are geometry parameters: the SDK refuses to write them
while the camera is grabbing, so octacam transparently stops and restarts the
preview grab around the change (and always restores the preview, even if the
device rejects the value). Exposure, gain, and offsets are writable on a running
camera. A parameter a given model does not expose is simply omitted from the
Camera tab.

### Enumeration and opening

- With **no** camera serials pinned in the config, every detected camera is
  used, sorted by serial number.
- With serials pinned (`[[cameras]]` entries), cameras are used in the listed
  order; any pinned serial that is not connected is logged as a warning and
  skipped.

Cameras are opened, configured, and started **concurrently** (one worker
thread per camera). Because each SDK releases the GIL on its blocking USB
calls, opening N cameras takes roughly one camera's time rather than N times as
long. If any camera fails to open, all cameras are closed and the error is
re-raised.

### Parameter files

Each backend persists one sensor-parameter file per camera, named
`<serial>.<extension>` (`.pfs`, `.json`, or `.fake`) in the rig's config
directory. A camera whose file is missing opens at its defaults, with a
warning.

## How backends handle triggering

Recording uses one of two trigger sources. The default is set by the
`trigger_source_default_index` key under the config's `[gui]` table
(`0` = software, `1` = external), and `octacam record --trigger` overrides it per
run (its accepted values are `software` and `hardware`, where `hardware`
corresponds to the config-level `external` trigger source):

- **`software`** (`--trigger software`) — octacam paces a software trigger from a
  dedicated timer thread at the configured fps.
- **`external`** (`--trigger hardware`) — a truly external master fires the
  cameras; octacam restores each camera's config-native trigger source and does
  not drive the trigger.

Live **preview is always software-triggered**, regardless of the recording
trigger source: each backend arms the `FrameStart` trigger with
`TriggerMode = On` and `TriggerSource = Software` when preview starts.

For the software path, `trigger_once()` is a genuine per-device SDK call: the
Basler backend calls `ExecuteSoftwareTrigger`, and the FLIR backend executes the
`TriggerSoftware` command node. The shared timer fires these serially across all
cameras at the target rate.

!!! note "Saving parameters normalizes the trigger"
    A "Save" taken while previewing must not bake `TriggerSource = Software`
    into the parameter file — that would make a later external-trigger recording
    silently never start. Every backend restores the camera's original
    (config-loaded) trigger source and sets `TriggerMode = Off` for `FrameStart`
    when saving. Keep this parity if you add a backend.

## Listing detected cameras

`octacam list-cameras` enumerates cameras through a chosen backend. The output
is tab-separated:

```bash
octacam list-cameras                    # basler (default): model<TAB>serial
octacam list-cameras --backend flir     # flir: serial<TAB>backend
octacam list-cameras --backend fake     # fake serials from OCTACAM_FAKE_CAMERAS
```

Set `PYLON_CAMEMU=N` to have the Basler enumeration report N emulated cameras.
If the requested backend's SDK is missing, the command exits with the same
"backend unavailable" message described above.
