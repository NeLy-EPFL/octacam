// Benchmark tab: run a frame-rate diagnostic and render the per-stage verdict.

import { api } from "./util.js";

const BOTTLENECK_LABEL = {
  acquisition: "acquisition — the camera can't deliver frames fast enough",
  transfer: "transfer — the cameras share more bus bandwidth than the link provides",
  encode: "encoding — the encoder can't keep up",
  host: "host contention — CPU / GIL",
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
    let r;
    try {
      r = await api("POST", "/api/diagnostics/run", body);
    } catch (e) {
      // The start request never reached the server, so no running-state
      // transition will ever fire to clear the bar — undo the optimistic UI
      // here, then rethrow so _onButton's catch still emits the notify.
      this.status.textContent = "";
      this._hideProgress();
      throw e;
    }
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
    const c = rep.ceilings;

    // ---- KEY RESULTS: verdict + the two headline max rates ----
    nodes.push(el("div", "bench-section-title", "Key results"));
    const verdict = el("div", "bench-verdict");
    if (rep.achievable) {
      verdict.classList.add("ok");
      verdict.textContent = `✓ ${fmt(rep.target_fps, 0)} fps is achievable`;
    } else {
      verdict.classList.add("bad");
      const label = BOTTLENECK_LABEL[rep.bottleneck] || rep.bottleneck;
      verdict.textContent =
        `✗ ${fmt(rep.target_fps, 0)} fps is not achievable — limited by ${label}`;
    }
    nodes.push(verdict);

    const maxes = el("div", "bench-summary");
    if (rep.measured_max_fps != null) {
      const conf = rep.max_confirmed ? "confirmed" : "safety margin";
      maxes.append(
        el(
          "div",
          null,
          `Synchronized (software) max: ${fmt(rep.measured_max_fps)} fps/cam (${conf})`
        )
      );
    } else if (rep.predicted_max_fps != null) {
      maxes.append(
        el(
          "div",
          null,
          `Synchronized (software) max: ${fmt(rep.predicted_max_fps)} fps/cam (predicted)`
        )
      );
    }
    if (rep.hardware_max_fps != null) {
      const measured = (rep.freerun_trials || []).length ? " (measured)" : "";
      maxes.append(
        el(
          "div",
          null,
          `Free-run / hardware max: ${fmt(rep.hardware_max_fps)} fps/cam${measured}`
        )
      );
    }
    nodes.push(maxes);

    // ---- BY STAGE: which pipeline stage caps the synchronized rate ----
    if (c) {
      nodes.push(
        el("div", "bench-section-title", "By stage — system ceiling = slowest camera")
      );
      const stages = el("div", "bench-summary");
      const stageRow = (name, text, key) => {
        const row = el("div", "bench-stage");
        row.append(el("span", null, text));
        if (rep.bottleneck === key) row.append(el("span", "bench-limits", " ← limits"));
        stages.append(row);
      };
      stageRow(
        "acquisition",
        `Acquisition: ${fmt(c.grab_min)} fps/cam (software; exposure+transfer serial)`,
        "acquisition"
      );
      if (rep.throughput_mbps_total != null) {
        const perCam = rep.n_cameras ? rep.throughput_mbps_total / rep.n_cameras : 0;
        const solo =
          c.grab_solo_min != null ? ` · alone ${fmt(c.grab_solo_min)} fps/cam` : "";
        stageRow(
          "transfer",
          `Transfer: ${fmt(perCam)} MB/s/cam · ${fmt(rep.throughput_mbps_total)} ` +
            `MB/s total (derived from frame size × fps${solo})`,
          "transfer"
        );
      }
      stageRow(
        "encode",
        c.encode_min != null
          ? `Encode: ${fmt(c.encode_min)} fps/cam`
          : "Encode: n/a (null sink — encoder not measured)",
        "encode"
      );
      nodes.push(stages);
    }

    // ---- BY CAMERA: per-camera ceilings + end-to-end trial detail ----
    nodes.push(el("div", "bench-section-title", "By camera"));
    const table = el("table", "bench-table");
    const head = el("tr");
    for (const h of [
      "camera",
      "size",
      "acq",
      "free",
      "enc",
      "fps",
      "drop%",
      "queue peak",
      "acquire p50/p99",
      "encode p50/p99",
    ]) {
      head.append(el("th", null, h));
    }
    table.append(head);
    for (const t of rep.trials || []) {
      const acq = t.stages?.acquire;
      const enc = t.stages?.encode;
      const s = t.serial;
      const row = el("tr");
      const cells = [
        t.name,
        `${t.width}×${t.height}`,
        fmt(c?.grab_fps?.[s]),
        fmt(c?.freerun_fps?.[s]),
        fmt(c?.encode_fps?.[s]),
        fmt(t.achieved_fps, 1),
        fmt(100 * t.drop_rate, 2),
        `${t.max_queue_depth} of 20`,
        acq ? `${fmt(acq.p50_ms, 1)}/${fmt(acq.p99_ms, 1)} ms` : "–",
        enc && enc.samples ? `${fmt(enc.p50_ms, 2)}/${fmt(enc.p99_ms, 2)} ms` : "–",
      ];
      cells.forEach((cell, i) =>
        row.append(el("td", i === 0 ? "bench-cam" : null, cell))
      );
      table.append(row);
    }
    nodes.push(table);
    nodes.push(
      el(
        "div",
        "bench-note",
        "acq/free/enc = per-camera ceilings (concurrent / free-run / encode); " +
          "fps/drop%/queue = the end-to-end trial at the target. drop% counts only " +
          "frames the encoder queue refused (host couldn't keep up), not camera " +
          "transport gaps."
      )
    );

    // ---- Free-run trial (real free-run pipeline, encoder in the loop) ----
    if ((rep.freerun_trials || []).length) {
      nodes.push(
        el(
          "div",
          "bench-section-title",
          "Free-run trial — real free-run pipeline, encoder in the loop"
        )
      );
      const ft = el("table", "bench-table");
      const fhead = el("tr");
      for (const h of ["camera", "fps", "drop%", "queue peak"]) {
        fhead.append(el("th", null, h));
      }
      ft.append(fhead);
      for (const t of rep.freerun_trials) {
        const row = el("tr");
        const cells = [
          t.name,
          fmt(t.achieved_fps, 1),
          fmt(100 * t.drop_rate, 2),
          `${t.max_queue_depth} of 20`,
        ];
        cells.forEach((cell, i) =>
          row.append(el("td", i === 0 ? "bench-cam" : null, cell))
        );
        ft.append(row);
      }
      nodes.push(ft);
    }

    // Host / machine probes.
    const extras = [];
    if (rep.system_cpu_percent != null)
      extras.push(`machine load (pre-run) ${fmt(rep.system_cpu_percent)}% cpu`);
    if (rep.cpu_percent != null) extras.push(`benchmark cpu ${fmt(rep.cpu_percent)}%`);
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
