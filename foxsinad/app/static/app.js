// foxsinad UI. Talks to the measurement engine only through `transport`,
// so the same page runs on the desktop (HTTP + server-sent events) and,
// later, in an Android WebView (a JavaScript bridge object). Messages are
// specified in docs/ui-protocol.md.
"use strict";

// ---- transport -------------------------------------------------------------
function httpTransport() {
  let onEvent = () => {}, onLink = () => {};
  const es = new EventSource("/events");
  es.onopen = () => onLink(true);
  es.onerror = () => onLink(false);
  es.onmessage = (m) => onEvent(JSON.parse(m.data));
  return {
    send: (cmd) => fetch("/cmd", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cmd),
    }).then((r) => r.json()).catch((e) => ({ok: false, error: `no reply from the engine (${e.message})`})),
    events: (fn) => { onEvent = fn; },
    link: (fn) => { onLink = fn; },
  };
}

// Android: the app injects window.FoxsinadBridge with send(json) -> json, and
// calls window.foxsinadEvent(json) for each event.
function bridgeTransport(bridge) {
  let onEvent = () => {};
  window.foxsinadEvent = (json) => onEvent(JSON.parse(json));
  return {
    send: (cmd) => Promise.resolve(JSON.parse(bridge.send(JSON.stringify(cmd)))),
    events: (fn) => { onEvent = fn; },
    link: (fn) => fn(true),
  };
}

const transport = window.FoxsinadBridge ? bridgeTransport(window.FoxsinadBridge) : httpTransport();

// ---- state -----------------------------------------------------------------
const S = {
  state: null,          // engine state: mode, connected, setup, ...
  status: null,         // PicoFox status
  cal: null,            // {att: [...], dbm: [...]}
  results: [],          // finished sweeps this engine session
  records: [],          // finished records this engine session
  run: null,            // current sweep: {label, index, of, points: [], mark}
  prevRuns: [],         // earlier sweeps' points, drawn faint
  readings: [],         // [{t, sinad, ...}] for meter, ballpark and record
  log: [],
  foxEvents: [],
  selectedMode: loadPref("mode", "sweep"),
  tab: loadPref("tab", "setup"),
  linkOk: false,
};
const TIME_WINDOW_S = 120;
const RECORD_WINDOW_S = 300;     // record shows more history
const windowFor = (mode) => (mode === "record" ? RECORD_WINDOW_S : TIME_WINDOW_S);

// The workflow tabs (foxsinad's page; a page without #tabbar, such as
// rfbench's, keeps the Sweep/Ballpark/Meter buttons). Each step's tab picks
// the mode Start runs; Setup and the tools tab run none.
const TABBED = !!document.getElementById("tabbar");
const TAB_MODE = {setup: null, audio: "meter", level: "ballpark", sweep: "sweep", record: "record", tools: null};
const MODE_TAB = {meter: "audio", ballpark: "level", sweep: "sweep", record: "record"};
const TARGET_DB = 12;

// What a level means. Only a cabled path gives the level at the radio;
// over the air the path loss is unknown, so levels are the PicoFox's output.
const REF = {
  radio: {text: "at the radio", axis: "level at the radio (dBm)", row: "At the radio"},
  antenna: {text: "into the transmit antenna", axis: "level into the transmit antenna (dBm)", row: "Into the antenna"},
  output: {text: "at the PicoFox output", axis: "PicoFox output (dBm)", row: null},
};
function setupRef() {
  const s = S.state && S.state.setup;
  if (!s) return "output";
  const known = s.pad_db !== null && s.pad_db !== undefined && s.pad_db !== "";
  return s.cabled && known ? "radio" : (s.pad_db ? "antenna" : "output");
}
const attText = (db) => `${(+db).toFixed(2)} dB (ATT ${Math.round(db * 4)})`;

function loadPref(k, dflt) {
  try { return localStorage.getItem(`foxsinad.${k}`) || dflt; } catch { return dflt; }
}
function savePref(k, v) {
  try { localStorage.setItem(`foxsinad.${k}`, v); } catch { /* private window */ }
}

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(+v)) ? "–" : (+v).toFixed(d).replace("-", "−");
const running = () => S.state && S.state.mode !== "idle";
const tabMode = () => (TABBED ? TAB_MODE[S.tab] : S.selectedMode);
const plotMode = () => (running() ? S.state.mode : tabMode() || S.selectedMode);

function calDbm(att) {
  const c = S.cal;
  if (!c || !c.att.length) return null;
  if (att <= c.att[0]) return c.dbm[0];
  for (let i = 1; i < c.att.length; i++) {
    if (att <= c.att[i]) {
      const f = (att - c.att[i - 1]) / (c.att[i] - c.att[i - 1]);
      return c.dbm[i - 1] + f * (c.dbm[i] - c.dbm[i - 1]);
    }
  }
  return c.dbm[c.dbm.length - 1];
}

async function send(cmd) {
  const reply = await transport.send(cmd);
  if (!reply.ok) {
    toast(reply.error);
    if (reply.field) goToField(reply.field);   // the engine said which field stopped it
  }
  return reply;
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---- events from the engine -------------------------------------------------
transport.link((ok) => {
  S.linkOk = ok;
  $("link").textContent = ok ? "engine connected" : "engine not reachable";
  $("link").className = `link ${ok ? "ok" : "bad"}`;
  $("linkBanner").hidden = ok;
});

transport.events((ev) => {
  switch (ev.event) {
    case "snapshot":
      S.state = ev.state; S.status = ev.status; S.cal = ev.cal; S.results = ev.results || [];
      S.records = ev.records || [];
      renderAll();
      break;
    case "state":
      S.state = ev.state;
      renderHeader(); renderSetup(); renderStatus(); renderManual(); renderSteps(); draw();
      break;
    case "status":
      S.status = ev.status;
      renderStatus(); renderManual(); renderSetup();
      break;
    case "run_started":
      if (S.run && S.run.points.length) S.prevRuns.push(S.run.points);
      S.prevRuns = S.prevRuns.slice(-20);
      S.run = {label: ev.label, index: ev.index, of: ev.of, points: [], mark: null,
               ref: ev.level_ref || setupRef()};
      renderReadout(); draw();
      break;
    case "point":
      if (!S.run) S.run = {label: "", index: 1, of: 1, points: [], mark: null, ref: setupRef()};
      S.run.points.push({phase: ev.phase, level: ev.level_dbm, sinad: ev.sinad_db,
                         sd: ev.sinad_sd, att: ev.att});
      renderReadout(); draw();
      break;
    case "run_finished":
      S.results.push(ev);
      if (S.run) S.run.mark = ev.point_dbm;
      renderResults(); renderReadout(); renderSteps(); draw();
      break;
    case "record_finished":
      S.records.push(ev);
      renderRecords(); renderSteps();
      break;
    case "reading":
      S.readings.push({t: ev.t, sinad: ev.sinad_db, hint: ev.hint, level: ev.level_dbm,
                       ref: ev.level_ref, attDb: ev.att_db, clipped: ev.clipped,
                       under: ev.under_range, peak: ev.peak_dbfs, audioHint: ev.audio_hint,
                       box: ev.boxcar_db, id: ev.id_active, mode: plotMode()});
      S.readings = S.readings.filter((r) => ev.t - r.t <= windowFor(plotMode()) + 5);
      renderReadout(); draw();
      break;
    case "fox_event":
      S.foxEvents.unshift({t: ev.t, text: ev.text});
      S.foxEvents = S.foxEvents.slice(0, 30);
      renderFoxEvents();
      break;
    case "log":
      S.log.push({t: ev.t, level: ev.level, msg: ev.msg});
      S.log = S.log.slice(-300);
      renderLog();
      // An error (a mode that stopped, say) also shows where you're looking.
      if (ev.level === "error") toast(String(ev.msg).split("\n")[0]);
      break;
  }
});

// ---- header -----------------------------------------------------------------
document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => {
  if (running()) return;
  S.selectedMode = b.dataset.mode;
  savePref("mode", S.selectedMode);
  renderHeader(); renderReadout(); draw();
}));
// Every mode needs the PicoFox, so until it's connected this button connects it.
// The Audio step sets the volume on the strongest signal, so it starts at ATT 0.
$("startBtn").addEventListener("click", () => {
  if (!S.state.connected) return send({cmd: "connect"});
  const mode = tabMode();
  if (!mode) return;
  if (!S.state.setup.cabled && !callsignEntered()) return needCallsign();
  if (mode !== "sweep") S.readings = [];
  send(TABBED && S.tab === "audio" ? {cmd: "start", mode, att_db: 0} : {cmd: "start", mode});
});
$("stopBtn").addEventListener("click", () => send({cmd: "stop"}));

const MODE_HELP = {
  sweep: "Steps the PicoFox down through its range and finds the 12 dB SINAD point. "
       + "Cabled, it's the level at the radio; over the air, the PicoFox output. "
       + "Open the radio's squelch first.",
  ballpark: "Transmits at the ballpark attenuation and shows SINAD live. Adjust the step "
          + "attenuator (cabled) or the spacing (over the air) until it reads about 12 dB, "
          + "then Stop. Cabled: enter the total external attenuation.",
  meter: "Shows the radio's SINAD live. With the PicoFox connected it transmits the tone "
       + "at the setup's frequency and the attenuator as set; Stop turns RF off.",
  record: "Holds one level and records SINAD every 0.25 s until Stop.",
};
const MODE_NAME = {meter: "Audio", ballpark: "Level", sweep: "Sweep", record: "Record"};

function renderHeader() {
  const st = S.state;
  const mode = plotMode();
  document.querySelectorAll(".seg").forEach((b) => {
    b.setAttribute("aria-checked", String(b.dataset.mode === mode));
    b.disabled = running() && b.dataset.mode !== mode;
  });
  $("simBadge").hidden = !(st && st.sim);
  const connected = st && st.connected;
  $("startBtn").disabled = !st || running() || (connected && !tabMode());
  $("startBtn").textContent = st && !connected ? "Connect" : "Start";
  $("startBtn").title = connected && !tabMode() ? "Open Audio, Level, Sweep or Record to start one" : "";
  const rfOn = S.status && S.status.mode && S.status.mode !== "OFF";
  $("stopBtn").disabled = !st || !(running() || rfOn);
  const name = (m) => (TABBED ? MODE_NAME[m] : `${m[0].toUpperCase()}${m.slice(1)}`);
  $("runState").textContent = !st ? "" : running() ? `${name(st.mode)} running` : "Idle";
  renderRf(rfOn);
  $("cabledBanner").hidden = !(st && st.setup.cabled);
  if ($("modeHelp")) $("modeHelp").textContent = MODE_HELP[mode];
  const ref = S.run && running() ? S.run.ref : setupRef();
  $("plotTitle").textContent = mode === "sweep"
    ? `SINAD vs ${REF[ref].axis.replace(" (dBm)", "")}` : "SINAD over time";
}

// Whether the PicoFox is transmitting, always in view (pages without the
// element, such as rfbench's, skip it).
function renderRf(rfOn) {
  const el = $("rfState");
  if (!el) return;
  const st = S.status;
  el.classList.toggle("on", !!rfOn);
  if (!st) { el.textContent = "PicoFox not connected"; return; }
  if (!rfOn) { el.textContent = "RF off"; return; }
  let txt = `RF ON · ${st.mode.toLowerCase()} · ${st.cabled === "1" ? "cabled" : "on air"}`
    + ` · ${(+st.freq).toFixed(3)} MHz`;
  if (st.out_dbm !== undefined) txt += ` · ${fmt(st.out_dbm, 1)} dBm`;
  if (st.id_active === "1" || st.id_active === true) txt += " · sending ID";
  el.textContent = txt;
}

// Transmitting on air needs a callsign for the Morse ID. The field as typed
// counts too: its change may not have come back from the engine yet.
function callsignEntered() {
  const input = $("f_call");
  return !!((input && input.value.trim()) || S.state.setup.call);
}

function needCallsign() {
  toast("Enter a callsign (or use cabled mode)");
  goToField("call");
}

// Whenever a field blocks a run: open its tab, scroll to it, focus it and
// outline it until it's edited.
function goToField(k) {
  const input = $(`f_${k}`);
  if (!input) return;
  const panel = input.closest(".tabpanel");
  if (panel) selectTab(panel.dataset.tab);
  const field = input.closest(".field");
  field.classList.add("invalid", "attn");
  input.scrollIntoView({block: "center", behavior: "smooth"});
  input.focus({preventScroll: true});
  input.addEventListener(input.type === "checkbox" ? "change" : "input",
                         () => field.classList.remove("invalid", "attn"), {once: true});
}

// ---- workflow tabs ------------------------------------------------------------
document.querySelectorAll(".tab").forEach((b) =>
  b.addEventListener("click", () => selectTab(b.dataset.tab)));

function selectTab(t) {
  if (!TABBED || !(t in TAB_MODE)) return;
  S.tab = t;
  savePref("tab", t);
  if (TAB_MODE[t] && !running()) S.selectedMode = TAB_MODE[t];
  document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.dataset.tab !== t; });
  // the one readout and plot go into the open step's tab
  const host = document.querySelector(`.tabpanel[data-tab="${t}"] .plothost`);
  const pane = $("plotPane");
  if (host && pane.parentElement !== host) host.appendChild(pane);
  pane.hidden = !host;
  window.scrollTo(0, 0);
  renderSteps(); renderHeader(); renderReadout(); draw();
}

// Each step's checkmark, from the engine's state: guidance only, nothing is
// refused because an earlier step isn't checked.
function stepDone(t) {
  const st = S.state;
  if (!st) return false;
  const s = st.setup;
  const mine = (r) => (r.radio || "") === (s.radio || "");
  switch (t) {
    case "setup": return !!(st.connected && (s.call || s.cabled) && s.freq);
    case "audio": return !!st.audio_set;
    case "level": return s.pad_db !== null && s.pad_db !== undefined;
    case "sweep": return S.results.some((r) => mine(r) && r.point_dbm !== null);
    case "record": return S.records.some(mine);
    default: return false;
  }
}

function renderSteps() {
  if (!TABBED) return;
  const runTab = running() ? MODE_TAB[S.state.mode] : null;
  document.querySelectorAll(".tab").forEach((b) => {
    const t = b.dataset.tab;
    b.setAttribute("aria-selected", String(t === S.tab));
    b.classList.toggle("done", stepDone(t));
    b.classList.toggle("running", t === runTab);
  });
  renderStepNotes();
}

function renderStepNotes() {
  const st = S.state;
  if (!st) return;
  const a = st.audio_set;
  $("audioSet").innerHTML = a
    ? `<span class="tag good">Audio set: ${fmt(a.peak_dbfs, 1)} dBFS peak</span>`
    : `<span class="muted">Not set yet for this radio.</span>`;
  const r = recordStart();
  $("recordStart").innerHTML = r
    ? `Starts at <strong>${attText(r.att / 4)}</strong>${r.out === null ? "" : `, ${fmt(r.out - (st.setup.pad_db || 0), 1)} dBm ${REF[setupRef()].text}`} <span class="muted">(${esc(r.why)})</span>`
    : "";
}

// Where record starts, as the engine decides it (_record_level).
function recordStart() {
  const st = S.state;
  if (!st) return null;
  const s = st.setup;
  let att, why;
  if (s.record_att_db !== null && s.record_att_db !== undefined) {
    att = Math.round(s.record_att_db * 4); why = "as set";
  } else {
    const last = [...S.results].reverse().find((r) => r.point_dbm !== null && r.point_dbm !== undefined);
    if (last && S.cal) {
      const out = last.point_dbm + (last.pad_db || 0);
      att = 0;
      for (let a = 1; a < 128; a++) if (Math.abs(calDbm(a) - out) < Math.abs(calDbm(att) - out)) att = a;
      why = "nearest the last sweep's 12 dB point";
    } else {
      att = Math.round(s.ballpark_att_db * 4); why = "the ballpark attenuation; no sweep yet";
    }
  }
  return {att, why, out: calDbm(att)};
}

if (TABBED) {
  $("nextRadioBtn").addEventListener("click", async () => {
    if (running()) return toast("Stop first");
    const r = await send({cmd: "setup", radio: "", pad_db: ""});
    if (!r.ok) return;
    S.readings = [];
    selectTab("setup");
    goToField("radio");
  });
  document.querySelectorAll(".stepper button").forEach((b) => b.addEventListener("click", () => {
    const att = S.status && +S.status.att;
    if (att === undefined || Number.isNaN(att)) return;
    const next = Math.max(0, Math.min(127, att + +b.dataset.step));
    send({cmd: "fox", action: "att", value: next / 4});
  }));
}

function renderRecords() {
  const el = $("records");
  if (!el) return;
  if (!S.records.length) { el.innerHTML = `<span class="muted">none yet</span>`; return; }
  const rows = S.records.flatMap((r) => r.summary.map((g, i) => `<tr title="${esc(r.csv)}">
      <td>${i ? "" : esc(r.label)}</td><td class="num">${attText(g.att / 4)}</td>
      <td class="num">${fmt(g.level_dbm, 1)}</td><td class="num">${fmt(g.seconds, 0)}</td>
      <td class="num">${fmt(g.median_db, 1)}</td><td class="num">${fmt(g.p10_db, 1)}–${fmt(g.p90_db, 1)}</td>
      <td class="num">${fmt(g.below_pct, 0)}%</td></tr>`)).join("");
  el.innerHTML = `<table><thead><tr><th>Record</th><th class="num">Attenuator</th><th class="num">Level (dBm)</th>
    <th class="num">Seconds</th><th class="num">Median SINAD</th><th class="num">10–90%</th>
    <th class="num">Below 12 dB</th></tr></thead><tbody>${rows}</tbody></table>`;
}

// ---- run setup -------------------------------------------------------------------
// group: the workflow tab a field belongs on (a page without tabs puts them
// all in #setup, in this order).
const FIELDS = [
  {k: "session", label: "Session", type: "text", hint: "runs are grouped in a folder of this name", group: "setup"},
  {k: "call", label: "Callsign", type: "text", group: "setup"},
  {k: "freq", label: "Frequency", unit: "MHz", type: "number", step: "0.005", group: "setup"},
  {k: "cabled", label: "Cabled: no Morse ID", type: "checkbox", group: "setup"},
  {sep: true, group: "setup"},
  {k: "radio", label: "Radio", type: "text", group: "setup"},
  {k: "condition", label: "Condition", type: "text", hint: "bench, antenna, summit …", group: "setup"},
  {k: "note", label: "Note", type: "text", group: "setup"},
  {sep: true, group: "setup"},
  {k: "tone", label: "Tone", unit: "Hz", type: "number", step: "1", group: "setup"},
  {k: "dev", label: "Deviation", unit: "Hz, commanded", type: "number", step: "100", group: "setup"},
  {k: "monitor", label: "Play the audio on the speakers", type: "checkbox", group: "setup"},
  {k: "audio_target_dbfs", label: "Audio target", unit: "dBFS peak", type: "number", step: "1", group: "audio",
   hint: "where the tone's peak should sit; set the radio's volume to it on a strong signal"},
  {k: "ballpark_att_db", label: "Ballpark attenuation", unit: "dB, PicoFox", type: "number", step: "0.25", group: "level"},
  {k: "pad_db", label: "External attenuation", unit: "dB", type: "number", step: "0.1", group: "level",
   hint: "between the PicoFox and the radio (cabled) or its antenna. Needed for cabled sweeps: only then is the level at the radio known."},
  {k: "repeat", label: "Repeat", unit: "sweeps", type: "number", step: "1", group: "sweep"},
  {k: "record_att_db", label: "Record level", unit: "dB, PicoFox; empty: automatic", type: "number", step: "0.25", group: "record"},
];

function buildSetup() {
  const fallback = $("setup");
  for (const f of FIELDS) {
    const form = $(`fields-${f.group}`) || fallback;
    if (f.sep) { form.insertAdjacentHTML("beforeend", '<hr class="sep">'); continue; }
    const id = `f_${f.k}`;
    const html = f.type === "checkbox"
      ? `<label class="field check" id="w_${f.k}"><input type="checkbox" id="${id}" aria-label="${esc(f.label)}"><span>${esc(f.label)}</span></label>`
      : `<label class="field" id="w_${f.k}"><span>${esc(f.label)}${f.unit ? ` <span class="unit">(${esc(f.unit)})</span>` : ""}</span>
           <input type="${f.type}" id="${id}" aria-label="${esc(f.label)}" ${f.step ? `step="${f.step}"` : ""} inputmode="${f.type === "number" ? "decimal" : "text"}">
           ${f.hint ? `<span class="hint">${esc(f.hint)}</span>` : ""}<span class="err" hidden></span></label>`;
    form.insertAdjacentHTML("beforeend", html);
    const input = $(id);
    input.addEventListener("change", () => onFieldChange(f, input));
  }
  // the field forms only: a dialog's form must submit, which closes it
  for (const form of document.querySelectorAll('form:not([method="dialog"])'))
    form.addEventListener("submit", (e) => e.preventDefault());
}

async function onFieldChange(f, input) {
  const wrap = $(`w_${f.k}`);
  const err = wrap.querySelector(".err");
  if (f.k === "cabled" && input.checked && !S.state.cabled_confirmed) {
    input.checked = false;
    const ok = await askCabled();
    if (!ok) return;
    const r = await send({cmd: "confirm_cabled", confirmed: true});
    if (!r.ok) return;
    input.checked = true;
  }
  const value = f.type === "checkbox" ? input.checked : input.value;
  const r = await send({cmd: "setup", [f.k]: value});
  wrap.classList.toggle("invalid", !r.ok);
  if (err) { err.hidden = r.ok; err.textContent = r.ok ? "" : r.error; }
  if (!r.ok) renderSetup(true);
}

function askCabled() {
  const d = $("cabledDialog");
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), {once: true});
    d.returnValue = "";
    d.showModal();
  });
}

function renderSetup(force = false) {
  if (!S.state) return;
  const s = S.state.setup;
  for (const f of FIELDS) {
    if (f.sep) continue;
    const input = $(`f_${f.k}`);
    if (!force && input === document.activeElement) continue;
    if (f.type === "checkbox") input.checked = !!s[f.k];
    else input.value = s[f.k] ?? "";
    input.disabled = running() || (f.k === "cabled" && S.status && S.status.mode !== "OFF");
  }
}

// ---- PicoFox status ------------------------------------------------------------
$("connectBtn").addEventListener("click", () =>
  send({cmd: S.state && S.state.connected ? "disconnect" : "connect"}));

function mmss(s) {
  s = Math.max(0, Math.round(s));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function renderStatus() {
  const st = S.state, f = S.status;
  const btn = $("connectBtn");
  btn.textContent = st && st.connected ? "Disconnect" : "Connect";
  btn.disabled = !st || running();
  const dl = $("foxStatus");
  if (!st || !st.connected || !f) {
    dl.innerHTML = `<dt>State</dt><dd class="muted">not connected</dd>`;
    renderHeader();
    return;
  }
  const att = +f.att;
  const out = f.out_dbm, lvl = f.level_dbm;
  const levelRow = f.level_ref && REF[f.level_ref].row;
  const on = f.mode !== "OFF";
  const outChip = !on ? `<span class="chip off">OFF</span>`
    : f.cabled === "1" ? `<span class="chip cabled">${esc(f.mode)} · cabled</span>`
    : `<span class="chip on">${esc(f.mode)} · on air</span>`;
  let id = "";
  if (f.id_active === "1" || f.id_active === true) id = `<span class="chip id">sending ID</span>`;
  else if (on && f.cabled !== "1") id = `next in ${mmss(f.next_id_s ?? 0)} <span class="muted">(last ${mmss(+f.since_id)} ago)</span>`;
  else if (on) id = `<span class="muted">none (cabled)</span>`;
  else id = `<span class="muted">—</span>`;
  const rows = [
    ["Output", outChip],
    ["Frequency", `${(+f.freq).toFixed(4)} MHz`],
    ["Attenuation", Number.isNaN(att) ? "–" : attText(att / 4)],
    ["PicoFox out", out === undefined ? "–" : `${fmt(out, 1)} dBm`],
    ...(levelRow ? [[levelRow, `${fmt(lvl, 1)} dBm`]] : []),
    ["Tone", on && f.mode === "TONE" ? `${esc(f.tone)} Hz · ${esc(f.dev)} Hz dev` : `<span class="muted">${esc(f.tone)} Hz · ${esc(f.dev)} Hz dev (set)</span>`],
    ["Callsign", esc(f.call)],
    ["Morse ID", id],
    ["Firmware", `<span class="muted">${esc(st.hello)}</span>`],
    ["Port", `<span class="muted">${esc(st.port)}</span>`],
  ];
  dl.innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  renderHeader();
}

function renderFoxEvents() {
  const ul = $("foxEvents");
  ul.innerHTML = S.foxEvents.length
    ? S.foxEvents.map((e) => `<li>${new Date(e.t * 1000).toLocaleTimeString()}  ${esc(e.text)}</li>`).join("")
    : `<li class="muted">none yet</li>`;
}

// ---- manual control -------------------------------------------------------------
let sliderBusy = false;
const slider = $("attSlider");
slider.addEventListener("pointerdown", () => { sliderBusy = true; });
slider.addEventListener("input", () => { sliderBusy = true; renderAttLabel(+slider.value); });  // dB
slider.addEventListener("change", async () => {
  await send({cmd: "fox", action: "att", value: +slider.value});
  sliderBusy = false;
});
$("toneBtn").addEventListener("click", () =>
  send({cmd: "fox", action: "tone", value: S.state.setup.tone, dev: S.state.setup.dev}));
$("carrierBtn").addEventListener("click", () => send({cmd: "fox", action: "carrier"}));
$("offBtn").addEventListener("click", () => send({cmd: "fox", action: "off"}));
$("idBtn").addEventListener("click", () => send({cmd: "fox", action: "id"}));
$("freqBtn").addEventListener("click", () => send({cmd: "fox", action: "freq", value: S.state.setup.freq}));

function renderAttLabel(db) {
  const out = calDbm(db * 4);
  const pad = (S.state && S.state.setup.pad_db) || 0;
  const ref = setupRef();
  let txt = attText(db);
  if (out !== null) txt += ` · out ${fmt(out, 1)} dBm`;
  if (out !== null && REF[ref].row) txt += ` · ${REF[ref].row.toLowerCase()} ${fmt(out - pad, 1)} dBm`;
  $("attLabel").textContent = txt;
}

function renderManual() {
  const st = S.state, f = S.status;
  const connected = st && st.connected;
  const mode = st ? st.mode : "idle";
  const all = ["toneBtn", "carrierBtn", "offBtn", "idBtn", "freqBtn"];
  for (const id of all) $(id).disabled = !connected || ["sweep", "ballpark", "record"].includes(mode);
  slider.disabled = !connected || mode === "sweep";
  if (f && !sliderBusy && !Number.isNaN(+f.att)) slider.value = f.att / 4;
  renderAttLabel(+slider.value);
  $("manualHint").textContent = !connected ? "Connect the PicoFox to use these."
    : mode === "sweep" ? "Locked while a sweep runs."
    : mode === "ballpark" || mode === "record" ? `Only the attenuator can change during ${mode}.`
    : `Tone and Carrier use the callsign, frequency, tone and deviation from ${TABBED ? "Setup" : "the Run pane"}.`;
}

// ---- log ------------------------------------------------------------------------
function renderLog() {
  const el = $("log");
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
  el.innerHTML = S.log.map((l) =>
    `<div class="${l.level}"><span class="t">${new Date(l.t * 1000).toLocaleTimeString()}</span> ${esc(l.msg)}</div>`).join("");
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ---- readout and results --------------------------------------------------------
const HINT_CLASS = (h) => !h ? "" : h.startsWith("GOOD") ? "good" : h.startsWith("NO") || h.startsWith("CLIP") ? "err" : "warn";

function renderReadout() {
  renderStrip();
  const el = $("readout");
  const mode = plotMode();
  if (mode === "sweep") {
    const r = S.run;
    if (!r) { el.innerHTML = `<span class="sub">No sweep yet in this session.</span><span class="sub"></span>`; return; }
    const last = r.points[r.points.length - 1];
    let html = `<span class="sub">${esc(r.label)} · run ${r.index} of ${r.of}</span>`;
    if (r.mark !== null && r.mark !== undefined) {
      html = `<span class="big">${fmt(r.mark, 2)}<span class="unit">dBm</span></span>
              <span class="sub">12 dB SINAD ${REF[r.ref].text} · ${esc(r.label)} · run ${r.index} of ${r.of}</span>`;
    } else if (last) {
      html += `<span class="sub">${last.phase} pass · ${attText(last.att / 4)} · ${fmt(last.level, 2)} dBm ${REF[r.ref].text} → ${fmt(last.sinad, 1)} dB SINAD</span>`;
    }
    el.innerHTML = html;
    return;
  }
  // Every indicator is always drawn ("–" until there's a reading), so the
  // page doesn't move when a run starts.
  const last = S.readings[S.readings.length - 1] || {};
  const shown = last.box ?? last.sinad;        // record: the 1 s average, not one window
  let html = `<span class="big">${fmt(shown, 1)}<span class="unit">dB SINAD</span></span>`;
  if (last.id) html += `<span class="tag warn">MORSE ID</span>`;
  else if (last.hint) html += `<span class="tag ${HINT_CLASS(last.hint)}">${esc(last.hint)}</span>`;
  else if (last.under) html += `<span class="tag err">NO AUDIO</span>`;
  else if (last.clipped) html += `<span class="tag err">CLIPPING</span>`;
  else if (mode === "meter" && last.audioHint)
    html += `<span class="tag ${last.audioHint === "OK" ? "good" : "warn"}">${esc(last.audioHint === "OK" ? "AUDIO ON TARGET" : last.audioHint.toUpperCase())}</span>`;
  else html += `<span class="tag" aria-hidden="true"></span>`;
  html += last.level !== null && last.level !== undefined
    ? `<span class="sub">${fmt(last.level, 1)} dBm ${REF[last.ref || "output"].text} · ${attText(last.attDb)}</span>`
    : `<span class="sub">${S.readings.length ? "" : startPrompt()}</span>`;
  // the audio bar lives in the strip when there is one
  el.innerHTML = html + ($("stripAudio") ? "" : audioLevel(last));
}

function startPrompt() {
  return S.state && !S.state.connected ? "Press Connect, then Start." : "Press Start to listen.";
}

// The strip: live SINAD and the audio bar, whatever tab is open.
function renderStrip() {
  if (!$("stripAudio")) return;
  const last = S.readings[S.readings.length - 1] || {};
  const pts = S.run ? S.run.points : [];
  const v = running() && S.state.mode === "sweep" ? (pts.length ? pts[pts.length - 1].sinad : null)
    : running() ? (last.box ?? last.sinad) : null;
  $("stripSinad").innerHTML = `${fmt(v, 1)}<span class="unit">dB SINAD</span>`;
  $("stripAudio").innerHTML = audioLevel(running() && S.state.mode !== "sweep" ? last : {});
}

// The sound card's peak level, for setting the radio's volume: a bar from
// -60 to 0 dBFS with the target band marked; green on target, amber off it
// or too quiet to measure, red when clipping.
const AUDIO_TOL_DB = 3;     // as the engine's
const dbfsPct = (db) => Math.max(0, Math.min(100, (db + 60) / 60 * 100));
function audioLevel(r) {
  const have = r.peak !== null && r.peak !== undefined;
  const pct = have ? dbfsPct(r.peak) : 0;
  const hint = r.audioHint || "";
  const cls = r.clipped ? "err" : (r.under || (hint && hint !== "OK")) ? "warn" : "good";
  const target = S.state && S.state.setup.audio_target_dbfs;
  const band = target === undefined || target === null ? "" :
    `<b class="target" style="left:${dbfsPct(target - AUDIO_TOL_DB).toFixed(1)}%;width:${(dbfsPct(target + AUDIO_TOL_DB) - dbfsPct(target - AUDIO_TOL_DB)).toFixed(1)}%"></b>`;
  return `<div class="audiolevel" title="Peak audio level at the sound card; the band is the target">
    <span class="lbl">Audio</span><span class="lvlbar ${cls}">${band}<i style="width:${pct.toFixed(1)}%"></i></span>
    <span class="lvlnum">${fmt(r.peak, 1)} dBFS</span><span class="lvlhint ${cls}">${esc(hint === "OK" ? "on target" : hint)}</span></div>`;
}

function renderResults() {
  const el = $("results");
  const rs = S.results;
  if (!rs.length) { el.innerHTML = TABBED ? `<span class="muted">none yet</span>` : ""; return; }
  const rows = rs.map((r, i) => `<tr title="${esc(r.csv)}">
      <td>${i + 1}</td><td>${esc(r.label)}</td>
      <td class="num">${r.point_dbm === null ? "none" : fmt(r.point_dbm, 2)}</td>
      <td class="num">${r.sigma_db === null ? "–" : "± " + fmt(r.sigma_db, 2)}</td>
      <td>${REF[r.level_ref || "radio"].text}</td>
      <td class="num">${r.fit_points ?? "–"}</td><td class="num">${fmt(r.pad_db, 1)}</td></tr>`).join("");
  const pts = rs.map((r) => r.point_dbm).filter((v) => v !== null);
  const refs = new Set(rs.map((r) => r.level_ref || "radio"));
  let summary = "";
  if (refs.size > 1) {
    summary = `<div class="summary">These runs measure levels in different places (cabled and over the air), so their spread isn't shown.</div>`;
  } else if (pts.length >= 2) {
    const mean = pts.reduce((a, b) => a + b, 0) / pts.length;
    const sd = Math.sqrt(pts.reduce((a, b) => a + (b - mean) ** 2, 0) / (pts.length - 1));
    summary = `<div class="summary">${pts.length} runs: mean ${fmt(mean, 2)} dBm, run-to-run sd ${fmt(sd, 2)} dB,
      range ${fmt(Math.max(...pts) - Math.min(...pts), 2)} dB. All runs since the app started, whatever their setup.</div>`;
  }
  el.innerHTML = `<table><thead><tr><th>#</th><th>Run</th><th class="num">12 dB SINAD (dBm)</th>
    <th class="num">Fit</th><th>Level</th><th class="num">Points</th><th class="num">Ext. atten. (dB)</th></tr></thead>
    <tbody>${rows}</tbody></table>${summary}`;
}

// ---- plot -----------------------------------------------------------------------
const canvas = $("plot");
const ctx = canvas.getContext("2d");
let hitPoints = [];       // [{x, y, text}] for the tooltip
let frame = null;         // plot area of the last draw

function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function niceTicks(lo, hi, target = 6) {
  const span = hi - lo || 1;
  const raw = span / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= target) || 10 * mag;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(6));
  return ticks;
}

function draw() {
  const wrap = $("canvasWrap");
  if (!wrap.clientWidth) return;           // the plot isn't on the open tab
  const dpr = window.devicePixelRatio || 1;
  const W = wrap.clientWidth, H = wrap.clientHeight;
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  hitPoints = [];
  const mode = plotMode();
  renderLegend(mode);
  if (mode === "sweep") drawSweep(W, H); else drawTime(W, H, mode);
}

function axes(W, H, x0, x1, y0, y1, xlabel, xfmt) {
  const m = {l: 46, r: 14, t: 10, b: 38};
  const f = {l: m.l, r: W - m.r, t: m.t, b: H - m.b};
  const X = (v) => f.l + (v - x0) / (x1 - x0) * (f.r - f.l);
  const Y = (v) => f.b - (v - y0) / (y1 - y0) * (f.b - f.t);
  ctx.font = "11px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.lineWidth = 1;
  for (const v of niceTicks(y0, y1, 6)) {
    ctx.strokeStyle = css("--grid"); ctx.beginPath(); ctx.moveTo(f.l, Y(v)); ctx.lineTo(f.r, Y(v)); ctx.stroke();
    ctx.fillStyle = css("--muted"); ctx.textAlign = "right"; ctx.textBaseline = "middle";
    ctx.fillText(String(v), f.l - 6, Y(v));
  }
  for (const v of niceTicks(x0, x1, Math.max(3, Math.floor((f.r - f.l) / 80)))) {
    ctx.strokeStyle = css("--grid"); ctx.beginPath(); ctx.moveTo(X(v), f.t); ctx.lineTo(X(v), f.b); ctx.stroke();
    ctx.fillStyle = css("--muted"); ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(xfmt(v), X(v), f.b + 6);
  }
  ctx.strokeStyle = css("--axis");
  ctx.beginPath(); ctx.moveTo(f.l, f.t); ctx.lineTo(f.l, f.b); ctx.lineTo(f.r, f.b); ctx.stroke();
  ctx.fillStyle = css("--ink2"); ctx.textAlign = "center"; ctx.textBaseline = "bottom";
  ctx.fillText(xlabel, (f.l + f.r) / 2, H - 2);
  ctx.save(); ctx.translate(12, (f.t + f.b) / 2); ctx.rotate(-Math.PI / 2);
  ctx.textBaseline = "middle"; ctx.fillText("SINAD (dB)", 0, 0); ctx.restore();
  // the 12 dB line
  ctx.setLineDash([5, 4]); ctx.strokeStyle = css("--axis"); ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(f.l, Y(TARGET_DB)); ctx.lineTo(f.r, Y(TARGET_DB)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = css("--muted"); ctx.textAlign = "left"; ctx.textBaseline = "bottom";
  ctx.fillText("12 dB", f.l + 4, Y(TARGET_DB) - 2);
  frame = f;
  return {X, Y, f};
}

function drawSweep(W, H) {
  const cur = S.run ? S.run.points : [];
  const all = cur.concat(...S.prevRuns);
  let x0 = -130, x1 = -90;
  if (all.length) {
    x0 = Math.floor(Math.min(...all.map((p) => p.level)) - 1);
    x1 = Math.ceil(Math.max(...all.map((p) => p.level)) + 1);
  }
  const y1 = Math.max(30, Math.ceil(Math.max(0, ...all.map((p) => p.sinad)) / 5) * 5 + 2);
  const ref = S.run ? S.run.ref : setupRef();
  const {X, Y, f} = axes(W, H, x0, x1, 0, y1, REF[ref].axis, (v) => String(v).replace("-", "−"));
  ctx.save(); ctx.beginPath(); ctx.rect(f.l, f.t, f.r - f.l, f.b - f.t); ctx.clip();
  // earlier runs: faint lines
  ctx.strokeStyle = css("--prev"); ctx.lineWidth = 1.5;
  for (const run of S.prevRuns) {
    const pts = [...run].sort((a, b) => a.level - b.level);
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(X(p.level), Y(p.sinad)) : ctx.moveTo(X(p.level), Y(p.sinad))));
    ctx.stroke();
  }
  // this run: coarse hollow, fine filled
  const accent = css("--accent"), surface = css("--surface");
  for (const p of cur) {
    const x = X(p.level), y = Y(p.sinad);
    ctx.beginPath(); ctx.arc(x, y, 4, 0, 2 * Math.PI);
    if (p.phase === "fine") { ctx.fillStyle = accent; ctx.fill(); ctx.strokeStyle = surface; ctx.lineWidth = 1.5; ctx.stroke(); }
    else { ctx.fillStyle = surface; ctx.fill(); ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.stroke(); }
    hitPoints.push({x, y, text: `${p.phase} · ${attText(p.att / 4)}<br>${fmt(p.level, 2)} dBm → ${fmt(p.sinad, 1)} dB SINAD (sd ${fmt(p.sd, 2)})`});
  }
  ctx.restore();
  const mark = S.run && S.run.mark;
  if (mark !== null && mark !== undefined && mark >= x0 && mark <= x1) {
    ctx.strokeStyle = css("--mark"); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(X(mark), f.t); ctx.lineTo(X(mark), f.b); ctx.stroke();
    ctx.fillStyle = css("--ink"); ctx.font = "600 12px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
    const label = `12 dB SINAD: ${fmt(mark, 2)} dBm`;
    const right = X(mark) + 6 + ctx.measureText(label).width < f.r;
    ctx.textAlign = right ? "left" : "right"; ctx.textBaseline = "top";
    ctx.fillText(label, X(mark) + (right ? 6 : -6), f.t + 4);
  }
  if (!all.length) emptyNote(f, running() ? "Waiting for the first reading …" : "Press Start to sweep.");
}

function drawTime(W, H, mode) {
  const now = S.readings.length ? S.readings[S.readings.length - 1].t : Date.now() / 1000;
  const span = windowFor(mode);
  const pts = S.readings.filter((r) => now - r.t <= span && r.sinad !== null && r.sinad !== undefined);
  const y1 = Math.max(30, Math.ceil(Math.max(0, ...pts.map((p) => p.sinad)) / 5) * 5 + 2);
  const {X, Y, f} = axes(W, H, -span, 0, 0, y1, "seconds ago", (v) => String(-v));
  if (mode === "record" && pts.length) return drawRecord(pts, now, X, Y, f);
  if (mode === "ballpark") {
    ctx.fillStyle = css("--band");
    ctx.fillRect(f.l, Y(16), f.r - f.l, Y(9) - Y(16));
    ctx.fillStyle = css("--good"); ctx.textAlign = "right"; ctx.textBaseline = "top";
    ctx.fillText("good range for a sweep", f.r - 4, Y(16) + 2);
  }
  if (pts.length) {
    ctx.strokeStyle = css("--accent"); ctx.lineWidth = 2; ctx.lineJoin = "round";
    ctx.beginPath();
    pts.forEach((p, i) => (i ? ctx.lineTo(X(p.t - now), Y(p.sinad)) : ctx.moveTo(X(p.t - now), Y(p.sinad))));
    ctx.stroke();
    const last = pts[pts.length - 1];
    ctx.beginPath(); ctx.arc(X(0), Y(last.sinad), 4.5, 0, 2 * Math.PI);
    ctx.fillStyle = css("--accent"); ctx.fill(); ctx.strokeStyle = css("--surface"); ctx.lineWidth = 2; ctx.stroke();
    for (const p of pts) hitPoints.push({x: X(p.t - now), y: Y(p.sinad),
      text: `${fmt(p.sinad, 1)} dB SINAD<br>${new Date(p.t * 1000).toLocaleTimeString()}${p.hint ? " · " + esc(p.hint) : ""}`});
  } else {
    emptyNote(f, running() ? "Listening …" : startPrompt());
  }
}

// Record: every 0.25 s window as a faint dot (Morse ID windows hollow), the
// 1 s average as the line.
function drawRecord(pts, now, X, Y, f) {
  ctx.save(); ctx.beginPath(); ctx.rect(f.l, f.t, f.r - f.l, f.b - f.t); ctx.clip();
  const prev = css("--prev"), accent = css("--accent");
  for (const p of pts) {
    ctx.beginPath(); ctx.arc(X(p.t - now), Y(p.sinad), 2, 0, 2 * Math.PI);
    if (p.id) { ctx.strokeStyle = css("--warn"); ctx.lineWidth = 1; ctx.stroke(); }
    else { ctx.fillStyle = prev; ctx.fill(); }
  }
  ctx.strokeStyle = accent; ctx.lineWidth = 2; ctx.lineJoin = "round";
  ctx.beginPath();
  let down = false;
  for (const p of pts) {
    if (p.box === null || p.box === undefined) { down = false; continue; }
    if (down) ctx.lineTo(X(p.t - now), Y(p.box)); else ctx.moveTo(X(p.t - now), Y(p.box));
    down = true;
  }
  ctx.stroke();
  ctx.restore();
  for (const p of pts) hitPoints.push({x: X(p.t - now), y: Y(p.box ?? p.sinad),
    text: `${fmt(p.sinad, 1)} dB (window) · ${fmt(p.box, 1)} dB (1 s)<br>${new Date(p.t * 1000).toLocaleTimeString()} · ${attText(p.attDb)}${p.id ? " · Morse ID" : ""}`});
}

function emptyNote(f, text) {
  ctx.fillStyle = css("--muted"); ctx.font = "13px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(text, (f.l + f.r) / 2, (f.t + f.b) / 2);
}

function renderLegend(mode) {
  const el = $("legend");
  if (mode === "record") {
    el.innerHTML = `<span><i style="background:var(--prev)"></i>each 0.25 s</span>
      <span><i class="line" style="background:var(--accent)"></i>1 s average</span>
      <span><i class="hollow" style="border-color:var(--warn)"></i>Morse ID</span>`;
    return;
  }
  if (mode !== "sweep") {
    const what = mode === "meter" ? "1 s average, every 0.25 s" : "1 s readings";
    el.innerHTML = `<span><i class="line" style="background:var(--accent)"></i>SINAD, ${what}</span>`;
    return;
  }
  let html = `<span><i class="hollow" style="border-color:var(--accent)"></i>coarse</span>
              <span><i style="background:var(--accent)"></i>fine</span>`;
  if (S.prevRuns.length) html += `<span><i class="line" style="background:var(--prev)"></i>earlier runs</span>`;
  if (S.run && S.run.mark !== null && S.run.mark !== undefined) html += `<span><i class="line" style="background:var(--mark)"></i>12 dB point</span>`;
  el.innerHTML = html;
}

canvas.addEventListener("mousemove", (e) => {
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  let best = null, bd = 14 * 14;
  for (const p of hitPoints) {
    const d = (p.x - mx) ** 2 + (p.y - my) ** 2;
    if (d < bd) { bd = d; best = p; }
  }
  const tip = $("tip");
  if (!best) { tip.hidden = true; return; }
  tip.innerHTML = best.text;
  tip.hidden = false;
  const left = Math.min(best.x + 12, r.width - tip.offsetWidth - 4);
  tip.style.left = `${Math.max(4, left)}px`;
  tip.style.top = `${Math.max(4, best.y - tip.offsetHeight - 10)}px`;
});
canvas.addEventListener("mouseleave", () => { $("tip").hidden = true; });
window.addEventListener("resize", () => draw());
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => draw());

// ---- start ------------------------------------------------------------------------
function renderAll() {
  renderHeader(); renderSetup(); renderStatus(); renderManual();
  renderResults(); renderRecords(); renderReadout(); renderLog(); renderSteps(); draw();
}
buildSetup();
if (TABBED) {
  // the tab bar sticks just under the strip, whose height changes with its contents
  const strip = document.querySelector(".strip");
  new ResizeObserver(() => document.documentElement.style.setProperty("--strip-h", `${strip.offsetHeight}px`)).observe(strip);
  selectTab(S.tab in TAB_MODE ? S.tab : "setup");
}
renderAll();
