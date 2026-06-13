// Save dialog: persist sensor params (.pfs) and/or the display layout (.toml)
// to the active config dir or a new sibling config dir. The browser owns the
// display state (runtime rotate/flip + tile layout), so it sends it up.

import { api } from "./util.js";

export class SaveDialog {
  constructor({ grid, notify, getRecording }) {
    this.grid = grid;
    this.notify = notify;
    this.getRecording = getRecording; // () => bool
    this.busy = false;

    this.dialog = document.getElementById("save-dialog");
    this.nameRow = document.getElementById("save-name-row");
    this.nameInput = document.getElementById("save-name");
    this.sensor = document.getElementById("save-sensor");
    this.display = document.getElementById("save-display");
    this.error = document.getElementById("save-error");
    this.confirmBtn = document.getElementById("save-confirm");
    this.openBtn = document.getElementById("save-config-btn");

    this.openBtn.addEventListener("click", () => this.open());
    document
      .getElementById("save-cancel")
      .addEventListener("click", () => this.close());
    this.confirmBtn.addEventListener("click", () => this._save());
    this.dialog.addEventListener("click", (e) => {
      if (e.target === this.dialog) this.close();
    });
    for (const el of document.querySelectorAll('input[name="save-target"]')) {
      el.addEventListener("change", () => this._syncTarget());
    }
    this.sensor.addEventListener("change", () => this._syncButtons());
    this.display.addEventListener("change", () => this._syncButtons());
    this.nameInput.addEventListener("input", () => this._syncButtons());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.dialog.classList.contains("hidden")) {
        this.close();
      }
    });
  }

  setConnected(connected) {
    this.openBtn.disabled = !connected;
    if (!connected) this.close();
  }

  open() {
    if (this.getRecording()) {
      this.notify("warning", "Stop the recording before saving the config.");
      return;
    }
    this.error.textContent = "";
    this._syncTarget();
    this.dialog.classList.remove("hidden");
  }

  close() {
    this.dialog.classList.add("hidden");
  }

  _target() {
    const el = document.querySelector('input[name="save-target"]:checked');
    return el ? el.value : "active";
  }

  _syncTarget() {
    this.nameRow.classList.toggle("hidden", this._target() !== "new");
    this._syncButtons();
  }

  _syncButtons() {
    const needsName = this._target() === "new";
    const haveName = this.nameInput.value.trim().length > 0;
    const something = this.sensor.checked || this.display.checked;
    this.confirmBtn.disabled =
      this.busy || !something || (needsName && !haveName);
  }

  async _save() {
    const target = this._target();
    const body = {
      target,
      save_sensor: this.sensor.checked,
      save_display: this.display.checked,
      cameras: this.grid.getDisplayParams(),
    };
    if (target === "new") body.name = this.nameInput.value.trim();

    this.busy = true;
    this._syncButtons();
    let r = await this._post(body);
    if (r && r.status === 409 && target === "new") {
      const ok = window.confirm(
        `${r.data?.detail || "Config already exists"}\n\nOverwrite it?`
      );
      r = ok ? await this._post({ ...body, overwrite: true }) : null;
    }
    this.busy = false;
    this._syncButtons();

    if (r && r.ok) {
      // Fold runtime rotate/flip into the base so it isn't double-applied.
      if (this.display.checked) this.grid.commitRuntime();
      this.notify("info", `Configuration saved to ${r.data?.config_dir || target}`);
      this.close();
    } else if (r) {
      this.error.textContent = r.data?.detail || `Save failed (HTTP ${r.status})`;
    }
  }

  async _post(body) {
    try {
      return await api("POST", "/api/config/save", body);
    } catch {
      this.error.textContent = "Save failed: server unreachable";
      return null;
    }
  }
}
