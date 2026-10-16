// 2-Photon tab: Arduino trigger status and arm-with-recording. While the next
// take will be armed it also offers the Record tab a "Synced with ThorImage"
// checkbox (the take ends when ThorImage stops) in place of Duration, with a
// warning when ThorSync would stop before the take can end.
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

// 320 -> "320 s", 3600 -> "60 min", 1000 -> "~16.7 min" (as the server's).
function fmtDuration(s) {
  if (s >= 120 && s % 60 === 0) return `${s / 60} min`;
  if (s < 600) return `${Math.round(s)} s`;
  return `~${(s / 60).toFixed(1)} min`;
}

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
    // ThorSync's duration limit as last read from the share.
    this.thorsync = null;
    this._offered = null; // the last offer sent to the Record tab
    this._lastWarning = null;
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
    if ("thorsync" in msg) this.thorsync = msg.thorsync || null;
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

  // When ThorSync (per the newest SyncData folder) would stop before this take
  // can end: it would miss the rest (timing, AirFlow). A synced take lasts as
  // long as ThorImage's newest plan when known, else up to the safety limit.
  thorsyncWarning(synced = this.matchThorImage ?? true) {
    const t = this.thorsync;
    if (!t || !t.limit_s) return null;
    const planned = synced && t.planned_s ? t.planned_s : null;
    const longest =
      planned ?? (synced ? this.maxDurationS : this._getRecordSettings()?.duration_s);
    if (!longest || t.limit_s >= longest) return null;
    const take = planned
      ? `ThorImage is set to acquire ${fmtDuration(planned)} (read from ${t.planned_folder})`
      : `this take can run up to ${fmtDuration(longest)}`;
    return (
      `ThorSync is set to stop after ${fmtDuration(t.limit_s)} (as of its last ` +
      `recording, ${t.folder}) but ${take} — ThorSync would miss the rest ` +
      "(timing, AirFlow). Raise the duration in ThorSync; this check sees it after " +
      "ThorSync's next recording."
    );
  }

  // Offer the synced checkbox while the next take will be armed, withdraw it
  // otherwise; re-sent only when the offer changes.
  _offerDurationSync() {
    if (!this._setDurationSync) return;
    const offer = Boolean(this.ready && this.armWithRec.checked);
    const warning = offer ? this.thorsyncWarning() : null;
    const key = `${offer}|${this.matchThorImage}|${warning}`;
    if (key === this._offered) return;
    this._offered = key;
    if (warning && warning !== this._lastWarning) this.notify("warning", warning);
    this._lastWarning = warning;
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
      warning,
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
