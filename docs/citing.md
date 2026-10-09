# Citing and credits

--8<-- "README.md:citing"

## Credits

octacam is developed in the [Neuroengineering Laboratory (Ramdya lab)](https://www.epfl.ch/labs/ramdya-lab/) at EPFL. It succeeds [SeptaCam](https://github.com/NeLy-EPFL/SeptaCam) (MIT), the lab's software for its seven-camera rig.

**People.** Thomas Ka Chung Lam wrote and maintains octacam. Matthias Durrieu wrote the composite grid videos, the transfer of recordings to storage, and the two-photon trigger plugin with its Arduino sketch.

**Camera drivers**, one per tier of the [backend cascade](guide/backends.md):

- [pypylon](https://github.com/basler/pypylon) (BSD-3-Clause), Basler's Python binding to its pylon SDK; the pylon runtime its wheels carry is Basler's.
- The [Spinnaker SDK](https://softwareservices.flir.com/Spinnaker/latest/index.html) and its PySpin binding (Teledyne FLIR, proprietary), installed separately for FLIR cameras.
- [pycameleon](https://github.com/Menchen/pycameleon) (MIT), Python bindings to [cameleon](https://github.com/cameleon-rs/cameleon) (MPL-2.0), a Rust library for GenICam USB3-Vision cameras: the floor tier, which needs no vendor SDK.

**Libraries.** [FFmpeg](https://ffmpeg.org), through [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause), which bundles an FFmpeg binary under FFmpeg's own license, encodes every video. [NumPy](https://numpy.org), [OpenCV](https://opencv.org), [FastAPI](https://fastapi.tiangolo.com) and [Uvicorn](https://github.com/encode/uvicorn) (the web GUI), [pydantic](https://docs.pydantic.dev) (the config), [Typer](https://typer.tiangolo.com) and [Rich](https://github.com/Textualize/rich) (the command line), and [pySerial](https://github.com/pyserial/pyserial) (the Arduino plugins) carry the rest. The web GUI is octacam's own code and vendors no third-party files.

--8<-- "README.md:ai"
