// Shutdown dialog: a three-way choice shown when the operator shuts down the
// server after recording — Cancel / Shut down / Shut down & process. The last
// one asks the server to start a detached processing job for this session on the
// way out (reattach from a terminal with `octacam jobs attach`).

import { ModalFocus } from "./util.js";

export class ShutdownDialog {
  constructor() {
    this.dialog = document.getElementById("shutdown-dialog");
    this.focus = new ModalFocus(this.dialog.querySelector(".modal-card"));
    this.msg = document.getElementById("shutdown-dialog-msg");
    this._resolve = null;

    document
      .getElementById("shutdown-cancel")
      .addEventListener("click", () => this._done("cancel"));
    document
      .getElementById("shutdown-plain")
      .addEventListener("click", () => this._done("shutdown"));
    document
      .getElementById("shutdown-process")
      .addEventListener("click", () => this._done("process"));
    this.dialog.addEventListener("click", (e) => {
      if (e.target === this.dialog) this._done("cancel");
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.dialog.classList.contains("hidden")) {
        this._done("cancel");
      }
    });
  }

  // Resolves to "cancel" | "shutdown" | "process".
  confirm({ recordingActive, hasWork, peerCount }) {
    // While recording, "& process" is meaningless (the server refuses a shutdown
    // mid-trial), so keep the plain binary confirm as a speed-bump.
    if (recordingActive) {
      const others = (peerCount || 1) - 1;
      const extra =
        others > 0
          ? ` ${others} other browser${others === 1 ? " is" : "s are"} connected and will be disconnected.`
          : "";
      const ok = window.confirm(
        "Shut down the octacam server on the rig? This releases all cameras " +
          "and disconnects every client." +
          extra
      );
      return Promise.resolve(ok ? "shutdown" : "cancel");
    }
    // Nothing recorded this session: quit without friction (today's behavior).
    if (!hasWork) return Promise.resolve("shutdown");
    // Recordings exist: offer to process them in a detached background job.
    this.msg.textContent =
      "Recordings were made this session. Start processing them (transcode, " +
      "grid, transfer) in a background job after shutting down? Reattach from a " +
      "terminal with `octacam jobs attach`.";
    this.dialog.classList.remove("hidden");
    this.focus.activate();
    return new Promise((resolve) => {
      this._resolve = resolve;
    });
  }

  _done(choice) {
    if (!this.dialog.classList.contains("hidden")) {
      this.dialog.classList.add("hidden");
      this.focus.deactivate();
    }
    const resolve = this._resolve;
    this._resolve = null;
    if (resolve) resolve(choice);
  }
}
