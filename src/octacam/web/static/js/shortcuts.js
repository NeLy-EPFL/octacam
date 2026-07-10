// Central keyboard-shortcut layer. ONE document-level keydown listener, ONE
// table that defines every binding, and a "?" help overlay + button-title hints
// generated from that same table, so the docs can never drift from behaviour.
//
// Design rules (each binding's comment adds specifics):
//  - Single-key shortcuts are suppressed while a text field, <select>, or
//    contenteditable holds focus, and while any modal (save / dir picker) is
//    open — the GUI is edit-heavy, so a bare key must never corrupt typing.
//  - No binding uses a bare Enter / Escape / Tab: those belong to the existing
//    field- and modal-level handlers. Our only combos on Enter/S still run
//    through the same suppress() guard.
//  - Actions map to the REAL control (click the button / call the grid method)
//    so state-aware labels, gating (disabled), and confirms are reused, never
//    duplicated. Recording/connection lockouts are expressed as `disabled`, so
//    respecting `disabled` reproduces the UI's own rules for free.

import { ModalFocus } from "./util.js";

// Digit -> tab, in a fixed order independent of the overflow-menu packing, so
// the same key always reaches the same tab whether it sits in the bar or "⋯".
// Plugin tabs that aren't loaded are simply absent from the DOM (no-op digit).
const TAB_ORDER = [
  ["record", "Record"],
  ["camera", "Camera"],
  ["view", "View"],
  ["benchmark", "Benchmark"],
  ["flywheel", "Flywheel"],
  ["twophoton", "2-Photon"],
  ["triggerbox", "triggerbox"],
];
const PLUGIN_TABS = new Set(["flywheel", "twophoton", "triggerbox"]);

// A slightly coarser step than the wheel's 1.15 so a keypress moves visibly.
const KBD_ZOOM = 1.3;

// Sections in the order the help overlay lists them.
const SECTION_ORDER = [
  "Global",
  "Recording",
  "Preview",
  "View tab",
  "Camera tab",
  "Benchmark tab",
  "Plugin tabs",
];

// A normalized signature for an event, e.g. "mod+Enter", "shift+r", "=", "[".
// "mod" folds Ctrl and Cmd together; single characters are lower-cased so a
// Shift-produced uppercase still matches (Shift+R -> "shift+r").
export function keySig(e) {
  const parts = [];
  if (e.ctrlKey || e.metaKey) parts.push("mod");
  if (e.shiftKey) parts.push("shift");
  if (e.altKey) parts.push("alt");
  parts.push(e.key.length === 1 ? e.key.toLowerCase() : e.key);
  return parts.join("+");
}

// Install the shortcut layer. `grid` is the CameraGrid (preview shortcuts call
// its methods); everything else is reached by stable id so no other refs are
// needed. Returns {openHelp, closeHelp, handleKey} for wiring/tests.
export function initShortcuts({ grid } = {}) {
  const byId = (id) => document.getElementById(id);

  // Click a control only when it's actionable. `disabled` mirrors the app's own
  // gating, so honouring it reproduces the UI rules. The globally-intended
  // buttons (record/theme/save) must work from any tab even while their panel
  // is display:none, so fire() ignores visibility; controls that live in a
  // hidden block (plugin status/flash) use fireVisible() instead.
  const fire = (id) => {
    const el = byId(id);
    if (el && !el.disabled) el.click();
  };
  const fireVisible = (id) => {
    const el = byId(id);
    if (el && !el.disabled && el.offsetParent !== null) el.click();
  };

  const activeTab = () =>
    byId("tabs")?.querySelector("button[data-tab].active")?.dataset.tab || null;
  const clickTab = (name) =>
    byId("tabs")?.querySelector(`button[data-tab="${name}"]`)?.click();

  // ---- the single source of truth ----
  const bindings = [
    // Recording — deliberately a modifier combo, never a bare key: a stray
    // keystroke must never start or (worse) abort a live trial. #record-button
    // is one control that dispatches start/abort/stop on its own state, and it
    // is disabled while disconnected/finishing/pending, so fire() is a no-op in
    // exactly those cases.
    {
      section: "Recording",
      caps: ["Ctrl", "Enter"],
      sigs: ["mod+Enter"],
      when: "global",
      hint: "record-button",
      desc: "Start / Stop / Abort recording",
      run: () => fire("record-button"),
    },

    // Preview — all operate on the selected tile (grid.selected).
    { section: "Preview", caps: ["["], sigs: ["["], when: "global",
      desc: "Select previous camera", run: () => grid?.selectPrev() },
    { section: "Preview", caps: ["]"], sigs: ["]"], when: "global",
      desc: "Select next camera", run: () => grid?.selectNext() },
    { section: "Preview", caps: ["F"], sigs: ["f"], when: "global",
      desc: "Maximize / restore tile", run: () => grid?.toggleMaximizeSelected() },
    { section: "Preview", caps: ["X"], sigs: ["x"], when: "global",
      desc: "Toggle crosshair overlay", run: () => fire("display-cross") },
    { section: "Preview", caps: ["="], sigs: ["=", "+", "shift++"], when: "global",
      desc: "Zoom in", run: () => grid?.zoomSelected(KBD_ZOOM) },
    { section: "Preview", caps: ["-"], sigs: ["-"], when: "global",
      desc: "Zoom out", run: () => grid?.zoomSelected(1 / KBD_ZOOM) },
    { section: "Preview", caps: ["0"], sigs: ["0"], when: "global",
      desc: "Reset zoom", run: () => grid?.resetZoomSelected() },

    // View tab.
    { section: "View tab", caps: ["R"], sigs: ["r"], when: "view", hint: "rotate-cw",
      desc: "Rotate 90° clockwise", run: () => grid?.applyView({ rotateDelta: 90 }, "selected") },
    { section: "View tab", caps: ["Shift", "R"], sigs: ["shift+r"], when: "view", hint: "rotate-ccw",
      desc: "Rotate 90° counter-clockwise", run: () => grid?.applyView({ rotateDelta: -90 }, "selected") },
    { section: "View tab", caps: ["H"], sigs: ["h"], when: "view", hint: "flip-h",
      desc: "Flip horizontal", run: () => grid?.applyView({ flipH: true }, "selected") },
    { section: "View tab", caps: ["V"], sigs: ["v"], when: "view", hint: "flip-v",
      desc: "Flip vertical", run: () => grid?.applyView({ flipV: true }, "selected") },
    { section: "View tab", caps: ["Backspace"], sigs: ["Backspace"], when: "view", hint: "view-reset",
      desc: "Reset rotation & flips", run: () => grid?.applyView({ reset: true }, "selected") },

    // Camera tab — no batch apply exists (widgets commit on their own change),
    // so the only useful key is jumping to the parameter filter.
    { section: "Camera tab", caps: ["/"], sigs: ["/"], when: "camera",
      desc: "Focus the parameter filter", run: () => byId("cam-filter")?.focus() },

    // Benchmark tab — behind Shift because it launches a long device run.
    { section: "Benchmark tab", caps: ["Shift", "B"], sigs: ["shift+b"], when: "benchmark", hint: "bench-run",
      desc: "Run / cancel benchmark", run: () => fire("bench-run") },

    // Plugin tabs — target whichever plugin tab is active. The status/flash
    // blocks are hidden unless relevant, so fireVisible() no-ops otherwise.
    { section: "Plugin tabs", caps: ["C"], sigs: ["c"], when: "plugin",
      desc: "Reconnect serial", run: () => fireVisible(`${activeTab()}-reconnect`) },
    { section: "Plugin tabs", caps: ["Shift", "F"], sigs: ["shift+f"], when: "plugin",
      desc: "Flash firmware", run: () => fireVisible(`${activeTab()}-fw-flash-btn`) },

    // Global.
    { section: "Global", caps: ["T"], sigs: ["t"], when: "global",
      desc: "Toggle light / dark theme", run: () => fire("theme-toggle") },
    { section: "Global", caps: ["Ctrl", "S"], sigs: ["mod+s"], when: "global", hint: "save-config-btn",
      desc: "Save configuration…", run: () => fire("save-config-btn") },
  ];

  // Tab switching 1..N — one dispatch entry per slot, one combined help row.
  TAB_ORDER.forEach(([name], i) => {
    bindings.push({
      section: "Global",
      sigs: [String(i + 1)],
      when: "global",
      hideInHelp: true,
      run: () => clickTab(name),
    });
  });

  // Tab-scoped bindings win over a same-key global one (none overlap today, but
  // keep the precedence explicit): sort non-global first.
  const ordered = [...bindings].sort(
    (a, b) => (a.when === "global") - (b.when === "global")
  );

  const contextMatches = (b, tab) =>
    b.when === "global" ||
    b.when === tab ||
    (b.when === "plugin" && PLUGIN_TABS.has(tab));

  function suppressed(e) {
    if (e.isComposing) return true;
    const t = e.target;
    if (
      t &&
      (t.isContentEditable ||
        (t.closest && t.closest("input, select, textarea, [contenteditable]")))
    ) {
      return true;
    }
    // Defer entirely while a modal owns the keyboard (each self-guards Escape).
    for (const id of ["save-dialog", "dir-dialog"]) {
      const m = byId(id);
      if (m && !m.classList.contains("hidden")) return true;
    }
    return false;
  }

  function dispatch(e) {
    const s = keySig(e);
    const tab = activeTab();
    for (const b of ordered) {
      if (b.sigs.includes(s) && contextMatches(b, tab)) {
        e.preventDefault();
        b.run();
        return true;
      }
    }
    return false;
  }

  // ---- help overlay (rendered from `bindings`, so it can't drift) ----
  const overlay = document.createElement("div");
  overlay.id = "shortcuts-overlay";
  overlay.className = "modal hidden";
  const card = document.createElement("div");
  card.className = "modal-card shortcuts-card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "true");
  card.setAttribute("aria-label", "Keyboard shortcuts");
  overlay.appendChild(card);
  document.body.appendChild(overlay);
  const focus = new ModalFocus(card);

  function keyCaps(caps) {
    const span = document.createElement("span");
    span.className = "shortcuts-keys";
    for (const cap of caps) {
      if (cap === "–") {
        const sep = document.createElement("span");
        sep.className = "shortcuts-sep";
        sep.textContent = "–";
        span.appendChild(sep);
      } else {
        const kbd = document.createElement("kbd");
        kbd.textContent = cap;
        span.appendChild(kbd);
      }
    }
    return span;
  }

  function buildHelp() {
    card.replaceChildren();
    const head = document.createElement("div");
    head.className = "shortcuts-head";
    const h = document.createElement("h3");
    h.textContent = "Keyboard shortcuts";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "btn shortcuts-close";
    close.textContent = "✕";
    close.title = "Close (Esc)";
    close.addEventListener("click", closeHelp);
    head.append(h, close);
    card.appendChild(head);

    // One combined row for the tab-switch digits (the per-digit bindings are
    // hidden from help).
    const tabRow = {
      caps: ["1", "–", String(TAB_ORDER.length)],
      desc: "Switch tab (" + TAB_ORDER.map(([, l]) => l).join(", ") + ")",
    };

    for (const section of SECTION_ORDER) {
      const rows = bindings.filter((b) => b.section === section && !b.hideInHelp);
      if (section === "Global") rows.push(tabRow);
      if (!rows.length) continue;
      const grp = document.createElement("div");
      grp.className = "shortcuts-group";
      const st = document.createElement("div");
      st.className = "shortcuts-group-head";
      st.textContent = section;
      grp.appendChild(st);
      for (const r of rows) {
        const row = document.createElement("div");
        row.className = "shortcuts-row";
        const desc = document.createElement("span");
        desc.className = "shortcuts-desc";
        desc.textContent = r.desc;
        row.append(keyCaps(r.caps), desc);
        grp.appendChild(row);
      }
      card.appendChild(grp);
    }
  }

  let helpOpen = false;
  function openHelp() {
    if (helpOpen) return;
    helpOpen = true;
    overlay.classList.remove("hidden");
    focus.activate();
  }
  function closeHelp() {
    if (!helpOpen) return;
    helpOpen = false;
    overlay.classList.add("hidden");
    focus.deactivate();
  }
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeHelp();
  });
  byId("shortcuts-help-btn")?.addEventListener("click", openHelp);
  buildHelp();

  // Append the shortcut to each anchor button's tooltip. Skip controls whose
  // title is rewritten dynamically (e.g. the theme toggle) so we don't fight
  // their own updates — those live only in the overlay.
  for (const b of bindings) {
    if (!b.hint) continue;
    const el = byId(b.hint);
    if (el && el.title && !el.dataset.kbdHinted) {
      el.title = `${el.title} (${b.caps.join("+")})`;
      el.dataset.kbdHinted = "1";
    }
  }

  function handleKey(e) {
    // Help overlay is modal: '?' / Esc close it and it swallows everything else
    // (handled first so it works even while the overlay itself holds focus).
    if (helpOpen) {
      if (e.key === "Escape" || e.key === "?") {
        e.preventDefault();
        closeHelp();
      }
      return;
    }
    if (suppressed(e)) return;
    if (e.key === "?") {
      e.preventDefault();
      openHelp();
      return;
    }
    dispatch(e);
  }

  document.addEventListener("keydown", handleKey);

  return { openHelp, closeHelp, handleKey };
}
