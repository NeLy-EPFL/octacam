// Record tab: settings inputs, start/stop button state machine, status line.

import { api, clamp, clampInput, formatBytes, formatHMS, store } from "./util.js";

const BUSY_STATES = new Set(["waiting", "recording", "finishing"]);

// The Advanced-options switch is remembered per browser; off by default.
const ADV_KEY = "octacam.record.advanced";

function trimNum(v) {
  return String(Math.round(v * 1000) / 1000);
}

export class RecordTab {
  constructor({ formats, getPluginParams, notify }) {
    this.getPluginParams = getPluginParams; // () => {name: start-params slice}
    this.notify = notify;
    this.settings = null;
    this.state = "idle";
    // The countdown's end in performance.now() time (see _anchor).
    this.deadline = null;
    this.totalMs = null;
    // The recording the deadline belongs to: a new one re-anchors even if the
    // states between them were never seen (coalesced sends, a reconnect).
    this.recordingId = null;
    // Skip the bar's width transition once, so a fresh countdown never sweeps
    // backward to its start.
    this.barJump = false;
    this.lastEvent = null;
    this.connected = false;
    this.requestPending = false;

    this.fields = document.getElementById("record-fields");
    this.durationValue = document.getElementById("duration-value");
    this.durationUnit = document.getElementById("duration-unit");
    this.fpsInput = document.getElementById("fps");
    // The server composes save_dir from these two halves.
    this.recordDir = document.getElementById("record-dir");
    this.relativeDir = document.getElementById("relative-dir");
    this.diskFree = document.getElementById("disk-free");
    this.advancedToggle = document.getElementById("record-advanced-toggle");
    this.advancedSection = document.getElementById("record-advanced");
    this.trigger = document.getElementById("trigger-source");
    this.previewTrigger = document.getElementById("preview-trigger-source");
    this.format = document.getElementById("format");
    this.ffmpegParams = document.getElementById("ffmpeg-params");
    // One encoder-params box per save method (see _syncSaveMethodFields).
    this.ffmpegParamsRow = document.getElementById("ffmpeg-params-row");
    this.nvencParams = document.getElementById("nvenc-params");
    this.nvencParamsRow = document.getElementById("nvenc-params-row");
    this.nvencSessionsRow = document.getElementById("nvenc-sessions-row");
    this.nvencAuto = document.getElementById("nvenc-auto");
    this.maxNvencSessions = document.getElementById("max-nvenc-sessions");
    this.nvencDetected = document.getElementById("nvenc-detected");
    this._nvencCaps = null; // see _ensureNvencCaps
    this._nvencCapsPending = false;
    this.recordForm = document.getElementById("record-form");
    this.saveFrameTimestamps = document.getElementById("save-frame-timestamps");
    this.writerQueueSize = document.getElementById("writer-queue-size");
    // Process section: baked into each recording's config snapshot for
    // `octacam process`.
    this.transcodeFfmpegParams = document.getElementById(
      "transcode-ffmpeg-params"
    );
    this.transferDir = document.getElementById("transfer-dir");
    this.transferChecksum = document.getElementById("transfer-checksum");
    this.button = document.getElementById("record-button");
    this.writerAlert = document.getElementById("record-writer-alert");
    this.status = document.getElementById("record-status");
    this.progress = document.getElementById("record-progress");
    this.progressBar = document.getElementById("record-progress-bar");

    for (const f of formats) {
      const opt = document.createElement("option");
      opt.value = f.save_method;
      opt.textContent = f.save_method;
      this.format.appendChild(opt);
    }

    this.durationValue.addEventListener("change", () => this._commitDuration());
    this.durationUnit.addEventListener("change", () => this._reexpressDuration());
    this.fpsInput.addEventListener("change", () =>
      this._put({ fps: clampInput(this.fpsInput) }, [this.fpsInput])
    );
    this.recordDir.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this.recordDir.blur(); // triggers change
      }
    });
    this.recordDir.addEventListener("change", () => this._commitRecordDir());
    this.relativeDir.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this.relativeDir.blur(); // triggers change
      }
    });
    this.relativeDir.addEventListener("change", () => this._commitRelativeDir());
    this.trigger.addEventListener("change", () =>
      this._put({ trigger_source: this.trigger.value }, [this.trigger])
    );
    this.previewTrigger.addEventListener("change", () =>
      this._put(
        { preview_trigger_source: this.previewTrigger.value },
        [this.previewTrigger]
      )
    );
    this.format.addEventListener("change", () => {
      this._put({ save_method: this.format.value }, [this.format]);
      this._syncSaveMethodFields();
    });
    this.ffmpegParams.addEventListener("change", () =>
      this._put({ ffmpeg_params: this.ffmpegParams.value }, [this.ffmpegParams])
    );
    this.nvencParams.addEventListener("change", () =>
      this._put({ nvenc_params: this.nvencParams.value }, [this.nvencParams])
    );
    // Auto-detect sends null (the server probes the GPU cap); turning it off
    // commits the box as an explicit cap, seeded from the detected cap when
    // blank, never 0 (which would put every camera on the CPU).
    this.nvencAuto.addEventListener("change", () => {
      const auto = this.nvencAuto.checked;
      this.maxNvencSessions.disabled = auto;
      if (auto) {
        this._put({ max_nvenc_sessions: null }, [this.nvencAuto]);
        return;
      }
      const v =
        this.maxNvencSessions.value.trim() === ""
          ? (this._nvencCaps?.max_sessions ?? 1)
          : Math.round(clampInput(this.maxNvencSessions));
      this.maxNvencSessions.value = String(v);
      this._put(
        { max_nvenc_sessions: v },
        [this.nvencAuto, this.maxNvencSessions]
      );
    });
    this.maxNvencSessions.addEventListener("change", () => {
      const v = Math.round(clampInput(this.maxNvencSessions));
      this.maxNvencSessions.value = String(v);
      this._put({ max_nvenc_sessions: v }, [this.maxNvencSessions]);
    });
    this.recordForm.addEventListener("change", () =>
      this._put({ record_form: this.recordForm.value }, [this.recordForm])
    );
    this.saveFrameTimestamps.addEventListener("change", () =>
      this._put(
        { save_frame_timestamps: this.saveFrameTimestamps.checked },
        [this.saveFrameTimestamps]
      )
    );
    // The server rejects a non-integer writer_queue_size (422).
    this.writerQueueSize.addEventListener("change", () => {
      const v = Math.round(clampInput(this.writerQueueSize));
      this.writerQueueSize.value = String(v);
      this._put({ writer_queue_size: v }, [this.writerQueueSize]);
    });
    this.transcodeFfmpegParams.addEventListener("change", () =>
      this._put(
        { transcode_ffmpeg_params: this.transcodeFfmpegParams.value },
        [this.transcodeFfmpegParams]
      )
    );
    this.transferDir.addEventListener("change", () =>
      this._put(
        { transfer_directory: this.transferDir.value.trim() },
        [this.transferDir]
      )
    );
    this.transferChecksum.addEventListener("change", () =>
      this._put(
        { transfer_checksum: this.transferChecksum.checked },
        [this.transferChecksum]
      )
    );
    this.button.addEventListener("click", () => this._onButton());
    this._initAdvancedToggle();

    // Re-render between telemetry updates; this only reads `this.deadline`.
    setInterval(() => {
      if (this.state === "recording" && this.deadline != null) {
        this.renderStatus();
      }
    }, 250);
  }

  // The rows inside keep their own `hidden` state (_syncSaveMethodFields).
  _initAdvancedToggle() {
    const open = store.get(ADV_KEY) === "1";
    this.advancedToggle.checked = open;
    this.advancedSection.hidden = !open;
    this.advancedToggle.addEventListener("change", () => {
      const shown = this.advancedToggle.checked;
      this.advancedSection.hidden = !shown;
      store.set(ADV_KEY, shown ? "1" : "0");
    });
  }

  // ------------------------------------------------------ server -> UI

  // "managed" needs a loaded trigger-generating plugin (the server says).
  setManagedAvailable(available) {
    this._managedAvailable = available;
    if (available) this._enableManagedOption();
  }

  _enableManagedOption() {
    const opt = this.trigger?.querySelector('option[value="managed"]');
    if (opt) opt.disabled = false;
  }

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
    if (typeof s.record_directory === "string" && canSet(this.recordDir)) {
      this.recordDir.value = s.record_directory;
    }
    if (typeof s.relative_directory === "string" && canSet(this.relativeDir)) {
      this.relativeDir.value = s.relative_directory;
    }
    if (s.trigger_source && canSet(this.trigger)) {
      // The server may promote external to "managed"; the option must be enabled.
      if (s.trigger_source === "managed") this._enableManagedOption();
      this.trigger.value = s.trigger_source;
    }
    if (s.preview_trigger_source && canSet(this.previewTrigger)) {
      this.previewTrigger.value = s.preview_trigger_source;
    }
    if (s.save_method && canSet(this.format)) {
      this.format.value = s.save_method;
    }
    if (typeof s.ffmpeg_params === "string" && canSet(this.ffmpegParams)) {
      this.ffmpegParams.value = s.ffmpeg_params;
    }
    if (typeof s.nvenc_params === "string" && canSet(this.nvencParams)) {
      this.nvencParams.value = s.nvenc_params;
    }
    // max_nvenc_sessions is null when auto-detecting the GPU cap, else an int cap.
    if (
      "max_nvenc_sessions" in s &&
      canSet(this.nvencAuto) &&
      canSet(this.maxNvencSessions)
    ) {
      const auto = s.max_nvenc_sessions == null;
      this.nvencAuto.checked = auto;
      this.maxNvencSessions.disabled = auto;
      if (!auto) this.maxNvencSessions.value = trimNum(s.max_nvenc_sessions);
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
    if (
      typeof s.writer_queue_size === "number" &&
      canSet(this.writerQueueSize)
    ) {
      this.writerQueueSize.value = trimNum(s.writer_queue_size);
    }
    if (
      typeof s.transcode_ffmpeg_params === "string" &&
      canSet(this.transcodeFfmpegParams)
    ) {
      this.transcodeFfmpegParams.value = s.transcode_ffmpeg_params;
    }
    if (typeof s.transfer_directory === "string" && canSet(this.transferDir)) {
      this.transferDir.value = s.transfer_directory;
    }
    if (
      typeof s.transfer_checksum === "boolean" &&
      canSet(this.transferChecksum)
    ) {
      this.transferChecksum.checked = s.transfer_checksum;
    }
    this._syncSaveMethodFields();
  }

  // Show the selected method's encoder params (none for "raw"); each method
  // keeps its own field, so switching never clobbers the other preset.
  _syncSaveMethodFields() {
    const method = this.format.value;
    this.ffmpegParamsRow.hidden = method !== "ffmpeg";
    this.nvencParamsRow.hidden = method !== "nvenc";
    this.nvencSessionsRow.hidden = method !== "nvenc";
    if (method === "nvenc") this._ensureNvencCaps();
  }

  // Fetch the detected NVENC session cap once, only when nvenc is selected
  // (the server probe briefly loads the GPU). Best-effort.
  async _ensureNvencCaps() {
    if (this._nvencCaps) {
      this._applyNvencCaps();
      return;
    }
    // applySettings runs on every telemetry tick: one request in flight at most.
    if (this._nvencCapsPending) return;
    this._nvencCapsPending = true;
    let r;
    try {
      r = await api("GET", "/api/nvenc/capabilities");
    } catch {
      return;
    } finally {
      this._nvencCapsPending = false;
    }
    if (r && r.ok && r.data) {
      this._nvencCaps = r.data;
      this._applyNvencCaps();
    }
  }

  _applyNvencCaps() {
    const caps = this._nvencCaps;
    if (!caps) return;
    if (!caps.available) {
      this.nvencDetected.textContent =
        "GPU NVENC unavailable — every camera will encode on the CPU (libx264).";
      return;
    }
    this.nvencDetected.textContent = `GPU: ${caps.max_sessions} concurrent NVENC session(s) detected.`;
    // Auto-detecting: show the cap that will be used in the disabled box.
    if (this.nvencAuto.checked && document.activeElement !== this.maxNvencSessions) {
      this.maxNvencSessions.value = String(caps.max_sessions);
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

  // A persistent banner naming the cameras whose writer failed (data may be
  // lost); an empty list hides it.
  setWriterFailure(failedNames) {
    const el = this.writerAlert;
    if (!el) return;
    if (failedNames && failedNames.length) {
      el.textContent =
        `⚠ Save failed: ${failedNames.join(", ")} — the recording may be ` +
        "incomplete. See the event log / server log.";
      el.classList.remove("hidden");
    } else {
      el.textContent = "";
      el.classList.add("hidden");
    }
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

  // null when no recording is counting down.
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
    // A new recording must not inherit the last one's elapsed deadline, which
    // Math.min would latch onto at 0:00.
    if (snap.recording_id !== this.recordingId) {
      this.recordingId = snap.recording_id;
      this.deadline = null;
      this.totalMs = null;
    }
    this._anchor(snap.remaining_ms);
  }

  // Turn a server-reported remaining time into an absolute deadline. A
  // recording's deadline is fixed, so once anchored it only moves earlier
  // (Math.min) and telemetry jitter can't make the countdown tick back up.
  _anchor(ms) {
    const target = performance.now() + ms;
    if (this.deadline == null) {
      // duration_s sizes the bar when connecting partway through a recording.
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
      // Jump, don't animate, out of the indeterminate sweep or a stale width.
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
      // No deadline yet: an indeterminate sweep (the CSS rule sets the width).
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

  // A unit change re-expresses the same duration (20 s -> 0.333 min) and sends
  // nothing; only editing the value commits a new duration.
  _reexpressDuration() {
    const factor = Number(this.durationUnit.value) || 1;
    const seconds = this.settings?.duration_s;
    if (typeof seconds === "number") {
      this.durationValue.value = trimNum(seconds / factor);
    }
  }

  // The base-directory text, possibly uncommitted (the picker opens there).
  getRecordDir() {
    return this.recordDir.value;
  }

  // Commit a folder chosen in the directory picker like a manual edit.
  setRecordDir(path) {
    this.recordDir.value = path;
    this._commitRecordDir();
  }

  async _commitRecordDir() {
    const path = this.recordDir.value.trim();
    if (this.settings && path === this.settings.record_directory) return;
    const ok = await this._put({ record_directory: path }, [this.recordDir]);
    if (ok) this._validateRecordDir();
  }

  _commitRelativeDir() {
    const rel = this.relativeDir.value.trim();
    if (this.settings && rel === this.settings.relative_directory) return;
    this._put({ relative_directory: rel }, [this.relativeDir]);
  }

  async _validateRecordDir() {
    try {
      const r = await api("POST", "/api/save-dir/validate", {
        path: this.settings.record_directory,
      });
      if (!r.ok || !r.data) return;
      this.diskFree.textContent = `${formatBytes(r.data.free_bytes)} free`;
      if (!r.data.exists && !r.data.creatable) {
        this.notify(
          "warning",
          `Directory cannot be created: ${r.data.resolved}`
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
    const pluginParams = this.getPluginParams?.() ?? {};
    if (Object.keys(pluginParams).length) body.plugin_params = pluginParams;
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
