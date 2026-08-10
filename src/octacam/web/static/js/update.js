// Dismissible "a newer octacam is available" banner.
//
// Read-only: octacam never updates itself from the browser (rewriting a running
// server with camera SDKs + Arduino links loaded is fragile — see the project's
// versioning notes). This just surfaces the notice the server computed in
// /api/system.update and shows the exact upgrade command for that install; the
// user runs it from a clean shell.
//
// Dismissal persists in localStorage keyed to the offered version, so a dismissed
// banner reappears only when a *newer* release ships (mirrors the theme/resize
// localStorage idiom). Usage: initUpdateBanner(system.update) once in app.js.

const DISMISS_KEY = "octacam.updateDismissed";

function readDismissed() {
  try {
    return localStorage.getItem(DISMISS_KEY);
  } catch {
    return null;
  }
}

function storeDismissed(version) {
  try {
    localStorage.setItem(DISMISS_KEY, version);
  } catch {
    // Private-mode / storage-disabled: dismissal just won't persist. Fine.
  }
}

// `update` is /api/system's `update` object: {available, current, latest, command}
// (or null when the check was skipped / found nothing). Returns true if the banner
// was shown, so it is testable without inspecting the DOM.
export function initUpdateBanner(update) {
  const banner = document.getElementById("update-banner");
  if (!banner) return false;
  banner.classList.add("hidden");
  if (!update || !update.available || !update.latest) return false;
  if (readDismissed() === update.latest) return false;

  banner.textContent = "";

  const msg = document.createElement("span");
  msg.className = "update-msg";
  msg.textContent = `octacam ${update.latest} is available` +
    (update.current ? ` (you have ${update.current})` : "") + ".";
  banner.appendChild(msg);

  if (update.command) {
    banner.appendChild(document.createTextNode(" Update with "));
    const code = document.createElement("code");
    code.className = "update-cmd";
    code.textContent = update.command;
    banner.appendChild(code);
  }

  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.id = "update-dismiss-btn";
  dismiss.className = "update-dismiss";
  dismiss.textContent = "Dismiss";
  dismiss.title = "Hide until a newer version is released";
  dismiss.addEventListener("click", () => {
    storeDismissed(update.latest);
    banner.classList.add("hidden");
  });
  banner.appendChild(dismiss);

  banner.classList.remove("hidden");
  return true;
}
