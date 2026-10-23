// "Ready?": a strip over the Record tab with what the rig needs before a
// recording (GET /api/ready): cameras, boards, disk, destination and each
// plugin's own checks. Click it for the details.

import { el, request, shortPath } from "./util.js";

const ICONS = { ok: "✓", info: "ℹ", warn: "⚠", error: "✗" };
const RANK = { error: 0, warn: 1, info: 2, ok: 3 };
const REFRESH_MS = 60_000;

export class ReadyStrip {
  constructor() {
    this.strip = document.getElementById("ready-strip");
    this.checks = [];
    this.open = false;
    this.strip.addEventListener("click", (e) => {
      if (e.target.closest(".ready-refresh")) this.refresh(true);
      else {
        this.open = !this.open;
        this.render();
      }
    });
    this._timer = setInterval(() => this.refresh(), REFRESH_MS);
  }

  async refresh(force = false) {
    const d = await request("GET", `/api/ready${force ? "?refresh=true" : ""}`);
    if (!d) return;
    this.checks = d.checks || [];
    this.render();
  }

  render() {
    const strip = this.strip;
    if (!this.checks.length) {
      strip.hidden = true;
      return;
    }
    const worst = this.checks.reduce(
      (w, c) => (RANK[c.status] < RANK[w] ? c.status : w),
      "ok"
    );
    const problems = this.checks.filter((c) => c.status === "error" || c.status === "warn");
    const head = el("div", "ready-head");
    head.append(
      el("span", `ready-icon ready--${worst}`, ICONS[worst]),
      el(
        "span",
        "ready-summary",
        problems.length
          ? `${problems.length} thing${problems.length > 1 ? "s" : ""} to check before recording`
          : "Ready to record"
      )
    );
    const refresh = el("button", "ready-refresh btn", "↻");
    refresh.type = "button";
    refresh.title = "Check again now";
    head.append(refresh);
    const parts = [head];
    const shown = this.open ? this.checks : problems;
    if (shown.length) {
      const list = el("ul", "ready-list");
      for (const c of shown) {
        const item = el("li", `ready-item ready--${c.status}`);
        // Paths shortened to their last folders; the whole detail on hover.
        const detail = (c.detail || "").replace(/\/[^\s()]+/g, (p) => shortPath(p));
        item.title = c.detail || "";
        item.append(
          el("span", "ready-icon", ICONS[c.status]),
          el("b", null, c.label),
          el("span", "ready-detail", detail ? ` ${detail}` : "")
        );
        list.append(item);
      }
      parts.push(list);
    }
    strip.title = this.open ? "Click to show only what needs attention" : "Click for every check";
    strip.className = `ready-strip ready-strip--${worst}`;
    strip.replaceChildren(...parts);
    strip.hidden = false;
  }
}
