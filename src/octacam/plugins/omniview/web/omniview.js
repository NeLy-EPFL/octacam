// omniview tab: Arduino trigger + LED-strobe status and arm-with-recording control.
//
// Served from /plugins/omniview/, so it cannot import core "./util.js" (that would
// 404). The shared fetch helper (api) and clampInput are passed in via the ctx the
// host (app.js) constructs.

const STATE_LABELS = {
  idle:    "Idle — waiting for arm command",
  running: "Running — triggering + strobing",
  done:    "Done",
};

export default class OmniviewTab {
  constructor({ notify, status, getRecordSettings, api, clampInput }) {
    this.notify = notify;
    this.api = api; // shared fetch helper (from util.js, injected by app.js)
    this.clampInput = clampInput;
    this._getRecordSettings = getRecordSettings;
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.arduinoState = status?.arduino_state || "idle";
    this.connected = false;

    this.statusBox    = document.getElementById("omniview-status");
    this.statusMsg    = document.getElementById("omniview-status-msg");
    this.reconnectBtn = document.getElementById("omniview-reconnect");
    this.stateValue   = document.getElementById("omniview-state-value");
    this.dutyInput    = document.getElementById("omniview-duty");
    this.armWithRec   = document.getElementById("omniview-arm-with-recording");

    // Seed the duty input from the plugin's configured default.
    if (this.dutyInput && typeof status?.duty_percent === "number") {
      this.dutyInput.value = status.duty_percent;
    }

    this.reconnectBtn.addEventListener("click", () => this._reconnect());

    this._refresh();
    this._renderState();
  }

  // -------------------------------------------------- WS / connection state

  setConnected(connected) {
    this.connected = connected;
    this._refresh();
  }

  // Called by app.js when an "omniview_state" WS message arrives.
  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if (msg.device) this.device = msg.device;
    // The backend reports link readiness with every state push, so a serial port
    // that dies mid-session disables the arm gate (and shows the reconnect notice)
    // instead of leaving a stale "ready" that would arm a dead link.
    if (typeof msg.ready === "boolean") {
      this.ready = msg.ready;
      this._refresh();
    }
    this._renderState();
  }

  // --------------------------------------------------------- start params

  // Returns {fps, duration_ms, duty_percent, cam_pulse_us} for the recording
  // start request, or null when "arm with recording" is unchecked or the serial
  // port is not open. fps/duration come from the Record tab; duty from this tab.
  getStartParams() {
    if (!this.ready || !this.armWithRec?.checked) return null;
    const s = this._getRecordSettings?.();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 80));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    let duty_percent = parseFloat(this.dutyInput?.value);
    if (!Number.isFinite(duty_percent)) duty_percent = 20;
    duty_percent = Math.max(0, Math.min(100, duty_percent));
    return { fps, duration_ms, duty_percent };
  }

  // --------------------------------------------------------- render

  _refresh() {
    if (this.ready) {
      this.statusBox.classList.add("hidden");
    } else {
      const where = this.device ? ` (${this.device})` : "";
      this.statusMsg.textContent =
        `Serial port${where} is not open — check the Arduino is plugged in ` +
        `and the device path matches the plugin config, then reconnect.`;
      this.statusBox.classList.remove("hidden");
    }
    // Gate the controls on serial being open (state display is always visible).
    const disabled = !this.ready || !this.connected;
    if (this.armWithRec) this.armWithRec.disabled = disabled;
    if (this.dutyInput) this.dutyInput.disabled = disabled;
  }

  _renderState() {
    const label = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    if (this.stateValue) {
      this.stateValue.textContent = label;
      this.stateValue.className = `omniview-state omniview-state--${this.arduinoState}`;
    }
  }

  // --------------------------------------------------------- reconnect

  async _reconnect() {
    this.reconnectBtn.disabled = true;
    let r;
    try {
      r = await this.api("POST", "/api/omniview/reconnect");
    } catch {
      this.reconnectBtn.disabled = false;
      this.notify("error", "Reconnect failed: server unreachable");
      return;
    }
    this.reconnectBtn.disabled = false;
    if (!r.ok) {
      this.notify("error", r.data?.detail || `Reconnect failed (HTTP ${r.status})`);
      return;
    }
    this.ready = Boolean(r.data?.ready);
    if (r.data?.device) this.device = r.data.device;
    if (r.data?.arduino_state) {
      this.arduinoState = r.data.arduino_state;
      this._renderState();
    }
    this._refresh();
    if (this.ready) {
      this.notify("info", `Serial port ${this.device} connected.`);
    } else {
      this.notify(
        "warning",
        r.data?.error
          ? `Serial port still unavailable: ${r.data.error}`
          : "Serial port still unavailable."
      );
    }
  }
}
