// Flywheel tab: stepper loop command and hold-to-jog. Served from
// /plugins/flywheel/, so core helpers come in the ctx or by absolute /js/ path.
import { fetchSerialPorts, populatePortSelect } from "/js/serial.js";
import { FirmwareFlash } from "/js/firmware-flash.js";

const STEPS_PER_REVOLUTION = 4096;

export default class FlywheelTab {
  constructor({ send, notify, status, api, clampInput }) {
    this.send = send; // sends a JSON message over the WS
    this.notify = notify;
    this.api = api;
    this.clampInput = clampInput;
    this.jogging = false;
    // The controls need both the serial port (ready) and the socket (connected).
    this.ready = Boolean(status?.ready);
    this.device = status?.device || "";
    this.connected = false;

    this.fields = document.getElementById("flywheel-fields");
    this.statusBox = document.getElementById("flywheel-status");
    this.statusMsg = document.getElementById("flywheel-status-msg");
    this.reconnectBtn = document.getElementById("flywheel-reconnect");
    this.portSelect = document.getElementById("flywheel-port");
    this.reconnectBtn.addEventListener("click", () => this._reconnect());

    // Hidden while jogging: a flash resets the board.
    this.fw = new FirmwareFlash({
      api: this.api,
      notify: this.notify,
      prefix: "flywheel",
      ids: {
        banner: "flywheel-fw-flash",
        msg: "flywheel-fw-flash-msg",
        btn: "flywheel-fw-flash-btn",
        log: "flywheel-fw-flash-log",
      },
      isActive: () => this.jogging,
    });
    this.fw.setReady(this.ready);

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

    for (const input of [
      this.steps,
      this.interval,
      this.rest,
      this.repeats,
      this.wait,
    ]) {
      input.addEventListener("input", () => this.updateInfo());
      input.addEventListener("change", () => {
        this.clampInput(input);
        this.updateInfo();
      });
    }
    document
      .getElementById("loop-execute")
      .addEventListener("click", () => this._execute());

    this._setupJog(document.getElementById("jog-ccw"), -1);
    this._setupJog(document.getElementById("jog-cw"), 1);

    this._seedCommand(status?.command);
    this.updateInfo();
    this._loadPorts();
    this._refresh();
    this.fw.load();
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
      this.clampInput(input);
    }
  }

  async _loadPorts() {
    populatePortSelect(this.portSelect, await fetchSerialPorts(this.api), this.device);
  }

  // ----------------------------------------------------- serial state

  setConnected(connected) {
    this.connected = connected;
    if (!connected) this.stopJog();
    this._refresh();
  }

  // A /api/system plugin status (the init's push or a reconnect).
  applyStatus(info) {
    if (!info) return;
    this.ready = Boolean(info.ready);
    if (info.device) this.device = info.device;
    this._refresh();
  }

  _refresh() {
    this.fields.disabled = !this.connected || !this.ready;
    this.fw?.setReady(this.ready);
    if (this.ready) {
      this.statusBox.classList.add("hidden");
    } else {
      const where = this.device ? ` (${this.device})` : "";
      this.statusMsg.textContent =
        `Serial port${where} is not open — check the Arduino is plugged in ` +
        `and the device path is correct, then reconnect.`;
      this.statusBox.classList.remove("hidden");
    }
  }

  async _reconnect() {
    this.reconnectBtn.disabled = true;
    // No selection reopens the configured device.
    const device = this.portSelect?.value || "";
    let r;
    try {
      r = await this.api("POST", "/api/serial/reconnect", device ? { device } : {});
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
    this.fw.applyState(r.data);
    this.fw.load();
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

  async _execute() {
    const cmd = this.command();
    if (!cmd) return;
    let r;
    try {
      r = await this.api("POST", "/api/serial/command", cmd);
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
        interval_us: this.clampInput(this.jogInterval),
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
