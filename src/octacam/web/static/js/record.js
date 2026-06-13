// Record tab: settings inputs, start/stop button state machine, status line.

import { api, clampInput, formatBytes, formatHMS } from "./util.js";

const BUSY_STATES = new Set(["waiting", "recording", "finishing"]);

function trimNum(v) {
  return String(Math.round(v * 1000) / 1000);
}

export class RecordTab {
  constructor({ formats, getArduinoCommand, notify }) {
    this.getArduinoCommand = getArduinoCommand;
    this.notify = notify;
    this.settings = null;
    this.state = "idle";
    this.remainingMs = null;
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
    this.button = document.getElementById("record-button");
    this.status = document.getElementById("record-status");

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
    this.button.addEventListener("click", () => this._onButton());

    // Local countdown tick between telemetry updates.
    setInterval(() => {
      if (this.state === "recording" && this.remainingMs != null) {
        this.remainingMs = Math.max(0, this.remainingMs - 1000);
        this.renderStatus();
      }
    }, 1000);
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
  }

  applyState(snap) {
    this.state = snap.state;
    if ("remaining_ms" in snap) this.remainingMs = snap.remaining_ms;
    if (typeof snap.disk_free_bytes === "number") {
      this.diskFree.textContent = `${formatBytes(snap.disk_free_bytes)} free`;
    }
    if (snap.settings) this.applySettings(snap.settings);
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

  renderStatus() {
    let text = "";
    let level = "";
    if (this.state === "waiting") {
      text = "Waiting for first trigger...";
    } else if (this.state === "recording") {
      text =
        this.remainingMs != null
          ? `Remaining time: ${formatHMS(this.remainingMs)}`
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
    const arduinoCommand = this.getArduinoCommand();
    if (arduinoCommand) body.plugin_params = { arduino: arduinoCommand };
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
