// Arduino tab: stepper loop command + hold-to-jog position adjustment.

import { api, clampInput } from "./util.js";

const STEPS_PER_REVOLUTION = 4096;

export class ArduinoTab {
  constructor({ send, notify }) {
    this.send = send; // sends a JSON message over the WS
    this.notify = notify;
    this.jogTimer = null;

    this.dirCw = document.getElementById("loop-dir-cw");
    this.steps = document.getElementById("loop-steps");
    this.interval = document.getElementById("loop-interval");
    this.rest = document.getElementById("loop-rest");
    this.repeats = document.getElementById("loop-repeats");
    this.wait = document.getElementById("loop-wait");
    this.info = document.getElementById("loop-info");
    this.withRecording = document.getElementById("loop-with-recording");
    this.jogInterval = document.getElementById("jog-interval");

    for (const input of [
      this.steps,
      this.interval,
      this.rest,
      this.repeats,
      this.wait,
    ]) {
      input.addEventListener("input", () => this.updateInfo());
      input.addEventListener("change", () => {
        clampInput(input);
        this.updateInfo();
      });
    }
    document
      .getElementById("loop-execute")
      .addEventListener("click", () => this._execute());

    this._setupJog(document.getElementById("jog-ccw"), -1);
    this._setupJog(document.getElementById("jog-cw"), 1);

    this.updateInfo();
  }

  // -------------------------------------------------------------- loop

  _read(input) {
    const v = parseInt(input.value, 10);
    if (!Number.isFinite(v)) return null;
    return Math.min(Number(input.max), Math.max(Number(input.min), v));
  }

  _loopValues() {
    const steps = this._read(this.steps);
    const interval = this._read(this.interval);
    const rest = this._read(this.rest);
    const repeats = this._read(this.repeats);
    const wait = this._read(this.wait);
    if ([steps, interval, rest, repeats, wait].some((v) => v === null)) {
      return null;
    }
    return { steps, interval, rest, repeats, wait };
  }

  command() {
    const v = this._loopValues();
    if (!v) return null;
    const direction = this.dirCw.checked ? 1 : -1;
    return {
      n_steps: direction * v.steps,
      step_interval_us: v.interval,
      rest_duration_ms: v.rest,
      n_repeats: v.repeats,
      init_wait_duration_s: v.wait,
    };
  }

  // Command to attach to /api/recording/start, or null.
  getStartCommand() {
    return this.withRecording.checked ? this.command() : null;
  }

  updateInfo() {
    const v = this._loopValues();
    if (!v) {
      this.info.textContent = "";
      return;
    }
    const durationUs = v.interval * v.steps;
    const rpm = 60_000_000 / (STEPS_PER_REVOLUTION * v.interval);
    const totalUs =
      (durationUs + v.rest * 1000) * v.repeats * 2 +
      v.wait * 1e6 -
      v.rest * 1000;
    this.info.textContent = `Total duration: ${(totalUs / 1e6).toFixed(
      3
    )} s, RPM: ${rpm.toFixed(3)}`;
  }

  async _execute() {
    const cmd = this.command();
    if (!cmd) return;
    let r;
    try {
      r = await api("POST", "/api/serial/command", cmd);
    } catch {
      this.notify("error", "Serial command failed: server unreachable");
      return;
    }
    if (!r.ok) {
      this.notify(
        "error",
        r.data?.detail || `Serial command failed (HTTP ${r.status})`
      );
    }
  }

  // --------------------------------------------------------------- jog

  _setupJog(button, direction) {
    const start = () => {
      if (this.jogTimer !== null) return;
      const intervalMs = clampInput(this.jogInterval);
      this.send({ type: "jog", n_steps: direction });
      this.jogTimer = setInterval(
        () => this.send({ type: "jog", n_steps: direction }),
        intervalMs
      );
    };
    button.addEventListener("pointerdown", start);
    button.addEventListener("pointerup", () => this.stopJog());
    button.addEventListener("pointercancel", () => this.stopJog());
    button.addEventListener("pointerleave", () => this.stopJog());
  }

  stopJog() {
    if (this.jogTimer === null) return;
    clearInterval(this.jogTimer);
    this.jogTimer = null;
    this.send({ type: "jog", n_steps: 0 });
  }
}
