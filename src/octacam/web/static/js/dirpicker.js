// Directory picker modal: browse the rig's filesystem to choose a save dir.
//
// Recording happens on the server, so the save directory is a server-side path
// the browser cannot pick natively. This walks /api/browse one level at a time
// and hands the chosen path back to the Record tab.

import { api } from "./util.js";

export class DirPicker {
  // `onPick(path)` receives the chosen directory; `getStart()` returns the
  // path to open at (the current base-directory text, possibly uncommitted/blank
  // — the server falls back to the active save directory when it is blank).
  constructor({ notify, onPick, getStart }) {
    this.notify = notify;
    this.onPick = onPick;
    this.getStart = getStart;
    this.path = "";
    this.parent = null;
    this.busy = false;

    this.dialog = document.getElementById("dir-dialog");
    this.pathEl = document.getElementById("dir-current");
    this.list = document.getElementById("dir-list");
    this.upBtn = document.getElementById("dir-up");
    this.newName = document.getElementById("dir-new");
    this.error = document.getElementById("dir-error");
    this.openBtn = document.getElementById("browse-dir-btn");
    this.selectBtn = document.getElementById("dir-select");

    this.openBtn.addEventListener("click", () => this.open());
    document
      .getElementById("dir-cancel")
      .addEventListener("click", () => this.close());
    this.selectBtn.addEventListener("click", () => this._select());
    this.upBtn.addEventListener("click", () => {
      if (this.parent != null) this._load(this.parent);
    });
    this.dialog.addEventListener("click", (e) => {
      if (e.target === this.dialog) this.close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !this.dialog.classList.contains("hidden")) {
        this.close();
      }
    });
  }

  setConnected(connected) {
    // The Browse button lives inside #record-fields, which the Record tab
    // already disables on disconnect; just dismiss the modal so a dead socket
    // can't leave a stale browser open.
    if (!connected) this.close();
  }

  async open() {
    this.error.textContent = "";
    this.newName.value = "";
    this.dialog.classList.remove("hidden");
    await this._load(this.getStart?.() ?? "");
  }

  close() {
    this.dialog.classList.add("hidden");
  }

  async _load(path) {
    this.busy = true;
    this._syncButtons();
    let r;
    try {
      r = await api("POST", "/api/browse", { path });
    } catch {
      this.error.textContent = "Browse failed: server unreachable.";
      this.busy = false;
      this._syncButtons();
      return;
    }
    this.busy = false;
    if (!r.ok || !r.data) {
      this.error.textContent =
        r.data?.detail || `Browse failed (HTTP ${r.status})`;
      this._syncButtons();
      return;
    }
    this.error.textContent = "";
    this.path = r.data.path;
    this.parent = r.data.parent;
    this.pathEl.textContent = this.path;
    this.pathEl.title = this.path;
    this._renderList(r.data.entries || []);
    this._syncButtons();
    if (r.data.writable === false) {
      this.error.textContent = "This folder is not writable.";
    }
  }

  _renderList(entries) {
    this.list.replaceChildren(
      ...entries.map((name) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "dir-entry";
        btn.textContent = name;
        btn.addEventListener("click", () => this._descend(name));
        return btn;
      })
    );
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "dir-empty";
      empty.textContent = "No subfolders";
      this.list.appendChild(empty);
    }
  }

  _syncButtons() {
    this.upBtn.disabled = this.busy || this.parent == null;
    this.selectBtn.disabled = this.busy || !this.path;
  }

  _join(base, name) {
    return base.endsWith("/") ? `${base}${name}` : `${base}/${name}`;
  }

  _descend(name) {
    if (this.busy) return;
    this._load(this._join(this.path, name));
  }

  _select() {
    if (this.busy || !this.path) return;
    const extra = this.newName.value.trim();
    const chosen = extra ? this._join(this.path, extra) : this.path;
    this.onPick?.(chosen);
    this.close();
  }
}
