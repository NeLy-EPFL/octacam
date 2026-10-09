// Shutdown dialog. After recordings it offers Cancel / Shut down / Shut down &
// process; the last asks the server to start a detached processing job for this
// session on the way out.

import { api, Modal } from "./util.js";

export class ShutdownDialog extends Modal {
  constructor() {
    super(document.getElementById("shutdown-dialog"));
    this.msg = document.getElementById("shutdown-dialog-msg");
    this._resolve = null;
    for (const [id, choice] of [
      ["shutdown-cancel", "cancel"],
      ["shutdown-plain", "shutdown"],
      ["shutdown-process", "process"],
    ]) {
      document.getElementById(id).addEventListener("click", () => this._done(choice));
    }
  }

  // Confirm, then ask the server to shut down. Resolves true once it accepted.
  async shutDown({ notify, ...session }) {
    const choice = await this.confirm(session);
    if (choice === "cancel") return false;
    let r;
    try {
      r = await api("POST", "/api/shutdown", { process_after: choice === "process" });
    } catch {
      notify("error", "Shutdown request failed: server unreachable");
      return false;
    }
    if (r.status === 409) {
      notify("warning", "Stop the recording before shutting down.");
      return false;
    }
    if (!r.ok) {
      notify("error", r.data?.detail || `Shutdown failed (HTTP ${r.status})`);
      return false;
    }
    return true;
  }

  // Resolves to "cancel" | "shutdown" | "process".
  confirm({ recordingActive, hasWork, peerCount }) {
    const others = (peerCount || 1) - 1;
    // Every other browser loses its cameras and socket too: say so on every path.
    const extra =
      others > 0
        ? ` ${others} other browser${others === 1 ? " is" : "s are"} connected and will be disconnected.`
        : "";
    // A plain confirm while recording (the server refuses the shutdown, so
    // "& process" is moot) or with nothing to process: one click on an icon
    // button must not shut the rig down.
    if (recordingActive || !hasWork) {
      const ok = window.confirm(
        "Shut down the octacam server on the rig? This releases all cameras " +
          "and disconnects every client." +
          extra
      );
      return Promise.resolve(ok ? "shutdown" : "cancel");
    }
    this.msg.textContent =
      "Recordings were made this session. Start processing them (transcode, " +
      "grid, transfer) in a background job after shutting down? Reattach from a " +
      "terminal with `octacam jobs attach`." +
      extra;
    this.open();
    return new Promise((resolve) => {
      this._resolve = resolve;
    });
  }

  dismiss() {
    this._done("cancel");
  }

  _done(choice) {
    this.close();
    const resolve = this._resolve;
    this._resolve = null;
    resolve?.(choice);
  }
}
