// The Record tab's user picker and the "Add yourself" dialog. Users live in
// the rig's config ([users.<initials>]): picking one points the transfer and
// the plugins' per-user folders at theirs (POST /api/users/select).

import { Modal, el, request, shortPath } from "./util.js";

const NEW_USER = "__new__";

export class UsersPanel {
  constructor({ notify, dirPicker, onChange }) {
    this.notify = notify;
    this.dirPicker = dirPicker;
    this.onChange = onChange || (() => {});
    this.select = document.getElementById("user-select");
    this.hint = document.getElementById("user-hint");
    this.data = { users: [], active: "", discovered: [], user_options: {} };
    this.dialog = new UserDialog({ notify, dirPicker, onAdded: (d) => this._apply(d) });
    this.select.addEventListener("change", () => this._picked());
  }

  async load() {
    const d = await request("GET", "/api/users", undefined, {
      action: "Loading users",
      notify: this.notify,
    });
    if (d) this._apply(d);
  }

  // The settings changed elsewhere (another browser picked a user).
  applySettings(s) {
    if (s && s.user !== this.data.active) this.load();
  }

  _apply(d) {
    const changed = d.active !== this.data.active;
    this.data = d;
    this._render();
    if (changed || d.settings) this.onChange(d);
  }

  _render() {
    const { users, active, discovered } = this.data;
    const options = [];
    const none = el("option", null, users.length ? "Choose who you are…" : "No users yet");
    none.value = "";
    options.push(none);
    for (const u of users) {
      const opt = el("option", null, u.name ? `${u.initials} — ${u.name}` : u.initials);
      opt.value = u.initials;
      opt.title = `Transfer: ${u.transfer_directory || "the rig's default"}`;
      options.push(opt);
    }
    for (const initials of discovered) {
      const opt = el("option", "user-discovered", `${initials} (on the NAS — set up…)`);
      opt.value = `${NEW_USER}:${initials}`;
      opt.title = "Has a folder on the NAS but is not set up on this rig yet";
      options.push(opt);
    }
    const add = el("option", null, "+ Add yourself…");
    add.value = NEW_USER;
    options.push(add);
    this.select.replaceChildren(...options);
    this.select.value = active || "";
    const user = users.find((u) => u.initials === active);
    this.hint.hidden = false;
    this.hint.title = user?.transfer_directory || "";
    this.hint.textContent = user
      ? `Your recordings go to ${shortPath(user.transfer_directory) || "the rig's default destination"}.`
      : users.length
        ? "Pick yourself so your data goes to your own folders."
        : "First time here? Add yourself so your data goes to your own folders.";
  }

  async _picked() {
    const value = this.select.value;
    if (value.startsWith(NEW_USER)) {
      this.select.value = this.data.active || "";
      const initials = value.split(":")[1] || "";
      this.dialog.openFor(initials, this.data.user_options);
      return;
    }
    const d = await request("POST", "/api/users/select", { initials: value }, {
      action: "Choosing the user",
      notify: this.notify,
    });
    if (d) this._apply(d);
    else this.select.value = this.data.active || "";
  }
}

class UserDialog extends Modal {
  constructor({ notify, dirPicker, onAdded }) {
    super(document.getElementById("user-dialog"));
    this.notify = notify;
    this.dirPicker = dirPicker;
    this.onAdded = onAdded;
    this.initials = document.getElementById("user-initials");
    this.name = document.getElementById("user-name");
    this.transfer = document.getElementById("user-transfer");
    this.pluginFields = document.getElementById("user-plugin-fields");
    this.error = document.getElementById("user-error");
    this.createBtn = document.getElementById("user-create");
    this.inputs = {}; // plugin -> option -> input
    this._timers = new Map();

    document.getElementById("user-cancel").addEventListener("click", () => this.close());
    this.createBtn.addEventListener("click", () => this._create());
    this.initials.addEventListener("input", () => {
      this.initials.value = this.initials.value.toUpperCase().replace(/[^A-Z]/g, "");
      this._suggest();
    });
    this._watch(this.transfer);
    this.card.querySelector('[data-browse="user-transfer"]').addEventListener("click", () =>
      this.dirPicker.pick(this.transfer.value, (path) => this._set(this.transfer, path))
    );
  }

  openFor(initials, userOptions) {
    this.error.textContent = "";
    this.initials.value = initials || "";
    this.name.value = "";
    this.transfer.value = "";
    this._buildPluginFields(userOptions || {});
    this.open();
    (initials ? this.name : this.initials).focus();
    if (initials) this._suggest();
  }

  // One path field per plugin per-user option (e.g. twophoton's 2P share).
  _buildPluginFields(userOptions) {
    this.inputs = {};
    const blocks = [];
    for (const [plugin, options] of Object.entries(userOptions)) {
      for (const [option, label] of Object.entries(options)) {
        const id = `user-${plugin}-${option}`;
        const wrap = el("div", "col");
        const lab = el("label", "col-label", `${label}:`);
        lab.htmlFor = id;
        lab.title = `${plugin} plugin, option "${option}"`;
        const row = el("div", "path-pick");
        const input = el("input");
        input.id = id;
        input.type = "text";
        input.spellcheck = false;
        const browse = el("button", "btn", "Browse…");
        browse.type = "button";
        browse.addEventListener("click", () =>
          this.dirPicker.pick(input.value, (path) => this._set(input, path))
        );
        const status = el("span", "path-status");
        status.dataset.statusFor = id;
        row.append(input, browse, status);
        wrap.append(lab, row);
        blocks.push(wrap);
        this._watch(input);
        (this.inputs[plugin] ||= {})[option] = input;
      }
    }
    this.pluginFields.replaceChildren(...blocks);
  }

  _set(input, path) {
    input.value = path;
    this._check(input);
  }

  _watch(input) {
    input.addEventListener("input", () => {
      clearTimeout(this._timers.get(input));
      this._timers.set(input, setTimeout(() => this._check(input), 400));
    });
  }

  // A tick when the folder exists and is writable, else why not.
  async _check(input) {
    const status = this.card.querySelector(`[data-status-for="${input.id}"]`);
    if (!status) return;
    const path = input.value.trim();
    if (!path) {
      status.textContent = "";
      return;
    }
    const d = await request("GET", `/api/paths/status?path=${encodeURIComponent(path)}`);
    const state = d?.status || "missing";
    status.className = `path-status path-status--${state}`;
    status.textContent = { ok: "✓", missing: "✗ not found", readonly: "✗ read-only" }[state];
    status.title = {
      ok: "Found and writable",
      missing: "Not found: is the share or NAS mounted, and the path right?",
      readonly: "Exists but octacam cannot write there",
    }[state];
  }

  // Fill the empty fields from the other users' folders.
  async _suggest() {
    const initials = this.initials.value;
    if (initials.length < 2) return;
    const d = await request("GET", `/api/users/suggest?initials=${initials}`);
    if (!d || initials !== this.initials.value) return;
    if (!this.transfer.value && d.transfer_directory) this._set(this.transfer, d.transfer_directory);
    for (const [plugin, options] of Object.entries(d.plugins || {})) {
      for (const [option, guess] of Object.entries(options)) {
        const input = this.inputs[plugin]?.[option];
        if (input && !input.value) this._set(input, guess.path);
      }
    }
  }

  async _create() {
    const plugins = {};
    for (const [plugin, options] of Object.entries(this.inputs)) {
      for (const [option, input] of Object.entries(options)) {
        if (input.value.trim()) (plugins[plugin] ||= {})[option] = input.value.trim();
      }
    }
    this.createBtn.disabled = true;
    const d = await request(
      "POST",
      "/api/users",
      {
        initials: this.initials.value,
        name: this.name.value.trim(),
        transfer_directory: this.transfer.value.trim(),
        plugins,
      },
      { action: "Adding the user", notify: (_, msg) => (this.error.textContent = msg) }
    );
    this.createBtn.disabled = false;
    if (!d) return;
    this.close();
    this.notify("info", `Added ${d.active}; your recordings now go to your folders.`);
    this.onAdded(d);
  }
}
