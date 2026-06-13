// Entry point: fetch system + state, build UI, open the WebSocket.

import { api, sleep } from "./util.js";
import { ReconnectingSocket } from "./ws.js";
import { CameraGrid } from "./grid.js";
import { RecordTab } from "./record.js";
import { ArduinoTab } from "./arduino.js";
import { initViewTab } from "./view.js";
import { CameraTab } from "./camera.js";
import { SaveDialog } from "./save.js";

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
  const [system, snap] = await loadInitial();

  const versionEl = document.getElementById("version");
  versionEl.textContent = `octacam ${system.version}`;
  versionEl.title = system.config_dir;

  setupTabs();
  // The Arduino tab is contributed by the opt-in `arduino` plugin; show it
  // only when that plugin is loaded and its serial port is ready.
  const arduinoReady = Boolean(system.plugins?.arduino?.ready);
  if (!arduinoReady) {
    document.querySelector('#tabs button[data-tab="arduino"]').remove();
    document.getElementById("tab-arduino").remove();
  }

  let cameraTab = null;
  let saveDialog = null;
  const grid = new CameraGrid(document.getElementById("grid"), system.cameras, {
    onSelect: (i) => cameraTab?.selectCamera(i),
  });
  initViewTab(grid);

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

  const arduino = arduinoReady
    ? new ArduinoTab({ send: (m) => sock.send(m), notify })
    : null;

  record = new RecordTab({
    formats: system.formats,
    getArduinoCommand: () => (arduino ? arduino.getStartCommand() : null),
    notify,
  });

  cameraTab = new CameraTab({
    cameras: system.cameras,
    notify,
    onSelect: (i) => grid.select(i),
  });
  saveDialog = new SaveDialog({
    grid,
    notify,
    getRecording: () => recordingActive,
  });

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
    cameraTab?.setConnected(connected);
    saveDialog?.setConnected(connected);
    for (const id of ["arduino-fields", "view-fields"]) {
      const fs = document.getElementById(id);
      if (fs) fs.disabled = !connected;
    }
    if (!connected) arduino?.stopJog();

    const disconnectBtn = document.getElementById("disconnect-btn");
    disconnectBtn.textContent = mode === "offline" ? "Connect" : "Disconnect";
    disconnectBtn.disabled = mode === "stopped";
    document.getElementById("shutdown-btn").disabled = mode === "stopped";
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
        cameraTab?.setRecording(recordingActive);
        if (Array.isArray(msg.cameras)) applyCameraStats(msg.cameras);
        break;
      case "settings":
        record.applySettings(msg);
        break;
      case "camera_params":
        cameraTab?.applyParams(msg);
        break;
      case "event":
        addEvent(msg);
        record.handleEvent(msg);
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
    const ok = window.confirm(
      "Shut down the octacam server on the rig? This releases all cameras " +
        "and disconnects every client."
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
