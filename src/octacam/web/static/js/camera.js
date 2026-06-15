// Camera tab: per-camera sensor-parameter editing.
//
// Width/Height are applied server-side by transparently cycling the preview
// grab (pylon forbids the write while grabbing); exposure/gain/offset are
// written live. Every sensor param is locked while a recording is active.

import { api } from "./util.js";

const PARAMS = ["width", "height", "exposure", "gain", "offset_x", "offset_y"];

const trimNum = (v) => String(Math.round(v * 1000) / 1000);

function rangeHint(d) {
  const parts = [];
  if (d.min != null && d.max != null) {
    parts.push(`${trimNum(d.min)}–${trimNum(d.max)}`);
  }
  if (d.inc != null) parts.push(`inc ${trimNum(d.inc)}`);
  if (d.unit) parts.push(d.unit);
  if (d.writable === false) parts.push("read-only");
  return parts.join(", ");
}

export class CameraTab {
  // `cameras` is the /api/system camera list (each has index, serial, name,
  // width, height, params). `onSelect` syncs the grid's selected tile;
  // `onRename(index, name)` relabels the matching grid tile after a rename.
  constructor({ cameras, notify, onSelect, onRename }) {
    this.cameras = cameras;
    this.notify = notify;
    this.onSelect = onSelect;
    this.onRename = onRename;
    this.connected = false;
    this.recording = false;
    this.busy = false;
    this.selected = cameras.length ? 0 : -1;

    this.fields = document.getElementById("camera-fields");
    this.target = document.getElementById("cam-target");
    this.status = document.getElementById("camera-status");
    this.resetBtn = document.getElementById("cam-reset");
    this.inputs = {};
    this.ranges = {};
    for (const name of PARAMS) {
      this.inputs[name] = document.getElementById(`cam-${name}`);
      this.ranges[name] = document.getElementById(`cam-${name}-range`);
    }

    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = String(cam.index);
      opt.textContent = cam.name;
      this.target.appendChild(opt);
    }

    this.target.addEventListener("change", () => {
      this.selected = Number(this.target.value);
      this.onSelect?.(this.selected);
      this.render();
    });
    for (const name of PARAMS) {
      this.inputs[name].addEventListener("change", () => this._commit(name));
    }
    this.resetBtn.addEventListener("click", () => this._reset());

    this.render();
  }

  // ------------------------------------------------------ server -> UI

  // Called when the grid's selection changes (tile click) to keep the picker
  // in sync; does not re-fire onSelect.
  selectCamera(index) {
    if (index < 0 || index >= this.cameras.length) return;
    this.selected = index;
    this.target.value = String(index);
    this.render();
  }

  setConnected(connected) {
    this.connected = connected;
    this._updateDisabled();
  }

  setRecording(recording) {
    this.recording = recording;
    this.status.textContent = recording
      ? "Parameters are locked while recording."
      : "";
    this._updateDisabled();
  }

  // A camera_params broadcast (this client's own change, or another client's).
  applyParams(entry) {
    const cam = this.cameras[entry.index];
    if (!cam) return;
    if (entry.params) cam.params = { ...cam.params, ...entry.params };
    if (typeof entry.width === "number") cam.width = entry.width;
    if (typeof entry.height === "number") cam.height = entry.height;
    if (entry.index === this.selected) this.render();
  }

  // A camera_name broadcast (this client's own rename, or another client's):
  // relabel the picker option and grid tile, and refresh the name field.
  applyName(entry) {
    const cam = this.cameras[entry.index];
    if (!cam || typeof entry.name !== "string") return;
    cam.name = entry.name;
    const opt = this.target.querySelector(`option[value="${entry.index}"]`);
    if (opt) opt.textContent = entry.name;
    this.onRename?.(entry.index, entry.name);
    if (entry.index === this.selected) this.render();
  }

  render() {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    for (const name of PARAMS) {
      const input = this.inputs[name];
      const range = this.ranges[name];
      const d = cam.params?.[name];
      if (!d) {
        input.value = "";
        range.textContent = "unavailable";
        continue;
      }
      if (document.activeElement !== input) input.value = trimNum(d.value);
      if (d.min != null) input.min = d.min;
      else input.removeAttribute("min");
      if (d.max != null) input.max = d.max;
      else input.removeAttribute("max");
      input.step = d.inc != null ? d.inc : "any";
      range.textContent = rangeHint(d);
    }
    this._updateDisabled();
  }

  // -------------------------------------------------------------- helpers

  _scope() {
    const el = document.querySelector('input[name="cam-apply-to"]:checked');
    return el ? el.value : "selected";
  }

  _updateDisabled() {
    const locked = !this.connected || this.recording || this.busy;
    this.fields.disabled = locked;
    const cam = this.cameras[this.selected];
    if (!cam) return;
    for (const name of PARAMS) {
      const d = cam.params?.[name];
      // Per-field read-only when the node isn't writable (e.g. no Gain control).
      this.inputs[name].disabled = locked || !d || d.writable === false;
    }
  }

  // ------------------------------------------------------- UI -> server

  // Rename a camera from the grid's inline (double-click) tile editor. Sends
  // the PUT, then applies the result locally (picker option + grid tile via
  // applyName). Returns the canonical new name on success, or null on a no-op
  // or failure, so the editor knows whether to keep or revert. The server is
  // the real validator (uniqueness, safe filename stem) and rejects renames
  // while recording with 409.
  async renameCamera(index, name) {
    const cam = this.cameras[index];
    if (!cam) return null;
    const trimmed = name.trim();
    if (!trimmed || trimmed === cam.name) return null; // blank/unchanged: no-op
    let r;
    try {
      r = await api("PUT", `/api/cameras/${index}/name`, { name: trimmed });
    } catch {
      this.notify("error", "Rename failed: server unreachable");
      return null;
    }
    if (r.ok && r.data) {
      this.applyName(r.data);
      this.notify("info", `Renamed camera to ${r.data.name}`);
      return r.data.name;
    }
    this.notify("error", r.data?.detail || `Rename failed (HTTP ${r.status})`);
    return null;
  }

  async _commit(name) {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    const value = parseFloat(this.inputs[name].value);
    if (!Number.isFinite(value)) {
      this.render(); // revert a blank/garbage entry
      return;
    }
    const scope = this._scope();
    this.busy = true;
    this._updateDisabled();
    let r;
    try {
      r = await api("PUT", `/api/cameras/${cam.index}/params`, {
        name,
        value,
        scope,
      });
    } catch {
      this.notify("error", "Parameter update failed: server unreachable");
      this.busy = false;
      this.render();
      return;
    }
    this.busy = false;
    if (r.ok && r.data) {
      for (const entry of r.data.updated) this.applyParams(entry);
      this.render();
    } else {
      this.notify(
        "error",
        r.data?.detail || `Parameter update failed (HTTP ${r.status})`
      );
      this.render(); // snap back to the last device value
    }
  }

  // Reload the active config's saved sensor params onto the selected camera
  // (or all, per the Apply-to scope), discarding live edits.
  async _reset() {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    const scope = this._scope();
    this.busy = true;
    this._updateDisabled();
    let r;
    try {
      r = await api("POST", `/api/cameras/${cam.index}/params/reset`, { scope });
    } catch {
      this.notify("error", "Reset failed: server unreachable");
      this.busy = false;
      this.render();
      return;
    }
    this.busy = false;
    if (r.ok && r.data) {
      for (const entry of r.data.updated) this.applyParams(entry);
      this.render();
      this.notify(
        "info",
        scope === "all"
          ? "Reset all cameras to the saved config"
          : `Reset ${cam.name} to the saved config`
      );
    } else {
      this.notify("error", r.data?.detail || `Reset failed (HTTP ${r.status})`);
      this.render();
    }
  }
}
