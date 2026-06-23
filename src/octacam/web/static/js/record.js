// Record tab: settings inputs, start/stop button state machine, status line.

import { api, clamp, clampInput, formatBytes, formatHMS } from "./util.js";

const BUSY_STATES = new Set(["waiting", "recording", "finishing"]);

function trimNum(v) {
  return String(Math.round(v * 1000) / 1000);
}

export class RecordTab {
  constructor({ formats, getFlywheelCommand, getTwoPhotonParams, notify }) {
    this.getFlywheelCommand = getFlywheelCommand;
    this.getTwoPhotonParams = getTwoPhotonParams;
    this.notify = notify;
    this.settings = null;
    this.state = "idle";
    // The countdown is driven by an absolute end time in the client's monotonic
    // clock (performance.now()), not by mutating a remaining-ms counter, so it
    // survives jitter between the local tick and server telemetry. See _anchor.
    this.deadline = null;
    this.totalMs = null;
    // Identity of the recording the deadline belongs to, so a new recording
    // re-anchors fresh even if we never saw the non-recording states between
    // them (coalesced sends, or a reconnect spanning a recording boundary).
    this.recordingId = null;
    // One-shot flag: skip the progress-bar width transition on the first frame
    // of a fresh countdown so the bar never sweeps backward to its start.
    this.barJump = false;
    this.lastEvent = null;
    this.connected = false;
    this.requestPending = false;

    this.fields = document.getElementById("record-fields");
    this.durationValue = document.getElementById("duration-value");
    this.durationUnit = document.getElementById("duration-unit");
    this.fpsInput = document.getElementById("fps");
    this.saveDir = document.getElementById("save-dir");
    this.diskFree = document.getElementById("disk-free");
    this.trigger = document.getElementById("trigger-source");
    this.format = document.getElementById("format");
    this.recordForm = document.getElementById("record-form");
    this.saveFrameTimestamps = document.getElementById("save-frame-timestamps");
    this.button = document.getElementById("record-button");
    this.status = document.getElementById("record-status");
    this.progress = document.getElementById("record-progress");
    this.progressBar = document.getElementById("record-progress-bar");

    for (const f of formats) {
      const opt = document.createElement("option");
      opt.value = f.codec;
      opt.textContent = f.label;
      this.format.appendChild(opt);
    }

    this.durationValue.addEventListener("change", () => this._commitDuration());
    this.durationUnit.addEventListener("change", () => this._commitDuration());
    this.fpsInput.addEventListener("change", () =>
      this._put({ fps: clampInput(this.fpsInput) }, [this.fpsInput])
    );
    this.saveDir.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this.saveDir.blur(); // triggers change
      }
    });
    this.saveDir.addEventListener("change", () => this._commitSaveDir());
    this.trigger.addEventListener("change", () =>
      this._put({ trigger_source: this.trigger.value }, [this.trigger])
    );
    this.format.addEventListener("change", () =>
      this._put({ codec: this.format.value }, [this.format])
    );
    this.recordForm.addEventListener("change", () =>
      this._put({ record_form: this.recordForm.value }, [this.recordForm])
    );
    this.saveFrameTimestamps.addEventListener("change", () =>
      this._put(
        { save_frame_timestamps: this.saveFrameTimestamps.checked },
        [this.saveFrameTimestamps]
      )
    );
    this.button.addEventListener("click", () => this._onButton());

    // Re-render between telemetry updates so the countdown and progress bar
    // advance smoothly. Both are derived from `this.deadline`, so this only
    // reads the clock - it never mutates the remaining time, which is what made
    // the old per-second decrement race with telemetry and tick back up.
    setInterval(() => {
      if (this.state === "recording" && this.deadline != null) {
        this.renderStatus();
      }
    }, 250);
  }

  // ------------------------------------------------------ server -> UI

  // Never overwrite an input the user is editing, unless it is listed in
  // `force` (the input that originated the change).
  applySettings(s, force = []) {
    this.settings = s;
    const canSet = (el) => force.includes(el) || document.activeElement !== el;
    if (typeof s.fps === "number" && canSet(this.fpsInput)) {
      this.fpsInput.value = trimNum(s.fps);
    }
    if (
      typeof s.duration_s === "number" &&
      canSet(this.durationValue) &&
      canSet(this.durationUnit)
    ) {
      const factor = Number(this.durationUnit.value) || 1;
      this.durationValue.value = trimNum(s.duration_s / factor);
    }
    if (typeof s.save_dir === "string" && canSet(this.saveDir)) {
      this.saveDir.value = s.save_dir;
    }
    if (s.trigger_source && canSet(this.trigger)) {
      this.trigger.value = s.trigger_source;
    }
    if (s.codec && canSet(this.format)) {
      this.format.value = s.codec;
    }
    if (s.record_form && canSet(this.recordForm)) {
      this.recordForm.value = s.record_form;
    }
    if (
      typeof s.save_frame_timestamps === "boolean" &&
      canSet(this.saveFrameTimestamps)
    ) {
      this.saveFrameTimestamps.checked = s.save_frame_timestamps;
    }
  }

  applyState(snap) {
    this.state = snap.state;
    // Settings first: _syncCountdown reads duration_s to size the progress bar.
    if (snap.settings) this.applySettings(snap.settings);
    this._syncCountdown(snap);
    if (typeof snap.disk_free_bytes === "number") {
      this.diskFree.textContent = `${formatBytes(snap.disk_free_bytes)} free`;
    }
    this.updateControls();
    this.renderStatus();
  }

  handleEvent(evt) {
    this.lastEvent = evt;
    this.renderStatus();
  }

  setConnected(connected) {
    this.connected = connected;
    this.updateControls();
  }

  // ------------------------------------------------------------ render

  updateControls() {
    this.fields.disabled = BUSY_STATES.has(this.state) || !this.connected;
    const btn = this.button;
    btn.classList.remove("start", "stop");
    if (this.state === "waiting") {
      btn.textContent = "Abort recording";
      btn.classList.add("stop");
    } else if (this.state === "recording") {
      btn.textContent = "Stop recording";
      btn.classList.add("stop");
    } else if (this.state === "finishing") {
      btn.textContent = "Finishing…";
    } else {
      btn.textContent = "Start recording";
      btn.classList.add("start");
    }
    btn.disabled =
      !this.connected || this.state === "finishing" || this.requestPending;
  }

  // Remaining time derived from the deadline, clamped at zero. Returns null
  // when no recording is counting down.
  _remainingMs() {
    if (this.deadline == null) return null;
    return Math.max(0, this.deadline - performance.now());
  }

  _syncCountdown(snap) {
    if (snap.state !== "recording" || snap.remaining_ms == null) {
      this.deadline = null;
      this.totalMs = null;
      this.recordingId = null;
      return;
    }
    // A different recording must not inherit the previous trial's (now stale,
    // possibly already-elapsed) deadline, which Math.min would latch onto and
    // pin the countdown at 0:00. Drop the anchor so _anchor starts fresh.
    if (snap.recording_id !== this.recordingId) {
      this.recordingId = snap.recording_id;
      this.deadline = null;
      this.totalMs = null;
    }
    this._anchor(snap.remaining_ms);
  }

  // Convert a server-reported remaining time into an absolute deadline. The
  // deadline for a recording is fixed, so once anchored we only ever pull it
  // *earlier* (Math.min): jitter between the local tick and telemetry samples
  // can no longer make the displayed countdown tick back up.
  _anchor(ms) {
    const target = performance.now() + ms;
    if (this.deadline == null) {
      // Fresh recording, or a connect partway through one. Adopt the value as
      // the total; duration_s covers the mid-recording case, where `ms` is only
      // the leftover and would otherwise under-size the progress bar.
      this.deadline = target;
      this.totalMs = Math.max(ms, (this.settings?.duration_s ?? 0) * 1000);
      this.barJump = true;
    } else {
      this.deadline = Math.min(this.deadline, target);
    }
  }

  renderStatus() {
    const remaining = this._remainingMs();
    let text = "";
    let level = "";
    if (this.state === "waiting") {
      text = "Waiting for first trigger...";
    } else if (this.state === "recording") {
      text =
        remaining != null
          ? `Remaining time: ${formatHMS(remaining)}`
          : "Recording...";
    } else if (this.state === "finishing") {
      text = "Finishing…";
    } else if (this.lastEvent) {
      text = this.lastEvent.message;
      level = this.lastEvent.level;
    }
    this.status.textContent = text;
    this.status.className =
      level === "error" ? "error" : level === "warning" ? "warning" : "";
    this._renderProgress(remaining);
  }

  _renderProgress(remaining) {
    const prog = this.progress;
    const bar = this.progressBar;
    if (this.state === "recording" && this.deadline != null) {
      const frac =
        this.totalMs > 0 ? clamp(1 - remaining / this.totalMs, 0, 1) : 1;
      // Entering determinate from waiting/hidden, or starting a fresh
      // countdown, would otherwise animate the bar *backward* from the 35%
      // indeterminate sweep (or a stale finished width) down to its start.
      const jump =
        this.barJump ||
        prog.classList.contains("hidden") ||
        prog.classList.contains("indeterminate");
      this.barJump = false;
      prog.classList.remove("hidden", "indeterminate");
      const width = `${(frac * 100).toFixed(1)}%`;
      if (jump) {
        bar.style.transition = "none";
        bar.style.width = width;
        void bar.offsetWidth; // commit the jump before re-enabling the glide
        bar.style.transition = "";
      } else {
        bar.style.width = width;
      }
    } else if (this.state === "waiting") {
      // Armed but no deadline yet: show an indeterminate sweep rather than a
      // bar stuck at zero. Clear the inline width so the CSS rule drives it.
      bar.style.width = "";
      prog.classList.remove("hidden");
      prog.classList.add("indeterminate");
    } else if (this.state === "finishing") {
      prog.classList.remove("hidden", "indeterminate");
      bar.style.width = "100%";
    } else {
      bar.style.width = "";
      prog.classList.add("hidden");
      prog.classList.remove("indeterminate");
    }
  }

  // ------------------------------------------------------ UI -> server

  _commitDuration() {
    const v = clampInput(this.durationValue);
    const factor = Number(this.durationUnit.value) || 1;
    this._put({ duration_s: v * factor }, [
      this.durationValue,
      this.durationUnit,
    ]);
  }

  // Current save-dir text (possibly uncommitted), so the directory picker can
  // open near wherever the operator is pointing.
  getSaveDir() {
    return this.saveDir.value;
  }

  // Adopt a path chosen in the directory picker and commit it like a manual
  // edit (PUT + revalidate, updating the disk-free readout).
  setSaveDir(path) {
    this.saveDir.value = path;
    this._commitSaveDir();
  }

  async _commitSaveDir() {
    const path = this.saveDir.value.trim();
    if (this.settings && path === this.settings.save_dir) return;
    const ok = await this._put({ save_dir: path }, [this.saveDir]);
    if (ok) this._validateSaveDir();
  }

  async _validateSaveDir() {
    try {
      const r = await api("POST", "/api/save-dir/validate", {
        path: this.settings.save_dir,
      });
      if (!r.ok || !r.data) return;
      this.diskFree.textContent = `${formatBytes(r.data.free_bytes)} free`;
      if (!r.data.exists && !r.data.creatable) {
        this.notify(
          "warning",
          `Save directory cannot be created: ${r.data.resolved}`
        );
      }
    } catch {
      // non-fatal; telemetry keeps disk free up to date
    }
  }

  async _put(partial, force = []) {
    let r;
    try {
      r = await api("PUT", "/api/settings", partial);
    } catch {
      this.notify("error", "Settings update failed: server unreachable");
      return false;
    }
    if (r.ok && r.data) {
      this.applySettings(r.data, force);
      return true;
    }
    this.notify(
      "error",
      r.data?.detail || `Settings update failed (HTTP ${r.status})`
    );
    if (this.settings) this.applySettings(this.settings, force); // revert
    return false;
  }

  async _onButton() {
    if (this.requestPending) return;
    this.requestPending = true;
    this.updateControls();
    try {
      if (this.state === "waiting") {
        await api("POST", "/api/recording/abort");
      } else if (this.state === "recording") {
        await api("POST", "/api/recording/stop");
      } else {
        await this._start();
      }
    } catch {
      this.notify("error", "Request failed: server unreachable");
    } finally {
      this.requestPending = false;
      this.updateControls();
    }
  }

  async _start() {
    const body = { confirm_overwrite: false };
    const flywheelCommand = this.getFlywheelCommand();
    const tpParams = this.getTwoPhotonParams?.();
    if (flywheelCommand || tpParams) {
      body.plugin_params = {};
      if (flywheelCommand) body.plugin_params.flywheel = flywheelCommand;
      if (tpParams) body.plugin_params.twophoton = tpParams;
    }
    let r = await api("POST", "/api/recording/start", body);
    if (r.status === 409 && r.data?.status === "needs_confirm") {
      if (!window.confirm(r.data.message)) return;
      r = await api("POST", "/api/recording/start", {
        ...body,
        confirm_overwrite: true,
      });
    }
    if (!r.ok) {
      this.notify(
        "error",
        r.data?.message || `Recording start failed (HTTP ${r.status})`
      );
    }
  }
}
