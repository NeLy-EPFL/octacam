// Directory picker: the save directory is a path on the server, so browse it
// through /api/browse one level at a time.

import { el, Modal, request } from "./util.js";

export class DirPicker extends Modal {
  // `getStart()` is the path to open at; blank opens the save directory.
  constructor({ notify, onPick, getStart }) {
    super(document.getElementById("dir-dialog"));
    this.notify = notify;
    this.onPick = onPick;
    this.getStart = getStart;
    this.path = "";
    this.parent = null;
    this.busy = false;

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
  }

  setConnected(connected) {
    // The Record tab disables the Browse button itself.
    if (!connected) this.close();
  }

  async open() {
    this.error.textContent = "";
    this.newName.value = "";
    super.open();
    await this._load(this.getStart?.() ?? "");
  }

  async _load(path) {
    this.busy = true;
    this._syncButtons();
    const d = await request("POST", "/api/browse", { path }, {
      action: "Browse",
      notify: (_, msg) => (this.error.textContent = msg),
    });
    this.busy = false;
    if (!d) {
      this._syncButtons();
      return;
    }
    this.error.textContent = "";
    this.path = d.path;
    this.parent = d.parent;
    this.pathEl.textContent = this.path;
    this.pathEl.title = this.path;
    this._renderList(d.entries || []);
    this._syncButtons();
    if (d.writable === false) {
      this.error.textContent = "This folder is not writable.";
    }
  }

  _renderList(entries) {
    this.list.replaceChildren(
      ...entries.map((name) => {
        const btn = el("button", "dir-entry", name);
        btn.type = "button";
        btn.addEventListener("click", () => this._descend(name));
        return btn;
      })
    );
    if (!entries.length) this.list.appendChild(el("div", "dir-empty", "No subfolders"));
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
