// Dismissible "a newer octacam is available" banner showing the upgrade
// command from /api/system.update. octacam never updates itself. A dismissal is
// stored per offered version, so the banner returns for a newer release.

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

// `update`: {available, current, latest, command}, or null. Returns whether the
// banner is shown.
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
