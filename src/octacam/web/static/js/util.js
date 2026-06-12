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
