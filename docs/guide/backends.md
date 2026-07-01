# Camera backends

octacam drives more than one camera vendor through a common interface. You pick
the backend **per rig** with a top-level `backend` key in `octacam_config.toml`
(one vendor per config directory):

```toml
backend = "basler"   # default — omit the key and Basler is assumed
# backend = "flir"   # FLIR / Teledyne (Spinnaker / PySpin)
```

| `backend` | SDK | Per-camera parameter file | Notes |
| --- | --- | --- | --- |
| `basler` | pypylon (bundled) | `<serial>.pfs` | Default. |
| `flir` | Spinnaker / PySpin | `<serial>.json` | Runs the sensor in Mono8. |
| `fake` | none (in-memory) | `<serial>.json` | Synthetic frames for tests/CI. |

Everything else behaves identically across backends — preview, recording
(monochrome H.264 or raw), the software trigger, the per-camera
exposure/gain/ROI controls, the recording summary (and opt-in timestamp CSVs),
and the web GUI.

`doctor` takes a matching `--backend` to enumerate just one vendor:

```bash
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
