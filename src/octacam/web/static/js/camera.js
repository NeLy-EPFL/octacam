// Camera tab: full device node-map browser.
//
// The set of parameters is model-dependent — the server walks each camera's
// GenApi node map (GET /api/cameras/{i}/features) and returns every readable
// feature, grouped by category, typed as int/float/bool/enum/string/command.
// Writable ones are editable with per-node validation (min/max/inc, enum
// entries); octacam-managed nodes (PixelFormat, TriggerMode, …) are shown
// locked; ROI offsets can be auto-centered. Every field has a "reset to config"
// (else factory default) button, and command nodes run behind a confirm.
//
// Width/Height are applied server-side by cycling the preview grab; other nodes
// are written live. A write can change other nodes' state, so the whole list is
// re-read from the server response. Everything is locked while recording.

import { api } from "./util.js";

const trimNum = (v) => {
  if (typeof v !== "number") return String(v);
  return String(Math.round(v * 1000) / 1000);
};

// GenICam visibility levels, least to most advanced. The level selector shows
// every feature at or below the chosen level (Guru includes all). The server
// walk never sends Invisible nodes, so only these three reach the browser.
const VIS_RANK = { beginner: 0, expert: 1, guru: 2 };

// A short "min–max, inc X, unit" hint for a numeric node.
function rangeHint(f) {
  const parts = [];
  if (f.min != null && f.max != null) {
    parts.push(`${trimNum(f.min)}–${trimNum(f.max)}`);
  }
  if (f.inc != null) parts.push(`inc ${trimNum(f.inc)}`);
  if (f.unit) parts.push(f.unit);
  return parts.join(", ");
}

// The offset nodes get an inline "center" toggle keyed to a center_* flag.
const CENTER_AXIS = { OffsetX: "x", OffsetY: "y" };

export class CameraTab {
  // `cameras` is the /api/system camera list (index, serial, name, center_x,
  // center_y). `onSelect` syncs the grid's selected tile; `onRename(index,
  // name)` relabels the matching grid tile after a rename.
  constructor({ cameras, notify, onSelect, onRename }) {
    this.cameras = cameras;
    this.notify = notify;
    this.onSelect = onSelect;
    this.onRename = onRename;
    this.connected = false;
    this.recording = false;
    this.busy = false;
    this.selected = cameras.length ? 0 : -1;

    // Per-camera feature payloads, fetched lazily and invalidated on a dirty
    // ping or disconnect. Categories the user collapsed persist across renders.
    this.featuresByIndex = {};
    this.collapsed = new Set();
    this.filter = "";
    this.visLevel = "beginner"; // max GenICam visibility shown (see VIS_RANK)
    // A write broadcasts camera_features_dirty to every client, including this
    // one. We already applied the authoritative response locally, so ignore the
    // echo (per camera index) for a short window instead of refetching. The
    // last focused feature name is restored across re-renders so committing a
    // field doesn't lose keyboard position.
    this._suppressDirtyUntil = {};
    this._lastFocusedFeature = null;

    this.fields = document.getElementById("camera-fields");
    this.target = document.getElementById("cam-target");
    this.status = document.getElementById("camera-status");
    this.params = document.getElementById("camera-params");
    this.nameInput = document.getElementById("cam-name");
    this.renameBtn = document.getElementById("cam-rename");
    this.filterInput = document.getElementById("cam-filter");
    this.visInput = document.getElementById("cam-visibility");

    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = String(cam.index);
      opt.textContent = cam.name;
      this.target.appendChild(opt);
    }

    this.target.addEventListener("change", () => {
      this.selected = Number(this.target.value);
      this._lastFocusedFeature = null; // don't carry focus across cameras
      this.onSelect?.(this.selected);
      this.render();
    });
    this.filterInput.addEventListener("input", () => {
      this.filter = this.filterInput.value.trim().toLowerCase();
      this._renderParams();
    });
    this.visInput.addEventListener("change", () => {
      this.visLevel = this.visInput.value;
      this._renderParams();
    });
    // Track the focused feature so a re-render (commit / soft refresh) can put
    // keyboard focus back where it was.
    this.params.addEventListener("focusin", (e) => {
      const row = e.target.closest?.(".cam-feat");
      this._lastFocusedFeature = row ? row.dataset.feature : null;
    });
    this.renameBtn.addEventListener("click", () => this._commitName());
    this.nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        this._commitName();
      } else if (e.key === "Escape") {
        e.preventDefault();
        const cam = this.cameras[this.selected];
        if (cam) this.nameInput.value = cam.name;
        this.nameInput.blur();
      }
    });

    this.render();
  }

  // ------------------------------------------------------ server -> UI

  selectCamera(index) {
    if (index < 0 || index >= this.cameras.length) return;
    if (index !== this.selected) this._lastFocusedFeature = null;
    this.selected = index;
    this.target.value = String(index);
    this.render();
  }

  setConnected(connected) {
    const was = this.connected;
    this.connected = connected;
    if (!connected) this.featuresByIndex = {}; // refetch fresh on reconnect
    if (connected && !was) this.render();
    else this._updateDisabled();
  }

  setRecording(recording) {
    this.recording = recording;
    this.status.textContent = recording
      ? "Parameters are locked while recording."
      : "";
    this._updateDisabled();
  }

  // A camera_features_dirty ping (this or another client changed a feature):
  // update the cached center flags and refresh if it's the selected camera.
  // The server broadcasts to every client including the one that made the
  // change, which already applied the authoritative response — so ignore the
  // echo of our own recent write instead of doing a redundant full refetch.
  applyFeaturesDirty(entry) {
    const cam = this.cameras[entry.index];
    if (cam) {
      if (typeof entry.center_x === "boolean") cam.center_x = entry.center_x;
      if (typeof entry.center_y === "boolean") cam.center_y = entry.center_y;
    }
    if (Date.now() < (this._suppressDirtyUntil[entry.index] || 0)) return;
    if (entry.index === this.selected) {
      this._loadFeatures(true); // soft: keep the current panel until fresh data lands
    } else {
      delete this.featuresByIndex[entry.index]; // refetch on next select
    }
  }

  // Legacy /api/cameras/{i}/params WS broadcast — the new UI drives everything
  // through /features, so this is a no-op kept only so an old broadcast (or
  // another tool hitting the legacy endpoint) can't error the client.
  applyParams() {}

  applyName(entry) {
    const cam = this.cameras[entry.index];
    if (!cam || typeof entry.name !== "string") return;
    cam.name = entry.name;
    const opt = this.target.querySelector(`option[value="${entry.index}"]`);
    if (opt) opt.textContent = entry.name;
    this.onRename?.(entry.index, entry.name);
    if (entry.index === this.selected && document.activeElement !== this.nameInput) {
      this.nameInput.value = entry.name;
    }
  }

  render() {
    const cam = this.cameras[this.selected];
    if (!cam) {
      this.params.replaceChildren();
      return;
    }
    if (document.activeElement !== this.nameInput) this.nameInput.value = cam.name;
    this.filterInput.value = this.filter;
    this.visInput.value = this.visLevel;
    if (this.connected && !this.featuresByIndex[this.selected]) {
      this._loadFeatures();
    } else {
      this._renderParams();
    }
    this._updateDisabled();
  }

  // `soft` refreshes an already-shown camera without flashing "Loading…" and,
  // on a transient failure, keeps the current (valid) panel rather than blanking
  // it — so a background dirty-ping refresh never destroys good on-screen state.
  async _loadFeatures(soft = false) {
    const index = this.selected;
    const hadCache = !!this.featuresByIndex[index];
    if (!soft || !hadCache) {
      this.params.replaceChildren(this._message("Loading parameters…"));
    }
    let r;
    try {
      r = await api("GET", `/api/cameras/${index}/features`);
    } catch {
      if (soft && hadCache) return;
      this.params.replaceChildren(this._message("Parameters unavailable: server unreachable"));
      return;
    }
    if (!r.ok || !r.data) {
      if (soft && hadCache) return;
      this.params.replaceChildren(this._message(`Parameters unavailable (HTTP ${r.status})`));
      return;
    }
    this.featuresByIndex[index] = r.data;
    const cam = this.cameras[index];
    if (cam) {
      cam.center_x = r.data.center_x;
      cam.center_y = r.data.center_y;
    }
    if (index === this.selected) this._renderParams();
  }

  // ----------------------------------------------------------- rendering

  _message(text) {
    const p = document.createElement("p");
    p.className = "cam-empty";
    p.textContent = text;
    return p;
  }

  _renderParams() {
    if (!this.connected) {
      this.params.replaceChildren(this._message("Connect to view camera parameters."));
      this._updateDisabled();
      return;
    }
    const payload = this.featuresByIndex[this.selected];
    if (!payload) return; // a load is in flight
    const features = payload.features || [];
    if (!features.length) {
      this.params.replaceChildren(
        this._message("This camera's backend does not expose a parameter list.")
      );
      return;
    }

    // Group by category, preserving first-seen order.
    const groups = new Map();
    for (const f of features) {
      if (!this._visible(f)) continue;
      const cat = f.category || "Other";
      if (!groups.has(cat)) groups.set(cat, []);
      groups.get(cat).push(f);
    }

    const frag = document.createDocumentFragment();
    if (!groups.size) {
      frag.appendChild(this._message("No parameters match the filter."));
    }
    const filtering = this.filter.length > 0;
    for (const [cat, items] of groups) {
      const details = document.createElement("details");
      details.className = "cam-group";
      // Filtering force-opens matching groups; otherwise honor the user's toggle.
      details.open = filtering || !this.collapsed.has(cat);
      const summary = document.createElement("summary");
      summary.textContent = `${cat} (${items.length})`;
      details.appendChild(summary);
      details.addEventListener("toggle", () => {
        if (filtering) return;
        if (details.open) this.collapsed.delete(cat);
        else this.collapsed.add(cat);
      });
      for (const f of items) details.appendChild(this._renderFeature(f));
      frag.appendChild(details);
    }
    this.params.replaceChildren(frag);
    this._updateDisabled();
    this._restoreFocus();
  }

  // Put keyboard focus back on the widget of the last-focused feature (if it is
  // still present and enabled) so a commit / refresh doesn't drop tab position.
  _restoreFocus() {
    const name = this._lastFocusedFeature;
    if (!name) return;
    const row = this.params.querySelector(`.cam-feat[data-feature="${CSS.escape(name)}"]`);
    const widget = row && row.querySelector("input, select, button");
    if (widget && !widget.disabled) widget.focus();
  }

  // Whether a feature passes the current filter + visibility-level selector.
  // Features above the chosen level (Beginner < Expert < Guru) are hidden.
  _visible(f) {
    if ((VIS_RANK[f.visibility] ?? 0) > (VIS_RANK[this.visLevel] ?? 0)) return false;
    if (this.filter) {
      const hay = `${f.name} ${f.display_name}`.toLowerCase();
      if (!hay.includes(this.filter)) return false;
    }
    return true;
  }

  _renderFeature(f) {
    const row = document.createElement("div");
    row.className = "cam-feat";
    row.dataset.feature = f.name;
    if (f.managed) row.classList.add("managed");

    const label = document.createElement("label");
    label.className = "cam-feat-label";
    label.textContent = f.display_name || f.name;
    if (f.tooltip) label.title = f.tooltip;

    const control = document.createElement("div");
    control.className = "cam-feat-control";

    const locked = this._locked(f);
    const widget = this._widget(f, locked);
    if (widget) control.appendChild(widget);

    // ROI offsets get an inline center toggle.
    const axis = CENTER_AXIS[f.name];
    if (axis) control.appendChild(this._centerToggle(axis));

    // Reset-to-default for editable value nodes (not commands/managed/read-only).
    if (f.type !== "command" && !f.managed && f.writable) {
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "cam-reset-field";
      reset.textContent = "↺";
      reset.title = "Reset to the saved config value (else the factory default)";
      reset.disabled = locked;
      reset.addEventListener("click", () => this._resetFeature(f.name));
      control.appendChild(reset);
    }

    const hint = document.createElement("span");
    hint.className = "cam-feat-hint";
    if (f.managed) hint.textContent = "🔒 managed by octacam";
    else if (!f.writable && f.type !== "command") hint.textContent = "read-only";
    else if (f.type === "int" || f.type === "float") hint.textContent = rangeHint(f);

    row.append(label, control, hint);
    return row;
  }

  // The typed input/select/button for one feature. `locked` folds in the
  // tab-wide lock (disconnected/recording/busy) on top of the per-node state.
  _widget(f, locked) {
    const disabled = locked || !f.writable;
    if (f.type === "command") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn cam-cmd";
      btn.textContent = "Run";
      btn.disabled = locked || !f.writable;
      btn.addEventListener("click", () => this._runCommand(f));
      return btn;
    }
    if (f.type === "bool") {
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = !!f.value;
      input.disabled = disabled;
      input.addEventListener("change", () => this._commit(f.name, input.checked));
      return input;
    }
    if (f.type === "enum") {
      const sel = document.createElement("select");
      sel.disabled = disabled;
      for (const e of f.entries || []) {
        const opt = document.createElement("option");
        opt.value = e.value;
        opt.textContent = e.display || e.value;
        if (e.available === false) opt.disabled = true;
        if (e.value === f.value) opt.selected = true;
        sel.appendChild(opt);
      }
      // A current value not in the entry list (rare) still shows.
      if (f.value != null && ![...sel.options].some((o) => o.value === f.value)) {
        const opt = document.createElement("option");
        opt.value = f.value;
        opt.textContent = f.value;
        opt.selected = true;
        sel.appendChild(opt);
      }
      sel.addEventListener("change", () => this._commit(f.name, sel.value));
      return sel;
    }
    if (f.type === "string") {
      const input = document.createElement("input");
      input.type = "text";
      input.value = f.value == null ? "" : String(f.value);
      input.spellcheck = false;
      input.disabled = disabled;
      input.addEventListener("change", () => this._commit(f.name, input.value));
      return input;
    }
    // int / float
    const input = document.createElement("input");
    input.type = "number";
    input.value = f.value == null ? "" : trimNum(f.value);
    input.disabled = disabled;
    if (f.min != null) input.min = f.min;
    if (f.max != null) input.max = f.max;
    input.step = f.inc != null ? f.inc : "any";
    input.addEventListener("change", () => {
      const v = parseFloat(input.value);
      if (!Number.isFinite(v)) {
        this._renderParams(); // revert blank/garbage
        return;
      }
      this._commit(f.name, v);
    });
    return input;
  }

  _centerToggle(axis) {
    const cam = this.cameras[this.selected];
    const wrap = document.createElement("label");
    wrap.className = "cam-center";
    wrap.title = "Auto-center the ROI on this axis (offset is computed from the sensor and image size)";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = !!(cam && cam[`center_${axis}`]);
    box.disabled = !this.connected || this.recording || this.busy;
    box.addEventListener("change", () => this._toggleCenter(axis, box.checked));
    const text = document.createElement("span");
    text.textContent = "center";
    wrap.append(box, text);
    return wrap;
  }

  // -------------------------------------------------------------- helpers

  // A node is locked if the tab is locked, or the node is managed/read-only, or
  // it's an offset whose axis is auto-centered.
  _locked(f) {
    if (!this.connected || this.recording || this.busy) return true;
    if (f.managed) return true;
    const axis = CENTER_AXIS[f.name];
    if (axis) {
      const cam = this.cameras[this.selected];
      if (cam && cam[`center_${axis}`]) return true;
    }
    return false;
  }

  _updateDisabled() {
    // Keep the fieldset live while connected (even recording) so the picker,
    // filter, and browsing stay usable; per-widget locking (_locked) disables
    // the editable controls. Only a request in flight freezes the whole tab.
    this.fields.disabled = !this.connected || this.busy;
    const editLocked = !this.connected || this.recording || this.busy;
    this.nameInput.disabled = editLocked;
    this.renameBtn.disabled = editLocked;
  }

  _applyUpdated(data) {
    const now = Date.now();
    for (const entry of data.updated || []) {
      this.featuresByIndex[entry.index] = entry;
      // We now hold the authoritative post-write state; ignore the matching
      // camera_features_dirty echo for a short window so we don't refetch it.
      this._suppressDirtyUntil[entry.index] = now + 1200;
      const cam = this.cameras[entry.index];
      if (cam) {
        cam.center_x = entry.center_x;
        cam.center_y = entry.center_y;
      }
    }
    this._renderParams();
  }

  // ------------------------------------------------------- UI -> server

  async _commit(name, value) {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    await this._request(
      () => api("PUT", `/api/cameras/${cam.index}/features`, { name, value }),
      "Parameter update"
    );
  }

  async _resetFeature(name) {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    await this._request(
      () => api("POST", `/api/cameras/${cam.index}/features/reset`, { name }),
      "Reset"
    );
  }

  async _toggleCenter(axis, enabled) {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    await this._request(
      () => api("PUT", `/api/cameras/${cam.index}/center`, { axis, enabled }),
      "Centering"
    );
  }

  async _runCommand(f) {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    const label = f.display_name || f.name;
    if (!window.confirm(`Run the "${label}" command on ${cam.name}?`)) return;
    await this._request(
      () => api("POST", `/api/cameras/${cam.index}/commands`, { name: f.name }),
      `Command ${label}`
    );
  }

  // Shared request wrapper: lock the tab, run `call`, apply the refreshed
  // feature payload on success or re-render (snapping back) on failure.
  async _request(call, action) {
    this.busy = true;
    this._updateDisabled();
    let r;
    try {
      r = await call();
    } catch {
      this.notify("error", `${action} failed: server unreachable`);
      this.busy = false;
      this._renderParams();
      return;
    }
    this.busy = false;
    if (r.ok && r.data) {
      this._applyUpdated(r.data);
    } else {
      this.notify("error", r.data?.detail || `${action} failed (HTTP ${r.status})`);
      this._renderParams(); // snap back to the last device value
    }
  }

  // Rename shares with the grid's inline editor (see the original doc): PUT the
  // new name, then relabel the picker/grid tile via applyName.
  async renameCamera(index, name) {
    const cam = this.cameras[index];
    if (!cam) return null;
    const trimmed = name.trim();
    if (!trimmed || trimmed === cam.name) return null;
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

  async _commitName() {
    const cam = this.cameras[this.selected];
    if (!cam) return;
    this.busy = true;
    this._updateDisabled();
    const canonical = await this.renameCamera(this.selected, this.nameInput.value);
    this.busy = false;
    this._updateDisabled();
    this.nameInput.value = canonical ?? cam.name;
  }
}
