// Shared serial-port picker helpers for the Arduino plugin tabs.
//
// Served from /js/, so plugin modules import it via the ABSOLUTE path
// "/js/serial.js". A relative "./serial.js" would resolve under
// /plugins/<name>/ and 404. Kept dependency-free so any plugin can use it.

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

// Fill `select` with the plausible board ports, keeping `current` selected.
//
// Only microcontroller-class ports are listed — a host can expose dozens of
// legacy /dev/ttyS* that are never an Arduino, and flooding the dropdown with
// them buries the real candidates. `current` is always added (even if generic
// or an "auto" sentinel), so the active device is never dropped. Arduino-class
// ports are floated to the top and flagged.
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
