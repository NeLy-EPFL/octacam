# octacam

Synchronized video from many scientific cameras: octacam previews and records Basler, FLIR, and any GenICam USB3-Vision camera from a live web GUI, and turns a day's recordings into archived videos with one command. It picks the best available driver for each camera (the [backend cascade](guide/backends.md)) and is the successor to SeptaCam.

<p align="center">
  <img src="https://github.com/user-attachments/assets/a7b6ac6e-5ae3-45fa-ae5a-2e3f5281e5c3" width="560"/>
</p>

## Install

```bash
git clone https://github.com/NeLy-EPFL/octacam && cd octacam
uv tool install .
```

Basler and other USB3-Vision cameras work out of the box; FLIR cameras also need the Spinnaker SDK ([Installation](installation.md)).

## Quickstart

```bash
PYLON_CAMEMU=8 octacam gui configs/emulate_basler   # 8 emulated cameras, no hardware
octacam doctor <config_dir>                         # check the install and a rig
octacam gui <config_dir>                            # preview and record in the browser
octacam process --all                               # transcode, grid videos, copy to storage
```

`octacam record <config_dir>` records without the browser, for scripted or remote runs. The [Quickstart](quickstart.md) walks through a first recording.

## Where to next

| Page | |
| --- | --- |
| [Web GUI](guide/gui.md) | Preview, record, and remote operation over SSH |
| [Recording](guide/recording.md) | What a recording holds: videos, summary, timestamps |
| [Processing](guide/processing.md) | Transcode, grid videos, and transfer to storage |
| [Camera backends](guide/backends.md) | The auto-detect cascade (Basler, FLIR, Spinnaker, pycameleon) |
| [Plugins](guide/plugins.md) | Flywheel turntable, two-photon trigger, and the triggerbox |
| [Configuration](guide/configuration.md) | The `octacam_config.toml` reference |
| [CLI reference](cli.md) | Every command and option |
| [Troubleshooting](reference/troubleshooting.md) | Common errors and fixes |
