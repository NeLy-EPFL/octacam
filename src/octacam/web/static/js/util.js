// Small shared helpers. Plugin tabs import this by the absolute path
// "/js/util.js" (a relative one resolves under /plugins/<name>/).

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// localStorage that never throws (it can in private mode or a sandbox): a
// failed read is null and a failed write is dropped.
export const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, String(value));
    } catch {
      // not persisted
    }
  },
};

export function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

// Clamp a number input to its min/max attributes; returns the clamped value
// and writes it back if it changed.
export function clampInput(input) {
  let v = parseFloat(input.value);
  const lo = input.min !== "" ? parseFloat(input.min) : -Infinity;
  const hi = input.max !== "" ? parseFloat(input.max) : Infinity;
  if (!Number.isFinite(v)) v = Number.isFinite(lo) ? lo : 0;
  const c = clamp(v, lo, hi);
  if (String(c) !== input.value) input.value = c;
  return c;
}

export function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let v = Math.max(0, n);
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${v.toFixed(2)} ${units[i]}`;
}

export function formatHMS(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// A .modal overlay: open() shows it, moves focus into its card and traps Tab
// there; close() hides it and gives focus back to the opener. A backdrop click
// or Escape calls dismiss(), which a subclass overrides to resolve a choice.
export class Modal {
  constructor(overlay) {
    this.overlay = overlay;
    this.card = overlay.querySelector(".modal-card");
    this.opener = null;
    this._trapTab = (e) => {
      if (e.key !== "Tab") return;
      const items = this._focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) this.dismiss();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.isOpen) this.dismiss();
    });
  }

  get isOpen() {
    return !this.overlay.classList.contains("hidden");
  }

  // Visible, enabled, focusable descendants in DOM order (offsetParent is null
  // for display:none subtrees, so a hidden row's field is skipped).
  _focusable() {
    const sel =
      'a[href], button:not(:disabled), input:not(:disabled), ' +
      'select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
    return [...this.card.querySelectorAll(sel)].filter(
      (n) => n.offsetParent !== null
    );
  }

  open(first) {
    if (this.isOpen) return;
    this.overlay.classList.remove("hidden");
    this.opener = document.activeElement;
    this.card.addEventListener("keydown", this._trapTab);
    (first || this._focusable()[0] || this.card).focus();
  }

  close() {
    if (!this.isOpen) return;
    this.overlay.classList.add("hidden");
    this.card.removeEventListener("keydown", this._trapTab);
    const opener = this.opener;
    this.opener = null;
    if (opener && typeof opener.focus === "function") opener.focus();
  }

  dismiss() {
    this.close();
  }
}

// fetch wrapper: returns {ok, status, data} where data is the parsed JSON
// body (or null). Network errors propagate as exceptions.
export async function api(method, url, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    // empty or non-JSON body
  }
  return { ok: resp.ok, status: resp.status, data };
}

// api() for a caller that only needs the outcome: the JSON body on success
// ({} when empty), else null after notify("error", "<action> failed: ...").
export async function request(method, url, body, { action, notify } = {}) {
  let r;
  try {
    r = await api(method, url, body);
  } catch {
    notify?.("error", `${action} failed: server unreachable`);
    return null;
  }
  if (r.ok) return r.data ?? {};
  notify?.("error", r.data?.detail || `${action} failed (HTTP ${r.status})`);
  return null;
}
