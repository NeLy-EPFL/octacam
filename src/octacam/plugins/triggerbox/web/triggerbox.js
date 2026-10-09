// triggerbox tab: camera trigger lines, 3 light channels (off / strobe /
// continuous / pulse train), arm-with-recording, and a frame-timing diagram of
// the live camera exposures.
import { SerialTab } from "/js/serial.js";
import { clamp, request } from "/js/util.js";

const STATE_LABELS = {
  idle: "Idle — waiting for arm command",
  running: "Running — triggering",
  done: "Done",
};
const DEFAULT_GUARD_US = 100;
const FIRMWARE_DEFAULT_CAM_PULSE_US = 500; // matches kDefaultCamPulseUs in the .ino

// Non-reserved pins available for camera lines (D2/D3/D4 are the status LED).
const CAMERA_PINS = [
  "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12", "D13",
  "A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7",
];
const LIGHT_PIN = { 1: "D5", 2: "D6", 3: "D7" };
const LIGHT_MODES = [
  ["off", "Off"],
  ["strobe", "Strobe"],
  ["continuous", "Continuous"],
  ["pulse_train", "Pulse train"],
];

function fmtDur(us) {
  if (!Number.isFinite(us)) return "–";
  return us >= 1000 ? `${(us / 1000).toFixed(2)} ms` : `${Math.round(us)} µs`;
}
// Compact axis-tick label: trims trailing zeros so 12500 → "12.5 ms", 5000 → "5 ms".
function fmtTick(us) {
  if (us === 0) return "0";
  if (us >= 1000) return `${(us / 1000).toFixed(1).replace(/\.0$/, "")} ms`;
  return `${Math.round(us)} µs`;
}
// Round up to a "nice" 1/2/5×10ⁿ step for readable axis ticks.
function niceNum(x) {
  if (x <= 0) return 1;
  const exp = Math.floor(Math.log10(x));
  const f = x / Math.pow(10, exp);
  const nf = f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10;
  return nf * Math.pow(10, exp);
}
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

const MARKUP = `
  <div class="col">
    <div class="row" title="Current state of the Arduino trigger board">
      <span>Arduino state:</span>
      <span id="triggerbox-state-value" class="triggerbox-state triggerbox-state--idle"></span>
    </div>
    <div id="triggerbox-firmware" class="hint"></div>

    <div class="triggerbox-section">
      <div class="row triggerbox-section-head">
        <span>Camera trigger lines</span>
        <button type="button" id="triggerbox-add-camera" class="btn btn-icon"
          title="Add a camera trigger line">＋</button>
      </div>
      <div id="triggerbox-cameras"></div>
    </div>

    <div class="triggerbox-section">
      <div class="row triggerbox-section-head"><span>Light channels</span></div>
      <div id="triggerbox-lights"></div>
    </div>

    <div class="triggerbox-timing">
      <div class="row triggerbox-timing-head">
        <span>Frame timing</span>
      </div>
      <div id="triggerbox-timing-viz" class="triggerbox-timing-viz"></div>
      <div id="triggerbox-timing-summary" class="hint"></div>
    </div>

    <p class="hint">
      FPS and duration come from the Record tab. Each camera line pulses on
      its pin every frame; each of the three CCS light channels is
      independent — <strong>off</strong>, a frame-locked <strong>strobe</strong>
      (Auto sizes the on-time to bracket the longest camera exposure, or set a
      Manual duty), <strong>continuous</strong>, or an independent optogenetic
      <strong>pulse train</strong>. When "Arm with recording" is checked the
      board is armed each time a recording starts. Cameras must be set to
      external hardware trigger.
    </p>
    <label class="center"
      title="Arm the Arduino automatically when a recording starts">
      <input type="checkbox" id="triggerbox-arm-with-recording" checked>
      Arm with recording
    </label>
  </div>`;

export default class TriggerboxTab extends SerialTab {
  static label = "triggerbox";
  static title = "triggerbox rig: arm the Arduino camera trigger + LED strobe";

  constructor(ctx) {
    super(ctx, MARKUP);
    const status = ctx.status;
    this.send = ctx.send; // pushes a JSON message over the WS (live spec edits)
    this._getRecordSettings = ctx.getRecordSettings;
    this.firmwareOk = true;
    this.error = status.error || null;
    this.arduinoState = "idle";
    this.guardUs = Number.isFinite(status.guard_us) ? status.guard_us : DEFAULT_GUARD_US;
    this.exposures = []; // [{index, name, exposure_us, trigger_delay_us}] from /exposures

    this._seedCameras(status.cameras);
    this._seedLights(status.lights);

    this.stateValue = document.getElementById("triggerbox-state-value");
    this.firmwareEl = document.getElementById("triggerbox-firmware");
    this.camerasEl = document.getElementById("triggerbox-cameras");
    this.lightsEl = document.getElementById("triggerbox-lights");
    this.addCameraBtn = document.getElementById("triggerbox-add-camera");
    this.timingViz = document.getElementById("triggerbox-timing-viz");
    this.timingSummary = document.getElementById("triggerbox-timing-summary");
    this.armWithRec = document.getElementById("triggerbox-arm-with-recording");

    this.addCameraBtn.addEventListener("click", () => this._addCamera());
    document.addEventListener("tab-shown", (e) => {
      if (e.detail?.tab === "triggerbox") this._renderTiming();
    });
    // Any camera feature change (from any client) may move an exposure or delay.
    document.addEventListener("camera-features-changed", () =>
      this._scheduleExposureReload()
    );
    // The diagram is drawn at 1 SVG unit = 1 CSS px so its text stays legible,
    // so it is redrawn whenever its width changes.
    this._lastVizW = 0;
    this._exposureReloadTimer = null;
    new ResizeObserver(() => {
      const w = Math.round(this.timingViz.clientWidth);
      if (w && w !== this._lastVizW) this._renderTiming();
    }).observe(this.timingViz);

    this._renderCameras();
    this._renderLights();
    this.start(status);
    this._loadExposures();
  }

  // A flash would cut a running train.
  boardBusy() {
    return this.arduinoState === "running";
  }

  // --------------------------------------------------------- seeding

  _seedCameras(list) {
    this.cameras = (Array.isArray(list) ? list : []).map((c) => ({
      pin: CAMERA_PINS.includes(c?.pin) ? c.pin : "D13",
      pulse_us: Number.isFinite(c?.pulse_us) ? c.pulse_us : 0,
      delay_us: Number.isFinite(c?.delay_us) ? c.delay_us : 0,
    }));
    if (!this.cameras.length) this.cameras = [{ pin: "D13", pulse_us: 0, delay_us: 0 }];
  }

  _seedLights(list) {
    const byCh = {};
    for (const l of Array.isArray(list) ? list : []) {
      if (l && LIGHT_PIN[l.channel]) byCh[l.channel] = l;
    }
    this.lights = {};
    for (const ch of [1, 2, 3]) {
      const d = byCh[ch] || {};
      this.lights[ch] = {
        channel: ch,
        pin: LIGHT_PIN[ch],
        mode: LIGHT_MODES.some(([v]) => v === d.mode) ? d.mode : "off",
        duty_mode: d.duty_mode === "auto" ? "auto" : "manual",
        duty_percent: Number.isFinite(d.duty_percent) ? d.duty_percent : 20,
        delay_us: Number.isFinite(d.delay_us) ? d.delay_us : 0,
        freq_hz: Number.isFinite(d.freq_hz) ? d.freq_hz : 10,
        pulse_us: Number.isFinite(d.pulse_us) ? d.pulse_us : 1000,
        start_delay_ms: Number.isFinite(d.start_delay_ms) ? d.start_delay_ms : 0,
        train_ms: Number.isFinite(d.train_ms) ? d.train_ms : 0,
      };
    }
  }

  _num(v, d) {
    const n = parseFloat(v);
    return Number.isFinite(n) ? Math.max(0, n) : d;
  }

  _el(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  // --------------------------------------------------------- WS / connection

  setConnected(connected) {
    const became = connected && !this.connected;
    super.setConnected(connected);
    // Debounced: coalesces with the handshake's `system` push.
    if (became) this._scheduleExposureReload();
  }

  applyLink(msg) {
    if (msg.arduino_state) this.arduinoState = msg.arduino_state;
    if (typeof msg.firmware_ok === "boolean") this.firmwareOk = msg.firmware_ok;
    super.applyLink(msg);
  }

  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if ("error" in msg) this.toastError(msg.error);
    super.applyState(msg);
  }

  applyStatus(info) {
    super.applyStatus(info);
    // The constructor's exposure read may have seen the empty placeholder
    // camera system.
    this._scheduleExposureReload();
  }

  // --------------------------------------------------------- start params

  // The full arm spec for a recording start, or null when arm-with-recording is
  // unchecked, the link is not open, or the firmware is incompatible.
  getStartParams() {
    if (!this.ready || !this.firmwareOk || !this.armWithRec.checked) return null;
    const s = this._getRecordSettings();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 80));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    const lights = [1, 2, 3]
      .map((ch) => this.lights[ch])
      .filter((l) => l.mode !== "off")
      .map((l) => ({ ...l }));
    return { fps, duration_ms, cameras: this.cameras.map((c) => ({ ...c })), lights };
  }

  // --------------------------------------------------------- camera rows

  _renderCameras() {
    this.camerasEl.innerHTML = "";
    this.cameras.forEach((cam, i) => {
      const opts = CAMERA_PINS.map(
        (p) => `<option value="${p}"${p === cam.pin ? " selected" : ""}>${p}</option>`
      ).join("");
      const row = this._el(
        `<div class="tb-cam-row">
           <select class="tb-cam-pin" title="Arduino line this camera group is wired to">${opts}</select>
           <label class="tb-field">pulse µs<input type="number" class="tb-cam-pulse" min="0" step="10" value="${cam.pulse_us}" placeholder="500"></label>
           <label class="tb-field">delay µs<input type="number" class="tb-cam-delay" min="0" step="10" value="${cam.delay_us}"></label>
           <button type="button" class="btn btn-icon tb-cam-remove" title="Remove this camera line">×</button>
         </div>`
      );
      row.querySelector(".tb-cam-pin").addEventListener("change", (e) => {
        cam.pin = e.target.value;
        this._renderTiming();
      });
      row.querySelector(".tb-cam-pulse").addEventListener("input", (e) => {
        cam.pulse_us = this._num(e.target.value, 0);
        this._renderTiming();
      });
      row.querySelector(".tb-cam-delay").addEventListener("input", (e) => {
        cam.delay_us = this._num(e.target.value, 0);
        this._renderTiming();
      });
      row.querySelector(".tb-cam-remove").addEventListener("click", () => {
        this.cameras.splice(i, 1);
        this._renderCameras();
        this._renderTiming();
      });
      this.camerasEl.appendChild(row);
    });
    this._applyDisabled();
  }

  _addCamera() {
    if (this.cameras.length >= 12) return;
    this.cameras.push({ pin: "D13", pulse_us: 0, delay_us: 0 });
    this._renderCameras();
    this._renderTiming();
  }

  // --------------------------------------------------------- light cards

  _renderLights() {
    this.lightsEl.innerHTML = "";
    for (const ch of [1, 2, 3]) {
      const l = this.lights[ch];
      const modeOpts = LIGHT_MODES.map(
        ([v, t]) => `<option value="${v}"${v === l.mode ? " selected" : ""}>${t}</option>`
      ).join("");
      const card = this._el(
        `<div class="tb-light-card">
           <div class="tb-light-head">
             <span>Channel ${ch} · ${LIGHT_PIN[ch]}</span>
             <select class="tb-light-mode">${modeOpts}</select>
           </div>
           <div class="tb-light-fields"></div>
         </div>`
      );
      const fields = card.querySelector(".tb-light-fields");
      card.querySelector(".tb-light-mode").addEventListener("change", (e) => {
        l.mode = e.target.value;
        this._renderLightFields(fields, l);
        this._renderTiming();
      });
      this._renderLightFields(fields, l);
      this.lightsEl.appendChild(card);
    }
    this._applyDisabled();
  }

  _renderLightFields(fields, l) {
    if (l.mode === "strobe") {
      fields.innerHTML =
        `<label class="tb-field">duty
           <select class="tb-l-dutymode">
             <option value="auto"${l.duty_mode === "auto" ? " selected" : ""}>Auto (cover exposure)</option>
             <option value="manual"${l.duty_mode === "manual" ? " selected" : ""}>Manual %</option>
           </select></label>` +
        `<label class="tb-field tb-l-dutyrow${l.duty_mode === "auto" ? " hidden" : ""}">%
           <input type="number" class="tb-l-duty" min="0" max="100" step="1" value="${l.duty_percent}"></label>` +
        `<label class="tb-field">delay µs
           <input type="number" class="tb-l-delay" min="0" step="10" value="${l.delay_us}"></label>`;
      fields.querySelector(".tb-l-dutymode").addEventListener("change", (e) => {
        l.duty_mode = e.target.value;
        this._renderLightFields(fields, l);
        this._renderTiming();
      });
      fields.querySelector(".tb-l-duty")?.addEventListener("input", (e) => {
        l.duty_percent = clamp(this._num(e.target.value, 20), 0, 100);
        this._renderTiming();
      });
      fields.querySelector(".tb-l-delay").addEventListener("input", (e) => {
        l.delay_us = this._num(e.target.value, 0);
        this._renderTiming();
      });
    } else if (l.mode === "pulse_train") {
      fields.innerHTML =
        `<label class="tb-field">freq Hz<input type="number" class="tb-l-freq" min="0.1" step="0.1" value="${l.freq_hz}"></label>` +
        `<label class="tb-field">pulse µs<input type="number" class="tb-l-pulse" min="1" step="10" value="${l.pulse_us}"></label>` +
        `<label class="tb-field">start ms<input type="number" class="tb-l-start" min="0" step="1" value="${l.start_delay_ms}"></label>` +
        `<label class="tb-field" title="0 = for the whole recording">train ms<input type="number" class="tb-l-train" min="0" step="1" value="${l.train_ms}"></label>`;
      fields.querySelector(".tb-l-freq").addEventListener("input", (e) => {
        l.freq_hz = this._num(e.target.value, 10);
        this._renderTiming();
      });
      fields.querySelector(".tb-l-pulse").addEventListener("input", (e) => {
        l.pulse_us = this._num(e.target.value, 1000);
        this._renderTiming();
      });
      fields.querySelector(".tb-l-start").addEventListener("input", (e) => {
        l.start_delay_ms = this._num(e.target.value, 0);
        this._renderTiming();
      });
      fields.querySelector(".tb-l-train").addEventListener("input", (e) => {
        l.train_ms = this._num(e.target.value, 0);
        this._renderTiming();
      });
    } else if (l.mode === "continuous") {
      fields.innerHTML = `<span class="hint">On for the whole recording.</span>`;
    } else {
      fields.innerHTML = `<span class="hint">Channel off.</span>`;
    }
  }

  // --------------------------------------------------------- render / gating

  linkMessage() {
    if (!this.ready) return super.linkMessage();
    if (!this.firmwareOk) {
      const fw = this.fw.firmware;
      return `Incompatible firmware${fw ? ` (${fw})` : ""} — reflash arduino/triggerbox. Arming is disabled.`;
    }
    // A failed arm (e.g. a wedged USB link a bus reset couldn't clear): the
    // recording won't be hardware-triggered.
    return this.error;
  }

  refresh() {
    super.refresh();
    this._applyDisabled();
    this.stateValue.textContent = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    this.stateValue.className = `triggerbox-state triggerbox-state--${this.arduinoState}`;
  }

  _applyDisabled() {
    const disabled = !this.ready || !this.connected || !this.firmwareOk;
    this.armWithRec.disabled = disabled;
    this.addCameraBtn.disabled = disabled;
    for (const root of [this.camerasEl, this.lightsEl]) {
      for (const el of root.querySelectorAll("input, select, button")) el.disabled = disabled;
    }
  }

  renderFirmware() {
    super.renderFirmware();
    const fw = this.fw;
    let txt = "";
    if (this.ready && fw.firmware) {
      txt = `Board firmware: ${fw.firmware}`;
      if (fw.state === "current") txt += " ✓ up to date";
    }
    this.firmwareEl.textContent = txt;
  }

  // --------------------------------------------------- exposures + timing

  // Re-read the live camera exposures once per burst of changes.
  _scheduleExposureReload() {
    clearTimeout(this._exposureReloadTimer);
    this._exposureReloadTimer = setTimeout(() => this._loadExposures(), 250);
  }

  async _loadExposures() {
    const d = await request("GET", "/api/triggerbox/exposures");
    if (d) {
      this.exposures = Array.isArray(d.cameras) ? d.cameras : [];
      if (Number.isFinite(d.guard_us)) this.guardUs = d.guard_us;
    }
    this._renderTiming();
  }

  _timingModel() {
    const s = this._getRecordSettings() || {};
    const fps = Math.max(1, Math.round(s.fps || 80));
    const periodUs = 1e6 / fps;

    const exps = (this.exposures || []).map((c) => {
      const exp = Number.isFinite(c.exposure_us) ? c.exposure_us : null;
      const delay = Number.isFinite(c.trigger_delay_us) ? c.trigger_delay_us : 0;
      return {
        name: c.name ?? `cam${c.index}`,
        exposureUs: exp,
        delayUs: delay,
        coverageUs: exp == null ? null : delay + exp,
      };
    });
    const coverages = exps.map((c) => c.coverageUs).filter((v) => v != null);
    const maxCoverage = coverages.length ? Math.max(...coverages) : null;
    const autoLedOnUs = maxCoverage != null ? maxCoverage + this.guardUs : null;

    const rows = [];
    for (const c of this.cameras) {
      const pulse = Math.min(c.pulse_us > 0 ? c.pulse_us : FIRMWARE_DEFAULT_CAM_PULSE_US, periodUs);
      rows.push({
        kind: "trigger",
        label: `Cam ${c.pin}`,
        delayUs: Math.min(c.delay_us || 0, periodUs),
        pulseUs: pulse,
      });
    }
    for (const ch of [1, 2, 3]) {
      const l = this.lights[ch];
      if (l.mode === "off") continue;
      if (l.mode === "continuous") {
        rows.push({ kind: "led", label: `Ch${ch} ${l.pin}`, delayUs: 0, onUs: periodUs, rawOnUs: periodUs, continuous: true });
      } else if (l.mode === "strobe") {
        const manualOn = (clamp(l.duty_percent, 0, 100) / 100) * periodUs;
        const auto = l.duty_mode === "auto";
        const on = auto && autoLedOnUs != null ? autoLedOnUs : manualOn;
        rows.push({
          kind: "led",
          label: `Ch${ch} ${l.pin}`,
          delayUs: Math.min(l.delay_us || 0, periodUs),
          onUs: Math.min(on, periodUs),
          rawOnUs: on,
          continuous: on >= periodUs,
          guardFrom: auto ? maxCoverage : null,
        });
      } else if (l.mode === "pulse_train") {
        const interval = l.freq_hz > 0 ? 1e6 / l.freq_hz : 0;
        rows.push({
          kind: "pulse",
          label: `Ch${ch} ${l.pin}`,
          intervalUs: interval,
          pulseUs: l.pulse_us,
          startDelayUs: (l.start_delay_ms || 0) * 1000,
          freqHz: l.freq_hz,
          trainMs: l.train_ms,
        });
      }
    }
    for (const c of exps) {
      rows.push({ kind: "exposure", label: c.name, delayUs: c.delayUs, exposureUs: c.exposureUs, coverageUs: c.coverageUs });
    }

    return { fps, periodUs, rows, maxCoverage, guardUs: this.guardUs, exps };
  }

  // Push the camera/light spec to the server (debounced): the managed preview
  // arms from it and re-strobes to match. The arm-with-recording checkbox
  // gates only the recording arm.
  _pushSpec() {
    clearTimeout(this._pushTimer);
    this._pushTimer = setTimeout(() => {
      const s = this._getRecordSettings();
      const fps = Math.max(1, Math.round(s?.fps || 80));
      const lights = [1, 2, 3].map((ch) => ({ ...this.lights[ch] }));
      this.send({
        type: "triggerbox_spec",
        spec: { fps, cameras: this.cameras.map((c) => ({ ...c })), lights },
      });
    }, 250);
  }

  _renderTiming() {
    this._pushSpec(); // every camera/light edit funnels through here
    const m = this._timingModel();
    // A hidden tab measures 0: keep the last known width.
    const cw = Math.round(this.timingViz.clientWidth) || this._lastVizW || 240;
    this._lastVizW = cw;
    this.timingViz.innerHTML = this._buildSvg(m, cw);
    this._renderSummary(m);
  }

  _renderSummary(m) {
    const nLed = m.rows.filter((r) => r.kind === "led").length;
    const nPulse = m.rows.filter((r) => r.kind === "pulse").length;
    const parts = [
      `${m.fps} fps · ${fmtDur(m.periodUs)} frame`,
      `${m.rows.filter((r) => r.kind === "trigger").length} camera line(s)`,
      `${nLed} frame-locked light(s)${nPulse ? `, ${nPulse} pulse-train(s)` : ""}`,
    ];
    if (m.maxCoverage != null) {
      parts.push(`longest exposure ${fmtDur(m.maxCoverage)} + ${fmtDur(m.guardUs)} guard`);
    }
    const pins = [...this.cameras.map((c) => c.pin), ...m.rows.filter((r) => r.kind === "led" || r.kind === "pulse").map((r) => r.label.split(" ")[1])];
    const dup = pins.find((p, i) => pins.indexOf(p) !== i);
    let note = "";
    if (dup) note = ` — ⚠ two outputs share pin ${dup}; the board will reject the arm.`;
    else if (m.maxCoverage == null && m.rows.some((r) => r.kind === "led"))
      note = " — no camera exposures yet; Auto strobes fall back to their manual duty.";
    this.timingSummary.textContent = parts.join(" · ") + note;
  }

  _buildSvg(m, cw) {
    // 1 SVG unit = 1 CSS px of the container width `cw`. Row labels sit above
    // the bars so the bars get the full width.
    const width = Math.max(200, Math.round(cw) || 240);
    const PAD_L = 1, PAD_R = 7, TOP = 6;
    const labelH = 15, barH = 13, rowGap = 9;
    const stride = labelH + barH + rowGap, axisH = 24;
    const plotW = width - PAD_L - PAD_R;
    const n = Math.max(1, m.rows.length);
    const H = TOP + n * stride + axisH;

    // The axis ends after the last activity unless something spans the frame,
    // so short exposures aren't squeezed into a sliver.
    let activity = 0, spansFrame = false;
    for (const r of m.rows) {
      if (r.kind === "trigger") activity = Math.max(activity, r.delayUs + r.pulseUs);
      else if (r.kind === "led") {
        if (r.continuous) spansFrame = true;
        else activity = Math.max(activity, r.delayUs + r.onUs);
      } else if (r.kind === "pulse") spansFrame = true;
      else if (r.kind === "exposure" && r.exposureUs != null)
        activity = Math.max(activity, r.delayUs + r.exposureUs);
    }
    if (m.maxCoverage != null) activity = Math.max(activity, m.maxCoverage + (m.guardUs || 0));
    activity = Math.min(activity || m.periodUs, m.periodUs);
    let axisMax;
    if (spansFrame || activity >= m.periodUs * 0.85) {
      axisMax = m.periodUs;
    } else {
      const s = niceNum((activity * 1.12) / 3.5);
      axisMax = Math.min(m.periodUs, Math.ceil((activity * 1.1) / s) * s);
    }
    const truncated = axisMax < m.periodUs * 0.999;

    const X = (t) => PAD_L + (clamp(t, 0, axisMax) / axisMax) * plotW;
    const barW = (a, b) => Math.max(2, X(b) - X(a));
    const barY = (i) => TOP + i * stride + labelH;
    const labelBaseY = (i) => TOP + i * stride + 11;
    const plotBottom = TOP + n * stride - rowGap + 2;
    // In-bar annotation, baseline near the bar's bottom (y = barY(i)).
    const tag = (x, y, text, anchor = "start") =>
      `<text x="${x}" y="${y + barH - 3}" class="tb-tag" text-anchor="${anchor}">${esc(text)}</text>`;

    const g = [];        // background: gridlines, guide, lanes, bars
    const labels = [];   // drawn last, so each label's halo knocks out any line behind it

    // No gridline near the right end, where the end tick label sits.
    const step = niceNum(axisMax / 4);
    const ticks = [];
    for (let t = step; t < axisMax * 0.75; t += step) ticks.push(t);
    for (const t of ticks)
      g.push(`<line x1="${X(t)}" y1="${barY(0) - 2}" x2="${X(t)}" y2="${plotBottom}" class="tb-grid"/>`);

    // Guide at the longest exposure's end, drawn under the labels.
    if (m.maxCoverage != null && m.maxCoverage < axisMax)
      g.push(
        `<line x1="${X(m.maxCoverage)}" y1="${barY(0) - 2}" x2="${X(m.maxCoverage)}" ` +
        `y2="${plotBottom}" class="tb-guide"/>`
      );

    m.rows.forEach((r, i) => {
      const y = barY(i);
      g.push(`<rect x="${PAD_L}" y="${y}" width="${plotW}" height="${barH}" class="tb-lane" rx="2"/>`);
      if (r.kind === "trigger") {
        g.push(
          `<rect x="${X(r.delayUs)}" y="${y}" width="${barW(r.delayUs, r.delayUs + r.pulseUs)}" ` +
          `height="${barH}" class="tb-trigger" rx="1"><title>pulse ${fmtDur(r.pulseUs)}` +
          `${r.delayUs ? `, delay ${fmtDur(r.delayUs)}` : ""}</title></rect>`
        );
      } else if (r.kind === "led") {
        if (r.continuous) {
          g.push(`<rect x="${PAD_L}" y="${y}" width="${plotW}" height="${barH}" class="tb-led" rx="1"/>`);
          labels.push(tag(X(axisMax) - 4, y, "continuous", "end"));
        } else if (r.onUs > 0) {
          g.push(
            `<rect x="${X(r.delayUs)}" y="${y}" width="${barW(r.delayUs, r.delayUs + r.onUs)}" ` +
            `height="${barH}" class="tb-led" rx="1"><title>on ${fmtDur(r.rawOnUs)}</title></rect>`
          );
          if (r.guardFrom != null && r.delayUs + r.onUs > r.guardFrom)
            g.push(
              `<rect x="${X(r.guardFrom)}" y="${y}" width="${barW(r.guardFrom, r.delayUs + r.onUs)}" ` +
              `height="${barH}" class="tb-guard" rx="1"><title>guard</title></rect>`
            );
        } else {
          labels.push(tag(PAD_L + 3, y, "off"));
        }
      } else if (r.kind === "pulse") {
        if (r.intervalUs > 0) {
          const pw = Math.min(r.pulseUs, r.intervalUs - 1);
          let drawn = 0;
          for (let t = r.startDelayUs; t < axisMax && drawn < 400; t += r.intervalUs, drawn++)
            g.push(`<rect x="${X(t)}" y="${y}" width="${barW(t, t + pw)}" height="${barH}" class="tb-pulse"/>`);
          labels.push(tag(X(axisMax) - 4, y, `${r.freqHz} Hz`, "end"));
        } else {
          labels.push(tag(PAD_L + 3, y, "invalid frequency"));
        }
      } else if (r.kind === "exposure") {
        if (r.exposureUs == null) {
          labels.push(tag(PAD_L + 3, y, "exposure n/a"));
        } else {
          const gov = r.coverageUs === m.maxCoverage;
          g.push(
            `<rect x="${X(r.delayUs)}" y="${y}" width="${barW(r.delayUs, r.delayUs + r.exposureUs)}" ` +
            `height="${barH}" class="tb-exposure${gov ? " tb-exposure--gov" : ""}" rx="1">` +
            `<title>${esc(r.label)}: expose ${fmtDur(r.exposureUs)}` +
            `${r.delayUs ? `, trigger delay ${fmtDur(r.delayUs)}` : ""}</title></rect>`
          );
        }
      }
      labels.push(
        `<text x="${PAD_L + 1}" y="${labelBaseY(i)}" class="tb-rowlabel">${esc(r.label)}</text>`
      );
    });

    if (truncated)
      labels.push(
        `<text x="${PAD_L + plotW}" y="${labelBaseY(0)}" class="tb-trunc" ` +
        `text-anchor="end">${fmtDur(m.periodUs)} frame ⇥</text>`
      );

    const ay = plotBottom + 4;
    g.push(`<line x1="${PAD_L}" y1="${ay}" x2="${PAD_L + plotW}" y2="${ay}" class="tb-axis"/>`);
    g.push(`<text x="${PAD_L}" y="${ay + 13}" class="tb-axis-label" text-anchor="start">0</text>`);
    for (const t of ticks)
      g.push(`<text x="${X(t)}" y="${ay + 13}" class="tb-axis-label" text-anchor="middle">${fmtTick(t)}</text>`);
    g.push(
      `<text x="${PAD_L + plotW}" y="${ay + 13}" class="tb-axis-label" text-anchor="end">${fmtTick(axisMax)}</text>`
    );

    return (
      `<svg viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" class="tb-svg" ` +
      `role="img" aria-label="triggerbox frame timing diagram">${g.join("")}${labels.join("")}</svg>`
    );
  }
}
