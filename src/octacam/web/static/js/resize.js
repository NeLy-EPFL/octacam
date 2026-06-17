// Sidebar resizer: drag the gutter between the camera grid and the control
// panel to widen/narrow the sidebar. The width drives the --sidebar-width CSS
// variable (read by both #sidebar and the connection banner) and is persisted
// so it survives a reload.

const MIN = 240;
const KEY = "octacam.sidebarWidth";

const readSaved = () => {
  try {
    return Number(localStorage.getItem(KEY));
  } catch {
    return NaN;
  }
};

const writeSaved = (w) => {
  try {
    localStorage.setItem(KEY, String(w));
  } catch {
    // storage unavailable (private mode / sandbox) — width just won't persist
  }
};

export function initSidebarResize() {
  const root = document.documentElement;
  const handle = document.getElementById("sidebar-resizer");
  if (!handle) return;

  // Cap the panel so the grid can never be squeezed away entirely.
  const maxWidth = () => Math.min(680, Math.round(window.innerWidth * 0.6));
  const clampW = (w) => Math.max(MIN, Math.min(maxWidth(), Math.round(w)));
  const apply = (w) => root.style.setProperty("--sidebar-width", `${w}px`);
  const current = () =>
    parseInt(getComputedStyle(root).getPropertyValue("--sidebar-width"), 10);

  // Restore a previously chosen width, re-clamped to the current viewport.
  const saved = readSaved();
  if (Number.isFinite(saved) && saved > 0) apply(clampW(saved));

  let active = false;
  handle.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    active = true;
    handle.setPointerCapture(e.pointerId);
    handle.classList.add("dragging");
    document.body.style.cursor = "ew-resize";
  });
  handle.addEventListener("pointermove", (e) => {
    if (!active) return;
    apply(clampW(window.innerWidth - e.clientX));
  });
  const end = (e) => {
    if (!active) return;
    active = false;
    handle.releasePointerCapture?.(e.pointerId);
    handle.classList.remove("dragging");
    document.body.style.cursor = "";
    const w = current();
    if (Number.isFinite(w)) writeSaved(w);
  };
  handle.addEventListener("pointerup", end);
  handle.addEventListener("pointercancel", end);

  // Keep the panel within bounds if the window is later resized smaller.
  window.addEventListener("resize", () => {
    const w = current();
    if (Number.isFinite(w)) apply(clampW(w));
  });
}
