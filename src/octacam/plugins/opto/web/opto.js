// Opto tab: the LED board's link and firmware, its channels, and an override
// per channel that holds its LED on, whatever the closed loop does, until it
// is pressed again.
import { SerialTab } from "/js/serial.js";
import { el, request } from "/js/util.js";

const MARKUP = `
  <div class="col">
    <table class="opto-channels">
      <thead><tr>
        <th title="The opto channel a closed-loop pair lights">Channel</th>
        <th title="The board pin driving the LED">Pin</th>
        <th title="How the LED is driven while on">Light</th>
        <th title="Hold the LED on until pressed again, whatever the closed loop does">Override</th>
      </tr></thead>
      <tbody id="opto-rows"></tbody>
    </table>
    <p class="hint">An override holds its LED on, through recordings too, until
      pressed again; a recording logs it.</p>
  </div>`;

export default class OptoTab extends SerialTab {
  static label = "Opto LEDs";
  static title = "The optogenetic LED board, and an LED-on override";
  // A section of the closed loop's tab when that is loaded.
  static embedIn = "closedloop";

  constructor(ctx) {
    super(ctx, MARKUP);
    this.rows = document.getElementById("opto-rows");
    this.channels = ctx.status.channels || [];
    this.forced = new Set(ctx.status.forced || []);
    this.start(ctx.status);
  }

  applyLink(msg) {
    if (Array.isArray(msg.forced)) this.forced = new Set(msg.forced);
    super.applyLink(msg);
  }

  applyState(msg) {
    this.toastError(msg.error);
    super.applyState(msg);
  }

  refresh() {
    super.refresh();
    this.renderChannels();
  }

  async toggle(channel) {
    const on = !this.forced.has(channel);
    const r = await request("POST", "/api/opto/override", { channel, on }, {
      action: `LED ${channel} override`,
      notify: this.notify,
    });
    if (!r) return;
    this.forced = new Set(r.forced);
    if (!r.ok) this.notify?.("error", `LED ${channel}: the board did not take the command`);
    this.renderChannels();
  }

  renderChannels() {
    const usable = this.connected && this.ready && !this.fw.needsFlash;
    this.rows.replaceChildren(
      ...this.channels.map((c, i) => {
        const channel = i + 1;
        const forced = this.forced.has(channel);
        const form = c.freq_hz > 0 ? `${c.pulse_ms} ms at ${c.freq_hz} Hz` : "steady";
        const btn = el("button", `btn opto-override${forced ? " on" : ""}`, forced ? "ON" : "On");
        btn.type = "button";
        btn.title = forced ? `LED ${channel} is held on: press to release` : `Hold LED ${channel} on`;
        btn.setAttribute("aria-pressed", String(forced));
        btn.disabled = !usable && !forced; // a forced LED can always be released
        btn.addEventListener("click", () => this.toggle(channel));
        const cell = el("td");
        cell.append(btn);
        const tr = el("tr");
        tr.append(el("td", null, `${channel}`), el("td", null, c.pin), el("td", null, form), cell);
        return tr;
      }),
    );
  }
}
