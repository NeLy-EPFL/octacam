// 2-Photon tab: Arduino hardware trigger status and arm-with-recording control.
//
// Served from /plugins/twophoton/, so it cannot import core "./util.js" (that
// would 404). The shared fetch helper (api) is passed in via the ctx the host
// (app.js) constructs. The serial helpers live at /js/ (absolute path, since a
// relative import would resolve under /plugins/twophoton/ and 404).
import { fetchSerialPorts, populatePortSelect } from "/js/serial.js";

const STATE_LABELS = {
  idle:      "Idle — waiting for arm command",
  armed:     "Armed — waiting for ThorSync",
  triggered: "Triggered — capture running",
  done:      "Done",
};

export default class TwoPhotonTab {
  constructor({ notify, status, getRecordSettings, api }) {
    this.notify = notify;
    this.api = api; // shared fetch helper (from util.js, injected by app.js)
    this._getRecordSettings = getRecordSettings;
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.arduinoState = status?.arduino_state || "idle";
    this.connected = false;

    this.statusBox     = document.getElementById("twophoton-status");
    this.statusMsg     = document.getElementById("twophoton-status-msg");
    this.reconnectBtn  = document.getElementById("twophoton-reconnect");
    this.portSelect    = document.getElementById("twophoton-port");
    this.stateLabel    = document.getElementById("twophoton-state-label");
    this.stateValue    = document.getElementById("twophoton-state-value");
    this.armWithRec    = document.getElementById("twophoton-arm-with-recording");

    this.reconnectBtn.addEventListener("click", () => this._reconnect());

    this._loadPorts();
    this._refresh();
    this._renderState();
  }

  // Populate the port dropdown with the currently detected serial ports,
  // keeping the active device selected.
  async _loadPorts() {
    populatePortSelect(this.portSelect, await fetchSerialPorts(this.api), this.device);
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
    // The backend reports link readiness with every state push, so a serial
    // port that dies mid-session disables the arm gate (and shows the reconnect
    // notice) instead of leaving a stale "ready" that would arm a dead link.
    if (typeof msg.ready === "boolean") {
      this.ready = msg.ready;
      this._refresh();
    }
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
    // Connect to the port picked in the dropdown (device override); with no
    // selection the backend reopens the configured device.
    const device = this.portSelect?.value || "";
    let r;
    try {
      r = await this.api("POST", "/api/twophoton/reconnect", device ? { device } : {});
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
    this._loadPorts(); // refresh the list + selection after the attempt
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
