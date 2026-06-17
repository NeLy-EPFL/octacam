// Dark/light theme toggle. Dark is the default (the stylesheet's :root); the
// light theme is opt-in via data-theme="light" on <html>. An inline script in
// index.html applies the saved choice before first paint to avoid a flash of
// the wrong colours; this module owns the footer toggle button and persists the
// user's choice (mirroring the localStorage idiom in resize.js).

const KEY = "octacam.theme";

const writeSaved = (t) => {
  try {
    localStorage.setItem(KEY, t);
  } catch {
    // storage unavailable (private mode / sandbox) — choice just won't persist
  }
};

export function initTheme() {
  const root = document.documentElement;
  const btn = document.getElementById("theme-toggle");

  const isLight = () => root.dataset.theme === "light";

  // Reflect the current theme on the toggle: it shows the icon of the theme it
  // switches *to* (sun while dark, moon while light).
  const render = () => {
    if (!btn) return;
    const light = isLight();
    btn.textContent = light ? "☾" : "☀"; // ☾ : ☀
    btn.title = light ? "Switch to dark theme" : "Switch to light theme";
    btn.setAttribute("aria-pressed", String(light));
  };

  render();

  btn?.addEventListener("click", () => {
    const next = isLight() ? "dark" : "light";
    if (next === "light") root.dataset.theme = "light";
    else delete root.dataset.theme; // absent attribute → default dark :root
    writeSaved(next);
    render();
  });
}
