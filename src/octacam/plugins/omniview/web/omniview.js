// omniview tab: Arduino trigger + LED-strobe status, arm-with-recording control,
// auto/manual strobe duty, and a frame-timing visualization.
//
// Served from /plugins/omniview/, so it cannot import core "./util.js" (that would
// 404). The shared fetch helper (api) and clampInput are passed in via the ctx the
// host (app.js) constructs. The serial helpers live at /js/ (absolute path, since
// a relative import would resolve under /plugins/omniview/ and 404).
import { fetchSerialPorts, populatePortSelect } from "/js/serial.js";

const STATE_LABELS = {
  idle:    "Idle — waiting for arm command",
  running: "Running — triggering + strobing",
  done:    "Done",
};

const DEFAULT_GUARD_US = 100;
const FIRMWARE_DEFAULT_CAM_PULSE_US = 500; // matches kDefaultCamPulseUs in the .ino

// A short human duration: ms with 2 decimals once ≥1 ms, else whole µs.
function fmtDur(us) {
  if (!Number.isFinite(us)) return "–";
  return us >= 1000 ? `${(us / 1000).toFixed(2)} ms` : `${Math.round(us)} µs`;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );
}

export default class OmniviewTab {
  constructor({ notify, status, getRecordSettings, api, clampInput }) {
    this.notify = notify;
    this.api = api; // shared fetch helper (from util.js, injected by app.js)
    this.clampInput = clampInput;
    this._getRecordSettings = getRecordSettings;
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.firmware = status?.firmware || null;
    this.arduinoState = status?.arduino_state || "idle";
    this.connected = false;

    // Strobe-timing model. Camera exposures are fetched from the backend; the
    // rest seed from the plugin's configured defaults.
    this.mode = status?.duty_auto ? "auto" : "manual";
    this.guardUs = Number.isFinite(status?.guard_us) ? status.guard_us : DEFAULT_GUARD_US;
    this.camPulseUs = Number.isFinite(status?.cam_pulse_us) ? status.cam_pulse_us : 0;
    this.cameras = []; // [{index, name, exposure_us, trigger_delay_us}]

    this.statusBox    = document.getElementById("omniview-status");
    this.statusMsg    = document.getElementById("omniview-status-msg");
    this.reconnectBtn = document.getElementById("omniview-reconnect");
    this.portSelect   = document.getElementById("omniview-port");
    this.stateValue   = document.getElementById("omniview-state-value");
    this.firmwareEl   = document.getElementById("omniview-firmware");
    this.dutyInput    = document.getElementById("omniview-duty");
    this.dutyMode     = document.getElementById("omniview-duty-mode");
    this.dutyRow      = document.getElementById("omniview-duty-row");
    this.timingViz    = document.getElementById("omniview-timing-viz");
    this.timingSummary = document.getElementById("omniview-timing-summary");
    this.timingRefresh = document.getElementById("omniview-timing-refresh");
    this.armWithRec   = document.getElementById("omniview-arm-with-recording");

    // Seed the duty input + mode from the plugin's configured defaults.
    if (this.dutyInput && typeof status?.duty_percent === "number") {
      this.dutyInput.value = status.duty_percent;
    }
    if (this.dutyMode) this.dutyMode.value = this.mode;

    this.reconnectBtn.addEventListener("click", () => this._reconnect());
    this.dutyMode?.addEventListener("change", () => this._onModeChange());
    this.dutyInput?.addEventListener("input", () => this._renderTiming());
    this.timingRefresh?.addEventListener("click", () => this._loadExposures());
    // Re-read the Record-tab fps and redraw when this tab becomes visible.
    document.addEventListener("tab-shown", (e) => {
      if (e.detail?.tab === "omniview") this._renderTiming();
    });

    this._loadPorts();
    this._refresh();
    this._renderState();
    this._renderFirmware();
    this._applyMode();
    this._loadExposures(); // fetch exposures + first render
  }

  // Populate the port dropdown with the currently detected serial ports,
  // keeping the active device selected.
  async _loadPorts() {
    populatePortSelect(this.portSelect, await fetchSerialPorts(this.api), this.device);
  }

  // -------------------------------------------------- WS / connection state

  setConnected(connected) {
    const became = connected && !this.connected;
    this.connected = connected;
    this._refresh();
    // The exposures endpoint reads the cameras (not the Arduino), so refresh it
    // whenever the control socket (re)connects — camera exposures may have
    // changed while this client was away.
    if (became) this._loadExposures();
  }

  // Called by app.js when an "omniview_state" WS message arrives.
  applyState(msg) {
    this.arduinoState = msg.state || "idle";
    if (msg.device) this.device = msg.device;
    if ("firmware" in msg) {
      this.firmware = msg.firmware || null;
      this._renderFirmware();
    }
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

  // Returns {fps, duration_ms, duty_percent, duty_auto} for the recording start
  // request, or null when "arm with recording" is unchecked or the serial port
  // is not open. fps/duration come from the Record tab; duty from this tab. In
  // Auto mode the backend sizes the strobe from the live camera exposures and
  // ignores duty_percent (kept as the fallback if no exposure can be read).
  getStartParams() {
    if (!this.ready || !this.armWithRec?.checked) return null;
    const s = this._getRecordSettings?.();
    if (!s) return null;
    const fps = Math.max(1, Math.round(s.fps || 80));
    const duration_ms = Math.max(1, Math.round((s.duration_s || 10) * 1000));
    let duty_percent = parseFloat(this.dutyInput?.value);
    if (!Number.isFinite(duty_percent)) duty_percent = 20;
    duty_percent = Math.max(0, Math.min(100, duty_percent));
    return { fps, duration_ms, duty_percent, duty_auto: this.mode === "auto" };
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
    // Gate the arm controls on serial being open (state + timing display stay
    // visible so timing can be planned before the board is connected).
    const disabled = !this.ready || !this.connected;
    if (this.armWithRec) this.armWithRec.disabled = disabled;
    if (this.dutyInput) this.dutyInput.disabled = disabled;
    if (this.dutyMode) this.dutyMode.disabled = disabled;
    // The exposures endpoint needs the server, not the board.
    if (this.timingRefresh) this.timingRefresh.disabled = !this.connected;
  }

  _renderState() {
    const label = STATE_LABELS[this.arduinoState] ?? this.arduinoState;
    if (this.stateValue) {
      this.stateValue.textContent = label;
      this.stateValue.className = `omniview-state omniview-state--${this.arduinoState}`;
    }
  }

  _renderFirmware() {
    if (!this.firmwareEl) return;
    this.firmwareEl.textContent =
      this.ready && this.firmware ? `Board firmware: ${this.firmware}` : "";
  }

  // --------------------------------------------------------- strobe mode

  _onModeChange() {
    this.mode = this.dutyMode?.value === "manual" ? "manual" : "auto";
    this._applyMode();
    this._renderTiming();
  }

  _applyMode() {
    // Manual duty input only matters in manual mode.
    if (this.dutyRow) this.dutyRow.classList.toggle("hidden", this.mode !== "manual");
  }

  // --------------------------------------------------- exposures + timing

  async _loadExposures() {
    let r;
    try {
      r = await this.api("GET", "/api/omniview/exposures");
    } catch {
      this._renderTiming();
      return;
    }
    if (r?.ok && r.data) {
      this.cameras = Array.isArray(r.data.cameras) ? r.data.cameras : [];
      if (Number.isFinite(r.data.guard_us)) this.guardUs = r.data.guard_us;
      if (Number.isFinite(r.data.cam_pulse_us)) this.camPulseUs = r.data.cam_pulse_us;
    }
    this._renderTiming();
  }

  // Resolve the current frame-timing model from the Record-tab fps, the fetched
  // camera exposures, and the mode/duty. Pure — used by both the SVG + summary.
  _timingModel() {
    const s = this._getRecordSettings?.() || {};
    const fps = Math.max(1, Math.round(s.fps || 80));
    const periodUs = 1e6 / fps;
    const camPulseUs = Math.min(
      this.camPulseUs > 0 ? this.camPulseUs : FIRMWARE_DEFAULT_CAM_PULSE_US,
      periodUs
    );

    const cams = (this.cameras || []).map((c) => {
      const exp = Number.isFinite(c.exposure_us) ? c.exposure_us : null;
      const delay = Number.isFinite(c.trigger_delay_us) ? c.trigger_delay_us : 0;
      return {
        name: c.name ?? `cam${c.index}`,
        exposureUs: exp,
        delayUs: delay,
        coverageUs: exp == null ? null : delay + exp,
      };
    });
    const coverages = cams.map((c) => c.coverageUs).filter((v) => v != null);
    const maxCoverage = coverages.length ? Math.max(...coverages) : null;
    const autoLedOnUs = maxCoverage != null ? maxCoverage + this.guardUs : null;

    let dutyPercent = parseFloat(this.dutyInput?.value);
    if (!Number.isFinite(dutyPercent)) dutyPercent = 20;
    dutyPercent = Math.max(0, Math.min(100, dutyPercent));
    const manualLedOnUs = (dutyPercent / 100) * periodUs;

    const isAuto = this.mode === "auto";
    // In auto with no readable exposure the backend falls back to manual duty,
    // so mirror that here.
    const autoUsable = isAuto && autoLedOnUs != null;
    const ledOnUs = autoUsable ? autoLedOnUs : manualLedOnUs;
    const continuous = ledOnUs >= periodUs;

    return {
      fps, periodUs, camPulseUs, cams, maxCoverage,
      autoLedOnUs, manualLedOnUs, dutyPercent, isAuto, autoUsable,
      ledOnUs: Math.min(ledOnUs, periodUs), rawLedOnUs: ledOnUs, continuous,
      effDutyPercent: Math.min(100, (ledOnUs / periodUs) * 100),
    };
  }

  _renderTiming() {
    if (!this.timingViz) return;
    const m = this._timingModel();
    this.timingViz.innerHTML = this._buildSvg(m);
    this._renderSummary(m);
  }

  _renderSummary(m) {
    if (!this.timingSummary) return;
    const parts = [
      `${m.fps} fps · ${fmtDur(m.periodUs)} frame`,
      `LED on ${fmtDur(m.rawLedOnUs)}${m.continuous ? " (continuous)" : ""} · ` +
        `${m.effDutyPercent.toFixed(1)}% duty`,
    ];
    if (m.maxCoverage != null) {
      parts.push(
        `longest exposure window ${fmtDur(m.maxCoverage)} + ${fmtDur(this.guardUs)} guard`
      );
    }
    let note = "";
    if (m.isAuto && m.maxCoverage == null) {
      note =
        " — no camera exposures available; Auto will fall back to the manual duty.";
    } else if (m.continuous) {
      note = " — LED on-time ≥ frame period, so the lights stay on continuously.";
    } else if (m.maxCoverage != null && m.maxCoverage > m.periodUs) {
      note = " — exposure window exceeds the frame period; lower the fps or exposure.";
    }
    this.timingSummary.textContent = parts.join(" · ") + note;
  }

  // Build the frame-timing SVG. One frame period maps to the plot width; rows
  // are the trigger pulse, the LED strobe window (with its guard region), and
  // one exposure bar per camera. Colours come from core CSS vars via classes.
  _buildSvg(m) {
    const W = 1000, L = 132, R = 18, TOP = 10;
    const rowH = 22, barH = 12, axisH = 26;
    const plotW = W - L - R;
    const rows = 2 + Math.max(1, m.cams.length); // trigger + LED + cameras (min 1)
    const plotH = rows * rowH;
    const H = TOP + plotH + axisH;

    const X = (t) => L + (Math.max(0, Math.min(t, m.periodUs)) / m.periodUs) * plotW;
    const barW = (t0, t1) => Math.max(2, X(t1) - X(t0));
    const rowY = (i) => TOP + i * rowH + (rowH - barH) / 2;
    const label = (i, text, cls = "") =>
      `<text x="${L - 8}" y="${TOP + i * rowH + rowH / 2}" ` +
      `class="ov-label ${cls}" text-anchor="end" dominant-baseline="middle">${esc(text)}</text>`;

    const g = [];
    // Frame background (the whole period) + start/end guide.
    g.push(`<rect x="${L}" y="${TOP}" width="${plotW}" height="${plotH}" class="ov-frame"/>`);

    let row = 0;
    // Trigger pulse (D13): rising edge at 0, high for the pulse width.
    g.push(label(row, "Trigger D13"));
    g.push(
      `<rect x="${X(0)}" y="${rowY(row)}" width="${barW(0, m.camPulseUs)}" ` +
      `height="${barH}" class="ov-trigger"><title>trigger pulse ${fmtDur(m.camPulseUs)}</title></rect>`
    );
    row++;

    // LED strobe (D5/D6): on from 0 for the on-time; guard region shaded.
    g.push(label(row, "LED D5/D6"));
    if (m.ledOnUs > 0) {
      g.push(
        `<rect x="${X(0)}" y="${rowY(row)}" width="${barW(0, m.ledOnUs)}" ` +
        `height="${barH}" class="ov-led"><title>LED on ${fmtDur(m.rawLedOnUs)} ` +
        `(${m.effDutyPercent.toFixed(1)}%)</title></rect>`
      );
    } else {
      g.push(
        `<text x="${X(0)}" y="${TOP + row * rowH + rowH / 2}" class="ov-tag" ` +
        `dominant-baseline="middle">off (0%)</text>`
      );
    }
    // Guard = the slice of the LED window past the longest exposure end.
    if (m.maxCoverage != null && m.ledOnUs > m.maxCoverage) {
      g.push(
        `<rect x="${X(m.maxCoverage)}" y="${rowY(row)}" ` +
        `width="${barW(m.maxCoverage, m.ledOnUs)}" height="${barH}" ` +
        `class="ov-guard"><title>guard ${fmtDur(m.ledOnUs - m.maxCoverage)}</title></rect>`
      );
    }
    if (m.continuous) {
      g.push(
        `<text x="${X(m.periodUs) - 4}" y="${TOP + row * rowH + rowH / 2}" ` +
        `class="ov-tag" text-anchor="end" dominant-baseline="middle">continuous</text>`
      );
    }
    row++;

    // One exposure bar per camera (start = trigger delay, width = exposure).
    if (!m.cams.length) {
      g.push(label(row, "Exposure"));
      g.push(
        `<text x="${X(0)}" y="${TOP + row * rowH + rowH / 2}" class="ov-tag" ` +
        `dominant-baseline="middle">camera exposures unavailable</text>`
      );
      row++;
    } else {
      for (const c of m.cams) {
        g.push(label(row, c.name));
        if (c.exposureUs == null) {
          g.push(
            `<text x="${X(0)}" y="${TOP + row * rowH + rowH / 2}" class="ov-tag" ` +
            `dominant-baseline="middle">exposure n/a</text>`
          );
        } else {
          const gov = c.coverageUs === m.maxCoverage;
          g.push(
            `<rect x="${X(c.delayUs)}" y="${rowY(row)}" ` +
            `width="${barW(c.delayUs, c.delayUs + c.exposureUs)}" height="${barH}" ` +
            `class="ov-exposure${gov ? " ov-exposure--gov" : ""}">` +
            `<title>${esc(c.name)}: expose ${fmtDur(c.exposureUs)}` +
            `${c.delayUs ? `, trigger delay ${fmtDur(c.delayUs)}` : ""}</title></rect>`
          );
        }
        row++;
      }
    }

    // Vertical guides: LED-off edge, and (auto) the longest-exposure-end edge.
    const guide = (t, cls) =>
      `<line x1="${X(t)}" y1="${TOP}" x2="${X(t)}" y2="${TOP + plotH}" class="${cls}"/>`;
    if (m.maxCoverage != null && m.maxCoverage < m.periodUs) {
      g.push(guide(m.maxCoverage, "ov-guide"));
    }
    if (!m.continuous) g.push(guide(m.ledOnUs, "ov-guide ov-guide--led"));

    // Axis: baseline with 0 and period ticks.
    const ay = TOP + plotH + 4;
    g.push(`<line x1="${L}" y1="${ay}" x2="${W - R}" y2="${ay}" class="ov-axis"/>`);
    g.push(
      `<text x="${L}" y="${ay + 14}" class="ov-axis-label" text-anchor="start">0</text>`
    );
    g.push(
      `<text x="${W - R}" y="${ay + 14}" class="ov-axis-label" text-anchor="end">` +
      `${fmtDur(m.periodUs)}</text>`
    );

    return (
      `<svg viewBox="0 0 ${W} ${H}" class="ov-svg" preserveAspectRatio="xMidYMid meet" ` +
      `role="img" aria-label="omniview frame timing diagram">${g.join("")}</svg>`
    );
  }

  // --------------------------------------------------------- reconnect

  async _reconnect() {
    this.reconnectBtn.disabled = true;
    // Connect to the port picked in the dropdown (device override); with no
    // selection the backend reopens the configured device.
    const device = this.portSelect?.value || "";
    let r;
    try {
      r = await this.api("POST", "/api/omniview/reconnect", device ? { device } : {});
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
    if (r.data?.arduino_state) {
      this.arduinoState = r.data.arduino_state;
      this._renderState();
    }
    this._renderFirmware();
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
