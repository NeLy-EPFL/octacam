// Entry point: fetch system + state, build UI, open the WebSocket.

import { api, clampInput, sleep } from "./util.js";
import { ReconnectingSocket } from "./ws.js";
import { CameraGrid } from "./grid.js";
import { RecordTab } from "./record.js";
import { ViewTab } from "./view.js";
import { CameraTab } from "./camera.js";
import { initSidebarResize } from "./resize.js";
import { initTheme } from "./theme.js";
import { SaveDialog } from "./save.js";
import { DirPicker } from "./dirpicker.js";

const MAX_EVENTS = 5;
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

function setupTabs() {
  const nav = document.getElementById("tabs");
  nav.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    for (const b of nav.querySelectorAll("button")) {
      b.classList.toggle("active", b === btn);
    }
    for (const panel of document.querySelectorAll(".tab")) {
      panel.classList.toggle("active", panel.id === `tab-${btn.dataset.tab}`);
    }
  });
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

  const versionEl = document.getElementById("version");
  versionEl.textContent = `octacam ${system.version}`;
  versionEl.title = system.config_dir;

  setupTabs();
  // Show optional plugin tabs only when the plugin is loaded. A not-ready
  // plugin still shows its tab (with a "serial unavailable" notice and a
  // Reconnect button) so a missing/unplugged board is diagnosable. Plugin tab
  // buttons and panels carry data-plugin="<name>" in index.html, so this is
  // name-agnostic — no per-plugin code here.
  for (const el of document.querySelectorAll("[data-plugin]")) {
    if (!system.plugins?.[el.dataset.plugin]) el.remove();
  }

  let cameraTab = null;
  let viewTab = null;
  let saveDialog = null;
  let dirPicker = null;
  const grid = new CameraGrid(document.getElementById("grid"), system.cameras, {
    onSelect: (i) => {
      cameraTab?.selectCamera(i);
      viewTab?.selectCamera(i);
    },
    onRename: (i, name) => cameraTab?.renameCamera(i, name),
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
  let peerCount = 1; // browsers connected to the server (control is shared)

  const sock = new ReconnectingSocket(wsUrl(), {
    onOpen: () => setConnectionMode("connected"),
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
  saveDialog = new SaveDialog({
    grid,
    notify,
    getRecording: () => recordingActive,
  });
  dirPicker = new DirPicker({
    notify,
    onPick: (path) => record.setSaveDir(path),
    getStart: () => record.getSaveDir(),
  });

  // Establish an initial current camera so the grid highlight and both pickers
  // agree from the start (and "Apply to: Selected" always has a target).
  if (system.cameras.length) grid.select(0);

  // Connection has four modes: "connected", "reconnecting" (unexpected drop),
  // "offline" (user disconnected, calm) and "stopped" (server shut down).
  function setConnectionMode(mode) {
    connMode = mode;
    const connected = mode === "connected";

    const banner = document.getElementById("banner");
    if (mode === "reconnecting") {
      banner.textContent = "Disconnected — reconnecting…";
      banner.classList.remove("hidden");
    } else if (mode === "stopped") {
      banner.textContent = "Server stopped.";
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden"); // connected, or user-initiated offline
    }
    banner.classList.toggle("stopped", mode === "stopped");

    const connState = document.getElementById("conn-state");
    connState.textContent =
      mode === "connected"
        ? "connected"
        : mode === "stopped"
          ? "server stopped"
          : "disconnected";
    connState.className = connected ? "online" : "offline";

    record.setConnected(connected);
    grid.setConnected(connected);
    cameraTab?.setConnected(connected);
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
    disconnectBtn.textContent = mode === "offline" ? "Connect" : "Disconnect";
    disconnectBtn.disabled = mode === "stopped";
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
    cameras.forEach((c, i) => {
      const index = grid.indexBySerial.has(c.serial)
        ? grid.indexBySerial.get(c.serial)
        : i;
      grid.updateStats(index, {
        fps: c.fps,
        dropped: c.dropped,
        writerFailed: c.writer_failed,
      });
    });
  }

  function handleJson(msg) {
    switch (msg.type) {
      case "state":
      case "telemetry":
        recordingActive = ["waiting", "recording", "finishing"].includes(
          msg.state
        );
        record.applyState(msg);
        grid.setRecording(recordingActive);
        cameraTab?.setRecording(recordingActive);
        if (Array.isArray(msg.cameras)) applyCameraStats(msg.cameras);
        break;
      case "settings":
        record.applySettings(msg);
        break;
      case "camera_params":
        cameraTab?.applyParams(msg);
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
    }
  }

  document.getElementById("disconnect-btn").addEventListener("click", () => {
    if (connMode === "offline") {
      userDisconnected = false;
      setConnectionMode("reconnecting");
      sock.connect();
    } else {
      userDisconnected = true;
      sock.disconnect();
      setConnectionMode("offline");
    }
  });

  document.getElementById("shutdown-btn").addEventListener("click", async () => {
    const others = peerCount - 1;
    const extra =
      others > 0
        ? ` ${others} other browser${others === 1 ? " is" : "s are"} connected and will be disconnected.`
        : "";
    const ok = window.confirm(
      "Shut down the octacam server on the rig? This releases all cameras " +
        "and disconnects every client." +
        extra
    );
    if (!ok) return;
    let r;
    try {
      r = await api("POST", "/api/shutdown");
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
  sock.connect();
}

main();
