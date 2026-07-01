# Camera backends

octacam drives more than one camera vendor through a common interface. By
default a rig **auto-detects every installed backend** and simply uses whatever
is connected, so a single rig can mix Basler and FLIR cameras — you don't have
to choose. The optional top-level `backend` key only *pins* a rig to one vendor:

```toml
# backend = "auto"   # default (also what an absent key means): use every
#                    # installed backend — Basler and FLIR together if both fit
# backend = "basler" # pin to Basler only
# backend = "flir"   # pin to FLIR / Teledyne (Spinnaker / PySpin) only
```

| `backend` | SDK | Per-camera parameter file | Notes |
| --- | --- | --- | --- |
| `auto` | any installed | per camera (see below) | **Default.** Sweeps Basler + FLIR. |
| `basler` | pypylon (bundled) | `<serial>.pfs` | Pin to Basler. |
| `flir` | Spinnaker / PySpin | `<serial>.json` | Pin to FLIR; runs the sensor in Mono8. |
| `fake` | none (in-memory) | `<serial>.json` | Synthetic frames for tests/CI. |

In a mixed rig each camera keeps its own vendor's parameter-file format — a
Basler camera persists as `<serial>.pfs`, a FLIR camera as `<serial>.json`, side
by side in the same config directory. Everything else behaves identically across
vendors — preview, recording (monochrome H.264 or raw), the software trigger,
the per-camera exposure/gain/ROI controls, the recording summary (and opt-in
timestamp CSVs), and the web GUI.

On a single-vendor machine `auto` resolves to just that vendor, so existing
configs (which omit the key, or set `backend = "basler"`) keep working unchanged.

`doctor` sweeps every available backend by default; pass `--backend` to
enumerate just one vendor:

```bash
octacam doctor              # every installed backend
octacam doctor --backend flir
```

## Basler

The default. The pypylon runtime is bundled with octacam, so a Basler rig works
out of the box — no separate SDK install. Per-camera sensor parameters live in
`<serial>.pfs` files (pylon's native format) next to the config.

To run without hardware, use Basler's built-in emulator:

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_8_cameras
```

## FLIR / Teledyne

FLIR / Teledyne cameras use the Spinnaker SDK's **PySpin** wheel, which is
**not on PyPI** — it ships with the SDK installer.

```bash
# 1. Install the Spinnaker SDK for your platform (from Teledyne).
# 2. Install the matching PySpin wheel into octacam's environment:
pip install spinnaker_python-*.whl
# 3. (optional) record the intent — installs nothing on its own:
pip install "octacam[flir]"
```

Per-camera sensor parameters persist as `<serial>.json` (written by the GUI's
*Save…* dialog), not as Basler `.pfs`. If PySpin is missing when a FLIR config is
served, octacam exits with a clear message rather than a traceback.

With no `[[cameras]]` entries, every detected FLIR camera is used; add entries
keyed by `serial_number` to pin names and the display layout. See
[configs/flir_example/](https://github.com/NeLy-EPFL/octacam/tree/main/configs/flir_example).

## Fake

The `fake` backend produces synthetic in-memory frames with no hardware or SDK.
It always reports `FAKE-*` serials and is used for tests and CI. Because it never
reflects real hardware, `doctor` only enumerates it when you ask explicitly
(`--backend fake`), never in the default all-backends sweep.
