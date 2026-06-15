// Camera grid: tiles positioned from config layout fractions (or auto-tiled
// in a 3-column grid), JPEG preview rendering, transforms, crosshair, and
// (in edit mode) drag-to-move / drag-corner-to-resize layout editing.

import { clamp } from "./util.js";

// Qt semantics (main_window.py): a camera contributes a manual layout if it
// has a valid position OR a valid size; positions/sizes are applied
// independently, each falling back to a default when absent.
const hasPos = (l) => l.window_x >= 0 && l.window_y >= 0;
const hasSize = (l) => l.window_width > 0 && l.window_height > 0;

// A pointer travel (px) below this is a click (select), not a drag.
const DRAG_THRESHOLD = 4;
const norm360 = (deg) => ((deg % 360) + 360) % 360;

export class CameraGrid {
  constructor(container, cameras, { onSelect, onRename } = {}) {
    this.container = container;
    this.onSelect = onSelect;
    this.onRename = onRename; // async (index, name) -> canonical name | null
    this.tiles = [];
    this.indexBySerial = new Map();
    this.selected = -1;
    this._editing = false; // layout editing (drag tiles)
    // Inline name editing (double-click a tile title); locked unless connected
    // and not recording. The recording state is only authoritative once a
    // state/telemetry message arrives, so _recordingKnown holds the lock on
    // until then (a (re)connect mid-recording must not briefly offer an edit).
    this._connected = false;
    this._recording = false;
    this._recordingKnown = false;
    this._renameLocked = true;
    this._nameEdit = null;

    // Auto-tile only when NO camera carries a manual layout; otherwise honor
    // the configured layouts (a single unconfigured camera must not discard
    // everyone else's, as the Qt app preserves them).
    this.autoTile = !cameras.some((c) => hasPos(c.layout) || hasSize(c.layout));
    container.classList.toggle("auto-tile", this.autoTile);

    for (const cam of cameras) this._buildTile(cam);

    new ResizeObserver(() => this._layoutAll()).observe(container);
    this._layoutAll();
  }

  _buildTile(cam) {
    const index = this.tiles.length;
    this.indexBySerial.set(cam.serial, index);

    const el = document.createElement("div");
    el.className = "tile";
    el.innerHTML = `
      <div class="tile-title">
        <span class="tile-name"></span>
        <span class="tile-stats">
          <span class="tile-dropped"></span>
          <span class="tile-fps">-- fps</span>
        </span>
      </div>
      <div class="tile-body">
        <canvas width="0" height="0"></canvas>
        <div class="tile-cross"><div class="cross-h"></div><div class="cross-v"></div></div>
      </div>
      <div class="tile-grip" title="Drag to resize"></div>`;
    const nameEl = el.querySelector(".tile-name");
    nameEl.textContent = cam.name;
    nameEl.title = `serial ${cam.serial}`;
    this.container.appendChild(el);

    const canvas = el.querySelector("canvas");
    const tile = {
      cam,
      index,
      el,
      canvas,
      ctx: canvas.getContext("2d"),
      body: el.querySelector(".tile-body"),
      nameEl,
      fpsEl: el.querySelector(".tile-fps"),
      droppedEl: el.querySelector(".tile-dropped"),
      runtime: { rot: 0, fx: 1, fy: 1 },
      natW: 0,
      natH: 0,
      busy: false,
      pendingBlob: null,
      suppressClick: false,
    };
    this.tiles.push(tile);
    el.addEventListener("click", () => {
      if (tile.suppressClick) {
        tile.suppressClick = false;
        return;
      }
      this.select(index);
    });
    el.addEventListener("pointerdown", (e) => this._onTilePointerDown(e, tile));
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      this._startNameEdit(tile);
    });
    this._applyTransform(tile);
  }

  select(index) {
    this.selected = index;
    this.tiles.forEach((t, i) => t.el.classList.toggle("selected", i === index));
    this.onSelect?.(index);
  }

  setCrossVisible(visible) {
    this.container.classList.toggle("show-cross", visible);
  }

  // Relabel a tile after a camera rename (the camera object is shared with the
  // Camera tab, so cam.name is already updated; this refreshes the DOM text).
  setName(index, name) {
    const t = this.tiles[index];
    if (!t) return;
    t.cam.name = name;
    t.nameEl.textContent = name;
  }

  // ------------------------------------------------ inline name editing

  // Inline rename is locked unless the socket is up and no recording is in
  // progress (the server also rejects renames while recording with 409).
  setConnected(connected) {
    this._connected = connected;
    // On a drop the last-seen recording flag is stale; require a fresh
    // state message after reconnecting before unlocking again.
    if (!connected) this._recordingKnown = false;
    this._refreshRenameLock();
  }

  setRecording(recording) {
    this._recording = recording;
    this._recordingKnown = true;
    this._refreshRenameLock();
  }

  _refreshRenameLock() {
    this._renameLocked =
      !this._connected || this._recording || !this._recordingKnown;
    this.container.classList.toggle("can-rename", !this._renameLocked);
    if (this._renameLocked) this._cancelNameEdit(); // abort an open editor
  }

  // Double-click a tile title -> edit its name in place. The committed name
  // goes through onRename (the Camera tab's renameCamera), which relabels the
  // tile via setName on success; on a no-op/failure the title reverts.
  _startNameEdit(tile) {
    if (this._renameLocked || this._editing || this._nameEdit) return;

    const title = tile.el.querySelector(".tile-title");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "tile-name-edit";
    input.value = tile.cam.name;
    input.spellcheck = false;
    input.maxLength = 64;

    let done = false;
    const finish = async (commit) => {
      if (done) return; // guard against blur firing during teardown
      done = true;
      input.removeEventListener("keydown", onKey);
      input.removeEventListener("blur", onBlur);
      if (commit) {
        input.disabled = true;
        await this.onRename?.(tile.index, input.value);
      }
      input.remove();
      title.classList.remove("editing");
      this._nameEdit = null;
    };
    const onKey = (e) => {
      e.stopPropagation();
      if (e.key === "Enter") {
        e.preventDefault();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      }
    };
    const onBlur = () => finish(true);

    input.addEventListener("keydown", onKey);
    input.addEventListener("blur", onBlur);
    // Keep clicks inside the field from selecting the tile or starting a drag.
    input.addEventListener("pointerdown", (e) => e.stopPropagation());
    input.addEventListener("click", (e) => e.stopPropagation());

    this._nameEdit = { tile, finish };
    title.classList.add("editing");
    tile.nameEl.insertAdjacentElement("afterend", input);
    input.focus();
    input.select();
  }

  _cancelNameEdit() {
    this._nameEdit?.finish(false);
  }

  // op: {rotateDelta?, flipH?, flipV?, reset?}; scope: "all" | "selected"
  applyView(op, scope) {
    const targets =
      scope === "all"
        ? this.tiles
        : this.selected >= 0
          ? [this.tiles[this.selected]]
          : [];
    for (const t of targets) {
      const r = t.runtime;
      if (op.reset) {
        r.rot = 0;
        r.fx = 1;
        r.fy = 1;
      } else {
        r.rot = (r.rot + (op.rotateDelta || 0)) % 360;
        if (op.flipH) r.fx = -r.fx;
        if (op.flipV) r.fy = -r.fy;
      }
      this._applyTransform(t);
      this._layoutCanvas(t);
    }
  }

  handleFrame(frame) {
    const t = this.tiles[frame.cameraIndex];
    if (!t) return;
    t.fpsEl.textContent = `${frame.fps.toFixed(2)} fps`;
    this._setDropped(t, frame.dropped);
    t.el.classList.toggle("rec", frame.recording);
    const blob = new Blob([frame.jpeg], { type: "image/jpeg" });
    if (t.busy) t.pendingBlob = blob; // keep only the latest pending frame
    else this._draw(t, blob);
  }

  updateStats(index, { fps, dropped, writerFailed }) {
    const t = this.tiles[index];
    if (!t) return;
    if (typeof fps === "number") t.fpsEl.textContent = `${fps.toFixed(2)} fps`;
    if (typeof dropped === "number") this._setDropped(t, dropped);
    if (writerFailed !== undefined) {
      t.nameEl.classList.toggle("failed", writerFailed);
      t.nameEl.title = writerFailed
        ? `serial ${t.cam.serial} — writer failed`
        : `serial ${t.cam.serial}`;
    }
  }

  _setDropped(t, dropped) {
    t.droppedEl.textContent = dropped > 0 ? `${dropped} dropped` : "";
  }

  async _draw(t, blob) {
    t.busy = true;
    try {
      const bmp = await createImageBitmap(blob);
      if (bmp.width !== t.natW || bmp.height !== t.natH) {
        t.natW = bmp.width;
        t.natH = bmp.height;
        t.canvas.width = bmp.width;
        t.canvas.height = bmp.height;
        this._layoutCanvas(t);
      }
      t.ctx.drawImage(bmp, 0, 0);
      bmp.close();
    } catch {
      // corrupt frame — skip
    }
    t.busy = false;
    if (t.pendingBlob) {
      const next = t.pendingBlob;
      t.pendingBlob = null;
      this._draw(t, next);
    }
  }

  _applyTransform(t) {
    const b = t.cam.transform;
    const sx = (b.scale_x || 1) * t.runtime.fx;
    const sy = (b.scale_y || 1) * t.runtime.fy;
    const deg = (b.rotation_deg || 0) + t.runtime.rot;
    t.canvas.style.transform = `scale(${sx}, ${sy}) rotate(${deg}deg)`;
  }

  // Size the canvas so the (possibly rotated/scaled) frame fits the tile
  // body while keeping its aspect ratio.
  _layoutCanvas(t) {
    if (!t.natW || !t.natH) return;
    const bw = t.body.clientWidth;
    const bh = t.body.clientHeight;
    if (!bw || !bh) return;
    const b = t.cam.transform;
    const theta =
      (((b.rotation_deg || 0) + t.runtime.rot) * Math.PI) / 180;
    const effW = t.natW * Math.abs(b.scale_x || 1);
    const effH = t.natH * Math.abs(b.scale_y || 1);
    const c = Math.abs(Math.cos(theta));
    const s = Math.abs(Math.sin(theta));
    const boundW = effW * c + effH * s;
    const boundH = effW * s + effH * c;
    const k = Math.min(bw / boundW, bh / boundH);
    t.canvas.style.width = `${t.natW * k}px`;
    t.canvas.style.height = `${t.natH * k}px`;
  }

  _applyTileBox(t) {
    const w = this.container.clientWidth;
    const h = this.container.clientHeight;
    const l = t.cam.layout;
    t.el.style.left = hasPos(l) ? `${Math.round(l.window_x * w)}px` : "0px";
    t.el.style.top = hasPos(l) ? `${Math.round(l.window_y * h)}px` : "0px";
    t.el.style.width = hasSize(l)
      ? `${Math.round(l.window_width * w)}px`
      : `${Math.round(w / 3)}px`;
    t.el.style.height = hasSize(l)
      ? `${Math.round(l.window_height * h)}px`
      : `${Math.round(h / 3)}px`;
    this._layoutCanvas(t);
  }

  _layoutAll() {
    if (!this.autoTile) {
      for (const t of this.tiles) this._applyTileBox(t);
    } else {
      for (const t of this.tiles) this._layoutCanvas(t);
    }
  }

  // ----------------------------------------------------- layout editing

  setLayoutEditing(enabled) {
    this._editing = enabled;
    this.container.classList.toggle("layout-editing", enabled);
    // Editing needs explicit per-tile boxes; convert the CSS-grid auto layout
    // into the current positions/sizes as fractions so they can be edited.
    if (enabled && this.autoTile) this._materializeAutoLayout();
  }

  _materializeAutoLayout() {
    const cr = this.container.getBoundingClientRect();
    for (const t of this.tiles) {
      const r = t.el.getBoundingClientRect();
      t.cam.layout = {
        window_x: (r.left - cr.left) / cr.width,
        window_y: (r.top - cr.top) / cr.height,
        window_width: r.width / cr.width,
        window_height: r.height / cr.height,
      };
    }
    this.autoTile = false;
    this.container.classList.remove("auto-tile");
    this._layoutAll();
  }

  _onTilePointerDown(e, tile) {
    if (!this._editing || e.button !== 0) return;
    const resize = e.target.classList.contains("tile-grip");
    const cr = this.container.getBoundingClientRect();
    const l = tile.cam.layout;
    const start = {
      x: e.clientX,
      y: e.clientY,
      lx: hasPos(l) ? l.window_x : 0,
      ly: hasPos(l) ? l.window_y : 0,
      lw: hasSize(l) ? l.window_width : 1 / 3,
      lh: hasSize(l) ? l.window_height : 1 / 3,
      moved: false,
    };
    const move = (ev) => {
      if (
        !start.moved &&
        Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < DRAG_THRESHOLD
      ) {
        return;
      }
      start.moved = true;
      const dx = (ev.clientX - start.x) / cr.width;
      const dy = (ev.clientY - start.y) / cr.height;
      if (resize) {
        tile.cam.layout = {
          window_x: start.lx,
          window_y: start.ly,
          window_width: clamp(start.lw + dx, 0.05, 1),
          window_height: clamp(start.lh + dy, 0.05, 1),
        };
      } else {
        tile.cam.layout = {
          window_x: clamp(start.lx + dx, 0, 1),
          window_y: clamp(start.ly + dy, 0, 1),
          window_width: start.lw,
          window_height: start.lh,
        };
      }
      this._applyTileBox(tile);
    };
    const up = () => {
      tile.el.removeEventListener("pointermove", move);
      tile.el.removeEventListener("pointerup", up);
      tile.el.removeEventListener("pointercancel", up);
      if (start.moved) tile.suppressClick = true; // don't select after a drag
    };
    tile.el.setPointerCapture(e.pointerId);
    tile.el.addEventListener("pointermove", move);
    tile.el.addEventListener("pointerup", up);
    tile.el.addEventListener("pointercancel", up);
    e.preventDefault();
  }

  // ------------------------------------------------ display-param capture

  // Per-camera display state for saving: base transform composed with the
  // browser-only runtime rotate/flip, plus the current layout fractions.
  getDisplayParams() {
    return this.tiles.map((t) => {
      const b = t.cam.transform;
      const r = t.runtime;
      const l = t.cam.layout;
      return {
        serial: t.cam.serial,
        name: t.cam.name,
        scale_x: (b.scale_x || 1) * r.fx,
        scale_y: (b.scale_y || 1) * r.fy,
        rotation_deg: norm360((b.rotation_deg || 0) + r.rot),
        window_x: l.window_x,
        window_y: l.window_y,
        window_width: l.window_width,
        window_height: l.window_height,
      };
    });
  }

  // After a save, fold runtime into the base so the persisted transform isn't
  // double-applied; the on-screen result is unchanged.
  commitRuntime() {
    for (const t of this.tiles) {
      const b = t.cam.transform;
      const r = t.runtime;
      t.cam.transform = {
        scale_x: (b.scale_x || 1) * r.fx,
        scale_y: (b.scale_y || 1) * r.fy,
        rotation_deg: norm360((b.rotation_deg || 0) + r.rot),
      };
      t.runtime = { rot: 0, fx: 1, fy: 1 };
      this._applyTransform(t);
      this._layoutCanvas(t);
    }
  }
}
