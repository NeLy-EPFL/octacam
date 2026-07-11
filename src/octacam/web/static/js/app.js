// Entry point: fetch system + state, build UI, open the WebSocket.

import { api, clampInput, sleep } from "./util.js";
import { ReconnectingSocket } from "./ws.js";
import { CameraGrid } from "./grid.js";
import { RecordTab } from "./record.js";
import { ViewTab } from "./view.js";
import { CameraTab } from "./camera.js";
import { BenchmarkTab } from "./diagnose.js";
import { initSidebarResize } from "./resize.js";
import { initTheme, applyConfigTheme } from "./theme.js";
import { SaveDialog } from "./save.js";
import { ShutdownDialog } from "./shutdown.js";
import { DirPicker } from "./dirpicker.js";
import { initShortcuts } from "./shortcuts.js";
import { initUpdateBanner } from "./update.js";

// The server replays a recent slice of its event backlog on (re)connect, so the
// client keeps a generous scrollback to actually hold that history plus the
// live tail; the panel (#events) is scrollable.
const MAX_EVENTS = 200;
const events = [];

function addEvent(evt) {
  events.push(evt);
  while (events.length > MAX_EVENTS) events.shift();
  const list = document.getElementById("events");
  list.replaceChildren(
    ...events.map((e) => {
      const div = document.createElement("div");
      div.className = `event ${e.level || "info"}`;
      const time = document.createElement("time");
      time.textContent = new Date(e.time * 1000).toLocaleTimeString([], {
        hour12: false,
      });
      const span = document.createElement("span");
      span.textContent = e.message;
      div.append(time, span);
      return div;
    })
  );
  list.scrollTop = list.scrollHeight;
}

// Wire the tab bar and its "priority+" overflow menu. Returns a reflow() the
// caller runs once the set of tabs is final (plugin tabs are removed after this
// is called), so the overflow packing is computed against the real tab list.
function setupTabs() {
  const nav = document.getElementById("tabs");

  // Tabs that don't fit the sidebar width collapse into a "⋯" dropdown, so the
  // bar stays a single row no matter how many plugins contribute tabs. The
  // active tab is always kept out of the menu.
  const moreBtn = document.createElement("button");
  moreBtn.type = "button";
  moreBtn.id = "tabs-more";
  moreBtn.className = "tabs-more";
  moreBtn.setAttribute("aria-haspopup", "true");
  moreBtn.setAttribute("aria-expanded", "false");
  moreBtn.title = "More tabs";
  moreBtn.textContent = "⋯";
  moreBtn.hidden = true;
  const menu = document.createElement("div");
  menu.id = "tabs-menu";
  menu.className = "tabs-menu";
  nav.append(moreBtn, menu);

  let order = null; // stable tab-button list, captured after plugin tabs settle
  const closeMenu = () => {
    menu.classList.remove("open");
    moreBtn.setAttribute("aria-expanded", "false");
  };

  function reflow() {
    if (!order) order = [...nav.querySelectorAll("button[data-tab]")];
    // Put every tab back in the row (before the menu button) and measure.
    for (const b of order) nav.insertBefore(b, moreBtn);
    menu.replaceChildren();
    moreBtn.hidden = true;
    closeMenu();

    const avail = nav.clientWidth;
    const widths = order.map((b) => b.offsetWidth);
    if (widths.reduce((a, w) => a + w, 0) <= avail) return; // all fit

    moreBtn.hidden = false;
    // Keep a contiguous prefix of tabs visible and overflow the rest, so tabs
    // never reorder or leave a gap (stop at the first one that doesn't fit).
    let used = moreBtn.offsetWidth;
    let cut = order.length;
    for (let i = 0; i < order.length; i++) {
      if (used + widths[i] <= avail) used += widths[i];
      else { cut = i; break; }
    }
    const visible = order.slice(0, cut);
    // Keep the active tab visible: if it overflowed, evict trailing visible tabs
    // until it fits, then show it at the end of the row.
    const active = order.find((b) => b.classList.contains("active"));
    if (active && !visible.includes(active)) {
      while (visible.length && used + active.offsetWidth > avail) {
        used -= visible.pop().offsetWidth;
      }
      visible.push(active);
    }
    for (const b of order) if (!visible.includes(b)) menu.appendChild(b);
  }

  nav.addEventListener("click", (e) => {
    if (e.target.closest("#tabs-more")) {
      const open = menu.classList.toggle("open");
      moreBtn.setAttribute("aria-expanded", String(open));
      return;
    }
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    for (const b of nav.querySelectorAll("button[data-tab]")) {
      b.classList.toggle("active", b === btn);
    }
    for (const panel of document.querySelectorAll(".tab")) {
      panel.classList.toggle("active", panel.id === `tab-${btn.dataset.tab}`);
    }
    closeMenu();
    reflow(); // pull the newly-active tab out of the overflow menu if it was in it
    // Let tabs (incl. plugin tabs) react when they become visible — e.g. the
    // triggerbox tab re-reads the Record-tab fps to redraw its timing diagram.
    document.dispatchEvent(
      new CustomEvent("tab-shown", { detail: { tab: btn.dataset.tab } })
    );
  });

  // Close the dropdown when clicking outside the tab bar.
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#tabs")) closeMenu();
  });

  // Re-pack when the sidebar is resized. reflow() never changes nav's own width
  // (the menu is absolutely positioned), so this can't loop.
  if (typeof ResizeObserver !== "undefined") {
    let lastW = 0;
    new ResizeObserver(() => {
      const w = Math.round(nav.clientWidth);
      if (w && w !== lastW) { lastW = w; reflow(); }
    }).observe(nav);
  }

  return reflow;
}

function wsUrl() {
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  return `${proto}${location.host}/api/ws`;
}

// Inject a plugin's stylesheet (served from its own /plugins/<name>/ folder),
// once per href. Plugin CSS lives with the plugin rather than in core style.css.
function loadPluginCss(href) {
  if (document.querySelector(`link[data-plugin-css="${href}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  link.dataset.pluginCss = href;
  document.head.appendChild(link);
}

async function loadInitial() {
  const banner = document.getElementById("banner");
  for (;;) {
    try {
      const [sys, snap] = await Promise.all([
        api("GET", "/api/system"),
        api("GET", "/api/state"),
      ]);
      if (sys.ok && snap.ok) {
        banner.classList.add("hidden");
        return [sys.data, snap.data];
      }
    } catch {
      // server not reachable yet
    }
    banner.textContent = "Cannot reach octacam server — retrying…";
    banner.classList.remove("hidden");
    await sleep(2000);
  }
}

async function main() {
  initTheme();
  initSidebarResize();
  const [system, snap] = await loadInitial();

  // Apply the rig's configured default theme now that the config has loaded
  // (a per-browser toggle choice in localStorage still wins).
  applyConfigTheme(system.theme);

  const versionEl = document.getElementById("version");
  versionEl.textContent = `octacam ${system.version}`;
  versionEl.title = system.config_dir;

  // Read-only "a newer octacam is available" banner (dismissible; see update.js).
  // The server computes system.update; octacam never self-updates from here.
  initUpdateBanner(system.update);

  const reflowTabs = setupTabs();
  // Show optional plugin tabs only when the plugin is loaded. A not-ready
  // plugin still shows its tab (with a "serial unavailable" notice and a
  // Reconnect button) so a missing/unplugged board is diagnosable. Plugin tab
  // buttons and panels carry data-plugin="<name>" in index.html, so this is
  // name-agnostic — no per-plugin code here.
  for (const el of document.querySelectorAll("[data-plugin]")) {
    if (!system.plugins?.[el.dataset.plugin]) el.remove();
  }
  reflowTabs(); // pack the (now-final) tab set into the bar + overflow menu

  let cameraTab = null;
  let viewTab = null;
  let saveDialog = null;
  let dirPicker = null;

  // The grid reports (via onViewChange) whenever the resolution/pause state the
  // server should honor changes — a tile resized, maximized, or zoomed. Coalesce
  // a burst of those into one WS message per animation frame, and skip the send
  // when the composed spec is unchanged.
  let viewRaf = 0;
  let lastViewJson = "";
  function sendViewNow() {
    viewRaf = 0;
    const cameras = grid.getViewSpec();
    const json = JSON.stringify(cameras);
    if (json === lastViewJson) return;
    if (sock.send({ type: "view", cameras })) lastViewJson = json;
  }
  function scheduleViewSend() {
    if (!viewRaf) viewRaf = requestAnimationFrame(sendViewNow);
  }
  // devicePixelRatio can change (browser zoom, dragging the window between
  // monitors) with no element resize, so refresh the spec on window resize too.
  window.addEventListener("resize", scheduleViewSend);

  const grid = new CameraGrid(document.getElementById("grid"), system.cameras, {
    onSelect: (i) => {
      cameraTab?.selectCamera(i);
      viewTab?.selectCamera(i);
    },
    onRename: (i, name) => cameraTab?.renameCamera(i, name),
    onViewChange: scheduleViewSend,
  });
  viewTab = new ViewTab({
    cameras: system.cameras,
    grid,
    onSelect: (i) => grid.select(i),
  });

  let record = null;
  const notify = (level, message) => {
    const evt = { time: Date.now() / 1000, level, message };
    addEvent(evt);
    record?.handleEvent(evt);
  };

  let connMode = "offline";
  let userDisconnected = false; // user clicked Disconnect — suppress reconnect
  let serverStopped = false; // server was shut down from the UI
  let recordingActive = false; // a trial is in progress on the rig
  let recordingsMade = 0; // recordings finished this session (for shut-down-&-process)
  let peerCount = 1; // browsers connected to the server (control is shared)

  const sock = new ReconnectingSocket(wsUrl(), {
    onOpen: () => {
      setConnectionMode("connected");
      // A (re)connected socket starts with no server-side view state, so resend
      // the current spec unconditionally.
      lastViewJson = "";
      scheduleViewSend();
    },
    onClose: () =>
      setConnectionMode(
        serverStopped
          ? "stopped"
          : userDisconnected
            ? "offline"
            : "reconnecting"
      ),
    onFrame: (frame) => grid.handleFrame(frame),
    onJson: (msg) => handleJson(msg),
  });

  // Plugin tabs are loaded dynamically below from each plugin's own folder.
  // Declared before `record` so its getPluginParams closure captures this Map
  // by reference; the Map is read only at start-recording time, by which point
  // the loader loop has populated it.
  const pluginTabs = new Map();

  record = new RecordTab({
    formats: system.formats,
    // Collect each plugin's start-params slice ({name: params}); record.js
    // packs it into POST /api/recording/start as plugin_params. No plugin names
    // are hardcoded here.
    getPluginParams: () => {
      const params = {};
      for (const [name, tab] of pluginTabs) {
        const slice = tab.getStartParams?.();
        if (slice != null) params[name] = slice;
      }
      return params;
    },
    notify,
  });
  // Enable the "managed" trigger-source option only when a driving plugin is
  // loaded (e.g. the triggerbox); the server computes this from plugin capability.
  record.setManagedAvailable(!!system.managed_trigger_available);

  // Disconnecting this browser (the rig keeps recording) is a rare, easy-to-
  // misclick action, so — like the Record tab's own rarely-used knobs — it
  // stays tucked behind the same Advanced-options switch.
  {
    const advancedToggle = document.getElementById("record-advanced-toggle");
    const syncDisconnectVisibility = () => {
      document.getElementById("disconnect-btn").hidden = !advancedToggle.checked;
    };
    syncDisconnectVisibility();
    advancedToggle.addEventListener("change", syncDisconnectVisibility);
  }

  // Each plugin that ships a UI advertises its entry module + optional CSS in
  // /api/system; import it from the plugin's own /plugins/<name>/ folder and
  // instantiate its tab. Per-plugin try/catch so one broken/missing module
  // can't blank the page or block the others (mirrors the backend's "a broken
  // plugin must not crash core"). The ctx is a superset bag each tab
  // destructures — api/clampInput are passed in (the tab can't import core
  // util.js once served from its own folder).
  for (const [name, info] of Object.entries(system.plugins ?? {})) {
    if (!info.web?.module) continue;
    try {
      if (info.web.css) loadPluginCss(info.web.css);
      const mod = await import(info.web.module);
      const Tab = mod.default;
      if (typeof Tab !== "function") {
        console.warn(`plugin ${name}: UI module has no default export`);
        continue;
      }
      pluginTabs.set(
        name,
        new Tab({
          name,
          status: info,
          send: (m) => sock.send(m),
          notify,
          api,
          clampInput,
          getRecordSettings: () => record.settings,
        })
      );
    } catch (e) {
      console.warn(`plugin ${name}: UI module failed to load`, e);
    }
  }

  cameraTab = new CameraTab({
    cameras: system.cameras,
    notify,
    onSelect: (i) => grid.select(i),
    onRename: (i, name) => {
      grid.setName(i, name);
      viewTab?.applyName(i, name);
    },
  });
  const benchmark = new BenchmarkTab({ notify });
  saveDialog = new SaveDialog({
    grid,
    notify,
    getRecording: () => recordingActive,
  });
  dirPicker = new DirPicker({
    notify,
    onPick: (path) => record.setRecordDir(path),
    getStart: () => record.getRecordDir(),
  });

  // Establish an initial current camera so the grid highlight and both pickers
  // agree from the start.
  if (system.cameras.length) grid.select(0);

  // Global keyboard shortcuts (one document-level listener; see shortcuts.js).
  // Wired here, after grid/record/cameraTab/benchmark/plugins exist, so its
  // bindings reach real, built controls; it drives the same buttons/grid
  // methods the mouse does, inheriting their gating.
  initShortcuts({ grid });

  // Connection has five modes: "connecting" (initial handshake / manual
  // reconnect, calm), "connected", "reconnecting" (unexpected drop),
  // "offline" (user disconnected, calm) and "stopped" (server shut down).
  function setConnectionMode(mode) {
    connMode = mode;
    const connected = mode === "connected";

    const banner = document.getElementById("banner");
    if (mode === "reconnecting") {
      banner.textContent = "Disconnected — reconnecting…";
      banner.classList.remove("hidden");
    } else if (mode === "stopped") {
      // Actionable: the socket won't come back on its own, so point at the
      // recovery (the relabelled Reconnect button reloads the page).
      banner.textContent = "Server stopped — reload to reconnect.";
      banner.classList.remove("hidden");
    } else {
      // connected, user-initiated offline, or the calm initial "connecting".
      banner.classList.add("hidden");
    }
    banner.classList.toggle("stopped", mode === "stopped");

    const connState = document.getElementById("conn-state");
    connState.textContent =
      mode === "connected"
        ? "connected"
        : mode === "connecting"
          ? "connecting…"
          : mode === "stopped"
            ? "server stopped"
            : "disconnected";
    connState.className =
      mode === "connected"
        ? "online"
        : mode === "connecting"
          ? "connecting"
          : "offline";

    record.setConnected(connected);
    grid.setConnected(connected);
    cameraTab?.setConnected(connected);
    benchmark.setConnected(connected);
    saveDialog?.setConnected(connected);
    dirPicker?.setConnected(connected);
    // Plugin tabs own their own enable/disable (e.g. the Flywheel tab also
    // gates its fields on the serial port being open and stops a jog on
    // disconnect); just forward the connection state to each.
    for (const tab of pluginTabs.values()) tab.setConnected?.(connected);
    const viewFields = document.getElementById("view-fields");
    if (viewFields) viewFields.disabled = !connected;
    // Presence is only meaningful while connected; the server resends the
    // count on (re)connect, so just clear it when the socket is down.
    if (!connected) updatePeers(1);

    const disconnectBtn = document.getElementById("disconnect-btn");
    disconnectBtn.textContent =
      mode === "offline"
        ? "🔗" // Connect: re-establish the socket after a voluntary disconnect
        : mode === "stopped"
          ? "🔄" // Reconnect: the click actually reloads the page
          : "🔌"; // Disconnect: unplug this browser only
    disconnectBtn.title =
      mode === "stopped"
        ? "Reload the page to reconnect to the server"
        : mode === "offline"
          ? "Reconnect this browser to the server"
          : "Disconnect this browser (the recording keeps running on the rig)";
    disconnectBtn.setAttribute(
      "aria-label",
      mode === "offline" ? "Connect" : mode === "stopped" ? "Reconnect" : "Disconnect"
    );
    // Stay clickable when stopped so recovery doesn't need the browser's own
    // reload control; the click handler reloads the page in that mode.
    disconnectBtn.disabled = false;
    document.getElementById("shutdown-btn").disabled = mode === "stopped";
  }

  // Shared control: surface how many browsers are connected so an operator
  // knows when they are not alone. Shown only when others are present.
  function updatePeers(count) {
    peerCount = count;
    const el = document.getElementById("peers");
    const others = count - 1;
    if (others > 0) {
      el.textContent = `${count} connected`;
      el.title = `${others} other browser${others === 1 ? "" : "s"} connected to this server`;
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  }

  function applyCameraStats(cameras) {
    const failed = [];
    cameras.forEach((c, i) => {
      const index = grid.indexBySerial.has(c.serial)
        ? grid.indexBySerial.get(c.serial)
        : i;
      grid.updateStats(index, {
        fps: c.fps,
        dropped: c.dropped,
        writerFailed: c.writer_failed,
      });
      if (c.writer_failed) failed.push(c.name || `camera ${index}`);
    });
    // Surface any writer failure as a persistent, prominent Record-tab banner
    // (the grid badge covers the tiles; this covers the operator watching Record).
    record.setWriterFailure(failed);
  }

  function handleJson(msg) {
    switch (msg.type) {
      case "state":
      case "telemetry":
        recordingActive = ["waiting", "recording", "finishing"].includes(
          msg.state
        );
        recordingsMade = msg.recordings_made ?? recordingsMade;
        record.applyState(msg);
        benchmark.applyState(msg);
        grid.setRecording(recordingActive);
        cameraTab?.setRecording(recordingActive);
        if (Array.isArray(msg.cameras)) applyCameraStats(msg.cameras);
        break;
      case "settings":
        record.applySettings(msg);
        benchmark.applySettings(msg);
        break;
      case "diagnostics":
        benchmark.applyReport(msg);
        break;
      case "diagnostics_progress":
        benchmark.applyProgress(msg);
        break;
      case "camera_params":
        cameraTab?.applyParams(msg);
        break;
      case "camera_features_dirty":
        cameraTab?.applyFeaturesDirty(msg);
        // Let plugin tabs (e.g. triggerbox's timing diagram) react to a camera
        // feature change without coupling core to each plugin — the exposure or
        // trigger delay they read may have just moved.
        document.dispatchEvent(
          new CustomEvent("camera-features-changed", { detail: { index: msg.index } })
        );
        break;
      case "camera_name":
        cameraTab?.applyName(msg);
        break;
      case "presence":
        updatePeers(msg.clients);
        break;
      case "event":
        addEvent(msg);
        record.handleEvent(msg);
        break;
      case "twophoton_state":
        pluginTabs.get("twophoton")?.applyState(msg);
        break;
      case "triggerbox_state":
        pluginTabs.get("triggerbox")?.applyState(msg);
        break;
    }
  }

  document.getElementById("disconnect-btn").addEventListener("click", () => {
    if (connMode === "stopped") {
      // The server is gone; a full reload re-runs loadInitial + the handshake.
      location.reload();
    } else if (connMode === "offline") {
      userDisconnected = false;
      setConnectionMode("connecting");
      sock.connect();
    } else {
      userDisconnected = true;
      sock.disconnect();
      setConnectionMode("offline");
    }
  });

  const shutdownDialog = new ShutdownDialog();
  document.getElementById("shutdown-btn").addEventListener("click", async () => {
    const choice = await shutdownDialog.confirm({
      recordingActive,
      hasWork: recordingsMade > 0,
      peerCount,
    });
    if (choice === "cancel") return;
    let r;
    try {
      r = await api("POST", "/api/shutdown", {
        process_after: choice === "process",
      });
    } catch {
      notify("error", "Shutdown request failed: server unreachable");
      return;
    }
    if (r.status === 409) {
      notify("warning", "Stop the recording before shutting down.");
      return;
    }
    if (!r.ok) {
      notify("error", r.data?.detail || `Shutdown failed (HTTP ${r.status})`);
      return;
    }
    serverStopped = true;
    sock.disconnect();
    setConnectionMode("stopped");
  });

  // Recording continues on the rig if the tab closes, but a stray close
  // mid-trial is worth a speed-bump (browsers show a generic prompt).
  window.addEventListener("beforeunload", (e) => {
    if (recordingActive) {
      e.preventDefault();
      e.returnValue = "";
    }
  });

  record.applyState(snap);
  benchmark.applyState(snap);
  // Show a calm "connecting…" state through the initial WS handshake, so a
  // working HTTP page whose socket never upgrades reads as connecting rather
  // than a dead-looking blank status.
  setConnectionMode("connecting");
  sock.connect();
}

main();
