// 2-Photon tab: Arduino trigger status and arm-with-recording. While the next
// take will be armed it also offers the Record tab a "Synced with ThorImage"
// checkbox (the take ends when ThorImage stops) in place of Duration. Whether
// ThorSync is set to record long enough is in the Record tab's Ready? strip.
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
      FPS is taken from the Record tab. When "Arm with recording" is checked,
      the Arduino is armed each time a recording starts, then waits for the
      ThorSync rising edge before emitting camera trigger pulses. Synced with
      ThorImage (Record tab), the take ends when ThorImage stops.
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
    // Optional in the ctx: the Record tab's Duration-replacing checkbox.
    this._setDurationSync = ctx.setDurationSync;
    this.arduinoState = "idle";
    this.linkError = null; // the server's own reason the port is not open
    this.maxDurationS = null;
    // The config's match_thorimage seeds the choice once; after that it is the
    // operator's (state pushes never overwrite it).
    this.matchThorImage = null;
    this._offered = null; // the last offer sent to the Record tab
    this.stateValue = document.getElementById("twophoton-state-value");
    this.armWithRec = document.getElementById("twophoton-arm-with-recording");
    this.armWithRec.addEventListener("change", () => this.refresh());
    this.start(ctx.status);
  }

  // A flash would interrupt a capture.
  boardBusy() {
    return this.arduinoState === "armed" || this.arduinoState === "triggered";
  }

  applyLink(msg) {
    if (msg.arduino_state) this.arduinoState = msg.arduino_state;
    if ("error" in msg) this.linkError = msg.error || null;
    if (typeof msg.max_duration_s === "number") this.maxDurationS = msg.max_duration_s;
    if (this.matchThorImage == null && typeof msg.match_thorimage === "boolean") {
      this.matchThorImage = msg.match_thorimage;
    }
    super.applyLink(msg);
  }

  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    // An arm failure means the cameras wait on a trigger that never fires.
    this.toastError(msg.error);
    super.applyState(msg);
  }

  // The server's specific reason (which ports are connected, why the open
  // failed) over the generic one.
  linkMessage() {
    const generic = super.linkMessage();
    return generic == null ? null : this.linkError || generic;
  }

  // {fps, duration_ms, match_thorimage} for the recording start, or null when
  // not arming. Synced, the server ignores duration_ms.
  getStartParams() {
    if (!this.ready || !this.armWithRec.checked) return null;
    const s = this._getRecordSettings();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 100));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    return { fps, duration_ms, match_thorimage: this.matchThorImage ?? true };
  }

  // Offer the synced checkbox while the next take will be armed, withdraw it
  // otherwise; re-sent only when the offer changes.
  _offerDurationSync() {
    if (!this._setDurationSync) return;
    const offer = Boolean(this.ready && this.armWithRec.checked);
    const key = `${offer}|${this.matchThorImage}`;
    if (key === this._offered) return;
    this._offered = key;
    if (!offer) {
      this._setDurationSync(null);
      return;
    }
    const cap = this.maxDurationS
      ? ` (safety limit ${Math.round(this.maxDurationS / 60)} min)`
      : "";
    this._setDurationSync({
      label: "Synced with ThorImage:",
      title:
        `End the recording when ThorImage stops acquiring${cap}. ` +
        "Untick to enter a duration.",
      checked: this.matchThorImage ?? true,
      onChange: (checked) => {
        this.matchThorImage = checked;
        this._offerDurationSync(); // the take's longest length changed
      },
    });
  }

  refresh() {
    super.refresh();
    this.armWithRec.disabled = !this.ready || !this.connected;
    this.stateValue.textContent = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    this.stateValue.className = `twophoton-state twophoton-state--${this.arduinoState}`;
    this._offerDurationSync();
  }
}
