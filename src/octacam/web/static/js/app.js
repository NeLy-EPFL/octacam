// Entry point: fetch system + state, build UI, open the WebSocket.

import { api, sleep } from "./util.js";
import { ReconnectingSocket } from "./ws.js";
import { CameraGrid } from "./grid.js";
import { RecordTab } from "./record.js";
import { ArduinoTab } from "./arduino.js";
import { initViewTab } from "./view.js";

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
  if (!system.serial_available) {
    document.querySelector('#tabs button[data-tab="arduino"]').remove();
    document.getElementById("tab-arduino").remove();
  }

  const grid = new CameraGrid(document.getElementById("grid"), system.cameras);
  initViewTab(grid);

  let record = null;
  const notify = (level, message) => {
    const evt = { time: Date.now() / 1000, level, message };
    addEvent(evt);
    record?.handleEvent(evt);
  };

  const sock = new ReconnectingSocket(wsUrl(), {
    onOpen: () => setConnected(true),
    onClose: () => setConnected(false),
    onFrame: (frame) => grid.handleFrame(frame),
    onJson: (msg) => handleJson(msg),
  });

  const arduino = system.serial_available
    ? new ArduinoTab({ send: (m) => sock.send(m), notify })
    : null;

  record = new RecordTab({
    formats: system.formats,
    getArduinoCommand: () => (arduino ? arduino.getStartCommand() : null),
    notify,
  });

  function setConnected(connected) {
    const banner = document.getElementById("banner");
    banner.textContent = "Disconnected — reconnecting…";
    banner.classList.toggle("hidden", connected);
    const connState = document.getElementById("conn-state");
    connState.textContent = connected ? "connected" : "disconnected";
    connState.className = connected ? "online" : "offline";
    record.setConnected(connected);
    for (const id of ["arduino-fields", "view-fields"]) {
      const fs = document.getElementById(id);
      if (fs) fs.disabled = !connected;
    }
    if (!connected) arduino?.stopJog();
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
        record.applyState(msg);
        if (Array.isArray(msg.cameras)) applyCameraStats(msg.cameras);
        break;
      case "settings":
        record.applySettings(msg);
        break;
      case "event":
        addEvent(msg);
        record.handleEvent(msg);
        break;
    }
  }

  record.applyState(snap);
  sock.connect();
}

main();
