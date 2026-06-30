Perfect! Now I have comprehensive coverage. Let me compile the complete feature inventory:

---

# COMPLETE FEATURE INVENTORY FOR OCTACAM REPLACEMENT

## 1. GUI CONTROLS AND FEATURES

### Main Window Layout
- MDI (Multiple Document Interface) area for camera subwindows
- Dockable right panel with configurable width (dock_min_width: 200, dock_max_width: 300 pixels)
- Tabbed interface with Record, Arduino (if serial connected), and View tabs
- Tiling behavior: auto-tiles subwindows if no explicit window geometry in config; otherwise positions/resizes from octacam_config.yaml

### Camera Display (Per-Camera Subwindow)
- Graphics view (QGraphicsView/GraphicsView) with scene
- Pixmap item for frame rendering (from GrabResult.Array, uint8 grayscale)
- Toolbar with right-aligned FPS label ("X.XX fps" updated at display_refresh_interval_ms: 33ms)
- Window title = camera.name (from config or serial number fallback)
- Display transforms:
  - Per-camera scale_x, scale_y from config (applied at startup from octacam_config.yaml)
  - Per-camera rotation_deg from config (0-360 degrees)
  - Window geometry saved as window_x, window_y, window_width, window_height (normalized to mdi_area dimensions)
- Optional lime crosshair overlay (centered, toggleable from View tab, "Display cross" checkbox)

### Record Tab
- **Duration Input**
  - Value field (range: duration_min-duration_max, default 5.0s)
  - Unit selector: s / min / h (default_unit_default_index: 0 = seconds)
  - Returns duration in seconds internally
- **FPS Input** (QDoubleSpinBox)
  - Range: fps_min-fps_max (default 0.01-1000, default 100.0 fps)
  - Decimals: 2
  - Step: 1.0
  - Connected to set_software_trigger_frequency() on value change
- **Save Directory**
  - DirectoryEdit (QPlainTextEdit variant)
  - Default: save_directory_default from config (supports strftime codes: %y%m%d etc.)
  - Auto-expands ~ to home
  - Auto-increments 3-digit suffix (001 -> 002) after each recording via increment()
  - Browse button opens QFileDialog.getExistingDirectory()
  - Normalizes paths to absolute, converts backslash to forward slash
- **Trigger Source** (QComboBox)
  - Options: "software" (PreciseTimer-based), "external" (hardware)
  - Default index: trigger_source_default_index (0)
- **Video Writer** (QComboBox)
  - Options: "opencv MJPG avi", "opencv avc1 mp4"
  - Default index: video_writer_default_index (0)
  - Parsed as: "opencv <FOURCC> <extension>"
  - Supports fourcc: MJPG (MotionJPEG), avc1 (H.264)
  - Extensions: avi, mp4
- **Recording Control Button**
  - States: "Start recording" -> "Stop recording" -> "Abort recording"
  - Disabled during recording
  - All input widgets disabled during recording, re-enabled on stop/abort
- **Status Label**
  - Displays: "Waiting for first trigger...", countdown timer ("Remaining time: HH:MM:SS"), "Recording finished", "Recording aborted", "Recording stopped"
  - Center-aligned, word-wrapped
  - Updated by record_countdown_timer (interval: record_countdown_timer_interval_ms = 1000ms)

### Arduino Tab (conditional, only if SerialLink.is_open)
- **Loop Section**
  - Initial direction: Radio buttons "↺" (CCW) / "↻" (CW), CCW default
  - Steps: SpinBox (range 2-32767, default 4096)
  - Step interval: SpinBox (range 800-65535 microseconds, default 1465 μs)
  - Rest duration: SpinBox (range 0-65535 milliseconds, default 1000 ms)
  - Repeats: SpinBox (range 1-255, default 3)
  - Initial wait: SpinBox (range 0-255 seconds, default 10 s)
  - Step info label: Shows "Total duration: X.XXX s, RPM: X.XXX" (calculated, 4096 = full revolution)
  - Execute button: Sends Command struct to Arduino
  - Start with recording checkbox: Triggers loop execution on first frame capture during recording
- **Adjust Position Section**
  - Single step buttons: "↺" (CCW) / "↻" (CW)
  - press/release semantics: start step_ccw_timer/step_cw_timer on press (using Precise timers with 1ms base), stop on release
  - Interval: SpinBox (range 1-1000 ms, default 1 ms)
  - Sends Command(n_steps=±1) repeatedly via serial

### View Tab
- **Apply to**: Radio buttons "Selected" (active subwindow) / "All" (all cameras)
- **Rotate**:
  - "↺" button: -90° rotation delta
  - "↻" button: +90° rotation delta
  - Applied to pixmap items, refits view to content
- **Flip**:
  - "Horizontal" button: flip x-axis (mirror)
  - "Vertical" button: flip y-axis (mirror)
  - Applied via QTransform with center-point translation
  - Composed with existing transform
- **Reset** button: Clears all transforms (rotation=0, scale=1,1, flip removed)
- **Display Cross** checkbox: Toggles lime crosshair in GraphicsView.drawForeground()

## 2. CLI COMMANDS AND OPTIONS

### `octacam gui [CONFIG_DIR] [--serial-port PORT]`
- Default CONFIG_DIR: current directory (.)
- Default serial port: /dev/ttyACM0
- Launches Qt application with MainWindow

### `octacam record [CONFIG_DIR] [OPTIONS]`
- Default CONFIG_DIR: current directory (.)
- `--fps, -f FLOAT`: Recording frame rate (default: config.gui.fps_default)
- `--duration, -d FLOAT`: Duration in seconds (default: config.gui.duration_default)
- `--output, -o PATH`: Save directory (default: config.gui.save_directory_default)
- `--codec {mjpg,h264}`: Video codec (default: mjpg)
  - mjpg -> MJPG, avi
  - h264 -> avc1, mp4
- `--trigger {software,hardware}`: Trigger source (default: software)
  - software: PreciseTimer at specified fps
  - hardware: Uses TriggerSource configured in .pfs files
- Headless (no GUI), prints output video paths to stdout

### `octacam list-cameras`
- Lists detected cameras with format: "ModelName\tSerialNumber"

### Global `--log-level, -l {debug,info,warning,error}`
- Default: info

### `--version`
- Shows octacam version

## 3. SOFTWARE TRIGGER MECHANISM (PreciseTimer)

**PreciseTimer class** (src/octacam/trigger.py)
- Runs callback on dedicated daemon thread
- set_frequency(hz): Sets interval = 1.0 / hz seconds
- start(duration=None): Begins firing; if duration given, stops after it
- stop(): Stops and joins thread
- No catch-up protection: late ticks fire immediately to maintain average rate and frame count
- Uses time.monotonic() for scheduling, time.sleep() for delays

**CameraSystem.start_software_trigger(duration=None)**
- Calls camera.trigger_once() (ExecuteSoftwareTrigger) at scheduled intervals
- In GUI: paused during preview, active during record with duration_s
- In record CLI: active for entire duration

## 4. HARDWARE TRIGGER PATH

**Camera.set_trigger_source(use_software_trigger: bool)**
- If True: TriggerSource = "Software" (ExecuteSoftwareTrigger used)
- If False: TriggerSource = self._original_trigger_source (from .pfs file)
- Original source cached on camera.load_params()
- GenICam exception handling: logs warning, continues

**Camera.enable_frame_trigger()**
- Sets TriggerSelector = "FrameStart", TriggerMode = "On"
- Called in: start_preview() (always) and before headless record (via CameraSystem.enable_frame_trigger())

## 5. SERIAL/ARDUINO INTEGRATION

**SerialLink class** (src/octacam/serial_link.py)
- open(device, baud): serial.Serial(device, baud, timeout=0.1, write_timeout=1.0)
- Default: /dev/ttyACM0, 115200 baud
- write_command(Command): Packs to bytes and writes; catches SerialException, logs warning

**Command struct** (wire format: little-endian `<hHHBB` = 8 bytes)
- n_steps: int16 (positive=CW, negative=CCW, 0=release motor)
- step_interval_us: uint16 (microseconds between half-steps, range 800-65535)
- rest_duration_ms: uint16 (milliseconds between repetitions)
- n_repeats: uint8 (number of loop cycles, 1-255)
- init_wait_duration_s: uint8 (initial wait before starting, 0-255 seconds)

**Arduino Sketch** (arduino/stepper_motor/stepper_motor.ino)
- 4-pin stepper control: IN1, IN2, IN3, IN4 on pins 8-11
- Half-step sequence: 8-state array (0b0001, 0b0011, 0b0010, 0b0110, 0b0100, 0b1100, 0b1000, 0b1001)
- Executes looping commands: n_steps → forward/backward → rest → repeat → release coils
- Timing: busy-wait on micros() for precise step intervals
- Single-step mode: n_steps=0 releases motor; n_steps=±1 per command

## 6. CONFIG FILE FORMATS

### octacam_config.yaml (or octacam_config.yml, checked in order)
- Location: config_dir / octacam_config.yaml
- Structure:
```yaml
gui:
  fps_default: 100.0
  fps_min: 0.01
  fps_max: 1000.0
  duration_default: 5.0
  duration_min: 0.01
  duration_max: 1000000.0
  duration_unit_default_index: 0  # 0=s, 1=min, 2=h
  save_directory_default: /path/or/strftime  # %y%m%d etc. expanded at load time
  trigger_source_default_index: 0  # 0=software, 1=external
  video_writer_default_index: 0  # 0=MJPG, 1=H264
  display_refresh_interval_ms: 33
  record_countdown_timer_interval_ms: 1000
  check_record_started_timer_interval_ms: 100
  dock_min_width: 200
  dock_max_width: 300
  save_dir_edit_height_factor: 4

cameras:
  - serial_number: "40001978"  # scalar string (handles int/date-like YAML)
    name: "camera_LF"
    scale_x: 1.0
    scale_y: 1.0
    rotation_deg: 0.0
    window_x: 0.5  # -1.0 = use tiling
    window_y: 0.25
    window_width: 0.5
    window_height: 0.25
```
- All fields optional; missing use defaults
- save_directory_default: Expands strftime at load time (once)
- cameras not listed in config: ignored (if any specified); auto-enumerated if list empty

### .pfs Files (Basler GenICam Persistence)
- Format: Key\tValue pairs (tab-separated)
- Location: config_dir / {serial_number}.pfs (one per camera)
- Loaded via pylon.FeaturePersistence.LoadFromString() to camera node map
- Contains: ExposureAuto, GainAuto, Width, Height, OffsetX, OffsetY, PixelFormat, TriggerSource, etc.
- Processing: _drop_empty_pfs_values() removes entries with empty values (compatibility fix)
- Must set TriggerMode=On and TriggerSource (Software or hardware value) before recording

## 7. OUTPUT FILES PRODUCED

Per camera during recording (in output directory):

- **Video file**: {camera.name}.{extension}
  - extension: "avi" (MJPG codec) or "mp4" (avc1 codec)
  - Created by AsyncVideoWriter (cv2.VideoWriter on background thread)
  - Frame dropping possible if queue full (bounded to WRITER_QUEUE_SIZE=20)

- **Timestamp CSV**: {camera.name}.csv
  - Header: frame_index,timestamp,dropped
  - Per row: frame_number, timestamp_ns (from grab_result.TimeStamp or time.time_ns()), dropped_flag
  - Captures: frame timing for post-hoc analysis
  - Written after record loop exits

## 8. LIFECYCLE AND STATE MACHINE

### States (Camera)
- **Idle** (initial): camera open, no grabbing
- **Preview**:
  - Triggered by camera_system.start_preview()
  - TriggerMode=On, TriggerSource="Software"
  - Grab strategy: GrabStrategy_LatestImageOnly (skip buffered frames)
  - Thread: _preview_loop, continuous soft-trigger + frame retrieval
  - Frame handoff: LatestFrame single-slot (producer blocks if consumer hasn't consumed)
  - Timestamps recorded but not used in preview
- **Record**:
  - Triggered by camera_system.start_record(save_dir, fps, fourcc, extension)
  - Grab strategy: GrabStrategy_OneByOne (preserve all frames)
  - Thread: _record_loop, continuous frame grab + write to AsyncVideoWriter
  - Frames: copied to queue (or dropped if full), written to video on background thread
  - Timestamps: all stored for CSV
  - _started flag: set to True on first frame
  - Exits when stop_flag set or camera stops grabbing
  - CSV written on exit

### States (MainWindow / Recording)
- **Idle** (initial): preview running, all buttons/inputs enabled
  - record_button text: "Start recording"
  - FPS controlled by fps_edit value (synced to software trigger frequency)

- **Recording**:
  - Triggered by _on_record_button_clicked() -> _start_record()
  - record_button text: "Stop recording"
  - record_button and all input_widgets disabled
  - Software trigger running with duration = duration_input.get_duration()
  - check_record_started_timer: polls all_cameras_started, triggers Arduino execute if checkbox set
  - record_countdown_timer: counts down, updates status label
  - Stops when:
    - Duration elapsed -> _stop_record() (auto)
    - User clicks "Stop recording" -> _stop_record() (manual)
    - User clicks "Abort recording" (shown if not started) -> _stop_record() (manual)

- **Stopping**:
  - Calls camera_system.stop_software_trigger(), camera_system.start_preview()
  - Saves next directory (save_dir_edit.increment())
  - Re-enables inputs and record_button
  - Button text -> "Start recording"
  - Status shows final message

### Trigger Timing

**Preview Preview Mode** (continuous software trigger):
- PreciseTimer fires at fps_default (100 fps default)
- No duration limit
- Calls camera.trigger_once() for all cameras

**Recording Software Mode**:
- PreciseTimer fires at fps_edit value for duration_input duration
- Calls camera.trigger_once() for all cameras
- Frame capture driven by trigger, not frame availability

**Recording Hardware Mode**:
- Cameras trigger on external (hardware) signal
- PreciseTimer not used
- Software must not trigger; TriggerSource set to original value from .pfs

## 9. PERSISTENCE AND WINDOW GEOMETRY

**Window Positions** (from octacam_config.yaml):
- Per camera: window_x, window_y, window_width, window_height (all -1.0 = default/tiling)
- Applied in _setup_ui() and resizeEvent()
- Normalized: values interpreted as fractions of mdi_area dimensions
- No save-on-exit (read-only from config)

**View Transforms** (per-camera, volatile):
- Applied to pixmap items during runtime
- Reset on each View tab action or "Reset" button
- Not persisted across application restart

**Save Directory State**:
- Incremented after each recording (auto-appends session number)
- Path persisted across restarts in save_dir_edit (in-memory, not config file)

---

## SUMMARY OF DROPPED/MISSING FEATURES (from C++ to Python)

Reviewed cpp/src/main_window.hpp line 253:
- `QDoubleSpinBox *step_degrees_edit;` declared but not implemented in .cpp
- Appears to be unused/incomplete feature in C++ version as well

No significant features appear to have been intentionally dropped. Python port is faithful to C++ original.