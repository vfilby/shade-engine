/* Shade Engine Lovelace card.
 *
 * Served by the integration at /shade_engine/shade-engine-card.js and
 * auto-registered as a frontend resource — no manual resource setup.
 *
 * Minimal config (everything else is derived from the target sensor):
 *   type: custom:shade-engine-card
 *   entity: sensor.kitchen_shade_target
 * or:
 *   type: custom:shade-engine-card
 *   zone: kitchen
 *
 * Optional overrides: title, mode_entity, hold_entity, sun_entity,
 * glare_entity, switch_entity (for renamed entities); graph: false hides
 * the history strip, graph_hours (default 24) sets its window.
 */

const CARD_VERSION = "0.5.1";

const RAD = Math.PI / 180;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* Sun elevation in degrees, NOAA approximation with refraction — same port
 * as docs/simulator.html, verified against sun.sun. Deterministic, so the
 * curve is computed locally instead of fetched from history. */
function solarElevation(utcMs, lat, lon) {
  const jd = utcMs / 86400000 + 2440587.5;
  const T = (jd - 2451545) / 36525;
  const L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360;
  const M = 357.52911 + T * (35999.05029 - 0.0001537 * T);
  const e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T);
  const C =
    Math.sin(M * RAD) * (1.914602 - T * (0.004817 + 0.000014 * T)) +
    Math.sin(2 * M * RAD) * (0.019993 - 0.000101 * T) +
    Math.sin(3 * M * RAD) * 0.000289;
  const omega = 125.04 - 1934.136 * T;
  const lambda = L0 + C - 0.00569 - 0.00478 * Math.sin(omega * RAD);
  const eps0 =
    23 + (26 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60) / 60;
  const eps = eps0 + 0.00256 * Math.cos(omega * RAD);
  const decl = Math.asin(Math.sin(eps * RAD) * Math.sin(lambda * RAD)) / RAD;
  const y = Math.tan((eps * RAD) / 2) ** 2;
  const eqTime =
    (4 / RAD) *
    (y * Math.sin(2 * L0 * RAD) -
      2 * e * Math.sin(M * RAD) +
      4 * e * y * Math.sin(M * RAD) * Math.cos(2 * L0 * RAD) -
      0.5 * y * y * Math.sin(4 * L0 * RAD) -
      1.25 * e * e * Math.sin(2 * M * RAD));
  const minutesUTC = (utcMs / 60000) % 1440;
  const tst = (((minutesUTC + eqTime + 4 * lon) % 1440) + 1440) % 1440;
  let ha = tst / 4 - 180;
  if (ha < -180) ha += 360;
  const cosZen =
    Math.sin(lat * RAD) * Math.sin(decl * RAD) +
    Math.cos(lat * RAD) * Math.cos(decl * RAD) * Math.cos(ha * RAD);
  let elevation = 90 - Math.acos(clamp(cosZen, -1, 1)) / RAD;
  let refr = 0;
  if (elevation <= 85) {
    const te = Math.tan(elevation * RAD);
    if (elevation > 5) refr = 58.1 / te - 0.07 / te ** 3 + 0.000086 / te ** 5;
    else if (elevation > -0.575)
      refr = 1735 + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)));
    else refr = -20.774 / te;
    refr /= 3600;
  }
  return elevation + refr;
}

const REASON_LABELS = {
  command: ["Moving", "accent"],
  in_sync: ["In sync", "ok"],
  rate_limited: ["Rate limited — retrying", "warn"],
  hold_active: ["Manual hold", "hold"],
  disabled: ["Control off", "muted"],
  no_target: ["No target", "muted"],
};

class ShadeEngineCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity && !config.zone) {
      throw new Error("shade-engine-card: set `entity` (the shade_target sensor) or `zone`");
    }
    this._config = config;
    this._targetId = config.entity || null;
    this._rendered = null;
  }

  getCardSize() {
    return 3;
  }

  static getStubConfig(hass) {
    const id = Object.keys(hass.states).find(
      (k) => k.startsWith("sensor.") && hass.states[k].attributes.zone_id !== undefined
    );
    return id ? { entity: id } : { zone: "kitchen" };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._config) return;
    if (!this._targetId) this._resolveTarget();
    const watched = this._watchedIds();
    const snapshot = watched.map((id) => hass.states[id]);
    if (this._rendered && snapshot.every((s, i) => s === this._rendered[i])) {
      return; // nothing this card shows has changed
    }
    this._rendered = snapshot;
    this._render();
  }

  connectedCallback() {
    if (this._hass && this._config) this._render();
  }

  disconnectedCallback() {
    this._stopCountdown();
  }

  // -- entity resolution ----------------------------------------------------

  _resolveTarget() {
    const zone = this._config.zone;
    this._targetId =
      Object.keys(this._hass.states).find(
        (k) =>
          k.startsWith("sensor.") &&
          this._hass.states[k].attributes.zone_id === zone &&
          this._hass.states[k].attributes.covers !== undefined
      ) || null;
  }

  _ids() {
    const c = this._config;
    // All of a zone's entities share the object_id prefix derived from the
    // device name ("Kitchen Shade target" -> kitchen_shade_target), so
    // siblings are reachable by swapping the suffix.
    const prefix = (this._targetId || "sensor.unknown_shade_target")
      .replace(/^sensor\./, "")
      .replace(/_shade_target$/, "");
    return {
      target: this._targetId,
      mode: c.mode_entity || `select.${prefix}_shade_mode`,
      hold: c.hold_entity || `binary_sensor.${prefix}_shade_hold`,
      sun: c.sun_entity || `binary_sensor.${prefix}_sun_in_window`,
      glare: c.glare_entity || `sensor.${prefix}_glare_position`,
      control: c.switch_entity || `switch.${prefix}_shade_control`,
    };
  }

  _watchedIds() {
    if (!this._targetId) return [];
    const ids = Object.values(this._ids());
    const target = this._hass.states[this._targetId];
    return ids.concat(target ? target.attributes.covers || [] : []);
  }

  // -- rendering ------------------------------------------------------------

  _render() {
    const hass = this._hass;
    if (!this._targetId || !hass.states[this._targetId]) {
      this._renderShell(
        `<div class="warn-box">Shade Engine zone not found` +
          (this._config.zone ? ` (zone: ${this._config.zone})` : ` (${this._config.entity})`) +
          `. Is the integration loaded?</div>`
      );
      return;
    }

    const ids = this._ids();
    const target = hass.states[ids.target];
    const mode = hass.states[ids.mode];
    const hold = hass.states[ids.hold];
    const sun = hass.states[ids.sun];
    const glare = hass.states[ids.glare];
    const control = hass.states[ids.control];
    const attrs = target.attributes;
    const enabled = control ? control.state === "on" : attrs.enabled !== false;
    const holdActive = enabled && hold && hold.state === "on";
    const reason = !enabled ? "disabled" : holdActive ? "hold_active" : attrs.last_decision;
    const [reasonText, reasonClass] = REASON_LABELS[reason] || ["Waiting", "muted"];
    const sunOn = sun ? sun.state === "on" : glare && glare.attributes.sun_in_window;

    const covers = attrs.covers || [];
    const positions = covers
      .map((id) => hass.states[id])
      .map((s) =>
        s && s.attributes.current_position != null ? Math.round(s.attributes.current_position) : null
      );
    const known = positions.filter((p) => p !== null);
    const current = known.length ? known.join(" / ") : "—";

    const title =
      this._config.title ||
      (attrs.friendly_name || "").replace(/ Shade target$/i, "") ||
      this._prettify(attrs.zone_id);

    const chips = mode
      ? (mode.attributes.options || [])
          .map(
            (o) =>
              `<button class="chip${o === mode.state ? " active" : ""}" data-mode="${o}">${this._prettify(o)}</button>`
          )
          .join("")
      : "";

    this._maybeFetchHistory(ids);
    const graphHtml =
      this._config.graph === false
        ? ""
        : `<div class="graph">${this._graphSvg(ids, parseFloat(target.state))}</div>`;

    const holdUntil = (hold && hold.attributes.hold_until) || attrs.hold_until;
    const holdBanner = holdActive
      ? `<div class="hold-banner">
           <ha-icon icon="mdi:hand-back-right"></ha-icon>
           <span>Manual hold · <b data-countdown>${this._remaining(holdUntil)}</b> left</span>
           <button class="btn" data-action="release">Release</button>
         </div>`
      : "";

    this._renderShell(`
      <div class="head">
        <div class="title">${title}</div>
        <ha-icon class="sun ${sunOn ? "on" : ""}" icon="${sunOn ? "mdi:white-balance-sunny" : "mdi:weather-sunny-off"}"
                 title="Sun ${sunOn ? "in" : "not in"} window"></ha-icon>
        <label class="toggle" title="Shade control on/off">
          <input type="checkbox" data-action="control" ${enabled ? "checked" : ""}>
          <span class="track"></span>
        </label>
      </div>
      <div class="body ${enabled ? "" : "dimmed"}">
        <div class="stats">
          <div class="stat"><div class="num">${target.state}%</div><div class="lbl">target</div></div>
          <div class="stat"><div class="num">${current}</div><div class="lbl">current</div></div>
          <div class="stat"><div class="num">${glare ? glare.state + "%" : "—"}</div><div class="lbl">glare</div></div>
        </div>
        ${chips ? `<div class="chips">${chips}</div>` : ""}
      </div>
      ${graphHtml}
      <div class="foot">
        <span class="badge ${reasonClass}">${reasonText}</span>
        ${holdActive || !enabled ? "" : `<button class="btn ghost" data-action="hold" title="Pause automatic control for this zone's hold duration">Hold</button>`}
      </div>
      ${holdBanner}
    `);

    this._wire(ids, attrs);
    if (holdActive && holdUntil) this._startCountdown(holdUntil);
    else this._stopCountdown();
  }

  _renderShell(inner) {
    this.innerHTML = `
      <ha-card>
        <style>
          shade-engine-card .head { display: flex; align-items: center; gap: 10px; padding: 14px 16px 0; }
          shade-engine-card .title { flex: 1; font-size: 1.15em; font-weight: 500; color: var(--primary-text-color); }
          shade-engine-card .sun { color: var(--disabled-text-color); --mdc-icon-size: 20px; }
          shade-engine-card .sun.on { color: var(--warning-color, #ffa600); }
          shade-engine-card .body { padding: 4px 16px 0; }
          shade-engine-card .body.dimmed { opacity: 0.45; }
          shade-engine-card .stats { display: flex; gap: 8px; padding: 10px 0 4px; }
          shade-engine-card .stat { flex: 1; text-align: center; }
          shade-engine-card .num { font-size: 1.5em; font-weight: 400; color: var(--primary-text-color); }
          shade-engine-card .lbl { font-size: 0.72em; text-transform: uppercase; letter-spacing: 0.5px; color: var(--secondary-text-color); }
          shade-engine-card .chips { display: flex; flex-wrap: wrap; gap: 6px; padding: 6px 0 2px; }
          shade-engine-card .chip { border: 1px solid var(--divider-color); background: none; color: var(--primary-text-color);
            border-radius: 14px; padding: 4px 12px; font: inherit; font-size: 0.85em; cursor: pointer; }
          shade-engine-card .chip.active { background: var(--primary-color); border-color: var(--primary-color);
            color: var(--text-primary-color, #fff); }
          shade-engine-card .foot { display: flex; align-items: center; justify-content: space-between; padding: 8px 16px 14px; }
          shade-engine-card .badge { font-size: 0.8em; padding: 2px 10px; border-radius: 10px; }
          shade-engine-card .badge.ok { color: var(--success-color, #0a8754); background: rgba(10, 135, 84, 0.12); }
          shade-engine-card .badge.warn { color: var(--warning-color, #b5850b); background: rgba(181, 133, 11, 0.12); }
          shade-engine-card .badge.accent { color: var(--primary-color); background: rgba(33, 150, 243, 0.12); }
          shade-engine-card .badge.hold { color: var(--primary-color); background: rgba(103, 80, 164, 0.12); }
          shade-engine-card .badge.muted { color: var(--secondary-text-color); background: rgba(127, 127, 127, 0.12); }
          shade-engine-card .btn { border: none; border-radius: 6px; padding: 5px 14px; font: inherit; font-size: 0.85em;
            cursor: pointer; background: var(--primary-color); color: var(--text-primary-color, #fff); }
          shade-engine-card .btn.ghost { background: none; color: var(--primary-color); }
          shade-engine-card .hold-banner { display: flex; align-items: center; gap: 8px; margin: 0 12px 12px;
            padding: 8px 12px; border-radius: 8px; background: rgba(103, 80, 164, 0.12); color: var(--primary-text-color); }
          shade-engine-card .hold-banner ha-icon { --mdc-icon-size: 18px; }
          shade-engine-card .hold-banner span { flex: 1; font-size: 0.9em; }
          shade-engine-card .warn-box { padding: 16px; color: var(--secondary-text-color); }
          shade-engine-card .graph { padding: 8px 16px 0; }
          shade-engine-card .graph svg { display: block; width: 100%; height: 64px; }
          shade-engine-card .graph .elev { fill: var(--warning-color, #ffa600); opacity: 0.13; }
          shade-engine-card .graph .elevline { fill: none; stroke: var(--warning-color, #ffa600); opacity: 0.45;
            stroke-width: 1; vector-effect: non-scaling-stroke; }
          shade-engine-card .graph .target { fill: none; stroke: var(--primary-color); stroke-width: 2;
            vector-effect: non-scaling-stroke; stroke-linejoin: round; }
          shade-engine-card .graph .tick { stroke: var(--divider-color); stroke-width: 1;
            stroke-dasharray: 2, 4; vector-effect: non-scaling-stroke; }
          shade-engine-card .toggle { position: relative; display: inline-block; width: 36px; height: 20px; }
          shade-engine-card .toggle input { opacity: 0; width: 0; height: 0; }
          shade-engine-card .toggle .track { position: absolute; inset: 0; border-radius: 10px; cursor: pointer;
            background: var(--disabled-text-color); transition: background 0.15s; }
          shade-engine-card .toggle .track::before { content: ""; position: absolute; width: 14px; height: 14px;
            border-radius: 50%; background: #fff; top: 3px; left: 3px; transition: transform 0.15s; }
          shade-engine-card .toggle input:checked + .track { background: var(--primary-color); }
          shade-engine-card .toggle input:checked + .track::before { transform: translateX(16px); }
        </style>
        ${inner}
      </ha-card>
    `;
  }

  _wire(ids, attrs) {
    const zone = attrs.zone_id || this._config.zone;
    this.querySelectorAll(".chip").forEach((chip) =>
      chip.addEventListener("click", () =>
        this._hass.callService("select", "select_option", {
          entity_id: ids.mode,
          option: chip.dataset.mode,
        })
      )
    );
    const control = this.querySelector('[data-action="control"]');
    if (control)
      control.addEventListener("change", () =>
        this._hass.callService("switch", control.checked ? "turn_on" : "turn_off", {
          entity_id: ids.control,
        })
      );
    const release = this.querySelector('[data-action="release"]');
    if (release)
      release.addEventListener("click", () =>
        this._hass.callService("shade_engine", "release", { zone })
      );
    const holdBtn = this.querySelector('[data-action="hold"]');
    if (holdBtn)
      holdBtn.addEventListener("click", () =>
        this._hass.callService("shade_engine", "hold", { zone })
      );
  }

  // -- history graph --------------------------------------------------------

  _graphHours() {
    return clamp(Number(this._config.graph_hours) || 24, 1, 48);
  }

  _graphKey(ids) {
    return `${ids.target}|${this._graphHours()}`;
  }

  _maybeFetchHistory(ids) {
    if (this._config.graph === false || this._historyInFlight) return;
    const key = this._graphKey(ids);
    if (this._history && this._history.key === key && Date.now() - this._history.fetched < 300000) {
      return; // refreshed at most every 5 minutes
    }
    this._historyInFlight = true;
    const now = Date.now();
    const start = new Date(now - this._graphHours() * 3600000).toISOString();
    const end = encodeURIComponent(new Date(now).toISOString());
    this._hass
      .callApi(
        "GET",
        `history/period/${start}?filter_entity_id=${ids.target}&end_time=${end}&minimal_response&no_attributes`
      )
      .then((res) => {
        const points = ((res && res[0]) || [])
          .map((r) => ({ t: Date.parse(r.last_changed || r.last_updated), v: parseFloat(r.state) }))
          .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v));
        this._history = { key, fetched: Date.now(), points };
      })
      .catch(() => {
        this._history = { key, fetched: Date.now(), points: [] };
      })
      .finally(() => {
        this._historyInFlight = false;
        this._rendered = null;
        if (this._hass) this._render();
      });
  }

  _graphSvg(ids, currentTarget) {
    const W = 480;
    const H = 64;
    const now = Date.now();
    const start = now - this._graphHours() * 3600000;
    const x = (t) => ((clamp(t, start, now) - start) / (now - start)) * W;

    // Sun elevation (above the horizon only), computed locally — 0..90°
    // maps bottom..top on the same strip as the 0..100% target scale.
    const lat = this._hass.config.latitude;
    const lon = this._hass.config.longitude;
    let elevArea = "";
    let elevLine = "";
    if (lat != null && lon != null) {
      const pts = [];
      const N = 96;
      for (let i = 0; i <= N; i++) {
        const t = start + ((now - start) * i) / N;
        const e = clamp(solarElevation(t, lat, lon), 0, 90);
        pts.push(`${x(t).toFixed(1)},${(H - (e / 90) * H).toFixed(1)}`);
      }
      elevArea = `<path class="elev" d="M0,${H} L${pts.join(" L")} L${W},${H} Z"/>`;
      elevLine = `<path class="elevline" d="M${pts.join(" L")}"/>`;
    }

    // Shade target as a step line from recorder history + the live value.
    let targetLine = "";
    const hist = this._history && this._history.key === this._graphKey(ids) ? this._history.points : null;
    if (hist) {
      const pts = hist.slice();
      if (Number.isFinite(currentTarget)) pts.push({ t: now, v: currentTarget });
      if (pts.length) {
        const yT = (v) => H - (clamp(v, 0, 100) / 100) * H;
        let d = `M0,${yT(pts[0].v).toFixed(1)}`;
        for (const p of pts) d += ` H${x(p.t).toFixed(1)} V${yT(p.v).toFixed(1)}`;
        d += ` H${W}`;
        targetLine = `<path class="target" d="${d}"/>`;
      }
    }

    // A tick at each local midnight inside the window.
    let ticks = "";
    const midnight = new Date(start);
    midnight.setHours(24, 0, 0, 0);
    for (let t = midnight.getTime(); t < now; t += 86400000) {
      ticks += `<line class="tick" x1="${x(t).toFixed(1)}" y1="0" x2="${x(t).toFixed(1)}" y2="${H}"/>`;
    }

    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      ${elevArea}${elevLine}${ticks}
      <line class="tick" x1="0" y1="${H / 2}" x2="${W}" y2="${H / 2}"/>
      ${targetLine}
    </svg>`;
  }

  // -- hold countdown -------------------------------------------------------

  _remaining(iso) {
    const secs = Math.max(0, Math.round((new Date(iso).getTime() - Date.now()) / 1000));
    const h = Math.floor(secs / 3600);
    const m = Math.floor((secs % 3600) / 60);
    const s = secs % 60;
    const mm = String(m).padStart(2, "0");
    const ss = String(s).padStart(2, "0");
    return h ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
  }

  _startCountdown(iso) {
    this._stopCountdown();
    this._timer = setInterval(() => {
      const el = this.querySelector("[data-countdown]");
      if (!el) return this._stopCountdown();
      el.textContent = this._remaining(iso);
      // At zero the backend flips the hold sensor and the card re-renders.
      if (el.textContent === "0:00") this._stopCountdown();
    }, 1000);
  }

  _stopCountdown() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  // -- misc -----------------------------------------------------------------

  _prettify(slug) {
    return (slug || "")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }
}

/* Register the element only once Home Assistant's app has booted.
 *
 * The integration loads this module via `extra_module_url`, which the index
 * page imports concurrently with app.js. Since HA 2026.8 the frontend installs
 * the scoped-custom-element-registry polyfill, which REPLACES
 * window.customElements with a fresh registry; anything defined on the native
 * registry before that swap is invisible to customElements.get() and the
 * dashboard shows "Custom element doesn't exist". With a warm cache this
 * module reliably wins that race and loses the registry, so wait for HA's root
 * element and then define on whatever registry is current. */
function registerCard() {
  const registry = window.customElements;
  if (registry.get("shade-engine-card")) return;
  registry.define("shade-engine-card", ShadeEngineCard);
}
customElements
  .whenDefined("home-assistant")
  .then(registerCard, registerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "shade-engine-card",
  name: "Shade Engine Card",
  description:
    "Per-zone shade status and control: mode, target, manual-hold countdown, engine on/off.",
});

console.info(`%c SHADE-ENGINE-CARD %c ${CARD_VERSION} `, "background:#4a3aa7;color:#fff", "");
