// Benchmark tab: run a frame-rate diagnostic and render the per-stage verdict.

import { api } from "./util.js";

const BOTTLENECK_LABEL = {
  acquisition: "acquisition — the camera can't deliver frames fast enough",
  encode: "encoding — the encoder can't keep up",
  host: "host contention — shared USB bus / CPU / GIL",
  none: "none",
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmt(value, digits = 0) {
  return value == null ? "–" : Number(value).toFixed(digits);
}

export class BenchmarkTab {
  constructor({ notify }) {
    this.notify = notify;
    this.connected = false;
    this.running = false;
    this.requestPending = false;

    this.fields = document.getElementById("bench-fields");
    this.fps = document.getElementById("bench-fps");
    this.duration = document.getElementById("bench-duration");
    this.sink = document.getElementById("bench-sink");
    this.findMax = document.getElementById("bench-find-max");
    this.button = document.getElementById("bench-run");
    this.status = document.getElementById("bench-status");
    this.results = document.getElementById("bench-results");
    this.progress = document.getElementById("bench-progress");
    this.progressLabel = document.getElementById("bench-progress-label");
    this.progressFill = document.getElementById("bench-progress-fill");

    this.button.addEventListener("click", () => this._onButton());
    this.updateControls();
  }

  setConnected(connected) {
    this.connected = connected;
    this.updateControls();
  }

  // Keep the target-fps placeholder in step with the Record tab's fps so a blank
  // field visibly means "use the current recording fps".
  applySettings(s) {
    if (s && typeof s.fps === "number") {
      this.fps.placeholder = `Record FPS (${s.fps})`;
    }
  }

  applyState(snap) {
    if (snap.settings) this.applySettings(snap.settings);
    const running = snap.state === "diagnosing";
    if (running !== this.running) {
      this.running = running;
      if (running && !this.results.dataset.forThisRun) {
        this.status.textContent = "Benchmark running…";
        this.status.className = "";
        this._showProgress();
      }
      if (!running) this._hideProgress();
      this.updateControls();
    }
  }

  _showProgress() {
    this.progress.hidden = false;
    this.progressLabel.textContent = "Starting…";
    this._progressGoal = 0;
    const fill = this.progressFill;
    fill.style.transition = "none";
    fill.style.width = "0%";
    void fill.offsetWidth; // commit 0% so the first animation eases up from empty
  }

  _hideProgress() {
    this.progress.hidden = true;
  }

  // A structured progress update (msg.type === "diagnostics_progress"). Animates
  // the bar from its CURRENT width toward the phase's end target over the phase's
  // expected duration. The goal is clamped monotonic and the animation is never
  // reset to the phase start, so the bar keeps moving forward — repeated updates
  // within a phase (the max-fps probes) no longer snap it backward.
  applyProgress(msg) {
    if (this.results.dataset.forThisRun) return; // a report is already shown
    this.progress.hidden = false;
    const clamp = (v) => Math.max(0, Math.min(1, Number(v) || 0));
    const goal = Math.max(
      this._progressGoal || 0,
      clamp(msg.target != null ? msg.target : msg.fraction)
    );
    this._progressGoal = goal;
    if (msg.phase === "Done") {
      this.progressLabel.textContent = "Finishing…";
    } else {
      const label = msg.detail ? `${msg.phase} — ${msg.detail}` : msg.phase;
      this.progressLabel.textContent = `${label} · ${Math.round(goal * 100)}%`;
    }
    const eta = Math.max(0, Number(msg.eta_s) || 0);
    const fill = this.progressFill;
    fill.style.transition = `width ${eta > 0 ? eta : 0.3}s linear`;
    fill.style.width = `${goal * 100}%`;
  }

  updateControls() {
    this.fields.disabled = this.running || !this.connected;
    this.button.textContent = this.running ? "Cancel benchmark" : "Run benchmark";
    this.button.disabled = !this.connected || this.requestPending;
    this.button.classList.toggle("stop", this.running);
  }

  async _onButton() {
    if (!this.connected || this.requestPending) return;
    this.requestPending = true;
    this.updateControls();
    try {
      if (this.running) {
        await api("POST", "/api/diagnostics/cancel");
      } else {
        await this._run();
      }
    } catch {
      this.notify("error", "Benchmark request failed: server unreachable");
    } finally {
      this.requestPending = false;
      this.updateControls();
    }
  }

  async _run() {
    const body = {
      duration_s: Number(this.duration.value) || 5,
      find_max: this.findMax.checked,
      sink: this.sink.value,
    };
    const fps = Number(this.fps.value);
    if (fps > 0) body.target_fps = fps;
    this.results.replaceChildren();
    delete this.results.dataset.forThisRun;
    this.status.textContent = "Starting benchmark…";
    this.status.className = "";
    this._showProgress();
    const r = await api("POST", "/api/diagnostics/run", body);
    if (r.status !== 202) {
      this.status.textContent = "";
      this._hideProgress();
      this.notify(
        "error",
        r.data?.message || `Benchmark could not start (HTTP ${r.status})`
      );
    }
  }

  // A finished report pushed over the WebSocket (msg.type === "diagnostics").
  applyReport(rep) {
    this.status.textContent = "";
    this._hideProgress();
    this.results.dataset.forThisRun = "1";
    this.results.replaceChildren(...this._render(rep));
  }

  _render(rep) {
    const nodes = [];

    // Verdict banner.
    const verdict = el("div", "bench-verdict");
    if (rep.achievable) {
      verdict.classList.add("ok");
      verdict.textContent = `✓ ${fmt(rep.target_fps, 0)} fps is achievable`;
    } else {
      verdict.classList.add("bad");
      const label = BOTTLENECK_LABEL[rep.bottleneck] || rep.bottleneck;
      verdict.textContent =
        `✗ ${fmt(rep.target_fps, 0)} fps is not achievable — bottleneck: ${label}`;
    }
    nodes.push(verdict);

    // Ceilings + max.
    const summary = el("div", "bench-summary");
    if (rep.ceilings) {
      let line = `Acquisition ceiling: ${fmt(rep.ceilings.grab_min)} fps/cam (software)`;
      if (rep.ceilings.freerun_min != null) {
        line += ` · free-run ${fmt(rep.ceilings.freerun_min)} fps/cam`;
      }
      if (rep.ceilings.encode_min != null) {
        line += ` · encode ${fmt(rep.ceilings.encode_min)} fps/cam`;
      }
      summary.append(el("div", null, line));
    }
    if (rep.measured_max_fps != null) {
      const conf = rep.max_confirmed ? "confirmed" : "safety margin";
      summary.append(
        el(
          "div",
          null,
          `Stable max (measured): ${fmt(rep.measured_max_fps)} fps/cam ` +
            `(${conf}; predicted ${fmt(rep.predicted_max_fps)})`
        )
      );
    } else if (rep.predicted_max_fps != null) {
      summary.append(
        el("div", null, `Predicted max: ${fmt(rep.predicted_max_fps)} fps/cam`)
      );
    }
    if (rep.hardware_max_fps != null) {
      summary.append(
        el(
          "div",
          null,
          `External/hardware-trigger max (free-run): ` +
            `${fmt(rep.hardware_max_fps)} fps/cam`
        )
      );
    }
    nodes.push(summary);

    // Per-camera per-stage table.
    const table = el("table", "bench-table");
    const head = el("tr");
    for (const h of [
      "camera",
      "size",
      "fps",
      "drop%",
      "qmax",
      "acquire p50/p99",
      "encode p50/p99",
    ]) {
      head.append(el("th", null, h));
    }
    table.append(head);
    for (const t of rep.trials || []) {
      const acq = t.stages?.acquire;
      const enc = t.stages?.encode;
      const row = el("tr");
      const cells = [
        t.name,
        `${t.width}×${t.height}`,
        fmt(t.achieved_fps, 1),
        fmt(100 * t.drop_rate, 2),
        String(t.max_queue_depth),
        acq ? `${fmt(acq.p50_ms, 1)}/${fmt(acq.p99_ms, 1)} ms` : "–",
        enc && enc.samples ? `${fmt(enc.p50_ms, 2)}/${fmt(enc.p99_ms, 2)} ms` : "–",
      ];
      cells.forEach((c, i) => row.append(el("td", i === 0 ? "bench-cam" : null, c)));
      table.append(row);
    }
    nodes.push(table);

    // Host probes.
    const extras = [];
    if (rep.cpu_percent != null) extras.push(`cpu ${fmt(rep.cpu_percent)}%`);
    if (rep.jitter_p99_ms != null)
      extras.push(`scheduler jitter p99 ${fmt(rep.jitter_p99_ms, 2)} ms`);
    if (extras.length) nodes.push(el("div", "bench-extras", extras.join(" · ")));

    // Recommendations + notes.
    for (const rec of rep.recommendations || []) {
      nodes.push(el("div", "bench-rec", `→ ${rec}`));
    }
    for (const note of rep.notes || []) {
      nodes.push(el("div", "bench-note", note));
    }
    return nodes;
  }
}
