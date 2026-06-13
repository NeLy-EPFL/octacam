// View tab: runtime rotate/flip transforms and the crosshair toggle.

export function initViewTab(grid) {
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
  document
    .getElementById("layout-edit")
    .addEventListener("change", (e) => grid.setLayoutEditing(e.target.checked));
}
