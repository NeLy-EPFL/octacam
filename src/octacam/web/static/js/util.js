// Small shared helpers.

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

// Focus management for a modal dialog: on activate() remember the opener and
// move focus into the card; trap Tab within it (wrapping last<->first); on
// deactivate() restore focus to the opener. Keeps the save-config and
// directory-picker modals keyboard-usable and screen-reader-correct.
export class ModalFocus {
  constructor(card) {
    this.card = card;
    this.opener = null;
    this._onKey = (e) => {
      if (e.key !== "Tab") return;
      const items = this._focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      // Wrap around the ends so focus can never leave the open dialog.
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
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

  activate(first) {
    this.opener = document.activeElement;
    this.card.addEventListener("keydown", this._onKey);
    (first || this._focusable()[0] || this.card).focus();
  }

  deactivate() {
    this.card.removeEventListener("keydown", this._onKey);
    const opener = this.opener;
    this.opener = null;
    if (opener && typeof opener.focus === "function") opener.focus();
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
