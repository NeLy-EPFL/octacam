// 2-Photon tab: Arduino trigger status and arm-with-recording. Served from
// /plugins/twophoton/, so core helpers come in the ctx or by absolute /js/ path.
import { fetchSerialPorts, populatePortSelect } from "/js/serial.js";
import { FirmwareFlash } from "/js/firmware-flash.js";

const STATE_LABELS = {
  idle:      "Idle — waiting for arm command",
  armed:     "Armed — waiting for ThorSync",
  triggered: "Triggered — capture running",
  done:      "Done",
};

export default class TwoPhotonTab {
  constructor({ notify, status, getRecordSettings, api }) {
    this.notify = notify;
    this.api = api;
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

    // Hidden while armed or triggered: a flash would interrupt a capture.
    this.fw = new FirmwareFlash({
      api: this.api,
      notify: this.notify,
      prefix: "twophoton",
      ids: {
        banner: "twophoton-fw-flash",
        msg: "twophoton-fw-flash-msg",
        btn: "twophoton-fw-flash-btn",
        log: "twophoton-fw-flash-log",
      },
      isActive: () => this.arduinoState === "armed" || this.arduinoState === "triggered",
    });
    this.fw.setReady(this.ready);
    this.fw.applyState(status);

    this._loadPorts();
    this._refresh();
    this._renderState();
    this.fw.load();
  }

  async _loadPorts() {
    populatePortSelect(this.portSelect, await fetchSerialPorts(this.api), this.device);
  }

  // -------------------------------------------------- WS / connection state

  setConnected(connected) {
    this.connected = connected;
    this._refresh();
  }

  // A "twophoton_state" WS message.
  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if (msg.device) this.device = msg.device;
    // Every push carries link readiness, so a port that dies mid-session
    // disables arming instead of arming a dead link.
    if (typeof msg.ready === "boolean") {
      this.ready = msg.ready;
      this._refresh();
    }
    // An arm failure means the cameras wait on a trigger that never fires:
    // notify once per distinct error.
    if (msg.error) {
      if (msg.error !== this._lastShownError) {
        this._lastShownError = msg.error;
        this.notify("error", msg.error);
      }
    } else {
      this._lastShownError = null;
    }
    this.fw.applyState(msg);
    this._renderState();
  }

  // A /api/system plugin status (the init's push or a reconnect), whose state
  // field is `arduino_state`.
  applyStatus(info) {
    if (!info) return;
    this.applyState({ ...info, state: info.arduino_state });
  }

  // --------------------------------------------------------- start params

  // {fps, duration_ms} for the recording start, or null when not arming.
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
    // No selection reopens the configured device.
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
    this.fw.applyState(r.data);
    this.fw.load();
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
