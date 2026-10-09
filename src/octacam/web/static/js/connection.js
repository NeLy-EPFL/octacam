// The server connection: the initial load, the WebSocket and its mode, shown in
// the banner, the footer's state and peer count, and the Disconnect button.

import { api, sleep } from "./util.js";
import { ReconnectingSocket } from "./ws.js";

const byId = (id) => document.getElementById(id);

// [/api/system, /api/state], retrying until the server answers.
export async function loadInitial() {
  const banner = byId("banner");
  for (;;) {
    try {
      const [sys, snap] = await Promise.all([api("GET", "/api/system"), api("GET", "/api/state")]);
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

// Modes: "connecting" (handshake or manual reconnect), "connected",
// "reconnecting" (unexpected drop), "offline" (the user disconnected) and
// "stopped" (the server shut down). onMode runs on every change.
export class Connection {
  constructor({ onOpen, onFrame, onJson, onMode }) {
    this.mode = "offline";
    this.peerCount = 1; // browsers connected to the server (control is shared)
    this.onMode = onMode;
    this._userDisconnected = false; // suppresses the reconnect
    this._serverStopped = false;
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    this.sock = new ReconnectingSocket(`${proto}${location.host}/api/ws`, {
      onOpen: () => {
        this._setMode("connected");
        onOpen();
      },
      onClose: () =>
        this._setMode(
          this._serverStopped ? "stopped" : this._userDisconnected ? "offline" : "reconnecting"
        ),
      onFrame,
      onJson,
    });

    this.disconnectBtn = byId("disconnect-btn");
    this.disconnectBtn.addEventListener("click", () => {
      if (this.mode === "stopped") {
        // The server is gone; a full reload re-runs loadInitial + the handshake.
        location.reload();
      } else if (this.mode === "offline") {
        this.connect();
      } else {
        this._userDisconnected = true;
        this.sock.disconnect();
        this._setMode("offline");
      }
    });
    // Disconnect (a rare, easy-to-misclick action) hides behind the Advanced
    // switch while connected. Once the socket is down the button is the
    // Connect/Reconnect control and always shown: the switch sits in
    // #record-fields, which a disconnect disables, so it can't reveal it then.
    this.advancedToggle = byId("record-advanced-toggle");
    this.advancedToggle.addEventListener("change", () => this._syncDisconnectVisibility());
    this._syncDisconnectVisibility();
  }

  get connected() {
    return this.mode === "connected";
  }

  send(msg) {
    return this.sock.send(msg);
  }

  // A page whose socket never upgrades reads as connecting, not dead.
  connect() {
    this._userDisconnected = false;
    this._setMode("connecting");
    this.sock.connect();
  }

  // The server accepted a shutdown: the socket won't come back on its own.
  stopped() {
    this._serverStopped = true;
    this.sock.disconnect();
    this._setMode("stopped");
  }

  // Control is shared: show the browser count when others are connected.
  setPeers(count) {
    this.peerCount = count;
    const peers = byId("peers");
    const others = count - 1;
    peers.classList.toggle("hidden", others <= 0);
    if (others > 0) {
      peers.textContent = `${count} connected`;
      peers.title = `${others} other browser${others === 1 ? "" : "s"} connected to this server`;
    }
  }

  _syncDisconnectVisibility() {
    this.disconnectBtn.hidden = !(this.advancedToggle.checked || !this.connected);
  }

  _setMode(mode) {
    this.mode = mode;
    const banner = byId("banner");
    if (mode === "reconnecting") {
      banner.textContent = "Disconnected — reconnecting…";
      banner.classList.remove("hidden");
    } else if (mode === "stopped") {
      banner.textContent = "Server stopped — reload to reconnect.";
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
    banner.classList.toggle("stopped", mode === "stopped");

    const connState = byId("conn-state");
    connState.textContent = {
      connected: "connected",
      connecting: "connecting…",
      stopped: "server stopped",
    }[mode] ?? "disconnected";
    connState.className = { connected: "online", connecting: "connecting" }[mode] ?? "offline";

    // The server resends the presence count on (re)connect.
    if (!this.connected) this.setPeers(1);
    this.onMode();

    const btn = this.disconnectBtn;
    // Connect re-establishes the socket after a voluntary disconnect;
    // Reconnect (once stopped) reloads the page; Disconnect unplugs this
    // browser only. Clickable in every mode.
    const [icon, label, title] = {
      offline: ["🔗", "Connect", "Reconnect this browser to the server"],
      stopped: ["🔄", "Reconnect", "Reload the page to reconnect to the server"],
    }[mode] ?? ["🔌", "Disconnect", "Disconnect this browser (the recording keeps running on the rig)"];
    btn.textContent = icon;
    btn.title = title;
    btn.setAttribute("aria-label", label);
    btn.disabled = false;
    this._syncDisconnectVisibility();
    byId("shutdown-btn").disabled = mode === "stopped";
  }
}
