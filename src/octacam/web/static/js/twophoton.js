// 2-Photon tab: Arduino hardware trigger status and arm-with-recording control.

import { api } from "./util.js";

const STATE_LABELS = {
  idle:      "Idle — waiting for arm command",
  armed:     "Armed — waiting for ThorSync",
  triggered: "Triggered — capture running",
  done:      "Done",
};

export class TwoPhotonTab {
  constructor({ notify, status, getRecordSettings }) {
    this.notify = notify;
    this._getRecordSettings = getRecordSettings;
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.arduinoState = status?.arduino_state || "idle";
    this.connected = false;

    this.statusBox     = document.getElementById("twophoton-status");
    this.statusMsg     = document.getElementById("twophoton-status-msg");
    this.reconnectBtn  = document.getElementById("twophoton-reconnect");
    this.stateLabel    = document.getElementById("twophoton-state-label");
    this.stateValue    = document.getElementById("twophoton-state-value");
    this.armWithRec    = document.getElementById("twophoton-arm-with-recording");

    this.reconnectBtn.addEventListener("click", () => this._reconnect());

    this._refresh();
    this._renderState();
  }

  // -------------------------------------------------- WS / connection state

  setConnected(connected) {
    this.connected = connected;
    this._refresh();
  }

  // Called by app.js when a "twophoton_state" WS message arrives.
  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if (msg.device) this.device = msg.device;
    this._renderState();
  }

  // --------------------------------------------------------- start params

  // Returns {fps, duration_ms} to include in the recording start request, or
  // null when "arm with recording" is unchecked or the serial port is not open.
  getStartParams() {
    if (!this.ready || !this.armWithRec?.checked) return null;
    const s = this._getRecordSettings?.();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 100));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    return { fps, duration_ms };
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
    // Gate the checkbox on serial being open (state display is always visible).
    if (this.armWithRec) {
      this.armWithRec.disabled = !this.ready || !this.connected;
    }
  }

  _renderState() {
    const label = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    if (this.stateValue) {
      this.stateValue.textContent = label;
      this.stateValue.className = `twophoton-state twophoton-state--${this.arduinoState}`;
    }
  }

  // --------------------------------------------------------- reconnect

  async _reconnect() {
    this.reconnectBtn.disabled = true;
    let r;
    try {
      r = await api("POST", "/api/twophoton/reconnect");
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
