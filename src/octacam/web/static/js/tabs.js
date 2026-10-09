// The sidebar's tab bar and the plugin tabs in it.

import { el } from "./util.js";

// Tabs that don't fit collapse into a "⋯" menu, so the bar stays one row; the
// active tab is never in the menu. `order` is the stable tab list: the core
// tabs from index.html, then each plugin's in load order.
export class TabBar {
  constructor(nav) {
    this.nav = nav;
    this.order = [...nav.querySelectorAll("button[data-tab]")];

    this.moreBtn = el("button", "tabs-more", "⋯");
    this.moreBtn.type = "button";
    this.moreBtn.id = "tabs-more";
    this.moreBtn.setAttribute("aria-haspopup", "true");
    this.moreBtn.setAttribute("aria-expanded", "false");
    this.moreBtn.title = "More tabs";
    this.moreBtn.hidden = true;
    this.menu = el("div", "tabs-menu");
    this.menu.id = "tabs-menu";
    nav.append(this.moreBtn, this.menu);

    nav.addEventListener("click", (e) => this._onClick(e));
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#tabs")) this._closeMenu();
    });
    // Re-pack on sidebar resize. reflow() never changes nav's own width (the
    // menu is absolutely positioned), so this can't loop.
    let lastW = 0;
    new ResizeObserver(() => {
      const w = Math.round(nav.clientWidth);
      if (w && w !== lastW) {
        lastW = w;
        this.reflow();
      }
    }).observe(nav);
  }

  // Append a plugin's tab and return its empty panel.
  add(name, label, title) {
    const btn = el("button", null, label);
    btn.type = "button";
    btn.dataset.tab = name;
    btn.dataset.plugin = name;
    btn.title = title;
    this.order.push(btn);
    const panel = el("section", "tab");
    panel.id = `tab-${name}`;
    document.getElementById("events").before(panel);
    this.reflow();
    return panel;
  }

  _closeMenu() {
    this.menu.classList.remove("open");
    this.moreBtn.setAttribute("aria-expanded", "false");
  }

  reflow() {
    const { nav, moreBtn, menu, order } = this;
    for (const b of order) nav.insertBefore(b, moreBtn);
    menu.replaceChildren();
    moreBtn.hidden = true;
    this._closeMenu();

    const avail = nav.clientWidth;
    const widths = order.map((b) => b.offsetWidth);
    if (widths.reduce((a, w) => a + w, 0) <= avail) return; // all fit

    moreBtn.hidden = false;
    // A contiguous prefix stays visible, so tabs never reorder or leave a gap.
    let used = moreBtn.offsetWidth;
    let cut = order.length;
    for (let i = 0; i < order.length; i++) {
      if (used + widths[i] <= avail) used += widths[i];
      else {
        cut = i;
        break;
      }
    }
    const visible = order.slice(0, cut);
    // An overflowed active tab evicts trailing tabs until it fits at the end.
    const active = order.find((b) => b.classList.contains("active"));
    if (active && !visible.includes(active)) {
      while (visible.length && used + active.offsetWidth > avail) {
        used -= visible.pop().offsetWidth;
      }
      visible.push(active);
    }
    for (const b of order) if (!visible.includes(b)) menu.appendChild(b);
  }

  _onClick(e) {
    if (e.target.closest("#tabs-more")) {
      const open = this.menu.classList.toggle("open");
      this.moreBtn.setAttribute("aria-expanded", String(open));
      return;
    }
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    for (const b of this.order) b.classList.toggle("active", b === btn);
    for (const panel of document.querySelectorAll(".tab")) {
      panel.classList.toggle("active", panel.id === `tab-${btn.dataset.tab}`);
    }
    this._closeMenu();
    this.reflow();
    // Plugin tabs redraw on this (triggerbox re-reads the Record-tab fps).
    document.dispatchEvent(new CustomEvent("tab-shown", { detail: { tab: btn.dataset.tab } }));
  }
}

// Import each plugin's UI module (advertised in /api/system), give it a tab
// and build it there (see SerialTab in serial.js for what a tab receives and
// implements). One broken module must not blank the page. Returns {name: tab}.
export async function loadPluginTabs(plugins, tabBar, ctx) {
  const tabs = new Map();
  for (const [name, status] of Object.entries(plugins)) {
    if (!status.web?.module) continue;
    try {
      if (status.web.css) loadCss(status.web.css);
      const Tab = (await import(status.web.module)).default;
      const panel = tabBar.add(name, Tab.label, Tab.title);
      tabs.set(name, new Tab({ ...ctx, name, panel, status }));
    } catch (e) {
      console.warn(`plugin ${name}: UI module failed to load`, e);
    }
  }
  return tabs;
}

function loadCss(href) {
  const link = el("link");
  link.rel = "stylesheet";
  link.href = href;
  document.head.appendChild(link);
}
