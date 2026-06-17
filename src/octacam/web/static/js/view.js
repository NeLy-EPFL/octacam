// View tab: a per-camera selector (kept in sync with the grid and the Camera
// tab), runtime rotate/flip transforms, and the crosshair toggle.

export class ViewTab {
  // `cameras` is the /api/system camera list; `grid` is the CameraGrid;
  // `onSelect(index)` propagates a picker change to the grid (and from there
  // back to the Camera tab).
  constructor({ cameras, grid, onSelect }) {
    this.grid = grid;
    this.onSelect = onSelect;
    this.selected = cameras.length ? 0 : -1;
    this.target = document.getElementById("view-target");

    for (const cam of cameras) {
      const opt = document.createElement("option");
      opt.value = String(cam.index);
      opt.textContent = cam.name;
      this.target.appendChild(opt);
    }

    this.target.addEventListener("change", () => {
      this.selected = Number(this.target.value);
      this.onSelect?.(this.selected);
    });

    // "selected" applies to the grid's currently-selected tile (kept in lockstep
    // with the picker via onSelect/selectCamera); "all" applies to every camera.
    const scope = () =>
      document.querySelector('input[name="apply-to"]:checked').value;

    document
      .getElementById("rotate-ccw")
      .addEventListener("click", () => grid.applyView({ rotateDelta: -90 }, scope()));
    document
      .getElementById("rotate-cw")
      .addEventListener("click", () => grid.applyView({ rotateDelta: 90 }, scope()));
    document
      .getElementById("flip-h")
      .addEventListener("click", () => grid.applyView({ flipH: true }, scope()));
    document
      .getElementById("flip-v")
      .addEventListener("click", () => grid.applyView({ flipV: true }, scope()));
    document
      .getElementById("view-reset")
      .addEventListener("click", () => grid.applyView({ reset: true }, scope()));
    document
      .getElementById("display-cross")
      .addEventListener("change", (e) => grid.setCrossVisible(e.target.checked));
  }

  // Keep the picker in sync when the selection changes elsewhere (a tile click
  // or the Camera tab's picker). Does not re-fire onSelect.
  selectCamera(index) {
    if (index < 0 || index >= this.target.options.length) return;
    this.selected = index;
    this.target.value = String(index);
  }

  // Relabel a picker option after a camera rename.
  applyName(index, name) {
    const opt = this.target.querySelector(`option[value="${index}"]`);
    if (opt) opt.textContent = name;
  }
}
