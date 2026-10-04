// Serial-port picker helpers for the plugin tabs, which import this by the
// absolute path "/js/serial.js" (a relative one resolves under /plugins/<name>/).

// Fetch the detected serial ports (never throws; returns [] on any error).
export async function fetchSerialPorts(api) {
  try {
    const r = await api("GET", "/api/serial/ports");
    if (r.ok && Array.isArray(r.data?.ports)) return r.data.ports;
  } catch {
    /* server unreachable — fall through to [] */
  }
  return [];
}

// Fill `select` with the microcontroller-class ports, Arduinos first (a host
// can have dozens of /dev/ttyS* that bury them). `current` is always listed
// and selected, even when generic or "auto".
export function populatePortSelect(select, ports, current) {
  if (!select) return;
  const candidates = ports.filter((p) => p.likely_microcontroller);
  const sorted = [...candidates].sort(
    (a, b) => (b.likely_arduino === true) - (a.likely_arduino === true),
  );
  select.innerHTML = "";
  const seen = new Set();
  for (const p of sorted) {
    const opt = document.createElement("option");
    opt.value = p.device;
    const tag = p.likely_arduino ? " ★" : "";
    opt.textContent = `${p.board_name} — ${p.device}${tag}`;
    select.appendChild(opt);
    seen.add(p.device);
  }
  if (current && !seen.has(current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent =
      current === "auto" ? "auto (single detected board)" : current;
    select.insertBefore(opt, select.firstChild);
  }
  if (!select.options.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "no serial ports detected";
    select.appendChild(opt);
  }
  if (current) select.value = current;
}
