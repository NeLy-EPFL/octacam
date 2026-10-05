// 2-Photon tab: Arduino trigger status and arm-with-recording.
import { SerialTab } from "/js/serial.js";

const STATE_LABELS = {
  idle:      "Idle — waiting for arm command",
  armed:     "Armed — waiting for ThorSync",
  triggered: "Triggered — capture running",
  done:      "Done",
};

const MARKUP = `
  <div class="col">
    <div class="row" title="Current state of the Arduino trigger board">
      <span>Arduino state:</span>
      <span id="twophoton-state-value" class="twophoton-state twophoton-state--idle"></span>
    </div>
    <p class="hint">
      FPS and duration are taken from the Record tab settings.
      When "Arm with recording" is checked, the Arduino is armed
      automatically each time a recording starts, then waits for the
      ThorSync rising edge before emitting camera trigger pulses.
    </p>
    <label class="center"
      title="Arm the Arduino automatically when a recording starts">
      <input type="checkbox" id="twophoton-arm-with-recording" checked>
      Arm with recording
    </label>
  </div>`;

export default class TwoPhotonTab extends SerialTab {
  static label = "2-Photon";
  static title = "2-photon rig: arm the Arduino hardware trigger (ThorSync)";

  constructor(ctx) {
    super(ctx, MARKUP);
    this._getRecordSettings = ctx.getRecordSettings;
    this.arduinoState = "idle";
    this.stateValue = document.getElementById("twophoton-state-value");
    this.armWithRec = document.getElementById("twophoton-arm-with-recording");
    this.start(ctx.status);
  }

  // A flash would interrupt a capture.
  boardBusy() {
    return this.arduinoState === "armed" || this.arduinoState === "triggered";
  }

  applyLink(msg) {
    if (msg.arduino_state) this.arduinoState = msg.arduino_state;
    super.applyLink(msg);
  }

  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    // An arm failure means the cameras wait on a trigger that never fires.
    this.toastError(msg.error);
    super.applyState(msg);
  }

  // {fps, duration_ms} for the recording start, or null when not arming.
  getStartParams() {
    if (!this.ready || !this.armWithRec.checked) return null;
    const s = this._getRecordSettings();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 100));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    return { fps, duration_ms };
  }

  refresh() {
    super.refresh();
    this.armWithRec.disabled = !this.ready || !this.connected;
    this.stateValue.textContent = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    this.stateValue.className = `twophoton-state twophoton-state--${this.arduinoState}`;
  }
}
