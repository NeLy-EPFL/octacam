// Entry point: fetch system + state, build UI, open the WebSocket.

import { el } from "./util.js";
import { Connection, loadInitial } from "./connection.js";
import { TabBar, loadPluginTabs } from "./tabs.js";
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

const byId = (id) => document.getElementById(id);

// Room for the backlog the server replays on (re)connect plus the live tail.
const MAX_EVENTS = 200;
const events = [];

function addEvent(evt) {
  events.push(evt);
  while (events.length > MAX_EVENTS) events.shift();
  const list = byId("events");
  list.replaceChildren(
    ...events.map((e) => {
      const div = el("div", `event ${e.level || "info"}`);
      const time = new Date(e.time * 1000).toLocaleTimeString([], { hour12: false });
      div.append(el("time", null, time), el("span", null, e.message));
      return div;
    })
  );
  list.scrollTop = list.scrollHeight;
}

// A rig that opened fewer cameras than its config asks for must not pass for a
// healthy one with a smaller grid: name each missing camera and why.
function showMissingCameras(sys) {
  const alert = byId("rig-alert");
  const missing = sys.missing_cameras;
  alert.classList.toggle("hidden", missing.length === 0);
  if (missing.length === 0) return;
  const opened = sys.cameras.length;
  const title = el(
    "div",
    null,
    `⚠ Incomplete rig: ${opened} of ${opened + missing.length} configured cameras opened. Missing:`
  );
  const list = el("ul");
  for (const { serial, reason } of missing) list.append(el("li", null, `${serial}: ${reason}`));
  alert.replaceChildren(title, list);
}

async function main() {
  initTheme();
  initSidebarResize();
  const [system, snap] = await loadInitial();

  applyConfigTheme(system.theme);
  const versionEl = byId("version");
  versionEl.textContent = `octacam ${system.version}`;
  versionEl.title = system.config_dir;
  initUpdateBanner(system.update);
  showMissingCameras(system);

  // Camera-dependent UI is built by buildCameras() once the camera list
  // arrives (now, or with the init's `system` push); the closures below read
  // these by reference. systemReady/initError mirror the controller's init.
  let grid = null;
  let cameraTab = null;
  let viewTab = null;
  let saveDialog = null;
  let systemReady = Boolean(system.ready);
  let initError = system.init_error || null;
  let recordingActive = false;
  let recordingsMade = 0; // this session's, for "Shut down & process"

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
    if (conn.send({ type: "view", cameras })) lastViewJson = json;
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

  const conn = new Connection({
    onOpen: () => {
      // A new socket has no server-side view state: resend the spec.
      lastViewJson = "";
      scheduleViewSend();
    },
    onFrame: (frame) => grid?.handleFrame(frame),
    onJson: (msg) => handleJson(msg),
    onMode: () => syncEnabled(),
  });

  // Read only when a recording starts.
  let pluginTabs = new Map();
  record = new RecordTab({
    formats: system.formats,
    // {name: start-params slice}, sent as the start request's plugin_params.
    getPluginParams: () => {
      const params = {};
      for (const [name, tab] of pluginTabs) {
        const slice = tab.getStartParams();
        if (slice != null) params[name] = slice;
      }
      return params;
    },
    notify,
  });
  record.setManagedAvailable(!!system.managed_trigger_available);

  // A loaded but not-ready plugin keeps its tab (with a Reconnect button), so
  // an unplugged board is visible.
  const tabBar = new TabBar(byId("tabs"));
  pluginTabs = await loadPluginTabs(system.plugins, tabBar, {
    send: (m) => conn.send(m),
    notify,
    getRecordSettings: () => record.settings,
  });

  const benchmark = new BenchmarkTab({ notify });
  const dirPicker = new DirPicker({
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
  initShortcuts({ grid: gridShortcuts, tabs: tabBar });

  // Camera controls need the socket and open cameras (systemReady); plugin
  // tabs gate on their own serial readiness, so they only track the socket.
  function syncEnabled() {
    const connected = conn.connected;
    const camReady = connected && systemReady;
    record.setConnected(camReady);
    grid?.setConnected(camReady);
    cameraTab?.setConnected(camReady);
    benchmark.setConnected(camReady);
    // Until the Save dialog exists (no cameras yet, or init failed) its button
    // has nothing to save, so keep it disabled.
    if (saveDialog) saveDialog.setConnected(camReady);
    else byId("save-config-btn").disabled = true;
    dirPicker.setConnected(connected);
    for (const tab of pluginTabs.values()) tab.setConnected(connected);
    byId("view-fields").disabled = !camReady;
  }

  function applyCameraStats(cameras) {
    if (!grid) return; // stats arrive before the grid is built during init
    const failed = [];
    cameras.forEach((c, i) => {
      const index = grid.indexBySerial.has(c.serial) ? grid.indexBySerial.get(c.serial) : i;
      grid.updateStats(index, { fps: c.fps, dropped: c.dropped, writerFailed: c.writer_failed });
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
        recordingActive = ["waiting", "recording", "finishing"].includes(msg.state);
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
        conn.setPeers(msg.clients);
        break;
      case "event":
        addEvent(msg);
        record.handleEvent(msg);
        break;
      default:
        // A plugin's "<name>_state" push.
        if (msg.type.endsWith("_state")) pluginTabs.get(msg.type.slice(0, -6))?.applyState(msg);
    }
  }

  const shutdown = new ShutdownDialog();
  byId("shutdown-btn").addEventListener("click", async () => {
    const accepted = await shutdown.shutDown({
      notify,
      recordingActive,
      hasWork: recordingsMade > 0,
      peerCount: conn.peerCount,
    });
    if (accepted) conn.stopped();
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
    const gridEl = byId("grid");
    let ph = gridEl.querySelector(".grid-placeholder");
    if (!ph) {
      ph = el("div", "grid-placeholder");
      gridEl.appendChild(ph);
    }
    ph.classList.toggle("error", Boolean(initError));
    ph.replaceChildren();
    if (!initError) {
      const spin = el("div", "grid-spinner");
      spin.setAttribute("aria-hidden", "true");
      ph.appendChild(spin);
    }
    ph.appendChild(el("div", "grid-placeholder-msg", initError || "Connecting to cameras…"));
  }

  // Build the grid, View/Camera tabs and Save dialog. The camera set is fixed
  // for a session, so a later `system` message (a reconnect) is a no-op.
  function buildCameras(cameras) {
    if (grid) return;
    const gridEl = byId("grid");
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
    saveDialog = new SaveDialog({ grid, notify, getRecording: () => recordingActive });
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
    for (const [name, info] of Object.entries(sys.plugins)) {
      pluginTabs.get(name)?.applyStatus(info);
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
  conn.connect();
}

main();
