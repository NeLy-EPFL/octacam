// Record tab: settings inputs, start/stop button state machine, status line.

import { api, clamp, clampInput, el, formatBytes, formatHMS, request, store } from "./util.js";

const BUSY_STATES = new Set(["waiting", "recording", "finishing"]);

// The Advanced-options switch is remembered per browser; off by default.
const ADV_KEY = "octacam.record.advanced";

function trimNum(v) {
  return String(Math.round(v * 1000) / 1000);
}

// How each kind of input shows a server value (one of its type) and reads an
// edit.
const KINDS = {
  number: {
    accepts: (v) => typeof v === "number",
    show: (input, v) => (input.value = trimNum(v)),
    read: (input) => clampInput(input),
  },
  // Clamped and rounded, and written back: the server rejects a non-integer.
  int: {
    accepts: (v) => typeof v === "number",
    show: (input, v) => (input.value = trimNum(v)),
    read: (input) => {
      const v = Math.round(clampInput(input));
      input.value = String(v);
      return v;
    },
  },
  text: {
    accepts: (v) => typeof v === "string",
    show: (input, v) => (input.value = v),
    read: (input) => input.value,
  },
  path: {
    accepts: (v) => typeof v === "string",
    show: (input, v) => (input.value = v),
    read: (input) => input.value.trim(),
  },
  select: {
    accepts: (v) => Boolean(v),
    show: (input, v) => (input.value = v),
    read: (input) => input.value,
  },
  checkbox: {
    accepts: (v) => typeof v === "boolean",
    show: (input, v) => (input.checked = v),
    read: (input) => input.checked,
  },
};

// The plain settings: [server key, input id, kind]. A change PUTs that key.
// Duration, the two record-directory halves and the NVENC session cap are
// wired by hand below.
const FIELDS = [
  ["fps", "fps", "number"],
  ["trigger_source", "trigger-source", "select"],
  ["preview_trigger_source", "preview-trigger-source", "select"],
  ["save_method", "format", "select"],
  ["ffmpeg_params", "ffmpeg-params", "text"],
  ["nvenc_params", "nvenc-params", "text"],
  ["record_form", "record-form", "select"],
  ["save_frame_timestamps", "save-frame-timestamps", "checkbox"],
  ["writer_queue_size", "writer-queue-size", "int"],
  // The Process section: baked into each recording's config snapshot for
  // `octacam process`.
  ["transcode_ffmpeg_params", "transcode-ffmpeg-params", "text"],
  ["transfer_directory", "transfer-dir", "path"],
  ["transfer_checksum", "transfer-checksum", "checkbox"],
];

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
    // The server composes save_dir from these two halves.
    this.recordDir = document.getElementById("record-dir");
    this.relativeDir = document.getElementById("relative-dir");
    this.diskFree = document.getElementById("disk-free");
    this.advancedToggle = document.getElementById("record-advanced-toggle");
    this.advancedSection = document.getElementById("record-advanced");
    this.trigger = document.getElementById("trigger-source");
    this.format = document.getElementById("format");
    // One encoder-params box per save method (see _syncSaveMethodFields).
    this.ffmpegParamsRow = document.getElementById("ffmpeg-params-row");
    this.nvencParamsRow = document.getElementById("nvenc-params-row");
    this.nvencSessionsRow = document.getElementById("nvenc-sessions-row");
    this.nvencAuto = document.getElementById("nvenc-auto");
    this.maxNvencSessions = document.getElementById("max-nvenc-sessions");
    this.nvencDetected = document.getElementById("nvenc-detected");
    this._nvencCaps = null; // see _ensureNvencCaps
    this._nvencCapsPending = false;
    this.button = document.getElementById("record-button");
    this.writerAlert = document.getElementById("record-writer-alert");
    this.status = document.getElementById("record-status");
    this.progress = document.getElementById("record-progress");
    this.progressBar = document.getElementById("record-progress-bar");

    for (const f of formats) {
      const opt = el("option", null, f.save_method);
      opt.value = f.save_method;
      this.format.appendChild(opt);
    }

    this._fields = FIELDS.map(([key, id, kind]) => {
      const input = document.getElementById(id);
      input.addEventListener("change", () =>
        this._put({ [key]: KINDS[kind].read(input) }, [input])
      );
      return { key, input, kind: KINDS[kind] };
    });
    this.format.addEventListener("change", () => this._syncSaveMethodFields());

    this.durationValue.addEventListener("change", () => this._commitDuration());
    this.durationUnit.addEventListener("change", () => this._reexpressDuration());
    for (const input of [this.recordDir, this.relativeDir]) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          input.blur(); // triggers change
        }
      });
    }
    this.recordDir.addEventListener("change", () => this._commitRecordDir());
    this.relativeDir.addEventListener("change", () => this._commitRelativeDir());
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
          : KINDS.int.read(this.maxNvencSessions);
      this.maxNvencSessions.value = String(v);
      this._put({ max_nvenc_sessions: v }, [this.nvencAuto, this.maxNvencSessions]);
    });
    this.maxNvencSessions.addEventListener("change", () =>
      this._put(
        { max_nvenc_sessions: KINDS.int.read(this.maxNvencSessions) },
        [this.maxNvencSessions]
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
    if (available) this._enableManagedOption();
  }

  _enableManagedOption() {
    this.trigger.querySelector('option[value="managed"]').disabled = false;
  }

  // Never overwrite an input the user is editing, unless it is listed in
  // `force` (the input that originated the change).
  applySettings(s, force = []) {
    this.settings = s;
    const canSet = (input) => force.includes(input) || document.activeElement !== input;
    // The server may promote external to "managed"; the option must be enabled.
    if (s.trigger_source === "managed") this._enableManagedOption();
    for (const { key, input, kind } of this._fields) {
      if (kind.accepts(s[key]) && canSet(input)) kind.show(input, s[key]);
    }
    if (
      typeof s.duration_s === "number" &&
      canSet(this.durationValue) &&
      canSet(this.durationUnit)
    ) {
      const factor = Number(this.durationUnit.value) || 1;
      this.durationValue.value = trimNum(s.duration_s / factor);
    }
    for (const [key, input] of [
      ["record_directory", this.recordDir],
      ["relative_directory", this.relativeDir],
    ]) {
      if (KINDS.path.accepts(s[key]) && canSet(input)) input.value = s[key];
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
    const caps = await request("GET", "/api/nvenc/capabilities");
    this._nvencCapsPending = false;
    if (caps) {
      this._nvencCaps = caps;
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
    const failed = failedNames.length > 0;
    this.writerAlert.classList.toggle("hidden", !failed);
    this.writerAlert.textContent = failed
      ? `⚠ Save failed: ${failedNames.join(", ")} — the recording may be ` +
        "incomplete. See the event log / server log."
      : "";
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

  // Non-fatal on failure: telemetry keeps disk free up to date.
  async _validateRecordDir() {
    const d = await request("POST", "/api/save-dir/validate", {
      path: this.settings.record_directory,
    });
    if (!d) return;
    this.diskFree.textContent = `${formatBytes(d.free_bytes)} free`;
    if (!d.exists && !d.creatable) {
      this.notify("warning", `Directory cannot be created: ${d.resolved}`);
    }
  }

  async _put(partial, force = []) {
    const s = await request("PUT", "/api/settings", partial, {
      action: "Settings update",
      notify: this.notify,
    });
    if (s) this.applySettings(s, force);
    else if (this.settings) this.applySettings(this.settings, force); // revert
    return Boolean(s);
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
