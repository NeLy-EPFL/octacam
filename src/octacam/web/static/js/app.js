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

// Room for the backlog the server replays on (re)connect plus the live tail.
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

// A rig that opened fewer cameras than its config asks for must not pass for a
// healthy one with a smaller grid: name each missing camera and why.
function showMissingCameras(sys) {
  const el = document.getElementById("rig-alert");
  const missing = sys.missing_cameras;
  el.classList.toggle("hidden", missing.length === 0);
  if (missing.length === 0) return;
  const opened = sys.cameras.length;
  const title = document.createElement("div");
  title.textContent =
    `⚠ Incomplete rig: ${opened} of ${opened + missing.length} configured ` +
    "cameras opened. Missing:";
  const list = document.createElement("ul");
  for (const { serial, reason } of missing) {
    const item = document.createElement("li");
    item.textContent = `${serial}: ${reason}`;
    list.append(item);
  }
  el.replaceChildren(title, list);
}

// Wire the tab bar: tabs that don't fit collapse into a "⋯" menu, so the bar
// stays one row; the active tab is never in the menu. Returns reflow(), to run
// once the tab set is final (after unloaded plugins' tabs are removed).
function setupTabs() {
  const nav = document.getElementById("tabs");

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
    for (const b of order) nav.insertBefore(b, moreBtn);
    menu.replaceChildren();
    moreBtn.hidden = true;
    closeMenu();

    const avail = nav.clientWidth;
    const widths = order.map((b) => b.offsetWidth);
    if (widths.reduce((a, w) => a + w, 0) <= avail) return; // all fit

    moreBtn.hidden = false;
    // A contiguous prefix stays visible, so tabs never reorder or leave a gap.
    let used = moreBtn.offsetWidth;
    let cut = order.length;
    for (let i = 0; i < order.length; i++) {
      if (used + widths[i] <= avail) used += widths[i];
      else { cut = i; break; }
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
    reflow();
    // Plugin tabs redraw on this (triggerbox re-reads the Record-tab fps).
    document.dispatchEvent(
      new CustomEvent("tab-shown", { detail: { tab: btn.dataset.tab } })
    );
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest("#tabs")) closeMenu();
  });

  // Re-pack on sidebar resize. reflow() never changes nav's own width (the menu
  // is absolutely positioned), so this can't loop.
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

// Inject a plugin's stylesheet (from its /plugins/<name>/ folder) once.
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

  applyConfigTheme(system.theme);

  const versionEl = document.getElementById("version");
  versionEl.textContent = `octacam ${system.version}`;
  versionEl.title = system.config_dir;

  initUpdateBanner(system.update);
  showMissingCameras(system);

  const reflowTabs = setupTabs();
  // Drop the tabs of plugins that aren't loaded. A loaded but not-ready plugin
  // keeps its tab (with a Reconnect button), so an unplugged board is visible.
  for (const el of document.querySelectorAll("[data-plugin]")) {
    if (!system.plugins?.[el.dataset.plugin]) el.remove();
  }
  reflowTabs();

  // Camera-dependent UI is built by buildCameras() once the camera list
  // arrives (now, or with the init's `system` push); the closures below read
  // these by reference. systemReady/initError mirror the controller's init.
  let grid = null;
  let cameraTab = null;
  let viewTab = null;
  let saveDialog = null;
  let dirPicker = null;
  let systemReady = Boolean(system.ready);
  let initError = system.init_error || null;

  // The grid's view spec (per-tile resolution, crop, pause) goes up at most
  // once per animation frame, and only when it changed.
  let viewRaf = 0;
  let lastViewJson = "";
  function sendViewNow() {
    viewRaf = 0;
    if (!grid) return;
    const cameras = grid.getViewSpec();
    const json = JSON.stringify(cameras);
    if (json === lastViewJson) return;
    if (sock.send({ type: "view", cameras })) lastViewJson = json;
  }
  function scheduleViewSend() {
    if (!viewRaf) viewRaf = requestAnimationFrame(sendViewNow);
  }
  // devicePixelRatio can change (browser zoom, another monitor) with no
  // element resize.
  window.addEventListener("resize", scheduleViewSend);

  let record = null;
  const notify = (level, message) => {
    const evt = { time: Date.now() / 1000, level, message };
    addEvent(evt);
    record?.handleEvent(evt);
  };

  let connMode = "offline";
  let syncDisconnectVisibility = null; // set below; setConnectionMode may run first
  let userDisconnected = false; // user clicked Disconnect — suppress reconnect
  let serverStopped = false; // server was shut down from the UI
  let recordingActive = false;
  let recordingsMade = 0; // this session's, for "Shut down & process"
  let peerCount = 1; // browsers connected to the server (control is shared)

  const sock = new ReconnectingSocket(wsUrl(), {
    onOpen: () => {
      setConnectionMode("connected");
      // A new socket has no server-side view state: resend the spec.
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
    onFrame: (frame) => grid?.handleFrame(frame),
    onJson: (msg) => handleJson(msg),
  });

  // Filled by the plugin loader below; read only when a recording starts.
  const pluginTabs = new Map();

  record = new RecordTab({
    formats: system.formats,
    // {name: start-params slice}, sent as the start request's plugin_params.
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
  record.setManagedAvailable(!!system.managed_trigger_available);

  // Disconnect (a rare, easy-to-misclick action) hides behind the Advanced
  // switch while connected. Once the socket is down the button is the
  // Connect/Reconnect control and always shown: the switch sits in
  // #record-fields, which a disconnect disables, so it can't reveal it then.
  {
    const advancedToggle = document.getElementById("record-advanced-toggle");
    syncDisconnectVisibility = () => {
      const needRecovery = connMode !== "connected";
      document.getElementById("disconnect-btn").hidden = !(
        advancedToggle.checked || needRecovery
      );
    };
    syncDisconnectVisibility();
    advancedToggle.addEventListener("change", syncDisconnectVisibility);
  }

  // Import each plugin's UI module (advertised in /api/system) and build its
  // tab; one broken module must not blank the page. A tab served from
  // /plugins/<name>/ can't import ./util.js, so api/clampInput come in the ctx.
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

  const benchmark = new BenchmarkTab({ notify });
  dirPicker = new DirPicker({
    notify,
    onPick: (path) => record.setRecordDir(path),
    getStart: () => record.getRecordDir(),
  });

  // The grid is built later, so the shortcuts reach it through this stable
  // forwarder (a no-op until it exists).
  const gridShortcuts = {
    selectPrev: () => grid?.selectPrev(),
    selectNext: () => grid?.selectNext(),
    toggleMaximizeSelected: () => grid?.toggleMaximizeSelected(),
    zoomSelected: (f) => grid?.zoomSelected(f),
    resetZoomSelected: () => grid?.resetZoomSelected(),
    applyView: (v, target) => grid?.applyView(v, target),
  };
  initShortcuts({ grid: gridShortcuts });

  // Camera controls need the socket and open cameras (systemReady); plugin
  // tabs gate on their own serial readiness, so they only track the socket.
  function syncEnabled() {
    const connected = connMode === "connected";
    const camReady = connected && systemReady;
    record.setConnected(camReady);
    grid?.setConnected(camReady);
    cameraTab?.setConnected(camReady);
    benchmark.setConnected(camReady);
    // Until the Save dialog exists (no cameras yet, or init failed) its button
    // has nothing to save, so keep it disabled.
    if (saveDialog) {
      saveDialog.setConnected(camReady);
    } else {
      const saveBtn = document.getElementById("save-config-btn");
      if (saveBtn) saveBtn.disabled = true;
    }
    dirPicker?.setConnected(connected);
    for (const tab of pluginTabs.values()) tab.setConnected?.(connected);
    const viewFields = document.getElementById("view-fields");
    if (viewFields) viewFields.disabled = !camReady;
    // The server resends the presence count on (re)connect.
    if (!connected) updatePeers(1);
  }

  // Modes: "connecting" (handshake or manual reconnect), "connected",
  // "reconnecting" (unexpected drop), "offline" (user disconnected) and
  // "stopped" (server shut down).
  function setConnectionMode(mode) {
    connMode = mode;
    const connected = mode === "connected";

    const banner = document.getElementById("banner");
    if (mode === "reconnecting") {
      banner.textContent = "Disconnected — reconnecting…";
      banner.classList.remove("hidden");
    } else if (mode === "stopped") {
      // The socket won't come back on its own.
      banner.textContent = "Server stopped — reload to reconnect.";
      banner.classList.remove("hidden");
    } else {
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

    syncEnabled();

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
    // Clickable in every mode: when stopped, the click reloads the page.
    disconnectBtn.disabled = false;
    syncDisconnectVisibility?.();
    document.getElementById("shutdown-btn").disabled = mode === "stopped";
  }

  // Control is shared: show the browser count when others are connected.
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
    if (!grid) return; // stats arrive before the grid is built during init
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
    record.setWriterFailure(failed);
  }

  function handleJson(msg) {
    switch (msg.type) {
      case "system":
        applySystem(msg);
        break;
      case "state":
      case "telemetry":
        recordingActive = ["waiting", "recording", "finishing"].includes(
          msg.state
        );
        recordingsMade = msg.recordings_made ?? recordingsMade;
        // A state/telemetry tick may report readiness before the `system` push
        // arrives, or a late init failure; keep the gating + placeholder in sync.
        if (typeof msg.ready === "boolean" && msg.ready !== systemReady) {
          systemReady = msg.ready;
          syncEnabled();
        }
        if ("init_error" in msg && (msg.init_error || null) !== initError) {
          initError = msg.init_error || null;
          if (!grid) showGridPlaceholder();
        }
        record.applyState(msg);
        benchmark.applyState(msg);
        grid?.setRecording(recordingActive);
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
      case "camera_features_dirty":
        cameraTab?.applyFeaturesDirty(msg);
        // Plugin tabs that draw camera timing (triggerbox) re-read on this.
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

  // A spinner (or the init error) in the grid area until buildCameras runs.
  function showGridPlaceholder() {
    if (grid) return;
    const gridEl = document.getElementById("grid");
    let ph = gridEl.querySelector(".grid-placeholder");
    if (!ph) {
      ph = document.createElement("div");
      ph.className = "grid-placeholder";
      gridEl.appendChild(ph);
    }
    ph.classList.toggle("error", Boolean(initError));
    ph.replaceChildren();
    if (!initError) {
      const spin = document.createElement("div");
      spin.className = "grid-spinner";
      spin.setAttribute("aria-hidden", "true");
      ph.appendChild(spin);
    }
    const label = document.createElement("div");
    label.className = "grid-placeholder-msg";
    label.textContent = initError || "Connecting to cameras…";
    ph.appendChild(label);
  }

  // Build the grid, View/Camera tabs and Save dialog. The camera set is fixed
  // for a session, so a later `system` message (a reconnect) is a no-op.
  function buildCameras(cameras) {
    if (grid) return;
    const gridEl = document.getElementById("grid");
    gridEl.querySelector(".grid-placeholder")?.remove();
    grid = new CameraGrid(gridEl, cameras, {
      onSelect: (i) => {
        cameraTab?.selectCamera(i);
        viewTab?.selectCamera(i);
      },
      onRename: (i, name) => cameraTab?.renameCamera(i, name),
      onViewChange: scheduleViewSend,
    });
    viewTab = new ViewTab({ cameras, grid, onSelect: (i) => grid.select(i) });
    cameraTab = new CameraTab({
      cameras,
      notify,
      onSelect: (i) => grid.select(i),
      onRename: (i, name) => {
        grid.setName(i, name);
        viewTab?.applyName(i, name);
      },
    });
    saveDialog = new SaveDialog({
      grid,
      notify,
      getRecording: () => recordingActive,
    });
    if (cameras.length) grid.select(0);
    grid.setRecording(recordingActive);
    syncEnabled();
    lastViewJson = "";
    scheduleViewSend();
  }

  // Apply a /api/system descriptor (the WS handshake or the init's push).
  function applySystem(sys) {
    systemReady = Boolean(sys.ready);
    initError = sys.init_error || null;
    showMissingCameras(sys);
    record.setManagedAvailable(!!sys.managed_trigger_available);
    for (const [name, info] of Object.entries(sys.plugins ?? {})) {
      pluginTabs.get(name)?.applyStatus?.(info);
    }
    if (Array.isArray(sys.cameras) && sys.cameras.length) {
      buildCameras(sys.cameras);
    } else if (!grid) {
      showGridPlaceholder();
    }
    syncEnabled();
  }

  if (systemReady && Array.isArray(system.cameras) && system.cameras.length) {
    buildCameras(system.cameras);
  } else {
    showGridPlaceholder();
  }

  record.applyState(snap);
  benchmark.applyState(snap);
  // A page whose socket never upgrades reads as connecting, not dead.
  setConnectionMode("connecting");
  sock.connect();
}

main();
