// triggerbox tab: configure any number of camera trigger lines + 3 independent
// CCS light channels (off / strobe / continuous / pulse-train), arm-with-recording,
// and a frame-timing visualization driven by the live camera exposures.
//
// Served from /plugins/triggerbox/, so it can't import core "./util.js" (that would
// 404). The shared fetch helper (api) and clampInput are passed in via ctx from the
// host (app.js). Serial helpers live at /js/ (absolute path).
import { fetchSerialPorts, populatePortSelect } from "/js/serial.js";

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
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}
function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

export default class TriggerboxTab {
  constructor({ notify, status, getRecordSettings, api, clampInput }) {
    this.notify = notify;
    this.api = api;
    this.clampInput = clampInput;
    this._getRecordSettings = getRecordSettings;
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.firmware = status?.firmware || null;
    this.firmwareOk = status?.firmware_ok !== false;
    this.armError = status?.error || null;
    this.arduinoState = status?.arduino_state || "idle";
    this.connected = false;
    this.guardUs = Number.isFinite(status?.guard_us) ? status.guard_us : DEFAULT_GUARD_US;
    this.exposures = []; // [{index, name, exposure_us, trigger_delay_us}] from /exposures

    this._seedCameras(status?.cameras);
    this._seedLights(status?.lights);

    this.statusBox = document.getElementById("triggerbox-status");
    this.statusMsg = document.getElementById("triggerbox-status-msg");
    this.reconnectBtn = document.getElementById("triggerbox-reconnect");
    this.portSelect = document.getElementById("triggerbox-port");
    this.stateValue = document.getElementById("triggerbox-state-value");
    this.firmwareEl = document.getElementById("triggerbox-firmware");
    this.camerasEl = document.getElementById("triggerbox-cameras");
    this.lightsEl = document.getElementById("triggerbox-lights");
    this.addCameraBtn = document.getElementById("triggerbox-add-camera");
    this.timingViz = document.getElementById("triggerbox-timing-viz");
    this.timingSummary = document.getElementById("triggerbox-timing-summary");
    this.timingRefresh = document.getElementById("triggerbox-timing-refresh");
    this.armWithRec = document.getElementById("triggerbox-arm-with-recording");

    this.reconnectBtn.addEventListener("click", () => this._reconnect());
    this.addCameraBtn?.addEventListener("click", () => this._addCamera());
    this.timingRefresh?.addEventListener("click", () => this._loadExposures());
    document.addEventListener("tab-shown", (e) => {
      if (e.detail?.tab === "triggerbox") this._renderTiming();
    });

    this._loadPorts();
    this._renderCameras();
    this._renderLights();
    this._refresh();
    this._renderState();
    this._renderFirmware();
    this._loadExposures();
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

  async _loadPorts() {
    populatePortSelect(this.portSelect, await fetchSerialPorts(this.api), this.device);
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
    this.connected = connected;
    this._refresh();
    if (became) this._loadExposures();
  }

  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if (msg.device) this.device = msg.device;
    if ("firmware" in msg) this.firmware = msg.firmware || null;
    if (typeof msg.firmware_ok === "boolean") this.firmwareOk = msg.firmware_ok;
    if (typeof msg.ready === "boolean") this.ready = msg.ready;
    if ("error" in msg) {
      const next = msg.error || null;
      // Toast a newly-raised failure so the operator notices even off-tab.
      if (next && next !== this.armError) this.notify("error", next);
      this.armError = next;
    }
    this._renderFirmware();
    this._refresh();
    this._renderState();
  }

  // --------------------------------------------------------- start params

  // The full arm spec for a recording start, or null when arm-with-recording is
  // unchecked, the link is not open, or the firmware is incompatible.
  getStartParams() {
    if (!this.ready || !this.firmwareOk || !this.armWithRec?.checked) return null;
    const s = this._getRecordSettings?.();
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
    if (!this.camerasEl) return;
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
    if (!this.lightsEl) return;
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

  _refresh() {
    if (this.ready && this.firmwareOk && this.armError) {
      // Link + firmware are fine, but the board failed to arm (e.g. a wedged
      // USB link that a bus reset couldn't clear). Surface it — the recording
      // won't be hardware-triggered.
      this.statusMsg.textContent = this.armError;
      this.statusBox.classList.remove("hidden");
    } else if (this.ready && this.firmwareOk) {
      this.statusBox.classList.add("hidden");
    } else if (this.ready && !this.firmwareOk) {
      this.statusMsg.textContent =
        `Incompatible firmware${this.firmware ? ` (${this.firmware})` : ""} — ` +
        `reflash arduino/triggerbox. Arming is disabled.`;
      this.statusBox.classList.remove("hidden");
    } else {
      const where = this.device ? ` (${this.device})` : "";
      this.statusMsg.textContent =
        `Serial port${where} is not open — check the Arduino is plugged in ` +
        `and the device path matches the plugin config, then reconnect.`;
      this.statusBox.classList.remove("hidden");
    }
    this._applyDisabled();
    if (this.timingRefresh) this.timingRefresh.disabled = !this.connected;
  }

  _applyDisabled() {
    const disabled = !this.ready || !this.connected || !this.firmwareOk;
    for (const el of [this.armWithRec, this.addCameraBtn]) {
      if (el) el.disabled = disabled;
    }
    for (const root of [this.camerasEl, this.lightsEl]) {
      if (!root) continue;
      for (const el of root.querySelectorAll("input, select, button")) el.disabled = disabled;
    }
  }

  _renderState() {
    const label = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    if (this.stateValue) {
      this.stateValue.textContent = label;
      this.stateValue.className = `triggerbox-state triggerbox-state--${this.arduinoState}`;
    }
  }

  _renderFirmware() {
    if (!this.firmwareEl) return;
    this.firmwareEl.textContent =
      this.ready && this.firmware ? `Board firmware: ${this.firmware}` : "";
  }

  // --------------------------------------------------- exposures + timing

  async _loadExposures() {
    let r;
    try {
      r = await this.api("GET", "/api/triggerbox/exposures");
    } catch {
      this._renderTiming();
      return;
    }
    if (r?.ok && r.data) {
      this.exposures = Array.isArray(r.data.cameras) ? r.data.cameras : [];
      if (Number.isFinite(r.data.guard_us)) this.guardUs = r.data.guard_us;
    }
    this._renderTiming();
  }

  _timingModel() {
    const s = this._getRecordSettings?.() || {};
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

  _renderTiming() {
    if (!this.timingViz) return;
    const m = this._timingModel();
    this.timingViz.innerHTML = this._buildSvg(m);
    this._renderSummary(m);
  }

  _renderSummary(m) {
    if (!this.timingSummary) return;
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

  _buildSvg(m) {
    const W = 1000, L = 150, R = 18, TOP = 10;
    const rowH = 22, barH = 12, axisH = 26;
    const plotW = W - L - R;
    const rows = Math.max(1, m.rows.length);
    const plotH = rows * rowH;
    const H = TOP + plotH + axisH;

    const X = (t) => L + (clamp(t, 0, m.periodUs) / m.periodUs) * plotW;
    const barW = (a, b) => Math.max(2, X(b) - X(a));
    const rowY = (i) => TOP + i * rowH + (rowH - barH) / 2;
    const midY = (i) => TOP + i * rowH + rowH / 2;
    const label = (i, text, cls = "") =>
      `<text x="${L - 8}" y="${midY(i)}" class="tb-label ${cls}" text-anchor="end" ` +
      `dominant-baseline="middle">${esc(text)}</text>`;
    const tag = (x, i, text, anchor = "start") =>
      `<text x="${x}" y="${midY(i)}" class="tb-tag" text-anchor="${anchor}" ` +
      `dominant-baseline="middle">${esc(text)}</text>`;

    const g = [`<rect x="${L}" y="${TOP}" width="${plotW}" height="${plotH}" class="tb-frame"/>`];

    m.rows.forEach((r, i) => {
      g.push(label(i, r.label));
      if (r.kind === "trigger") {
        g.push(
          `<rect x="${X(r.delayUs)}" y="${rowY(i)}" width="${barW(r.delayUs, r.delayUs + r.pulseUs)}" ` +
          `height="${barH}" class="tb-trigger"><title>pulse ${fmtDur(r.pulseUs)}` +
          `${r.delayUs ? `, delay ${fmtDur(r.delayUs)}` : ""}</title></rect>`
        );
      } else if (r.kind === "led") {
        if (r.onUs > 0) {
          g.push(
            `<rect x="${X(r.delayUs)}" y="${rowY(i)}" width="${barW(r.delayUs, r.delayUs + r.onUs)}" ` +
            `height="${barH}" class="tb-led"><title>on ${fmtDur(r.rawOnUs)}</title></rect>`
          );
        } else {
          g.push(tag(X(0), i, "off"));
        }
        if (r.guardFrom != null && r.delayUs + r.onUs > r.guardFrom && !r.continuous) {
          g.push(
            `<rect x="${X(r.guardFrom)}" y="${rowY(i)}" width="${barW(r.guardFrom, r.delayUs + r.onUs)}" ` +
            `height="${barH}" class="tb-guard"><title>guard</title></rect>`
          );
        }
        if (r.continuous) g.push(tag(X(m.periodUs) - 4, i, "continuous", "end"));
      } else if (r.kind === "pulse") {
        if (r.intervalUs > 0) {
          const pw = Math.min(r.pulseUs, r.intervalUs - 1);
          let drawn = 0;
          for (let t = r.startDelayUs; t < m.periodUs && drawn < 200; t += r.intervalUs, drawn++) {
            g.push(
              `<rect x="${X(t)}" y="${rowY(i)}" width="${barW(t, t + pw)}" height="${barH}" class="tb-pulse"/>`
            );
          }
          g.push(tag(X(m.periodUs) - 4, i, `${r.freqHz} Hz · independent`, "end"));
        } else {
          g.push(tag(X(0), i, "invalid frequency"));
        }
      } else if (r.kind === "exposure") {
        if (r.exposureUs == null) {
          g.push(tag(X(0), i, "exposure n/a"));
        } else {
          const gov = r.coverageUs === m.maxCoverage;
          g.push(
            `<rect x="${X(r.delayUs)}" y="${rowY(i)}" width="${barW(r.delayUs, r.delayUs + r.exposureUs)}" ` +
            `height="${barH}" class="tb-exposure${gov ? " tb-exposure--gov" : ""}">` +
            `<title>${esc(r.label)}: expose ${fmtDur(r.exposureUs)}` +
            `${r.delayUs ? `, trigger delay ${fmtDur(r.delayUs)}` : ""}</title></rect>`
          );
        }
      }
    });

    // Vertical guide at the longest-exposure end.
    if (m.maxCoverage != null && m.maxCoverage < m.periodUs) {
      g.push(
        `<line x1="${X(m.maxCoverage)}" y1="${TOP}" x2="${X(m.maxCoverage)}" y2="${TOP + plotH}" class="tb-guide"/>`
      );
    }

    const ay = TOP + plotH + 4;
    g.push(`<line x1="${L}" y1="${ay}" x2="${W - R}" y2="${ay}" class="tb-axis"/>`);
    g.push(`<text x="${L}" y="${ay + 14}" class="tb-axis-label" text-anchor="start">0</text>`);
    g.push(
      `<text x="${W - R}" y="${ay + 14}" class="tb-axis-label" text-anchor="end">${fmtDur(m.periodUs)}</text>`
    );

    return (
      `<svg viewBox="0 0 ${W} ${H}" class="tb-svg" preserveAspectRatio="xMidYMid meet" ` +
      `role="img" aria-label="triggerbox frame timing diagram">${g.join("")}</svg>`
    );
  }

  // --------------------------------------------------------- reconnect

  async _reconnect() {
    this.reconnectBtn.disabled = true;
    const device = this.portSelect?.value || "";
    let r;
    try {
      r = await this.api("POST", "/api/triggerbox/reconnect", device ? { device } : {});
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
    this.firmware = r.data?.firmware || null;
    this.armError = null; // a fresh (re)connect clears any stale arm failure
    if (typeof r.data?.firmware_ok === "boolean") this.firmwareOk = r.data.firmware_ok;
    if (r.data?.arduino_state) {
      this.arduinoState = r.data.arduino_state;
      this._renderState();
    }
    this._renderFirmware();
    this._refresh();
    this._loadPorts();
    if (this.ready && this.firmwareOk) {
      this.notify("info", `Serial port ${this.device} connected.`);
    } else if (this.ready && !this.firmwareOk) {
      this.notify("warning", `Connected, but ${this.firmware || "firmware"} is incompatible — reflash triggerbox.`);
    } else {
      this.notify(
        "warning",
        r.data?.error ? `Serial port still unavailable: ${r.data.error}` : "Serial port still unavailable."
      );
    }
  }
}
