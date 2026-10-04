// Keyboard shortcuts: one document-level keydown listener and one binding
// table, from which the "?" help overlay and the button-title hints are also
// generated, so they can't drift. Invariants:
//  - Every binding goes through suppressed(): no shortcut fires while a text
//    field, <select> or contenteditable has focus, or while a modal is open.
//  - No binding uses a bare Enter / Escape / Tab (the fields and modals own them).
//  - Actions click the real control or call the real grid method, so its
//    gating (`disabled`), labels and confirms apply unchanged.

import { ModalFocus } from "./util.js";

// Digit -> tab in a fixed order, whether the tab sits in the bar or the "⋯"
// menu. An unloaded plugin's tab is absent, so its digit does nothing.
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

// Install the shortcut layer. `grid` takes the preview shortcuts; everything
// else is reached by id. Returns {openHelp, closeHelp, handleKey}.
export function initShortcuts({ grid } = {}) {
  const byId = (id) => document.getElementById(id);

  // Click a control unless it is disabled. fire() works from any tab (the
  // record/theme/save buttons may sit in a hidden panel); fireVisible() is for
  // controls in blocks that are hidden unless relevant (plugin status/flash).
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

  const bindings = [
    // A modifier combo, never a bare key: a stray keystroke must never start or
    // abort a trial. #record-button starts/stops/aborts by state and is
    // disabled exactly when it must not act.
    {
      section: "Recording",
      caps: ["Ctrl", "Enter"],
      sigs: ["mod+Enter"],
      when: "global",
      hint: "record-button",
      desc: "Start / Stop / Abort recording",
      run: () => fire("record-button"),
    },

    // Preview: the selected tile.
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

    // Camera tab widgets commit on their own change; only the filter needs a key.
    { section: "Camera tab", caps: ["/"], sigs: ["/"], when: "camera",
      desc: "Focus the parameter filter", run: () => byId("cam-filter")?.focus() },

    // Behind Shift: it launches a long device run.
    { section: "Benchmark tab", caps: ["Shift", "B"], sigs: ["shift+b"], when: "benchmark", hint: "bench-run",
      desc: "Run / cancel benchmark", run: () => fire("bench-run") },

    // The active plugin tab's controls, when shown.
    { section: "Plugin tabs", caps: ["C"], sigs: ["c"], when: "plugin",
      desc: "Reconnect serial", run: () => fireVisible(`${activeTab()}-reconnect`) },
    { section: "Plugin tabs", caps: ["Shift", "F"], sigs: ["shift+f"], when: "plugin",
      desc: "Flash firmware", run: () => fireVisible(`${activeTab()}-fw-flash-btn`) },

    { section: "Global", caps: ["T"], sigs: ["t"], when: "global",
      desc: "Toggle light / dark theme", run: () => fire("theme-toggle") },
    { section: "Global", caps: ["Ctrl", "S"], sigs: ["mod+s"], when: "global", hint: "save-config-btn",
      desc: "Save configuration…", run: () => fire("save-config-btn") },
  ];

  // Tab switching: one binding per digit, one combined help row.
  TAB_ORDER.forEach(([name], i) => {
    bindings.push({
      section: "Global",
      sigs: [String(i + 1)],
      when: "global",
      hideInHelp: true,
      run: () => clickTab(name),
    });
  });

  // A tab-scoped binding wins over a global one on the same key.
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
    // An open modal owns the keyboard (each guards its own Escape). Found by
    // class, not by id, so a new modal can't be missed: with focus on a modal's
    // <button>, Ctrl+Enter would otherwise start a recording behind it. The
    // help overlay is handled before this runs.
    for (const m of document.querySelectorAll(".modal")) {
      if (!m.classList.contains("hidden")) return true;
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

  // ---- help overlay ----
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

  // Append the shortcut to each anchor button's tooltip. Controls that rewrite
  // their own title (the theme toggle) have no `hint`.
  for (const b of bindings) {
    if (!b.hint) continue;
    const el = byId(b.hint);
    if (el && el.title && !el.dataset.kbdHinted) {
      el.title = `${el.title} (${b.caps.join("+")})`;
      el.dataset.kbdHinted = "1";
    }
  }

  function handleKey(e) {
    // The open help overlay swallows every key; '?' or Esc closes it.
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
