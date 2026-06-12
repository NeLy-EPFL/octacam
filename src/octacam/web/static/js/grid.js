// Camera grid: tiles positioned from config layout fractions (or auto-tiled
// in a 3-column grid), JPEG preview rendering, transforms, crosshair.

export class CameraGrid {
  constructor(container, cameras) {
    this.container = container;
    this.tiles = [];
    this.indexBySerial = new Map();
    this.selected = -1;

    // Any unset (< 0) layout value makes ALL cameras auto-tile.
    this.autoTile = cameras.some((c) => {
      const l = c.layout;
      return (
        l.window_x < 0 ||
        l.window_y < 0 ||
        l.window_width < 0 ||
        l.window_height < 0
      );
    });
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
      </div>`;
    const nameEl = el.querySelector(".tile-name");
    nameEl.textContent = cam.name;
    nameEl.title = `serial ${cam.serial}`;
    el.addEventListener("click", () => this.select(index));
    this.container.appendChild(el);

    const canvas = el.querySelector("canvas");
    const tile = {
      cam,
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
    };
    this.tiles.push(tile);
    this._applyTransform(tile);
  }

  select(index) {
    this.selected = index;
    this.tiles.forEach((t, i) => t.el.classList.toggle("selected", i === index));
  }

  setCrossVisible(visible) {
    this.container.classList.toggle("show-cross", visible);
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

  _layoutAll() {
    if (!this.autoTile) {
      const w = this.container.clientWidth;
      const h = this.container.clientHeight;
      for (const t of this.tiles) {
        const l = t.cam.layout;
        t.el.style.left = `${Math.round(l.window_x * w)}px`;
        t.el.style.top = `${Math.round(l.window_y * h)}px`;
        t.el.style.width = `${Math.round(l.window_width * w)}px`;
        t.el.style.height = `${Math.round(l.window_height * h)}px`;
      }
    }
    for (const t of this.tiles) this._layoutCanvas(t);
  }
}
