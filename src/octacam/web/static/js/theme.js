// Dark/light theme toggle. Dark is the default (the stylesheet's :root); the
// light theme is opt-in via data-theme="light" on <html>. An inline script in
// index.html applies the saved choice before first paint to avoid a flash of
// the wrong colours; this module owns the footer toggle button and persists the
// user's choice (mirroring the localStorage idiom in resize.js).

const KEY = "octacam.theme";

const readSaved = () => {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null; // storage unavailable (private mode / sandbox)
  }
};

const writeSaved = (t) => {
  try {
    localStorage.setItem(KEY, t);
  } catch {
    // storage unavailable (private mode / sandbox) — choice just won't persist
  }
};

// Re-render the toggle button; set by initTheme so applyConfigTheme can refresh
// the icon after applying a rig default. No-op until the toggle is wired.
let renderToggle = () => {};

const applyLight = (light) => {
  const root = document.documentElement;
  if (light) root.dataset.theme = "light";
  else delete root.dataset.theme; // absent attribute → default dark :root
};

export function initTheme() {
  const root = document.documentElement;
  const btn = document.getElementById("theme-toggle");

  const isLight = () => root.dataset.theme === "light";

  // Reflect the current theme on the toggle: it shows the icon of the theme it
  // switches *to* (sun while dark, moon while light).
  renderToggle = () => {
    if (!btn) return;
    const light = isLight();
    btn.textContent = light ? "☾" : "☀"; // ☾ : ☀
    btn.title = light ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute("aria-pressed", String(light));
  };

  renderToggle();

  btn?.addEventListener("click", () => {
    const next = isLight() ? "dark" : "light";
    applyLight(next === "light");
    writeSaved(next);
    renderToggle();
  });
}

// Apply the rig's configured default theme (from [gui].theme on the server).
// The inline pre-paint script only knows localStorage, so this runs once the
// config has loaded. A per-browser choice made with the toggle always wins, so
// this is a no-op when the user has saved a theme on this browser.
export function applyConfigTheme(theme) {
  if (readSaved()) return;
  applyLight(theme === "light");
  renderToggle();
}
