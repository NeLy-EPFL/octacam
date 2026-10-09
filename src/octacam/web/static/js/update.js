// Dismissible "a newer octacam is available" banner showing the upgrade
// command from /api/system.update. octacam never updates itself. A dismissal is
// stored per offered version, so the banner returns for a newer release.

import { el, store } from "./util.js";

const DISMISS_KEY = "octacam.updateDismissed";

// `update`: {available, current, latest, command}, or null. Returns whether the
// banner is shown.
export function initUpdateBanner(update) {
  const banner = document.getElementById("update-banner");
  if (!banner) return false;
  banner.classList.add("hidden");
  if (!update || !update.available || !update.latest) return false;
  if (store.get(DISMISS_KEY) === update.latest) return false;

  const have = update.current ? ` (you have ${update.current})` : "";
  banner.replaceChildren(el("span", "update-msg", `octacam ${update.latest} is available${have}.`));
  if (update.command) {
    banner.append(" Update with ", el("code", "update-cmd", update.command));
  }

  const dismiss = el("button", "update-dismiss", "Dismiss");
  dismiss.type = "button";
  dismiss.id = "update-dismiss-btn";
  dismiss.title = "Hide until a newer version is released";
  dismiss.addEventListener("click", () => {
    store.set(DISMISS_KEY, update.latest);
    banner.classList.add("hidden");
  });
  banner.appendChild(dismiss);

  banner.classList.remove("hidden");
  return true;
}
