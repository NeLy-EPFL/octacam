// Flywheel tab: stepper loop command and hold-to-jog.
import { SerialTab } from "/js/serial.js";
import { clampInput, request } from "/js/util.js";

const STEPS_PER_REVOLUTION = 4096;

const MARKUP = `
  <fieldset id="flywheel-fields" disabled>
    <h3 title="An automated sequence of back-and-forth turntable sweeps">Loop</h3>
    <div class="row" title="Rotation direction of the first sweep"><span>Initial direction:</span>
      <span class="radio-group">
        <label title="Start counter-clockwise"><input type="radio" name="loop-dir" id="loop-dir-ccw" checked> &#8634;</label>
        <label title="Start clockwise"><input type="radio" name="loop-dir" id="loop-dir-cw"> &#8635;</label>
      </span>
    </div>
    <label class="row" title="Number of motor steps in each sweep"><span>Steps:</span>
      <input id="loop-steps" type="number" min="2" max="32767" value="4096">
    </label>
    <label class="row" title="Delay between motor steps, in microseconds (smaller = faster rotation)"><span>Step interval:</span>
      <span class="suffixed">
        <input id="loop-interval" type="number" min="800" max="65535" value="1465">
        <span class="suffix">&micro;s</span>
      </span>
    </label>
    <label class="row" title="Pause at the end of each sweep before reversing, in milliseconds"><span>Rest duration:</span>
      <span class="suffixed">
        <input id="loop-rest" type="number" min="0" max="65535" value="1000">
        <span class="suffix">ms</span>
      </span>
    </label>
    <label class="row" title="How many sweeps to run in the loop"><span>Repeats:</span>
      <input id="loop-repeats" type="number" min="1" max="255" value="3">
    </label>
    <label class="row" title="Delay before the first sweep starts, in seconds"><span>Initial wait:</span>
      <span class="suffixed">
        <input id="loop-wait" type="number" min="0" max="255" value="10">
        <span class="suffix">s</span>
      </span>
    </label>
    <div id="loop-info"></div>
    <button type="button" id="loop-execute" class="btn wide"
      title="Send the configured loop program to the Arduino and run it now">Execute</button>
    <label class="center" title="Run the loop automatically whenever a recording starts">
      <input type="checkbox" id="loop-with-recording" checked> Start with recording</label>

    <h3 title="Manually nudge the turntable to a starting position">Adjust position</h3>
    <label class="row" title="Delay between steps while jogging, in microseconds (smaller = faster)"><span>Step interval:</span>
      <span class="suffixed">
        <input id="jog-interval" type="number" min="1000" max="65535" value="2000">
        <span class="suffix">&micro;s</span>
      </span>
    </label>
    <div class="row" title="Hold a button to rotate the turntable; release to stop"><span>Direction:</span>
      <span class="btn-pair">
        <button type="button" id="jog-ccw" class="btn" title="Hold to jog counter-clockwise">&#8634;</button>
        <button type="button" id="jog-cw" class="btn" title="Hold to jog clockwise">&#8635;</button>
      </span>
    </div>
  </fieldset>`;

export default class FlywheelTab extends SerialTab {
  static label = "Flywheel";
  static title = "Drive the turntable stepper via the Flywheel plugin";

  constructor(ctx) {
    super(ctx, MARKUP, "/api/serial/reconnect");
    this.send = ctx.send; // sends a JSON message over the WS
    this.jogging = false;

    this.fields = document.getElementById("flywheel-fields");
    this.dirCw = document.getElementById("loop-dir-cw");
    this.dirCcw = document.getElementById("loop-dir-ccw");
    this.steps = document.getElementById("loop-steps");
    this.interval = document.getElementById("loop-interval");
    this.rest = document.getElementById("loop-rest");
    this.repeats = document.getElementById("loop-repeats");
    this.wait = document.getElementById("loop-wait");
    this.info = document.getElementById("loop-info");
    this.withRecording = document.getElementById("loop-with-recording");
    this.jogInterval = document.getElementById("jog-interval");

    for (const input of [this.steps, this.interval, this.rest, this.repeats, this.wait]) {
      input.addEventListener("input", () => this.updateInfo());
      input.addEventListener("change", () => {
        clampInput(input);
        this.updateInfo();
      });
    }
    document.getElementById("loop-execute").addEventListener("click", () => this._execute());

    this._setupJog(document.getElementById("jog-ccw"), -1);
    this._setupJog(document.getElementById("jog-cw"), 1);

    this._seedCommand(ctx.status.command);
    this.updateInfo();
    this.start(ctx.status);
  }

  // A flash resets the board.
  boardBusy() {
    return this.jogging;
  }

  // Seed the loop from the configured command (options.command, also restored
  // from a recording's snapshot). Only at construction: a later status push
  // must never overwrite what the operator is editing.
  _seedCommand(command) {
    if (!command) return;
    const steps = Number(command.n_steps);
    if (Number.isFinite(steps)) {
      // The sign is the initial direction; the field itself is unsigned.
      this.dirCw.checked = steps >= 0;
      this.dirCcw.checked = steps < 0;
      this.steps.value = String(Math.abs(steps));
    }
    const fields = [
      [this.interval, command.step_interval_us],
      [this.rest, command.rest_duration_ms],
      [this.repeats, command.n_repeats],
      [this.wait, command.init_wait_duration_s],
    ];
    for (const [input, value] of fields) {
      if (Number.isFinite(Number(value))) input.value = String(Number(value));
    }
    for (const input of [this.steps, this.interval, this.rest, this.repeats, this.wait]) {
      clampInput(input);
    }
  }

  setConnected(connected) {
    if (!connected) this.stopJog();
    super.setConnected(connected);
  }

  refresh() {
    super.refresh();
    this.fields.disabled = !this.connected || !this.ready;
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

  // The recording start's plugin_params.flywheel, or null (also when the port
  // isn't open: there is no board to drive).
  getStartParams() {
    if (!this.ready) return null;
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

  _execute() {
    const cmd = this.command();
    if (cmd) request("POST", "/api/serial/command", cmd, { action: "Serial command", notify: this.notify });
  }

  // --------------------------------------------------------------- jog

  // A hold sends one start and one stop; the server's pulse clock paces the
  // steps.
  _setupJog(button, direction) {
    button.addEventListener("pointerdown", (e) => {
      if (this.jogging) return;
      // Capture so the hold survives the cursor leaving the button and the
      // other jog button can't steal events mid-hold.
      try {
        button.setPointerCapture(e.pointerId);
      } catch {
        /* capture unsupported; pointerup still stops the jog */
      }
      this.jogging = true;
      this.send({
        type: "jog",
        action: "start",
        direction,
        interval_us: clampInput(this.jogInterval),
      });
    });
    const stop = (e) => {
      if (button.hasPointerCapture?.(e.pointerId)) {
        button.releasePointerCapture(e.pointerId);
      }
      this.stopJog();
    };
    button.addEventListener("pointerup", stop);
    button.addEventListener("pointercancel", stop);
  }

  stopJog() {
    if (!this.jogging) return;
    this.jogging = false;
    this.send({ type: "jog", action: "stop" });
  }
}
