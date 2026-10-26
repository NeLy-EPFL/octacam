// 2-Photon tab: Arduino trigger status and arm-with-recording. While the next
// take will be armed it also offers the Record tab a "Synced with ThorImage"
// checkbox (the take ends when ThorImage stops) in place of Duration. Whether
// ThorSync is set to record long enough is in the Record tab's Ready? strip.
// ThorImage records ThorSync only with its ThorSync box ticked, so the session's
// first armed take (and the next one after a take ThorSync missed) asks first,
// and a take ThorSync did not record is announced in a popup. "UV light"
// switches the UV LED through the board, at once and in any state; it sits in
// the always-visible strip above the tabs, so its state is never out of sight.
import { el, Modal, request } from "/js/util.js";
import { SerialTab } from "/js/serial.js";

const REMINDER =
  "Is the ThorSync box ticked in ThorImage's Capture panel? Without it " +
  "ThorSync records nothing: no AirFlow, no ThorSync channel, and the take " +
  "can be paired with its 2P data by time only.";

// A two-button popup: ask() resolves true for OK, false for Cancel/Escape.
class ThorSyncDialog extends Modal {
  constructor() {
    const overlay = el("div", "modal hidden");
    const card = el("div", "modal-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    const title = el("h3");
    title.id = "twophoton-dialog-title";
    card.setAttribute("aria-labelledby", title.id);
    const msg = el("p");
    msg.id = "twophoton-dialog-msg";
    const cancel = el("button", "btn");
    cancel.type = "button";
    cancel.id = "twophoton-dialog-cancel";
    const ok = el("button", "btn");
    ok.type = "button";
    ok.id = "twophoton-dialog-ok";
    const actions = el("div", "modal-actions");
    actions.append(cancel, ok);
    card.append(title, msg, actions);
    overlay.append(card);
    overlay.id = "twophoton-dialog";
    document.body.append(overlay);
    super(overlay);
    Object.assign(this, { title, msg, ok, cancel, _resolve: null });
    ok.addEventListener("click", () => this._done(true));
    cancel.addEventListener("click", () => this._done(false));
  }

  // okLabel only: a notice (no Cancel).
  ask(title, message, okLabel, cancelLabel = null) {
    this._done(false); // a newer popup replaces an open one
    this.title.textContent = title;
    this.msg.textContent = message;
    this.ok.textContent = okLabel;
    this.cancel.textContent = cancelLabel || "";
    this.cancel.hidden = !cancelLabel;
    this.open(this.ok);
    return new Promise((resolve) => {
      this._resolve = resolve;
    });
  }

  dismiss() {
    this._done(false);
  }

  _done(choice) {
    this.close();
    const resolve = this._resolve;
    this._resolve = null;
    resolve?.(choice);
  }
}

const STATE_LABELS = {
  idle:      "Idle — waiting for arm command",
  armed:     "Armed — waiting for ThorSync",
  triggered: "Triggered — capture running",
  done:      "Done",
};

const UV_TITLE =
  "Switch the UV LED on or off now (also recorded by ThorSync and in the take)";
const UV_REFLASH_TITLE =
  "The board's firmware has no UV command: flash the current firmware to use it";

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

// The UV switch: lit (and reading ON) while the UV is on.
function uvControl() {
  const label = el("label", "twophoton-uv");
  label.id = "twophoton-uv-label";
  label.title = UV_TITLE;
  const box = el("input");
  box.type = "checkbox";
  box.id = "twophoton-uv";
  const state = el("span", "twophoton-uv-state", "OFF");
  state.id = "twophoton-uv-state";
  label.append(box, el("span", "twophoton-uv-name", "UV light"), state);
  return label;
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
    this._offered = null; // the last offer sent to the Record tab
    // Server facts (link_status): ask before the next armed take; the last
    // take's capture ThorSync did not record ({capture, message}).
    this.thorsyncRemind = false;
    this.thorsyncMissing = null;
    this._announced = null; // the missed capture already shown in a popup
    this.dialog = new ThorSyncDialog();
    this.stateValue = document.getElementById("twophoton-state-value");
    this.armWithRec = document.getElementById("twophoton-arm-with-recording");
    this.armWithRec.addEventListener("change", () => this.refresh());
    // Above the tabs when the app offers it, else at the end of this tab.
    const uv = uvControl();
    if (ctx.addControl) ctx.addControl(uv);
    else ctx.panel.append(uv);
    // The board's UV level (server truth) and whether its firmware has it.
    this.uvOn = false;
    this.uvSupported = null;
    this.uvBusy = false;
    this.uvBox = document.getElementById("twophoton-uv");
    this.uvLabel = document.getElementById("twophoton-uv-label");
    this.uvState = document.getElementById("twophoton-uv-state");
    this.uvBox.addEventListener("change", () => this._setUv(this.uvBox.checked));
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
    if (typeof msg.thorsync_remind === "boolean") this.thorsyncRemind = msg.thorsync_remind;
    if (typeof msg.uv === "boolean") this.uvOn = msg.uv;
    if ("uv_supported" in msg) this.uvSupported = msg.uv_supported;
    if ("thorsync_missing" in msg) {
      this.thorsyncMissing = msg.thorsync_missing || null;
      this._announceMissing();
    }
    super.applyLink(msg);
  }

  // The box shows what the board reports: a failed switch falls back to it.
  async _setUv(on) {
    this.uvBusy = true;
    this.refresh();
    const data = await request("POST", "/api/twophoton/uv", { on }, {
      action: `UV ${on ? "on" : "off"}`,
      notify: this.notify,
    });
    if (data && typeof data.uv === "boolean") this.uvOn = data.uv;
    this.uvBusy = false;
    this.refresh();
  }

  // Once per missed capture: the operator must fix ThorImage before the next take.
  _announceMissing() {
    const missing = this.thorsyncMissing;
    if (!missing || missing.capture === this._announced) return;
    this._announced = missing.capture;
    this.dialog.ask("ThorSync did not record this take", missing.message, "OK");
  }

  // Before an armed take that needs the reminder: false cancels the start.
  async confirmStart() {
    if (!this.thorsyncRemind || this.getStartParams() == null) return true;
    const title = this.thorsyncMissing
      ? "ThorSync missed the last take"
      : "Before the first take: ThorSync";
    return this.dialog.ask(title, REMINDER, "It is ticked — record", "Cancel");
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
    if (!this.uvBusy) this.uvBox.checked = this.uvOn;
    this.uvBox.disabled =
      !this.ready || !this.connected || this.uvBusy || this.uvSupported === false;
    this.uvLabel.title = this.uvSupported === false ? UV_REFLASH_TITLE : UV_TITLE;
    this.uvLabel.classList.toggle("twophoton-uv--on", this.uvOn);
    this.uvLabel.classList.toggle("twophoton-uv--disabled", this.uvBox.disabled);
    this.uvState.textContent = this.uvOn ? "ON" : "OFF";
    this.stateValue.textContent = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    this.stateValue.className = `twophoton-state twophoton-state--${this.arduinoState}`;
    this._offerDurationSync();
  }
}
