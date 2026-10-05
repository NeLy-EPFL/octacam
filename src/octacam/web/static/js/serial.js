// The base of every plugin tab (a serial board): the link-status block with
// its port picker and Reconnect button, and the "firmware out of date — Flash
// firmware" banner, followed by the plugin's own markup. Plugin modules import
// this by the absolute path "/js/serial.js" (a relative one resolves under
// /plugins/<name>/).
//
// The app constructs a tab as new Tab({name, panel, status, send, notify,
// getRecordSettings}) into the empty `panel` it made from the class's static
// `label` and `title`, then calls setConnected, applyStatus (a /api/system
// plugin status: the init's push, which is also the only sign that the cameras
// opened), applyState (a "<name>_state" WebSocket message) and getStartParams.

import { el, request } from "/js/util.js";

export class SerialTab {
  // `body` is the tab's own markup; the subclass calls start(status) once its
  // own state is set up.
  constructor({ name, panel, notify }, body, reconnectUrl = `/api/${name}/reconnect`) {
    this.name = name;
    this.notify = notify;
    this.reconnectUrl = reconnectUrl;
    this.connected = false;
    this.ready = false;
    this.device = "";
    this.error = null; // the last error toasted (toastError)
    this.fw = { firmware: null, state: null, needsFlash: false, canFlash: false, neededBuild: null };
    this.flashing = false;

    panel.innerHTML = `
      <div id="${name}-status" class="plugin-status hidden">
        <span id="${name}-status-msg"></span>
        <label class="serial-port-row" title="Choose which serial port to connect to">Port:
          <select id="${name}-port" class="serial-port-select"></select>
        </label>
        <button type="button" id="${name}-reconnect" class="btn"
          title="Connect to the selected serial port">Reconnect</button>
      </div>
      <div id="${name}-fw-flash" class="fw-flash hidden">
        <span id="${name}-fw-flash-msg"></span>
        <button type="button" id="${name}-fw-flash-btn" class="btn">Flash firmware</button>
      </div>
      <pre id="${name}-fw-flash-log" class="fw-log hidden"></pre>
      ${body}`;
    const byId = (suffix) => document.getElementById(`${name}-${suffix}`);
    this.statusBox = byId("status");
    this.statusMsg = byId("status-msg");
    this.portSelect = byId("port");
    this.reconnectBtn = byId("reconnect");
    this.fwBanner = byId("fw-flash");
    this.fwMsg = byId("fw-flash-msg");
    this.fwBtn = byId("fw-flash-btn");
    this.fwLog = byId("fw-flash-log");
    this.reconnectBtn.addEventListener("click", () => this.reconnect());
    this.fwBtn.addEventListener("click", () => this.flash());
  }

  start(status) {
    this.applyLink(status);
    this.refresh();
    this.loadPorts();
    this.loadFirmware();
  }

  setConnected(connected) {
    this.connected = connected;
    this.refresh();
  }

  // A /api/system plugin status, whose state field is `arduino_state`.
  applyStatus(info) {
    this.applyState({ ...info, state: info.arduino_state });
  }

  // A "<name>_state" WebSocket message.
  applyState(msg) {
    this.applyLink(msg);
    this.refresh();
  }

  getStartParams() {
    return null;
  }

  // True while a flash would interrupt the board (the banner hides).
  boardBusy() {
    return false;
  }

  // Fold the link fields every status, state push, reconnect and flash
  // response carries.
  applyLink(msg) {
    if (typeof msg.ready === "boolean") this.ready = msg.ready;
    if (msg.device) this.device = msg.device;
    if ("firmware" in msg) this.fw.firmware = msg.firmware || null;
    if ("firmware_state" in msg) this.fw.state = msg.firmware_state || null;
    if ("needs_flash" in msg) this.fw.needsFlash = Boolean(msg.needs_flash);
  }

  // Toast a newly raised error once, so the operator notices even off-tab.
  toastError(error) {
    const next = error || null;
    if (next && next !== this.error) this.notify("error", next);
    this.error = next;
  }

  // The status block's message, or null to hide it.
  linkMessage() {
    if (this.ready) return null;
    const where = this.device ? ` (${this.device})` : "";
    return (
      `Serial port${where} is not open — check the Arduino is plugged in ` +
      `and the device path matches the plugin config, then reconnect.`
    );
  }

  refresh() {
    const msg = this.linkMessage();
    this.statusBox.classList.toggle("hidden", msg == null);
    if (msg != null) this.statusMsg.textContent = msg;
    this.renderFirmware();
  }

  // Microcontroller-class ports, Arduinos first (a host can have dozens of
  // /dev/ttyS* that bury them); the current device is always listed and
  // selected, even when generic or "auto".
  async loadPorts() {
    const data = await request("GET", "/api/serial/ports");
    const ports = Array.isArray(data?.ports) ? data.ports : [];
    const current = this.device;
    const sorted = ports
      .filter((p) => p.likely_microcontroller)
      .sort((a, b) => (b.likely_arduino === true) - (a.likely_arduino === true));
    const options = sorted.map((p) =>
      option(p.device, `${p.board_name} — ${p.device}${p.likely_arduino ? " ★" : ""}`)
    );
    if (current && !sorted.some((p) => p.device === current)) {
      options.unshift(option(current, current === "auto" ? "auto (single detected board)" : current));
    }
    if (!options.length) options.push(option("", "no serial ports detected"));
    this.portSelect.replaceChildren(...options);
    if (current) this.portSelect.value = current;
  }

  async reconnect() {
    this.reconnectBtn.disabled = true;
    // No selection reopens the configured device.
    const device = this.portSelect.value;
    const d = await request("POST", this.reconnectUrl, device ? { device } : {}, {
      action: "Reconnect",
      notify: this.notify,
    });
    this.reconnectBtn.disabled = false;
    if (!d) return;
    this.error = null; // a reconnect clears a stale failure
    this.applyLink(d);
    this.refresh();
    this.loadPorts();
    this.loadFirmware();
    if (!this.ready) {
      this.notify(
        "warning",
        d.error ? `Serial port still unavailable: ${d.error}` : "Serial port still unavailable."
      );
      return;
    }
    const problem = this.linkMessage();
    this.notify(problem ? "warning" : "info", problem || `Serial port ${this.device} connected.`);
  }

  // ------------------------------------------------------------ firmware

  async loadFirmware() {
    const d = await request("GET", `/api/${this.name}/firmware`);
    if (!d) return;
    if ("state" in d) this.fw.state = d.state || null;
    this.fw.needsFlash = Boolean(d.needs_flash);
    this.fw.canFlash = Boolean(d.can_flash);
    this.fw.neededBuild = d.needed_build ?? null;
    if ("firmware" in d) this.fw.firmware = d.firmware || null;
    this.renderFirmware();
  }

  renderFirmware() {
    const fw = this.fw;
    const show = this.ready && fw.needsFlash && !this.boardBusy();
    this.fwBanner.classList.toggle("hidden", !show);
    if (!show) return;
    if (this.flashing) {
      this.fwMsg.textContent = "Flashing… compiling + uploading (~1 min). The board will reboot.";
    } else if (fw.state === "wrong_version" || fw.state === "wrong_board") {
      this.fwMsg.textContent =
        `Board firmware${fw.firmware ? ` (${fw.firmware})` : ""} is incompatible — ` +
        `flash the current firmware to enable arming.`;
    } else if (fw.state === "unidentified") {
      this.fwMsg.textContent =
        "The board sent no firmware identity — it may be blank or running old " +
        "firmware. Flash the current firmware?";
    } else {
      this.fwMsg.textContent =
        `Board firmware is out of date${fw.neededBuild ? ` (current build ${fw.neededBuild})` : ""}` +
        ` — flash the current firmware?`;
    }
    this.fwBtn.disabled = !fw.canFlash || this.flashing;
    this.fwBtn.textContent = this.flashing ? "Flashing…" : "Flash firmware";
    this.fwBtn.title = fw.canFlash
      ? "Compile and upload the current firmware to the board"
      : "arduino-cli or the sketch source is unavailable on the server — flash manually";
  }

  async flash() {
    if (this.flashing || !this.fw.canFlash) return;
    if (
      !window.confirm(
        "Compile and upload the current firmware to the board?\n\n" +
          "This takes about a minute; the board will reboot. Don't do this during a recording."
      )
    )
      return;
    this.flashing = true;
    this.fwLog.classList.remove("hidden");
    this.fwLog.textContent = "";
    this.renderFirmware();
    const d = await request("POST", `/api/${this.name}/flash`, {}, {
      action: "Flash",
      notify: this.notify,
    });
    this.flashing = false;
    if (!d) {
      this.renderFirmware();
      return;
    }
    if (d.log) this.fwLog.textContent = d.log;
    this.applyLink(d);
    const prov = d.provisioning || {};
    if ("state" in prov) this.fw.state = prov.state || null;
    this.fw.needsFlash = Boolean(prov.needs_flash);
    if ("can_flash" in prov) this.fw.canFlash = Boolean(prov.can_flash);
    if ("needed_build" in prov) this.fw.neededBuild = prov.needed_build ?? this.fw.neededBuild;
    this.notify(d.ok ? "info" : "error", d.message || (d.ok ? "Firmware flashed." : "Flash failed."));
    this.refresh();
    this.loadPorts();
  }
}

function option(value, text) {
  const opt = el("option", null, text);
  opt.value = value;
  return opt;
}
