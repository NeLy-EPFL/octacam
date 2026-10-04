// Dark/light theme toggle. Dark is the stylesheet's :root; light is
// data-theme="light" on <html>. An inline script in index.html applies the
// saved choice before first paint; this module owns the toggle and saves it.

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

// Set by initTheme, so applyConfigTheme can refresh the icon.
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

  // The toggle shows the icon of the theme it switches to.
  renderToggle = () => {
    if (!btn) return;
    const light = isLight();
    btn.textContent = light ? "🌙" : "☀️";
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

// Apply the rig's default theme ([gui].theme) once the config has loaded,
// unless this browser saved its own choice.
export function applyConfigTheme(theme) {
  if (readSaved()) return;
  applyLight(theme === "light");
  renderToggle();
}
