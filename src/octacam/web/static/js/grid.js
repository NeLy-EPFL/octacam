// Camera grid: MDI-style subwindows positioned from config layout fractions
// (or auto-tiled in a 3-column grid), JPEG preview rendering, transforms, and
// crosshair. Each tile behaves like a Qt MDI subwindow: drag its title bar to
// move, drag an edge/corner to resize, double-click the title (or click its
// button) to maximize/restore. Clicking a tile raises it above its peers.

import { api, clamp } from "./util.js";

// Qt semantics (main_window.py): a camera contributes a manual layout if it
// has a valid position OR a valid size; positions/sizes are applied
// independently, each falling back to a default when absent.
const hasPos = (l) => l.window_x >= 0 && l.window_y >= 0;
const hasSize = (l) => l.window_width > 0 && l.window_height > 0;

// A pointer travel (px) below this is a click (select), not a drag.
const DRAG_THRESHOLD = 4;
// Deepest scroll-zoom. 1 is the tight fit (you can't zoom out smaller than
// that); each wheel notch multiplies by ZOOM_STEP.
const ZOOM_MAX = 8;
const ZOOM_STEP = 1.15;
const norm360 = (deg) => ((deg % 360) + 360) % 360;

export class CameraGrid {
  constructor(container, cameras, { onSelect, onRename, onViewChange } = {}) {
    this.container = container;
    this.onSelect = onSelect;
    this.onRename = onRename; // async (index, name) -> canonical name | null
    // Called (debounced by the caller) whenever the resolution/crop the server
    // should send changes: tile resized, maximized/restored, or zoomed.
    this.onViewChange = onViewChange;
    this.tiles = [];
    this.indexBySerial = new Map();
    this.selected = -1;
    this._z = 0; // stacking counter — clicking a tile raises it above its peers
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
      <div class="tile-title" title="Drag to move; double-click to maximize">
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
      <button type="button" class="tile-max" title="Maximize" tabindex="-1"></button>
      <div class="tile-resize n" data-dir="n"></div>
      <div class="tile-resize s" data-dir="s"></div>
      <div class="tile-resize e" data-dir="e"></div>
      <div class="tile-resize w" data-dir="w"></div>
      <div class="tile-resize se" data-dir="se" title="Drag to resize"></div>
      <div class="tile-resize sw" data-dir="sw" title="Drag to resize"></div>`;
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
      maxBtn: el.querySelector(".tile-max"),
      runtime: { rot: 0, fx: 1, fy: 1 },
      // natW/natH are the decoded JPEG's pixel size (a crop, when zoomed);
      // sensorW/sensorH are the full sensor size used for all layout math; and
      // crop{X,Y,W,H} is the sensor sub-rectangle the current frame covers
      // (the whole sensor when un-cropped). They diverge once the server sends
      // a server-side crop for a zoomed tile.
      natW: 0,
      natH: 0,
      sensorW: cam.width || 0,
      sensorH: cam.height || 0,
      cropX: 0,
      cropY: 0,
      cropW: cam.width || 0,
      cropH: cam.height || 0,
      // On-screen footprint (px) of the fitted, un-zoomed full frame, and the
      // sensor->screen fit scale k; used to clamp the pan and to invert the
      // display transform when computing the visible crop.
      fitW: 0,
      fitH: 0,
      k: 0,
      zoom: 1,
      panX: 0,
      panY: 0,
      busy: false,
      pendingBlob: null,
      pendingMeta: null,
      suppressClick: false,
      maximized: false,
    };
    this.tiles.push(tile);

    // Clicking anywhere on a tile selects it and raises it above its peers; a
    // drag/resize sets suppressClick so the gesture doesn't also select.
    el.addEventListener("click", () => {
      if (tile.suppressClick) {
        tile.suppressClick = false;
        return;
      }
      this.select(index);
    });
    el.addEventListener("pointerdown", () => this._raise(tile));

    const title = el.querySelector(".tile-title");
    title.addEventListener("pointerdown", (e) => this._onMoveStart(e, tile));
    // Double-click the title bar (but not the name, where it renames) maximizes.
    title.addEventListener("dblclick", (e) => {
      if (e.target.closest(".tile-name, .tile-name-edit")) return;
      this.toggleMaximize(tile);
    });
    nameEl.addEventListener("dblclick", (e) => {
      e.stopPropagation();
      this._startNameEdit(tile);
    });
    tile.maxBtn.addEventListener("pointerdown", (e) => e.stopPropagation());
    tile.maxBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      this.toggleMaximize(tile);
    });
    for (const h of el.querySelectorAll(".tile-resize")) {
      h.addEventListener("pointerdown", (e) =>
        this._onResizeStart(e, tile, h.dataset.dir)
      );
    }
    tile.body.addEventListener("wheel", (e) => this._onWheel(e, tile), {
      passive: false,
    });
    // Catch any size change to this tile (drag-resize, maximize, container
    // reflow): re-fit the canvas, re-clamp the pan, and tell the server the
    // resolution this tile now needs.
    new ResizeObserver(() => {
      this._layoutCanvas(tile);
      this._notifyView();
    }).observe(tile.body);
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
    if (this._renameLocked || this._nameEdit) return;

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
      this._pushTransform(t);
    }
    // A rotate/flip/reset changes which sensor region a zoomed tile shows, so
    // re-request its server-side crop for the new orientation (otherwise the
    // server keeps sending the crop for the old one and the tile shows a stale
    // or blank region until the next zoom/resize).
    if (targets.length) this._notifyView();
  }

  // Mirror the on-screen transform to the server so a "display"-form recording
  // bakes in exactly what is shown (the runtime rotate/flip is otherwise
  // browser-only). Fire-and-forget; a 409 (locked while recording) is ignored.
  _pushTransform(t) {
    const b = t.cam.transform;
    const r = t.runtime;
    api("PUT", `/api/cameras/${t.index}/transform`, {
      scale_x: (b.scale_x || 1) * r.fx,
      scale_y: (b.scale_y || 1) * r.fy,
      rotation_deg: norm360((b.rotation_deg || 0) + r.rot),
    }).catch(() => {});
  }

  handleFrame(frame) {
    const t = this.tiles[frame.cameraIndex];
    if (!t) return;
    t.fpsEl.textContent = `${frame.fps.toFixed(2)} fps`;
    this._setDropped(t, frame.dropped);
    t.el.classList.toggle("rec", frame.recording);
    const blob = new Blob([frame.jpeg], { type: "image/jpeg" });
    const meta = {
      sensorW: frame.sensorW,
      sensorH: frame.sensorH,
      cropX: frame.cropX,
      cropY: frame.cropY,
      cropW: frame.cropW,
      cropH: frame.cropH,
    };
    if (t.busy) {
      // keep only the latest pending frame + its geometry
      t.pendingBlob = blob;
      t.pendingMeta = meta;
    } else {
      this._draw(t, blob, meta);
    }
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

  async _draw(t, blob, meta) {
    t.busy = true;
    try {
      const bmp = await createImageBitmap(blob);
      if (meta && meta.sensorW && meta.sensorH) {
        t.sensorW = meta.sensorW;
        t.sensorH = meta.sensorH;
        t.cropX = meta.cropX;
        t.cropY = meta.cropY;
        t.cropW = meta.cropW;
        t.cropH = meta.cropH;
      }
      if (bmp.width !== t.natW || bmp.height !== t.natH) {
        t.natW = bmp.width;
        t.natH = bmp.height;
        t.canvas.width = bmp.width;
        t.canvas.height = bmp.height;
      }
      // The crop rect (hence placement) can change while the decoded size stays
      // the same — e.g. re-aiming a zoom — so re-layout on every frame.
      this._layoutCanvas(t);
      t.ctx.drawImage(bmp, 0, 0);
      bmp.close();
    } catch {
      // corrupt frame — skip
    }
    t.busy = false;
    if (t.pendingBlob) {
      const next = t.pendingBlob;
      const nextMeta = t.pendingMeta;
      t.pendingBlob = null;
      t.pendingMeta = null;
      this._draw(t, next, nextMeta);
    }
  }

  _applyTransform(t) {
    const b = t.cam.transform;
    const sx = (b.scale_x || 1) * t.runtime.fx;
    const sy = (b.scale_y || 1) * t.runtime.fy;
    const deg = (b.rotation_deg || 0) + t.runtime.rot;
    // The canvas holds the current crop region (the whole sensor when
    // un-cropped). Shift it in pre-rotation sensor space (innermost) so the
    // crop sits at its true position and the rotate/flip still pivots about the
    // sensor centre; then zoom and pan in screen space (CSS applies the
    // rightmost function first). Un-cropped, the offset is 0 and this reduces
    // to the plain zoom/pan · flip · rotate transform.
    const k = t.k || 0;
    const sw = t.sensorW || t.natW || 0;
    const sh = t.sensorH || t.natH || 0;
    const offX = (t.cropX + t.cropW / 2 - sw / 2) * k;
    const offY = (t.cropY + t.cropH / 2 - sh / 2) * k;
    t.canvas.style.transform =
      `translate(${t.panX}px, ${t.panY}px) scale(${t.zoom}) ` +
      `scale(${sx}, ${sy}) rotate(${deg}deg) translate(${offX}px, ${offY}px)`;
  }

  // Fit the FULL sensor frame into the tile body (keeping aspect) to get the
  // sensor->screen scale k, then size the canvas to the current crop at that
  // same k (the whole sensor when un-cropped). k and the fitted footprint also
  // drive pan clamping and the visible-region computation.
  _layoutCanvas(t) {
    const sw = t.sensorW || t.natW;
    const sh = t.sensorH || t.natH;
    if (!sw || !sh) return;
    const bw = t.body.clientWidth;
    const bh = t.body.clientHeight;
    if (!bw || !bh) return;
    const b = t.cam.transform;
    const theta =
      (((b.rotation_deg || 0) + t.runtime.rot) * Math.PI) / 180;
    const effW = sw * Math.abs(b.scale_x || 1);
    const effH = sh * Math.abs(b.scale_y || 1);
    const c = Math.abs(Math.cos(theta));
    const s = Math.abs(Math.sin(theta));
    const boundW = effW * c + effH * s;
    const boundH = effW * s + effH * c;
    const k = Math.min(bw / boundW, bh / boundH);
    t.k = k;
    const cw = t.cropW || sw;
    const ch = t.cropH || sh;
    t.canvas.style.width = `${cw * k}px`;
    t.canvas.style.height = `${ch * k}px`;
    // On-screen footprint of the fitted FULL frame — the box the pan is clamped
    // against so a zoomed image can't be dragged past its own edges.
    t.fitW = boundW * k;
    t.fitH = boundH * k;
    this._applyTransform(t);
    this._clampPan(t);
  }

  // Keep the (possibly zoomed) canvas covering its tile: pan is limited to the
  // overflow on each axis, so it snaps back to centered at the tight fit.
  _clampPan(t) {
    const maxX = Math.max(0, (t.zoom * t.fitW - t.body.clientWidth) / 2);
    const maxY = Math.max(0, (t.zoom * t.fitH - t.body.clientHeight) / 2);
    t.panX = clamp(t.panX, -maxX, maxX);
    t.panY = clamp(t.panY, -maxY, maxY);
  }

  // Scroll to zoom toward the cursor. Zoom is clamped to [1, ZOOM_MAX]; 1 is
  // the tight fit, so the image can never be scrolled smaller than its
  // original size. The point under the cursor stays fixed across the zoom.
  _onWheel(e, t) {
    e.preventDefault();
    const rect = t.body.getBoundingClientRect();
    const cx = e.clientX - (rect.left + rect.width / 2);
    const cy = e.clientY - (rect.top + rect.height / 2);
    const z0 = t.zoom;
    const z1 = clamp(
      z0 * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP),
      1,
      ZOOM_MAX
    );
    if (z1 === z0) return;
    // Solve for the pan that pins the on-screen cursor point across the zoom;
    // in screen space this is correct regardless of rotation/flip.
    t.panX = cx - (z1 / z0) * (cx - t.panX);
    t.panY = cy - (z1 / z0) * (cy - t.panY);
    t.zoom = z1;
    this._clampPan(t);
    this._applyTransform(t);
    this._notifyView();
  }

  _notifyView() {
    this.onViewChange?.();
  }

  // Per-camera resolution / crop / pause request for the server. want=false for
  // a tile hidden behind another's maximized window (the server stops sending
  // it). For a zoomed tile, request a server-side crop of just the visible
  // region at the tile's resolution — full detail for the cost of a small
  // frame. Otherwise request the whole frame at the tile size × dpr × zoom, so
  // a maximize is sharp and a not-yet-cropped zoom still has finer pixels.
  getViewSpec() {
    const anyMax = this.tiles.some((t) => t.maximized);
    const dpr = window.devicePixelRatio || 1;
    const spec = {};
    for (const t of this.tiles) {
      if (anyMax && !t.maximized) {
        spec[t.index] = { want: false };
        continue;
      }
      const longEdge = Math.max(t.body.clientWidth, t.body.clientHeight);
      const crop = t.zoom > 1 ? this._visibleSensorRect(t) : null;
      const entry = { want: true, full: t.maximized || t.zoom > 1 };
      if (crop) {
        entry.crop = crop;
        entry.need = Math.max(1, Math.ceil(longEdge * dpr));
      } else {
        entry.need = Math.max(1, Math.ceil(longEdge * dpr * t.zoom));
      }
      spec[t.index] = entry;
    }
    return spec;
  }

  // The axis-aligned sensor rectangle currently visible in a tile, found by
  // inverting the display transform (zoom/pan, then flip, then rotate) over the
  // four viewport corners. This is what the server crops to. Returns null when
  // geometry isn't known yet or the view still covers ~the whole sensor.
  _visibleSensorRect(t) {
    const k = t.k;
    const sw = t.sensorW || t.natW;
    const sh = t.sensorH || t.natH;
    if (!k || !sw || !sh) return null;
    const bw = t.body.clientWidth;
    const bh = t.body.clientHeight;
    const b = t.cam.transform;
    const sx = (b.scale_x || 1) * t.runtime.fx;
    const sy = (b.scale_y || 1) * t.runtime.fy;
    const rad = -(((b.rotation_deg || 0) + t.runtime.rot) * Math.PI) / 180; // inverse
    const cos = Math.cos(rad);
    const sin = Math.sin(rad);
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [px, py] of [
      [-bw / 2, -bh / 2],
      [bw / 2, -bh / 2],
      [-bw / 2, bh / 2],
      [bw / 2, bh / 2],
    ]) {
      // Undo pan/zoom (screen space), then flip (S^-1), then rotate (R^-1).
      const dx = (px - t.panX) / t.zoom / sx;
      const dy = (py - t.panY) / t.zoom / sy;
      const lx = dx * cos - dy * sin;
      const ly = dx * sin + dy * cos;
      const i = lx / k + sw / 2;
      const j = ly / k + sh / 2;
      minX = Math.min(minX, i);
      maxX = Math.max(maxX, i);
      minY = Math.min(minY, j);
      maxY = Math.max(maxY, j);
    }
    // A small margin so small re-aims stay inside the already-sent crop.
    const mx = (maxX - minX) * 0.15;
    const my = (maxY - minY) * 0.15;
    const x = Math.max(0, Math.floor(minX - mx));
    const y = Math.max(0, Math.floor(minY - my));
    const w = Math.min(sw, Math.ceil(maxX + mx)) - x;
    const h = Math.min(sh, Math.ceil(maxY + my)) - y;
    if (w <= 0 || h <= 0) return null;
    if (x <= 0 && y <= 0 && w >= sw && h >= sh) return null; // ~whole frame
    return { x, y, w, h };
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

  // -------------------------------------------------- window interactions

  // Clicking/dragging a tile floats it above its peers (Qt MDI focus order).
  _raise(tile) {
    tile.el.style.zIndex = String(++this._z);
  }

  // Toggle a tile between its normal geometry and filling the whole grid. Only
  // one tile is maximized at a time; cam.layout keeps the normal box so a save
  // (or restore) reflects the real geometry, not the maximized override.
  toggleMaximize(tile) {
    const next = !tile.maximized;
    if (next) {
      for (const t of this.tiles) {
        if (t !== tile && t.maximized) this._setMaximized(t, false);
      }
    }
    this._setMaximized(tile, next);
    this._raise(tile);
    this._layoutCanvas(tile);
    // Maximizing flips want=false on every other tile and lets this one request
    // full resolution; restoring undoes both. Report the whole new view.
    this._notifyView();
  }

  _setMaximized(tile, on) {
    tile.maximized = on;
    tile.el.classList.toggle("maximized", on);
    tile.maxBtn.title = on ? "Restore" : "Maximize";
  }

  // The first move/resize converts the CSS-grid auto layout into explicit
  // per-tile fractions so windows can be placed freely; a no-op once done.
  _ensureMaterialized() {
    if (!this.autoTile) return;
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

  // Drag the title bar to move the window, kept fully inside the grid.
  _onMoveStart(e, tile) {
    if (e.button !== 0 || tile.maximized) return;
    this._ensureMaterialized();
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
      tile.cam.layout = {
        window_x: clamp(start.lx + dx, 0, 1 - start.lw),
        window_y: clamp(start.ly + dy, 0, 1 - start.lh),
        window_width: start.lw,
        window_height: start.lh,
      };
      this._applyTileBox(tile);
    };
    this._startDrag(e, tile, start, move);
  }

  // Drag an edge/corner handle to resize; `dir` combines n/s/e/w.
  _onResizeStart(e, tile, dir) {
    if (e.button !== 0 || tile.maximized) return;
    e.stopPropagation(); // a handle drag must not also move or select the tile
    this._ensureMaterialized();
    const MIN = 0.05;
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
      let { lx, ly, lw, lh } = start;
      if (dir.includes("e")) lw = clamp(start.lw + dx, MIN, 1 - start.lx);
      if (dir.includes("s")) lh = clamp(start.lh + dy, MIN, 1 - start.ly);
      if (dir.includes("w")) {
        const nx = clamp(start.lx + dx, 0, start.lx + start.lw - MIN);
        lx = nx;
        lw = start.lx + start.lw - nx;
      }
      if (dir.includes("n")) {
        const ny = clamp(start.ly + dy, 0, start.ly + start.lh - MIN);
        ly = ny;
        lh = start.ly + start.lh - ny;
      }
      tile.cam.layout = {
        window_x: lx,
        window_y: ly,
        window_width: lw,
        window_height: lh,
      };
      this._applyTileBox(tile);
    };
    this._startDrag(e, tile, start, move);
  }

  // Shared pointer-capture loop for move/resize: run `move` on each pointer
  // step and, if the pointer actually traveled, swallow the trailing select
  // click so a drag/resize doesn't also (de)select the tile.
  _startDrag(e, tile, start, move) {
    this._raise(tile);
    // Capture the pointer only once the gesture becomes a real drag (past the
    // threshold). Capturing on pointerdown retargets the trailing click AND
    // dblclick to the capture element (the tile), which swallowed the
    // tile-name double-click that opens the rename editor (and the title-bar
    // double-click that maximizes). No capture on a click => events keep their
    // real target; a genuine drag still captures so it tracks outside the tile.
    let captured = false;
    const onMove = (ev) => {
      move(ev);
      if (start.moved && !captured) {
        captured = true;
        tile.el.setPointerCapture(e.pointerId);
      }
    };
    const up = () => {
      tile.el.removeEventListener("pointermove", onMove);
      tile.el.removeEventListener("pointerup", up);
      tile.el.removeEventListener("pointercancel", up);
      if (captured) tile.el.releasePointerCapture?.(e.pointerId);
      if (start.moved) tile.suppressClick = true;
    };
    tile.el.addEventListener("pointermove", onMove);
    tile.el.addEventListener("pointerup", up);
    tile.el.addEventListener("pointercancel", up);
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
        // Live ROI-centering flags (updated by the Camera tab on the shared cam
        // object) so a save persists them alongside the display transform.
        center_x: !!t.cam.center_x,
        center_y: !!t.cam.center_y,
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
