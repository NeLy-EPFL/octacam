# Camera backends

octacam drives many cameras through one common interface, and by default it
**auto-detects the best available driver for each camera**. Rather than picking a
vendor, a rig runs a *preference cascade*: every camera is claimed by the
highest-priority tier that can see it, and there is always a working,
pip-installable floor. So a rig can mix Basler, FLIR, and any other
USB3-Vision (GenICam) camera and simply use whatever is plugged in.

The optional top-level `backend` key only *pins* a rig to one backend:

```toml
# backend = "auto"       # default (and what an absent key means): the cascade
# backend = "basler"     # pin to Basler (pypylon)
# backend = "flir"       # pin to FLIR / Teledyne (Spinnaker / PySpin)
# backend = "spinnaker"  # pin FLIRs to the Spinnaker C-API tier (no cp310 wheel limit)
# backend = "harvesters" # pin to a GenTL producer (any GenICam camera)
# backend = "pycameleon" # pin to the libusb USB3-Vision floor
```

## The cascade

Under `auto`, octacam tries the cascade tiers in order and claims each camera for
the first one that enumerates its serial (lower tiers skip serials already
claimed, so a camera is never opened twice). The `harvesters` GenTL tier and
`fake` are **not** swept by `auto` — they are opt-in only, reached by pinning the
`backend` key:

| Priority | `backend` | Driver | Install | Bounds / HW timestamp |
| --- | --- | --- | --- | --- |
| 1 (vendor) | `basler` | pypylon | pip (core) | full |
| 1 (vendor) | `flir` | Spinnaker + PySpin | **manual**, cp310 wheel only | full |
| 2 (FLIR C-API) | `spinnaker` | Spinnaker C API (`libSpinnaker_C.so`, ctypes) | **manual** SDK, any Python | full |
| 3 (floor) | `pycameleon` | libusb (USB3 Vision) | pip (core) | none¹ |
| — (opt-in) | `harvesters` | any GenTL `.cti` producer | pip (core) + a **producer** | full |
| — | `fake` | in-memory synthetic | pip (core) | — |

¹ pycameleon 0.2.x exposes node *values* only — no min/max/increment or
hardware timestamps — so the GUI sliders fall back to free-form entry and frame
timestamps use host time. It still previews, records, and drives exposure/gain/
ROI. See [Deferred work](#deferred).

Because pycameleon is a core dependency, the cascade **always has a working
backend** on any supported Python, even before you install a vendor SDK or a
GenTL producer. Installing a higher tier simply upgrades the cameras it can
claim — e.g. `pip install`-ed pypylon takes the Basler over from pycameleon,
gaining node bounds and hardware timestamps.

Each camera keeps its own backend's parameter-file format side by side in the
config directory — a Basler persists as `<serial>.pfs`, everything else as
`<serial>.txt` (the camera's native GenApi feature-persistence TSV). Everything
else behaves identically across tiers: preview,
recording (monochrome H.264 or raw), the software trigger, the per-camera
exposure/gain/ROI controls, the recording summary (and the opt-in timestamp file),
and the web GUI. Existing configs (which omit the key, or set
`backend = "basler"`) keep working unchanged.

`doctor` reports which tiers are available and **which backend each detected
camera would open through** under the cascade; pass `--backend` to enumerate just
one:

```bash
octacam doctor                 # tier availability + per-camera cascade choice
octacam doctor --backend flir
```

## Tier 1 — Basler (pypylon)

The pypylon runtime is bundled with octacam, so a Basler rig works out of the box
— no separate SDK install. Per-camera sensor parameters live in `<serial>.pfs`
files (pylon's native format).

To run without hardware, use Basler's built-in emulator:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

## Tier 1 — FLIR / Teledyne (Spinnaker + PySpin)

FLIR / Teledyne cameras use the Spinnaker SDK's **PySpin** wheel, which is **not
on PyPI** (it ships with the SDK installer) and only has a **cp310** wheel — so
the FLIR *vendor* tier is reachable **only on Python 3.10** (the PySpin wheel is
cp310-only). On newer Python this vendor tier is simply absent and the cascade
claims the FLIR through the `spinnaker` tier (the Spinnaker SDK C API via ctypes)
if the SDK is installed, otherwise through the always-present `pycameleon` floor —
both drive FLIR cameras (just without the PySpin vendor SDK). harvesters is never
selected automatically.

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment (Python 3.10):
pip install spinnaker_python-*.whl
# 3. (optional) record the intent — installs nothing on its own:
pip install "octacam[flir]"
```

Per-camera sensor parameters persist as `<serial>.txt` (the native GenApi
feature-persistence TSV). If a config pins
`backend = "flir"` and PySpin is missing, octacam exits with a clear message
rather than a traceback; under `auto` it just uses a lower tier.

## Tier 2 — spinnaker (Spinnaker C-API)

The `spinnaker` tier drives FLIR / Teledyne cameras through the Spinnaker SDK's
**C API** (`libSpinnaker_C.so`) via `ctypes` — no PySpin wheel, so it has **no
cp310 limit and runs on any modern Python**. It needs the Spinnaker SDK installed
(the same system SDK the `flir` vendor tier uses), but not the PySpin Python
wheel; when `libSpinnaker_C.so` can't be loaded the tier self-disables, exactly
like `flir`.

Because `ctypes` releases the GIL around the blocking grab call, a slow FLIR
exposure does not starve co-recorded cameras. The tier reports full node bounds
and hardware timestamps, and persists per-camera parameters as `<serial>.txt` (the
native GenApi feature-persistence TSV). In the `auto` cascade it sits **below the
`flir` vendor tier and above the `pycameleon` floor**, so on any Python where
PySpin is unavailable the FLIRs are claimed here before falling to pycameleon.

## harvesters + a GenTL producer (opt-in, not in the auto cascade)

This tier is used **only when a rig pins `backend = "harvesters"`** — `auto` never
selects it. [Harvesters](https://github.com/genicam/harvesters) is a vendor-neutral GenICam
GenTL *consumer*: it drives any camera reachable through an installed GenTL
*producer* — a `.cti` transport layer discovered on `GENICAM_GENTL64_PATH` (or
via octacam's `OCTACAM_GENTL_CTI` override). `harvesters` and `genicam` ship in
core, so this tier needs no per-vendor Python wheel; you only install the
**producer** (a system dependency, like the vendor SDKs). It gives full node
bounds and hardware timestamps for any GenICam camera.

Point octacam at a producer by installing its SDK (which sets
`GENICAM_GENTL64_PATH`) or listing the `.cti` explicitly:

```bash
export OCTACAM_GENTL_CTI=/opt/pylon/lib/gentlproducer/gtl/ProducerU3V.cti
octacam doctor    # confirm the producer + cameras are found
```

**Choosing a producer:**

- **Basler pylon `ProducerU3V`** (`/opt/pylon/lib/gentlproducer/gtl`) —
  *recommended.* Installed with the Basler pylon SDK, it opens and **closes
  cleanly** with no watermark. It enumerates **Basler** U3V cameras only (it does
  not see FLIR); drive FLIR cameras with the dedicated `spinnaker`/`flir`
  backends. octacam handles its one quirk transparently — it does not implement
  `Buffer.timestamp_ns`, so octacam falls back to the raw device timestamp.
- **Allied Vision Vimba X** — only for **Allied Vision** cameras or **third-party
  GigE Vision** cameras. Its bundled *USB* transport layer enumerates Allied
  Vision USB cameras *only* (Vimba X's third-party support is GigE Vision, not
  USB3), so it does **not** see a FLIR/Basler USB3 rig. Its bundled Camera
  Simulator TL is skipped by octacam because it presents phantom cameras.
- **Teledyne Spinnaker producer** — **denied by default.** Its `DevClose`
  deadlocks *while holding the Python GIL*, which wedges the whole process — no
  bounded thread, `os._exit`, or signal handler can recover, only an external
  `SIGKILL`. Use the vendor `spinnaker`/`flir` backend for FLIR. `octacam doctor`
  reports it as *detected but not used* so you can see it was found.
- **Balluff mvIMPACT / Impact Acquire** — **removed and denylisted.** It
  watermarks third-party frames after an ~8 s eval window and SIGSEGVs during the
  device scan. Do not use it as the octacam producer.
- **The Imaging Source** — vendor-locked to TIS cameras; unusable here.

**Selecting among several producers.** When more than one `.cti` is on the path,
set `OCTACAM_GENTL_PRODUCER` to a `:`-separated list of case-insensitive name
substrings to pin or prioritise them; a camera seen by two producers is bound to
the highest-priority one:

```bash
export OCTACAM_GENTL_PRODUCER=ProducerU3V              # load only pylon's U3V producer
export OCTACAM_GENTL_PRODUCER=ProducerU3V:ProducerGEV  # prefer U3V, then GEV
```

Left unset, octacam loads every discovered producer except the known-bad ones
(Spinnaker's GIL-deadlocking producer, mvIMPACT, and Vimba's Camera Simulator).

## Tier 3 — pycameleon (the always-available floor)

[pycameleon](https://pypi.org/project/pycameleon/) (MIT) speaks USB3 Vision
directly through `libusb` — no `.cti` producer, no vendor SDK, no EULA — and its
`cp39-abi3` wheel installs on any modern Python. It is a **core dependency**, so
it is always present and the guaranteed final fallback of the cascade. The only
system requirement is `libusb-1.0`.

Its limitations (no node bounds/increment/unit or writability; no per-frame
hardware timestamp) are documented under [Deferred work](#deferred); it degrades
gracefully to free-form parameter entry and host timestamps.

## Fake

The `fake` backend produces synthetic in-memory frames with no hardware or SDK.
It always reports `FAKE-*` serials and is used for tests and CI. Because it never
reflects real hardware, `doctor` only enumerates it when you ask explicitly
(`--backend fake`), never in the default cascade.

## Deferred

pycameleon's missing node bounds and hardware timestamps exist in the underlying
Rust `cameleon` core; the plan is to add the ~2 bindings (fork → publish wheels
or upstream) and depend on that. Until then pycameleon degrades gracefully.
