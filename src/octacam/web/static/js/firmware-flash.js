// Reusable "firmware out of date — Flash firmware" banner for serial-plugin tabs.
//
// A serial plugin whose board carries a firmware fingerprint (triggerbox,
// twophoton, flywheel) exposes GET/POST /api/<name>/firmware|flash. This controller
// drives a small banner + button from that: it shows when the board needs
// (re)flashing, runs the compile+upload, and reflects the result. Import it from a
// plugin tab (served at /plugins/<name>/, so use the absolute /js/ path):
//
//   import { FirmwareFlash } from "/js/firmware-flash.js";
//   this.fw = new FirmwareFlash({ api, notify, prefix: "twophoton",
//       ids: { banner: "twophoton-fw-flash", msg: "twophoton-fw-flash-msg",
//              btn: "twophoton-fw-flash-btn", log: "twophoton-fw-flash-log" },
//       isActive: () => this.arduinoState === "armed" || this.arduinoState === "triggered" });
//   // then: fw.load() on init/reconnect, fw.applyState(msg) on WS, fw.setReady(bool)

export class FirmwareFlash {
  constructor({ api, notify, prefix, ids, isActive }) {
    this.api = api;
    this.notify = notify;
    this.prefix = prefix;
    this.isActive = isActive || (() => false);
    this.flashing = false;
    this.state = {
      ready: false,
      firmware: null,
      firmwareState: null,
      needsFlash: false,
      canFlash: false,
      neededBuild: null,
    };
    this.banner = document.getElementById(ids.banner);
    this.msg = document.getElementById(ids.msg);
    this.btn = document.getElementById(ids.btn);
    this.log = ids.log ? document.getElementById(ids.log) : null;
    this.btn?.addEventListener("click", () => this.flash());
  }

  setReady(ready) {
    this.state.ready = Boolean(ready);
    this.render();
  }

  // Fold a WS <name>_state message (firmware / firmware_state / needs_flash / ready).
  applyState(msg) {
    if (!msg) return;
    if ("firmware" in msg) this.state.firmware = msg.firmware || null;
    if ("firmware_state" in msg) this.state.firmwareState = msg.firmware_state || null;
    if ("needs_flash" in msg) this.state.needsFlash = Boolean(msg.needs_flash);
    if (typeof msg.ready === "boolean") this.state.ready = msg.ready;
    this.render();
  }

  // Fold a reconnect/flash response body that carries the same fields.
  applyResponse(d) {
    if (!d) return;
    if ("firmware" in d) this.state.firmware = d.firmware || null;
    if ("firmware_state" in d) this.state.firmwareState = d.firmware_state || null;
    if ("needs_flash" in d) this.state.needsFlash = Boolean(d.needs_flash);
    if (typeof d.ready === "boolean") this.state.ready = d.ready;
    this.render();
  }

  async load() {
    let r;
    try {
      r = await this.api("GET", `/api/${this.prefix}/firmware`);
    } catch {
      return;
    }
    if (!r?.ok || !r.data) return;
    const d = r.data;
    if ("state" in d) this.state.firmwareState = d.state || null;
    this.state.needsFlash = Boolean(d.needs_flash);
    this.state.canFlash = Boolean(d.can_flash);
    this.state.neededBuild = d.needed_build ?? null;
    if ("firmware" in d) this.state.firmware = d.firmware || null;
    this.render();
  }

  render() {
    if (!this.banner) return;
    const s = this.state;
    const show = s.ready && s.needsFlash && !this.isActive();
    this.banner.classList.toggle("hidden", !show);
    if (!show) return;
    let m;
    if (s.firmwareState === "wrong_version" || s.firmwareState === "wrong_board") {
      m = `Board firmware${s.firmware ? ` (${s.firmware})` : ""} is incompatible — ` +
        `flash the current firmware to enable arming.`;
    } else if (s.firmwareState === "unidentified") {
      m = "The board sent no firmware identity — it may be blank or running old " +
        "firmware. Flash the current firmware?";
    } else {
      m = `Board firmware is out of date${s.neededBuild ? ` (current build ${s.neededBuild})` : ""}` +
        ` — flash the current firmware?`;
    }
    if (this.msg) this.msg.textContent = m;
    if (this.btn) {
      this.btn.disabled = !s.canFlash || this.flashing;
      this.btn.textContent = this.flashing ? "Flashing…" : "Flash firmware";
      this.btn.title = s.canFlash
        ? "Compile and upload the current firmware to the board"
        : "arduino-cli or the sketch source is unavailable on the server — flash manually";
    }
  }

  async flash() {
    if (this.flashing || !this.state.canFlash) return;
    if (
      !window.confirm(
        "Compile and upload the current firmware to the board?\n\n" +
          "This takes about a minute; the board will reboot. Don't do this during a recording."
      )
    )
      return;
    this.flashing = true;
    this.render();
    if (this.msg)
      this.msg.textContent = "Flashing… compiling + uploading (~1 min). The board will reboot.";
    if (this.log) {
      this.log.classList.remove("hidden");
      this.log.textContent = "";
    }
    let r;
    try {
      r = await this.api("POST", `/api/${this.prefix}/flash`, {});
    } catch {
      this.flashing = false;
      this.notify("error", "Flash failed: server unreachable");
      this.render();
      return;
    }
    this.flashing = false;
    if (!r.ok) {
      this.notify("error", r.data?.detail || `Flash failed (HTTP ${r.status})`);
      this.render();
      return;
    }
    const d = r.data || {};
    if (this.log && d.log) this.log.textContent = d.log;
    if ("firmware" in d) this.state.firmware = d.firmware || null;
    if (typeof d.ready === "boolean") this.state.ready = d.ready;
    const prov = d.provisioning || {};
    if ("state" in prov) this.state.firmwareState = prov.state || null;
    this.state.needsFlash = Boolean(prov.needs_flash);
    if ("can_flash" in prov) this.state.canFlash = Boolean(prov.can_flash);
    if ("needed_build" in prov) this.state.neededBuild = prov.needed_build ?? this.state.neededBuild;
    this.notify(d.ok ? "info" : "error", d.message || (d.ok ? "Firmware flashed." : "Flash failed."));
    this.render();
  }
}
