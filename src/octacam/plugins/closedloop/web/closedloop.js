// Closed-loop tab: the tracked balls on a map of the arena, each pair's pushes
// and light, the push rule, and a light test per opto channel; and the same on
// the tracked camera's preview (an overlay).
import { el, request } from "/js/util.js";

const MAP_W = 260;
const PUSH_FLASH_MS = 300;
const MAP_H = 180;
const SVG = "http://www.w3.org/2000/svg";

const MARKUP = `
  <div class="col">
    <div id="closedloop-message" class="hint"></div>
    <svg id="closedloop-map" class="closedloop-map" viewBox="0 0 ${MAP_W} ${MAP_H}"
      role="img" aria-label="Ball positions per corridor"></svg>
    <table class="closedloop-pairs">
      <thead><tr>
        <th title="The pair's corridors, left to right in the camera image: the highlighted one is tracked, the other is its control. Click to switch (not while recording).">Track</th>
        <th title="The opto channel both corridors' light is on">Light</th>
        <th title="Pushes in the tracked corridor (this recording, or since the last one)">Pushes</th>
        <th title="Times a push switched the light on (this recording)">Lit</th>
        <th></th>
      </tr></thead>
      <tbody id="closedloop-pair-rows"></tbody>
    </table>
    <div class="row" title="How long a Test button lights the channel">
      <span>Test light:</span>
      <span class="suffixed">
        <input id="closedloop-test-s" type="number" min="0.1" max="60" step="0.1" value="1">
        <span class="suffix">s</span>
      </span>
    </div>
    <label class="center closedloop-live-row"
      title="Test the loop without recording: pushes light their LEDs in preview too. Off at every start; a recording turns it off.">
      <input type="checkbox" id="closedloop-preview-live"> Light in preview (test)
    </label>
    <button type="button" id="closedloop-detect" class="btn wide"
      title="Find the balls again (after the arena was moved or replaced)">Detect balls</button>
    <div id="closedloop-embeds"></div>
    <fieldset id="closedloop-rule">
      <h3 title="What counts as a push, and the light it earns while recording: on for at least the minimum, and while the ball keeps moving">Push rule</h3>
      <label class="row" title="The ball movement that counts, in the camera image">
        <span>Direction:</span>
        <select id="closedloop-direction">
          <option value="up">up</option>
          <option value="down">down</option>
          <option value="any">either way</option>
        </select>
      </label>
      <label class="row" title="How far the ball must move from where it rested">
        <span>Push distance:</span>
        <span class="suffixed">
          <input id="closedloop-push-px" type="number" min="0.5" max="600" step="0.5">
          <span class="suffix">px</span>
        </span>
      </label>
      <label class="row" title="A push lights its pair at least this long, and longer while the ball keeps moving">
        <span>Light at least:</span>
        <span class="suffixed">
          <input id="closedloop-min-on-s" type="number" min="0.01" max="600" step="0.1">
          <span class="suffix">s</span>
        </span>
      </label>
      <label class="row" title="After the light goes off, how long pushes earn no light">
        <span>Refractory:</span>
        <span class="suffixed">
          <input id="closedloop-refractory-s" type="number" min="0" max="600" step="0.1">
          <span class="suffix">s</span>
        </span>
      </label>
      <label class="center" title="Off: track and log pushes, but never light">
        <input type="checkbox" id="closedloop-enabled"> Light on pushes while recording
      </label>
    </fieldset>
  </div>`;

export default class ClosedLoopTab {
  static label = "Closed loop";
  static title = "Ball tracking and the opto light it drives";

  constructor({ panel, status, notify, overlays }) {
    this.notify = notify;
    this.connected = false;
    this.state = status || {};
    this.flashUntil = new Map(); // corridor -> performance.now() a push flashes until
    overlays?.set("closedloop", (ctx, view) => this.drawOverlay(ctx, view));
    panel.innerHTML = MARKUP;
    const byId = (id) => document.getElementById(`closedloop-${id}`);
    this.message = byId("message");
    this.map = byId("map");
    this.rows = byId("pair-rows");
    this.testSeconds = byId("test-s");
    this.detectBtn = byId("detect");
    this.previewLive = byId("preview-live");
    this.previewLive.addEventListener("change", () => this.sendPreviewLive());
    this.ruleFields = byId("rule");
    this.inputs = {
      direction: byId("direction"),
      push_px: byId("push-px"),
      min_on_s: byId("min-on-s"),
      refractory_s: byId("refractory-s"),
      enabled: byId("enabled"),
    };
    for (const input of Object.values(this.inputs)) {
      input.addEventListener("change", () => this.sendRule());
    }
    this.detectBtn.addEventListener("click", () =>
      request("POST", "/api/closedloop/detect", undefined, {
        action: "Detect balls",
        notify: this.notify,
      }),
    );
    this.render();
  }

  setConnected(connected) {
    this.connected = connected;
    this.render();
  }

  // A /api/system plugin status, and a "closedloop_state" push: the same fields.
  applyStatus(info) {
    this.applyState(info);
  }

  applyState(msg) {
    const now = performance.now();
    const before = new Map((this.state.pairs || []).map((p) => [p.corridor, p.pushes]));
    for (const p of msg.pairs || []) {
      if (before.has(p.corridor) && p.pushes > before.get(p.corridor)) {
        this.flashUntil.set(p.corridor, now + PUSH_FLASH_MS);
      }
    }
    this.state = msg;
    this.render();
  }

  // On the tracked camera's preview, in its canvas (sensor px through `view`):
  // each pair's corridors tinted red while its LED is on, an LED dot above each
  // tracked ball (red: on), and the ball (green; orange on a weak match; red,
  // dashed, at its last position while lost), ringed yellow as a push lands.
  drawOverlay(ctx, view) {
    const s = this.state;
    const balls = s.balls || [];
    if (!balls.length) return;
    if (s.camera_serial ? s.camera_serial !== view.camera.serial : s.camera !== view.camera.name) {
      return;
    }
    const r = (s.ball_radius_px || 30) * view.scale;
    const lw = 2 / view.screen; // ~2 screen px at any zoom
    const xs = s.corridor_x || [];
    const now = performance.now();
    for (const p of s.pairs || []) {
      if (!p.light) continue;
      ctx.fillStyle = "rgba(255, 40, 40, 0.25)";
      for (const c of [p.corridor, p.control]) {
        if (c && xs[c - 1] != null) ctx.fillRect(view.x(xs[c - 1]) - 2 * r, 0, 4 * r, ctx.canvas.height);
      }
    }
    for (const b of balls) {
      const x = view.x(b.x);
      const y = view.y(b.y);
      const pair = (s.pairs || []).find((p) => p.corridor === b.corridor);
      ctx.lineWidth = lw;
      ctx.beginPath();
      ctx.arc(x, y - 2.2 * r, 0.45 * r, 0, 2 * Math.PI);
      if (pair?.light) {
        ctx.fillStyle = "#ff2828";
        ctx.fill();
      } else {
        ctx.strokeStyle = "rgba(200, 200, 200, 0.8)";
        ctx.stroke();
      }
      ctx.strokeStyle = b.lost ? "#ff3b30" : b.weak ? "#ffa500" : "#3ddc66";
      ctx.setLineDash(b.lost ? [4 * lw, 3 * lw] : []);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.stroke();
      ctx.setLineDash([]);
      if ((this.flashUntil.get(b.corridor) || 0) > now) {
        ctx.strokeStyle = "#ffe600";
        ctx.lineWidth = 2 * lw;
        ctx.beginPath();
        ctx.arc(x, y, r + 3 * lw, 0, 2 * Math.PI);
        ctx.stroke();
      }
    }
  }

  // The loop needs nothing from the start request: its rule lives server-side.
  getStartParams() {
    return null;
  }

  async sendPreviewLive() {
    const on = this.previewLive.checked;
    const r = await request("PUT", "/api/closedloop/preview", { on }, {
      action: "Light in preview",
      notify: this.notify,
    });
    this.previewLive.checked = r ? r.preview_live : !on;
    this.render();
  }

  async sendRule() {
    const i = this.inputs;
    const rule = {
      direction: i.direction.value,
      push_px: Number(i.push_px.value),
      min_on_s: Number(i.min_on_s.value),
      refractory_s: Number(i.refractory_s.value),
      enabled: i.enabled.checked,
    };
    const saved = await request("PUT", "/api/closedloop/rule", rule, {
      action: "Push rule",
      notify: this.notify,
    });
    this.fillRule(saved ?? this.state.rule);
  }

  fillRule(rule) {
    if (!rule) return;
    const i = this.inputs;
    // Never overwrite the field being edited.
    for (const [key, input] of Object.entries(i)) {
      if (document.activeElement === input) continue;
      if (key === "enabled") input.checked = !!rule.enabled;
      else input.value = String(rule[key]);
    }
  }

  async test(channel) {
    const duration_s = Number(this.testSeconds.value) || 1;
    const r = await request(
      "POST",
      "/api/closedloop/test",
      { channel, duration_s },
      { action: `Light ${channel} test`, notify: this.notify },
    );
    if (r && !r.ok) this.notify?.("error", `Light ${channel}: the board did not take the command`);
  }

  render() {
    const s = this.state;
    const recording = !!s.recording;
    let message = s.message;
    if (!message) {
      const balls = s.balls?.length ?? 0;
      message = `${recording ? "Recording" : "Preview"} — tracking ${balls} balls`
        + (s.rate_fps ? ` at ${s.rate_fps} fps, ${s.latency_ms} ms after each frame` : "");
    }
    if (s.preview_live && !recording) message += " — LEDs LIVE in preview (test)";
    this.message.textContent = message;
    this.message.classList.toggle("closedloop-warn", !!s.message || (s.preview_live && !recording));
    if (document.activeElement !== this.previewLive) this.previewLive.checked = !!s.preview_live;
    this.previewLive.disabled = !this.connected || recording;
    this.previewLive.parentElement.classList.toggle("live", !!s.preview_live && !recording);
    this.detectBtn.disabled = !this.connected || recording;
    this.ruleFields.disabled = !this.connected || recording;
    this.fillRule(s.rule);
    this.renderPairs(s.pairs || []);
    this.renderMap();
  }

  // The pair's corridors left to right, the tracked one highlighted; clicking
  // the other tracks it instead.
  sidePicker(p, index) {
    const box = el("span", "closedloop-side");
    const corridors = [p.corridor, p.control].filter((c) => c != null).sort((a, b) => a - b);
    for (const c of corridors) {
      const tracked = c === p.corridor;
      const b = el("button", `btn${tracked ? " on" : ""}`, `${c}`);
      b.type = "button";
      b.setAttribute("aria-pressed", String(tracked));
      b.title = tracked ? `Corridor ${c} is tracked` : `Track corridor ${c} instead (${p.corridor} becomes the control)`;
      b.disabled = tracked || !this.connected || !!this.state.recording;
      b.addEventListener("click", () => this.track(index, c));
      box.append(b);
    }
    return box;
  }

  async track(index, corridor) {
    await request("PUT", "/api/closedloop/pair", { pair: index, tracked: corridor }, {
      action: "Tracked corridor",
      notify: this.notify,
    });
  }

  // A section of this tab for another plugin's panel (the Opto LEDs).
  embed(name, label) {
    const section = el("div", "closedloop-embed");
    section.append(el("h3", null, label));
    const panel = el("div");
    panel.id = `tab-${name}`;
    section.append(panel);
    document.getElementById("closedloop-embeds").append(section);
    return panel;
  }

  renderPairs(pairs) {
    this.rows.replaceChildren(
      ...pairs.map((p, index) => {
        const tr = el("tr");
        const light = el("span", `closedloop-light${p.light ? " on" : ""}`, `${p.channel}`);
        light.title = p.forced
          ? "On: held by the override (Opto LEDs)"
          : p.light ? "Lit" : p.ready_in_s ? `Refractory: ${p.ready_in_s} s` : "Off";
        if (p.forced) light.textContent = `${p.channel}!`;
        const btn = el("button", "btn", "Test");
        btn.type = "button";
        btn.title = `Light opto channel ${p.channel} for the test duration`;
        btn.disabled = !this.connected;
        btn.addEventListener("click", () => this.test(p.channel));
        const cells = [el("td"), el("td"), el("td", null, `${p.pushes}`),
          el("td", null, `${p.stims}`), el("td")];
        cells[0].append(this.sidePicker(p, index));
        cells[1].append(light);
        cells[4].append(btn);
        tr.append(...cells);
        return tr;
      }),
    );
  }

  // Each corridor as a column at its detected x (tracked ones outlined, a lit
  // pair tinted), each tracked ball at its position (hollow while lost).
  renderMap() {
    const s = this.state;
    const [fw, fh] = s.frame_size || [1, 1];
    const role = new Map();
    for (const p of s.pairs || []) {
      role.set(p.corridor, { tracked: true, light: p.light });
      if (p.control) role.set(p.control, { tracked: false, light: p.light });
    }
    const pad = 12;
    const sx = (x) => pad + (x / fw) * (MAP_W - 2 * pad);
    const sy = (y) => pad + (y / fh) * (MAP_H - 2 * pad);
    const nodes = [];
    (s.corridor_x || []).forEach((x, i) => {
      const r = role.get(i + 1);
      const cls = ["closedloop-corridor", r?.tracked ? "tracked" : "", r?.light ? "lit" : ""];
      nodes.push(svg("rect", {
        class: cls.join(" "), x: sx(x) - 9, y: pad, width: 18, height: MAP_H - 2 * pad, rx: 3,
      }));
      const label = svg("text", { class: "closedloop-label", x: sx(x), y: pad - 2 });
      label.textContent = String(i + 1);
      nodes.push(label);
    });
    for (const b of s.balls || []) {
      nodes.push(svg("circle", {
        class: `closedloop-ball${b.lost ? " lost" : ""}`, cx: sx(b.x), cy: sy(b.y), r: 5,
      }));
    }
    this.map.replaceChildren(...nodes);
  }
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}
