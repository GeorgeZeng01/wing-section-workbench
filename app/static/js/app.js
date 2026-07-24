/* Wing Section Studio — application wiring. */

import { api, downloadExport } from "./api.js";
import { lineChart, airfoilPreview } from "./charts.js";
import { Viewport, SERIES } from "./viewport.js";

const $ = (id) => document.getElementById(id);
const NU = 1.5e-5;

const CONFIG_DEFAULTS = {
  stack_aoa_deg: 0, ride_height_mm: 30, chord_mm: 350, span_mm: 1400,
  speed_ms: 15, rho: 1.225, nu: NU, ncrit: 7,
  viscous_efficiency: 0.85, efficiency_3d: 1.0, span_efficiency: 0.9,
  n_panels_per_side: 70,
};

function withDefaults(cfg) {
  const merged = { ...CONFIG_DEFAULTS, ...cfg };
  merged.elements = (cfg.elements || []).map(e => ({
    airfoil: "s1223", chord_ratio: 1, deflection_deg: 0, dx: -0.03, dy: -0.03,
    ...e,
  }));
  return merged;
}

/* ---------------- state ---------------- */

const state = {
  config: {
    elements: [
      { airfoil: "s1223", chord_ratio: 1.0, deflection_deg: 0 },
      { airfoil: "s1223", chord_ratio: 0.35, deflection_deg: 12,
        slot_gap_pct: 1.5, slot_overlap_pct: 3.0 },
    ],
    stack_aoa_deg: 0.0, ride_height_mm: 30, chord_mm: 350, span_mm: 1400,
    speed_ms: 15, rho: 1.225, nu: NU, ncrit: 7,
    viscous_efficiency: 0.85, efficiency_3d: 0.9, span_efficiency: 0.9,
    n_panels_per_side: 70,
  },
  target: 250,
  geo: null,
  analysis: null,
  airfoilNames: {},           // spec -> display name
  customDat: {},              // spec -> dat text (for project save)
  optJob: null,
  optTarget: null,            // target snapshotted when the run started
  optPoll: null,
  ransJob: null,
  ransPoll: null,
  ransCEst: null,             // panel estimate snapshotted at RANS start
  polarElem: 0,
  xfoilCache: {},             // spec|re|ncrit -> polar
  screenRows: null,
  screenSort: { key: "CL_max", dir: -1 },
  screenShowLowConf: false,
  rulePresets: [],            // machine-level rule-envelope library
  ransRerank: null,           // last RANS re-rank rows (session-persisted)
  rerankPoll: null,
};

const viewport = new Viewport($("viewport"));
let configRevision = 0;   // bumped on every config edit (stale-result tracking)

/* ---------------- helpers ---------------- */

function toast(msg, kind = "err", ms = 5000) {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}

function busy(btn, on) {
  btn.classList.toggle("busy", on);
  btn.disabled = on;
}

function fmtN(v, d = 1) { return v == null ? "–" : (+v).toFixed(d); }
function fmtRe(re) {
  return re >= 1e6 ? (re / 1e6).toFixed(2) + "M" : Math.round(re / 1000) + "k";
}
function elementRe(i) {
  const c = state.config;
  return c.speed_ms * c.elements[i].chord_ratio * (c.chord_mm / 1000) / c.nu;
}
function roleName(i) { return i === 0 ? "main" : `flap ${i}`; }
function debounce(fn, ms) {
  let t = null;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- config form ---------------- */

const CFG_FIELDS = [
  ["cfg-speed", "speed_ms"], ["cfg-ride", "ride_height_mm"],
  ["cfg-chord", "chord_mm"], ["cfg-span", "span_mm"],
  ["cfg-aoa", "stack_aoa_deg"], ["cfg-ncrit", "ncrit"],
  ["cfg-rho", "rho"], ["cfg-nu", "nu"],
  ["cfg-visc-eff", "viscous_efficiency"], ["cfg-3d-eff", "efficiency_3d"],
  ["cfg-span-eff", "span_efficiency"],
  ["cfg-panels", "n_panels_per_side"],
];

function writeConfigToForm() {
  for (const [id, key] of CFG_FIELDS) $(id).value = state.config[key];
  $("cfg-kg").value = state.config.k_g ?? "";
  $("cfg-target").value = state.target;
  $("opt-target").value = state.target;
  const m = state.config.manufacturing;
  $("mfg-on").checked = !!m;
  $("mfg-body").hidden = !m;
  if (m) {
    $("mfg-te").value = m.te_gap_mm ?? 1.2;
    $("mfg-mode").value = m.te_mode || "thicken";
    $("mfg-tmin").value = m.min_thickness_mm ?? 0;
  }
  const env = state.config.rule_envelope;
  $("rules-on").checked = !!env;
  $("rules-body").hidden = !env;
  if (env) {
    $("rule-len").value = env.max_length_mm ?? "";
    $("rule-height").value = env.max_height_mm ?? "";
    $("rule-clear").value = env.min_ground_clearance_mm ?? "";
    $("rule-xoff").value = env.x_offset_mm ?? 0;
    $("rule-preset").value = env.preset_name ?? "";
  }
  buildElementCards();
  updateTargetC();
}

function bindConfigInputs() {
  for (const [id, key] of CFG_FIELDS) {
    $(id).addEventListener("input", () => {
      const v = parseFloat($(id).value);
      if (Number.isFinite(v)) {
        state.config[key] = v;
        onConfigChanged();
      }
    });
  }
  $("cfg-target").addEventListener("input", () => {
    const v = parseFloat($("cfg-target").value);
    if (Number.isFinite(v)) {
      state.target = v;
      $("opt-target").value = v;
      updateTargetC();
      renderTargetPill();
      persistSession();
    }
  });
  $("opt-target").addEventListener("input", () => {
    const v = parseFloat($("opt-target").value);
    if (Number.isFinite(v)) {
      state.target = v;
      $("cfg-target").value = v;
      updateTargetC();
      renderTargetPill();
      persistSession();
    }
  });
  // not in CFG_FIELDS: clearing the field must remove the key (server then
  // uses its automatic ride-height curve), not write NaN
  $("cfg-kg").addEventListener("input", () => {
    const raw = $("cfg-kg").value.trim();
    if (raw === "") {
      delete state.config.k_g;
      onConfigChanged();
      return;
    }
    const v = parseFloat(raw);
    if (Number.isFinite(v)) {
      state.config.k_g = Math.min(1, Math.max(0, v));
      onConfigChanged();
    }
  });
}

function updateTargetC() {
  const c = state.config;
  const q = 0.5 * c.rho * c.speed_ms ** 2;
  const area = (c.chord_mm / 1000) * (c.span_mm / 1000);
  const denom = q * area * (c.efficiency_3d || 1);
  $("target-c").textContent = denom > 0 ? (state.target / denom).toFixed(2) : "–";
}

/* ---------------- manufacturing ---------------- */

function readMfgForm() {
  return {
    te_gap_mm: parseFloat($("mfg-te").value) || 1.2,
    te_mode: $("mfg-mode").value,
    min_thickness_mm: parseFloat($("mfg-tmin").value) || 0,
  };
}

function bindManufacturing() {
  $("mfg-on").addEventListener("change", () => {
    if ($("mfg-on").checked) {
      state.config.manufacturing = readMfgForm();
    } else {
      delete state.config.manufacturing;
    }
    $("mfg-body").hidden = !$("mfg-on").checked;
    onConfigChanged();
  });
  for (const id of ["mfg-te", "mfg-mode", "mfg-tmin"]) {
    $(id).addEventListener("input", () => {
      if (!$("mfg-on").checked) return;
      state.config.manufacturing = readMfgForm();
      onConfigChanged();
    });
  }
}

/* ---------------- rules envelope ---------------- */

function readRulesForm() {
  const num = (id) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) ? v : null;
  };
  const env = {
    max_length_mm: num("rule-len"),
    max_height_mm: num("rule-height"),
    min_ground_clearance_mm: num("rule-clear"),
    x_offset_mm: num("rule-xoff") ?? 0,
  };
  const preset = $("rule-preset").value;
  if (preset) env.preset_name = preset;
  return env;
}

function bindRules() {
  $("rules-on").addEventListener("change", () => {
    if ($("rules-on").checked) {
      state.config.rule_envelope = readRulesForm();
    } else {
      delete state.config.rule_envelope;
    }
    $("rules-body").hidden = !$("rules-on").checked;
    onConfigChanged();
  });
  for (const id of ["rule-len", "rule-height", "rule-clear", "rule-xoff"]) {
    $(id).addEventListener("input", () => {
      if (!$("rules-on").checked) return;
      $("rule-preset").value = "";   // hand edits leave the preset behind
      state.config.rule_envelope = readRulesForm();
      onConfigChanged();
    });
  }
  $("rule-preset").addEventListener("change", () => {
    const name = $("rule-preset").value;
    const p = state.rulePresets.find((x) => x.name === name);
    if (p) {
      $("rule-len").value = p.envelope.max_length_mm ?? "";
      $("rule-height").value = p.envelope.max_height_mm ?? "";
      $("rule-clear").value = p.envelope.min_ground_clearance_mm ?? "";
      $("rule-xoff").value = p.envelope.x_offset_mm ?? 0;
      $("rule-preset-name").value = name;
    }
    if (!$("rules-on").checked) return;
    state.config.rule_envelope = readRulesForm();
    onConfigChanged();
  });
  $("rule-preset-save").addEventListener("click", saveRulePreset);
  $("rule-preset-del").addEventListener("click", deleteRulePreset);
}

function renderRulePresetOptions() {
  const sel = $("rule-preset");
  const cur = state.config.rule_envelope?.preset_name || "";
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "— none —";
  sel.appendChild(none);
  for (const p of state.rulePresets) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.name;     // textContent: preset names are user data
    sel.appendChild(o);
  }
  sel.value = state.rulePresets.some((p) => p.name === cur) ? cur : "";
}

async function loadRulePresets() {
  try {
    const res = await api.rulePresets();
    state.rulePresets = res.presets || [];
  } catch {
    state.rulePresets = [];    // endpoint unreachable: an empty library
  }
  renderRulePresetOptions();
}

async function saveRulePreset() {
  const name = ($("rule-preset-name").value || "").trim();
  if (!name) { toast("Give the preset a name first."); return; }
  const env = readRulesForm();
  delete env.preset_name;
  if (env.max_length_mm == null && env.max_height_mm == null
      && env.min_ground_clearance_mm == null) {
    toast("Enter at least one rule limit before saving.");
    return;
  }
  const presets = [...state.rulePresets.filter((p) => p.name !== name),
                   { name, envelope: env }]
    .sort((a, b) => a.name.localeCompare(b.name));
  try {
    await api.rulePresetsSave(presets);
    state.rulePresets = presets;
    renderRulePresetOptions();
    $("rule-preset").value = name;
    if ($("rules-on").checked) {
      state.config.rule_envelope = readRulesForm();
      onConfigChanged();
    }
    toast(`Rule preset "${name}" saved.`, "good");
  } catch (e) {
    toast(`Preset not saved: ${e.message}`);
  }
}

async function deleteRulePreset() {
  const name = $("rule-preset").value;
  if (!name) { toast("Select a preset to delete."); return; }
  const presets = state.rulePresets.filter((p) => p.name !== name);
  try {
    await api.rulePresetsSave(presets);
    state.rulePresets = presets;
    renderRulePresetOptions();
    toast(`Rule preset "${name}" deleted.`, "good");
  } catch (e) {
    toast(`Preset not deleted: ${e.message}`);
  }
}

/* ---------------- element cards ---------------- */

function buildElementCards() {
  const host = $("element-cards");
  host.innerHTML = "";
  $("el-count").textContent = state.config.elements.length;
  state.config.elements.forEach((e, i) => host.appendChild(elementCard(e, i)));
  state.polarElem = Math.min(state.polarElem, state.config.elements.length - 1);
  buildViewportLegend();
  buildPolarChips();
  buildScreenerTargets();
}

function elementCard(e, i) {
  const node = $("tpl-element-card").content.firstElementChild.cloneNode(true);
  node.dataset.idx = i;
  node.querySelector(".el-tag").textContent = `E${i + 1}`;
  node.querySelector(".el-title").textContent = roleName(i).toUpperCase();
  updateCardBadges(node, i);

  const afInput = node.querySelector(".af-input");
  afInput.value = state.airfoilNames[e.airfoil] || e.airfoil;
  wireAirfoilCombo(node, afInput, i);

  const flapFields = node.querySelector(".flap-only");
  if (i === 0) {
    flapFields.classList.add("hidden");
  } else {
    const set = (sel, val) => { node.querySelector(sel).value = val; };
    // don't round to whole % — a re-render would misreport a 33.5% chord
    set(".el-chord", +(e.chord_ratio * 100).toFixed(2));
    set(".el-defl", e.deflection_deg);
    // legacy configs carry dx/dy instead; inputs are backfilled with the
    // achieved values right after the first geometry refresh
    if (e.slot_gap_pct != null) set(".el-gap", e.slot_gap_pct);
    if (e.slot_overlap_pct != null) set(".el-ovl", e.slot_overlap_pct);
    const wire = (sel, fn) => {
      node.querySelector(sel).addEventListener("input", (ev) => {
        const v = parseFloat(ev.target.value);
        if (Number.isFinite(v)) { fn(v); onConfigChanged(); }
      });
    };
    wire(".el-chord", v => { state.config.elements[i].chord_ratio = v / 100; });
    wire(".el-defl", v => { state.config.elements[i].deflection_deg = v; });
    wire(".el-gap", v => { state.config.elements[i].slot_gap_pct = v; });
    wire(".el-ovl", v => { state.config.elements[i].slot_overlap_pct = v; });
  }
  return node;
}

function updateCardBadges(card, i) {
  const badges = card.querySelector(".el-badges");
  badges.innerHTML = "";
  const add = (txt, cls = "") => {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = txt;
    badges.appendChild(b);
  };
  add(`Re ${fmtRe(elementRe(i))}`);
  const ge = state.geo?.design?.[i];
  if (ge && ge.slot_gap != null) {
    const gp = ge.slot_gap * 100, op = ge.slot_overlap * 100;
    add(`gap ${gp.toFixed(1)}%`,
        ge.intersects || gp < 0.5 ? "crit" : (gp < 0.8 || gp > 3.5) ? "warn" : "");
    add(`ovl ${op.toFixed(1)}%`, (op < -1 || op > 5) ? "warn" : "");
  }
  if (ge?.mfg) {
    add(`TE ${ge.mfg.te_gap_mm} mm`, ge.mfg.te_ok ? "" : "crit");
    add(`t ${ge.mfg.max_thickness_mm} mm`, ge.mfg.thickness_ok ? "" : "crit");
    if (ge.mfg.min_aft_thickness_mm != null) {
      add(`waist ${ge.mfg.min_aft_thickness_mm} mm`,
          ge.mfg.waist_ok ? "" : "crit");
    }
  }
}

function refreshAllBadges() {
  document.querySelectorAll(".el-card").forEach((card) => {
    updateCardBadges(card, +card.dataset.idx);
  });
}

function wireAirfoilCombo(card, input, idx) {
  const list = card.querySelector(".af-list");
  const fileBtn = card.querySelector(".af-upload");
  const fileInput = card.querySelector(".af-file");

  const search = debounce(async () => {
    if (document.activeElement !== input) { list.hidden = true; return; }
    const q = input.value.trim();
    try {
      const res = await api.airfoils(q, 24);
      if (document.activeElement !== input) { list.hidden = true; return; }
      const items = [
        ...res.custom.filter(c =>
          c.name.toLowerCase().includes(q.toLowerCase()))
          .map(c => ({ spec: c.spec, label: c.name, note: "uploaded" })),
        ...res.library.map(n => ({ spec: n, label: n, note: "" })),
      ];
      list.innerHTML = "";
      if (!items.length) { list.hidden = true; return; }
      for (const it of items.slice(0, 24)) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "af-item";
        // it.label is an airfoil name that can originate from a shared
        // project file — append it as text, never as markup
        b.appendChild(document.createTextNode(it.label));
        if (it.note) {
          const small = document.createElement("small");
          small.textContent = it.note;
          b.appendChild(small);
        }
        b.addEventListener("pointerdown", (ev) => {
          ev.preventDefault();
          selectAirfoil(idx, it.spec, it.label);
          list.hidden = true;
        });
        list.appendChild(b);
      }
      list.hidden = false;
    } catch { list.hidden = true; }
  }, 180);

  input.addEventListener("input", search);
  input.addEventListener("focus", () => { input.select(); search(); });
  input.addEventListener("blur", () => setTimeout(() => {
    list.hidden = true;
    const e = state.config.elements[idx];
    if (!e || !input.isConnected) return;   // card may have been removed
    input.value = state.airfoilNames[e.airfoil] || e.airfoil;
  }, 150));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      const q = input.value.trim();
      if (q) selectAirfoil(idx, q.toLowerCase(), q.toLowerCase());
      list.hidden = true;
      input.blur();
    }
    if (ev.key === "Escape") { list.hidden = true; input.blur(); }
  });

  fileBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", async () => {
    const f = fileInput.files[0];
    if (!f) return;
    try {
      const text = await f.text();
      const up = await api.uploadAirfoil(f.name.replace(/\.(dat|txt)$/i, ""), text);
      state.customDat[up.spec] = text;
      selectAirfoil(idx, up.spec, up.name);
      toast(`Loaded ${up.name} (${up.info.n_points} points, ` +
            `t ${Math.round(up.info.max_thickness * 1000) / 10}%)`, "good");
    } catch (e) {
      toast(`Could not load airfoil: ${e.message}`);
    }
    fileInput.value = "";
  });
}

function selectAirfoil(idx, spec, label) {
  state.config.elements[idx].airfoil = spec;
  state.airfoilNames[spec] = label;
  const card = document.querySelector(`.el-card[data-idx="${idx}"]`);
  if (card) card.querySelector(".af-input").value = label;
  onConfigChanged();
  buildViewportLegend();
  buildPolarChips();
}

$("el-add").addEventListener("click", () => {
  const els = state.config.elements;
  if (els.length >= 4) { toast("Maximum of 4 elements.", "info"); return; }
  const last = els[els.length - 1];
  els.push({
    airfoil: last.airfoil,
    chord_ratio: Math.max(0.15, (last.chord_ratio || 0.35) * 0.7),
    deflection_deg: Math.min(60, (last.deflection_deg || 20) + 18),
    slot_gap_pct: 1.5, slot_overlap_pct: 2.0,
  });
  buildElementCards();
  onConfigChanged();
});

$("el-remove").addEventListener("click", () => {
  const els = state.config.elements;
  if (els.length <= 1) { toast("The main element cannot be removed.", "info"); return; }
  els.pop();
  buildElementCards();
  onConfigChanged();
});

/* ---------------- geometry preview ---------------- */

const scheduleGeometry = debounce(refreshGeometry, 260);

// a custom base may sit inside shape:/mfg: wrappers; the slug is always the
// last (colon-free) segment, so it can be pulled out of any wrapped spec
function usedCustomSpecs() {
  const used = new Set();
  for (const e of state.config.elements) {
    const m = String(e.airfoil || "").match(/custom:[a-z0-9_-]+$/);
    if (m) used.add(m[0]);
  }
  return used;
}

function sessionSnapshot() {
  const custom = {};
  for (const spec of usedCustomSpecs()) {
    if (state.customDat[spec]) {
      custom[spec] = state.customDat[spec];
    }
  }
  return { config: state.config, target: state.target,
           airfoil_names: state.airfoilNames, custom_airfoils: custom,
           rans_rerank: state.ransRerank };
}

// the desktop shell serves on a fresh port (= new origin) every launch, so
// localStorage rarely survives a restart — the server copy is what does.
// Both writers are gated on sessionReady (set once restoreSession finishes):
// an edit landing inside the restore window would otherwise snapshot the
// default template over the user's saved work.
const persistServer = debounce(() => {
  if (!sessionReady) return;
  api.sessionSave(sessionSnapshot()).catch(() => {});
}, 800);

const persistSession = debounce(() => {
  if (!sessionReady) return;
  try {
    localStorage.setItem("wss-session", JSON.stringify(sessionSnapshot()));
  } catch { /* storage full/blocked — the server copy still covers restore */ }
  persistServer();
}, 500);

// debounced writes lose the last edits if the tab closes inside the window.
// sessionReady gates the flush: before restoreSession() has finished, state
// still holds the default template, and a pagehide in that window (close
// the app right after opening it) would overwrite the user's saved work
// with the blank starting state.
let sessionReady = false;
function flushSession() {
  if (!sessionReady) return;
  try {
    const snap = sessionSnapshot();
    localStorage.setItem("wss-session", JSON.stringify(snap));
    navigator.sendBeacon("/api/session",
      new Blob([JSON.stringify({ state: snap })], { type: "application/json" }));
  } catch { /* best effort */ }
}
window.addEventListener("pagehide", flushSession);
window.addEventListener("beforeunload", flushSession);

function onConfigChanged() {
  configRevision++;
  markStale();
  // analysis overlays (CP marker) describe the previous geometry — clear
  // them until the next analyze completes
  if (viewport.xcp != null) viewport.setCp(null);
  updateTargetC();
  scheduleGeometry();
  persistSession();
}

let geoReqSeq = 0;

async function refreshGeometry() {
  const seq = ++geoReqSeq;
  try {
    const geo = await api.geometry(state.config);
    if (seq !== geoReqSeq) return;   // a newer request is in flight/landed
    state.geo = geo;
    viewport.setData(geo, state.config.chord_mm);
    // migrate legacy dx/dy elements to the gap/overlap parameterization by
    // adopting the achieved values OF THE SHARP GEOMETRY (with manufacturing
    // prep on, the server reports both; the as-built numbers depend on the
    // treatment and would bake the warped placement in permanently)
    geo.design.forEach((g, i) => {
      if (i === 0 || g.slot_gap == null) return;
      const e = state.config.elements[i];
      if (!e) return;
      if (e.slot_gap_pct == null) {
        const gv = g.slot_gap_sharp ?? g.slot_gap;
        e.slot_gap_pct = Math.round(gv * 1000) / 10;
        const inp = document.querySelector(`.el-card[data-idx="${i}"] .el-gap`);
        if (inp) inp.value = e.slot_gap_pct;
      }
      if (e.slot_overlap_pct == null) {
        const ov = g.slot_overlap_sharp ?? g.slot_overlap;
        e.slot_overlap_pct = Math.round(ov * 1000) / 10;
        const inp = document.querySelector(`.el-card[data-idx="${i}"] .el-ovl`);
        if (inp) inp.value = e.slot_overlap_pct;
      }
    });
    refreshAllBadges();
    const warn = geo.warnings.length ? ` · ${geo.warnings[0]}` : "";
    $("vp-status").textContent =
      `overall length ${geo.system_chord_mm.toFixed(0)} mm · ` +
      `ride height ${state.config.ride_height_mm} mm ` +
      `(${(geo.ride_height_c * 100).toFixed(1)}% chord)` + warn;
    $("vp-status").style.color = geo.warnings.length ? "var(--warning)" : "";
  } catch (e) {
    if (seq !== geoReqSeq) return;
    $("vp-status").textContent = e.message;
    $("vp-status").style.color = "var(--critical)";
  }
}

function buildViewportLegend() {
  const lg = $("vp-legend");
  lg.innerHTML = "";
  state.config.elements.forEach((e, i) => {
    const s = document.createElement("span");
    s.className = "lg";
    // airfoil names/specs can come from a shared project file — build the
    // node with textContent so a crafted name cannot inject markup (the
    // swatch is the only trusted HTML here)
    const sw = document.createElement("span");
    sw.className = "sw";
    sw.style.background = SERIES[i];
    s.appendChild(sw);
    s.appendChild(document.createTextNode(
      ` E${i + 1} ${state.airfoilNames[e.airfoil] || e.airfoil}`));
    lg.appendChild(s);
  });
}

// the viewport always draws as driven (inverted, ground at y = 0) — the
// orientation the analysis uses; upright output lives in the Export tab
$("vp-dims").addEventListener("change", (e) => viewport.setDims(e.target.checked));
$("vp-rules").addEventListener("change", (e) => {
  viewport.setRules(e.target.checked);
  try {
    localStorage.setItem("wss-show-rules", e.target.checked ? "1" : "0");
  } catch { /* storage blocked — the toggle just resets next launch */ }
});
try {
  if (localStorage.getItem("wss-show-rules") === "0") {
    $("vp-rules").checked = false;
    viewport.setRules(false);
  }
} catch { /* default stays on */ }
$("vp-fit").addEventListener("click", () => viewport.fit());

/* ---------------- analysis + results ---------------- */

function markStale() {
  if (state.analysis) $("results-stale").hidden = false;
}

let analyzeSeq = 0;

async function runAnalysis() {
  const btn = $("btn-analyze");
  const revAtStart = configRevision;
  const seq = ++analyzeSeq;
  busy(btn, true);
  try {
    const res = await api.analyze(state.config);
    if (seq !== analyzeSeq) return;   // a newer analysis owns the panels
    state.analysis = res;
    renderResults(res);
    renderCp(res);
    // only claim freshness if the form wasn't edited mid-flight
    if (configRevision === revAtStart) {
      $("results-stale").hidden = true;
      viewport.setCp(res.coefficients.x_cp_c);
    } else {
      $("results-stale").hidden = false;
    }
  } catch (e) {
    if (seq === analyzeSeq) toast(`Analysis failed: ${e.message}`);
  } finally {
    if (seq === analyzeSeq) busy(btn, false);
  }
}

function renderTargetPill() {
  const res = state.analysis;
  const pill = $("r-target-pill");
  if (!res) return;
  const f = res.forces.downforce_n;
  const d = f - state.target;
  const pct = state.target > 0 ? (d / state.target) * 100 : 0;
  pill.textContent = `target ${state.target} N · ${d >= 0 ? "+" : ""}${d.toFixed(0)} N ` +
                     `(${pct >= 0 ? "+" : ""}${pct.toFixed(0)}%)`;
  pill.className = "pill " + (Math.abs(pct) <= 8 ? "good"
    : Math.abs(pct) <= 25 ? "warn" : "crit");
}

function renderResults(res) {
  $("results-empty").hidden = true;
  $("results-body").hidden = false;
  const c = res.coefficients, f = res.forces;
  $("r-downforce").textContent = fmtN(f.downforce_n, 0);
  renderTargetPill();
  $("r-cest").textContent = fmtN(c.C_downforce_estimated, 2);
  $("r-cinv").textContent = `${fmtN(c.C_downforce_inviscid_ground, 2)} / ` +
                            `${fmtN(c.C_downforce_inviscid_free, 2)}`;
  $("r-gain").textContent = c.ground_gain_inviscid == null ? "–"
    : `+${(c.ground_gain_inviscid * 100).toFixed(0)}% inviscid`;
  $("r-drag").textContent = `${fmtN(f.drag_total_n, 1)} N`;
  $("r-drag-split").textContent =
    `${fmtN(f.drag_induced_n, 1)} induced · ${fmtN(f.drag_profile_n, 1)} profile`;
  $("drag-stat").title = f.induced_model
    ? `CDi ${f.induced_model.CDi} at wing CL ${f.induced_model.CL_wing} · ` +
      `AR ${f.induced_model.AR} · ground factor ` +
      `${f.induced_model.ground_factor_phi}`
    : "";
  $("r-ld").textContent = fmtN(f.efficiency_ld, 1);
  $("r-xcp").textContent =
    `${(c.x_cp_c * state.config.chord_mm).toFixed(0)} mm`;
  $("kg-eff").textContent = c.k_ground_realization == null ? "–"
    : `${fmtN(c.k_ground_realization, 2)} (${c.k_ground_source || "auto"})`;

  const host = $("r-loading");
  host.innerHTML = "";
  res.elements.forEach((e, i) => {
    const frac = e.loading_fraction;
    const gf = e.loading_fraction_ground;
    const row = document.createElement("div");
    row.className = "load-row";
    const scale = 1.3; // track spans 0..130% of CL_max
    const gload = gf == null ? "" :
      ` · <span class="gload${gf > 2.0 ? " warn" : ""}" title="realized ` +
      `ground-effect operating point: Cl ${fmtN(e.Cl_operating, 2)} — ` +
      `${fmtN(gf, 2)}× the isolated CLmax">ground load ${fmtN(gf, 2)}</span>`;
    row.innerHTML =
      `<div class="load-head"><span>E${i + 1} ${e.role} · ` +
      `Cl ${fmtN(e.Cl_checked, 2)} / ${fmtN(e.CL_max_isolated, 2)} · ` +
      `ground ×${fmtN(e.ground_multiplier, 1)}${gload}</span>` +
      `<span>${(frac * 100).toFixed(0)}%</span></div>` +
      `<div class="load-track">` +
      `<div class="load-fill" style="width:${Math.min(frac / scale, 1) * 100}%;` +
      `background:${SERIES[i]}"></div>` +
      `<div class="load-limit" style="left:${(1 / scale) * 100}%"></div></div>`;
    host.appendChild(row);
  });

  const w = $("r-warnings");
  w.innerHTML = "";
  for (const msg of res.warnings) {
    const d = document.createElement("div");
    const crit = /separation|intersect|choke/.test(msg);
    d.className = "warning-item" + (crit ? " crit" : "");
    d.textContent = msg;
    w.appendChild(d);
  }
}

$("btn-analyze").addEventListener("click", runAnalysis);

/* ---------------- pressure tab ---------------- */

function renderCp(res) {
  const host = $("cp-chart");
  $("cp-empty").hidden = true;
  host.hidden = false;
  const mm = state.config.chord_mm;
  lineChart(host, {
    series: res.cp_distributions.map((d, i) => ({
      name: `E${i + 1} ${d.role}`,
      color: SERIES[i],
      x: d.x.map(v => v * mm),
      y: d.cp,
    })),
    xLabel: "x [mm]", yLabel: "Cp (inviscid)", invertY: true, height: 235,
  });
}

/* ---------------- tabs ---------------- */

document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x =>
      x.classList.toggle("active", x === t));
    document.querySelectorAll(".tab-page").forEach(p =>
      p.classList.toggle("active", p.id === `tab-${t.dataset.tab}`));
    if (t.dataset.tab === "polars") renderPolars();
    if (t.dataset.tab === "screener") prefillScreener();
    if (t.dataset.tab === "rans") refreshRansAvailability();
    if (t.dataset.tab === "optimizer") reattachOptimizer();
  });
});
$("btn-goto-optimize").addEventListener("click", () => {
  document.querySelector('.tab[data-tab="optimizer"]').click();
});

/* ---------------- polars tab ---------------- */

function buildPolarChips() {
  const host = $("polar-chips");
  host.innerHTML = "";
  state.config.elements.forEach((e, i) => {
    const b = document.createElement("button");
    b.className = "chip" + (i === state.polarElem ? " active" : "");
    b.textContent = `E${i + 1} ${state.airfoilNames[e.airfoil] || e.airfoil} · ` +
                    `Re ${fmtRe(elementRe(i))}`;
    b.addEventListener("click", () => {
      state.polarElem = Math.min(i, state.config.elements.length - 1);
      buildPolarChips();
      renderPolars();
    });
    host.appendChild(b);
  });
}

let polarReqSeq = 0;

async function renderPolars(withXfoil = false) {
  const seq = ++polarReqSeq;
  const i = Math.min(state.polarElem, state.config.elements.length - 1);
  const e = state.config.elements[i];
  const re = elementRe(i);
  // with manufacturing prep on, show the polar of the as-built section
  const spec = state.geo?.design?.[i]?.airfoil_eff || e.airfoil;
  const asBuilt = spec !== e.airfoil;
  const note = $("polar-note");
  note.textContent = "computing…";
  try {
    const nf = await api.polar({ spec, re, ncrit: state.config.ncrit });
    const key = `${spec}|${Math.round(re)}|${state.config.ncrit}`;
    let xf = state.xfoilCache[key];
    if (withXfoil && !xf) {
      note.textContent = "running XFOIL — up to a minute…";
      xf = await api.polar({ spec, re, ncrit: state.config.ncrit,
                             engine: "xfoil" });
      state.xfoilCache[key] = xf;
    }
    // element chip switched (or config changed) while we were fetching —
    // a newer render owns the charts now
    if (seq !== polarReqSeq) return;
    const series = (xk, yk) => {
      const s = [{ name: "NeuralFoil", color: SERIES[i],
                   x: nf[xk], y: nf[yk] }];
      if (xf) s.push({ name: "XFOIL", color: "#e9edf6", markers: "only",
                       x: xf[xk], y: xf[yk] });
      return s;
    };
    lineChart($("polar-cl"), {
      series: series("alpha", "CL"),
      xLabel: "α [deg]", yLabel: "CL", height: 205,
    });
    lineChart($("polar-drag"), {
      series: series("CD", "CL"),
      xLabel: "CD", yLabel: "CL", height: 205,
    });
    const m = nf.metrics;
    note.textContent = (asBuilt ? "as-built section · " : "") +
      `CLmax ${m.CL_max.toFixed(2)} @ ${m.alpha_CL_max.toFixed(1)}° · ` +
      `L/D max ${m.LD_max.toFixed(0)} @ CL ${m.CL_at_LD_max.toFixed(2)} · ` +
      `confidence ${(m.confidence_at_CL_max * 100).toFixed(0)}%` +
      (xf ? ` · XFOIL: ${xf.alpha.length} converged points` : "");
  } catch (err) {
    if (seq !== polarReqSeq) return;
    note.textContent = `polar failed: ${err.message}`;
  }
}

$("btn-xfoil").addEventListener("click", async () => {
  busy($("btn-xfoil"), true);
  await renderPolars(true);
  busy($("btn-xfoil"), false);
});

/* ---------------- optimizer tab ---------------- */

$("btn-opt-run").addEventListener("click", startOptimization);

// a reloaded page (or second window) re-attaches to a search it can no
// longer see in its own state — the job id is server-side, same pattern
// as the RANS tab
async function reattachOptimizer() {
  if (state.optJob) return;
  try {
    const cur = await api.optimizeCurrent();
    // a run may have been started (Run clicked) while this GET was in flight
    // — do not hijack it by attaching to a different, older job
    if (state.optJob) return;
    if (cur.job_id
        && ["pending", "running", "finalizing"].includes(cur.state)) {
      state.optJob = cur.job_id;
      // keep any target snapshot we still hold (transient-outage re-attach in
      // the same session); after a genuine reload it is already null
      $("btn-opt-run").disabled = true;
      $("btn-opt-cancel").disabled = false;
      pollOptimizer();
    }
  } catch { /* rediscovery is best-effort */ }
}
$("btn-opt-cancel").addEventListener("click", async () => {
  if (state.optJob) { try { await api.optimizeCancel(state.optJob); } catch {} }
});
$("btn-opt-apply").addEventListener("click", applyBestDesign);
$("ov-af").addEventListener("change", () => {
  $("ov-af-pool").disabled = !$("ov-af").checked;
});
$("opt-objective").addEventListener("change", () => {
  const isMax = $("opt-objective").value === "max_downforce";
  $("opt-target").disabled = isMax;
  $("opt-target").title = isMax
    ? "Ignored while maximizing — the loading trust line is the constraint"
    : "";
});

// manufacturing guard: with prep off, the optimizer tunes knife-edge
// trailing edges that change once the wing is made buildable. Ask before
// running; "Optimize anyway" is remembered for the rest of the session.
let mfgGuardAck = false;

function startOptimization() {
  if ($("btn-opt-run").disabled) return;
  if (!state.config.manufacturing && !mfgGuardAck) {
    if (!$("mfg-guard-dialog").open) $("mfg-guard-dialog").showModal();
    return;
  }
  launchOptimization();
}

$("mfg-guard-enable").addEventListener("click", async () => {
  $("mfg-guard-dialog").close();
  // the same defaults the Manufacturing panel starts with
  state.config.manufacturing =
    { te_gap_mm: 1.2, te_mode: "thicken", min_thickness_mm: 0 };
  writeConfigToForm();
  onConfigChanged();
  await launchOptimization();
});
$("mfg-guard-anyway").addEventListener("click", async () => {
  $("mfg-guard-dialog").close();
  mfgGuardAck = true;
  await launchOptimization();
});
$("mfg-guard-cancel").addEventListener("click", () =>
  $("mfg-guard-dialog").close());

async function launchOptimization() {
  // disable BEFORE the request: a double-click on the button must not
  // spawn two concurrent server-side search jobs
  if ($("btn-opt-run").disabled) return;
  $("btn-opt-run").disabled = true;
  const dragW = parseFloat($("opt-drag-w").value);
  // snapshot the target: charts and hints must describe THIS run even if
  // the target field is edited while it searches
  const target = state.target;
  const minConf = parseFloat($("opt-minconf").value);
  const options = {
    target_downforce_n: target,
    objective: $("opt-objective").value,
    min_confidence: Number.isFinite(minConf) ? minConf : 0.5,
    mode: $("opt-mode").value,
    budget: parseInt($("opt-budget").value, 10),
    // 0 is a legal weight ("ignore drag") — don't || it away
    drag_weight: Number.isFinite(dragW) ? dragW : 0.1,
    opt_stack_aoa: $("ov-aoa").checked,
    opt_deflections: $("ov-defl").checked,
    opt_positions: $("ov-pos").checked,
    opt_chords: $("ov-chord").checked,
    opt_airfoils: $("ov-af").checked,
    opt_shape: $("ov-shape").checked,
    airfoil_pool: $("ov-af-pool").value,
  };
  const minLd = parseFloat($("opt-minld").value);
  if (Number.isFinite(minLd)) options.min_ld = minLd;
  if (![options.opt_stack_aoa, options.opt_deflections, options.opt_positions,
        options.opt_chords, options.opt_airfoils, options.opt_shape]
        .some(Boolean)) {
    toast("Enable at least one variable group.", "info");
    $("btn-opt-run").disabled = false;
    return;
  }
  // deflections, positions and chords only exist on flaps; a single element
  // still optimizes fine over stack angle, airfoil choice and shape
  if (state.config.elements.length === 1 && !options.opt_stack_aoa &&
      !options.opt_airfoils && !options.opt_shape) {
    const host = $("opt-hints");
    host.innerHTML = "";
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = "Flap deflections, slot positions and flap chords need " +
      "a flap — add an element, or enable stack angle, airfoil selection " +
      "or airfoil shape.";
    host.appendChild(d);
    $("btn-opt-run").disabled = false;
    return;
  }
  try {
    const { job_id } = await api.optimize(state.config, options);
    state.optJob = job_id;
    state.optTarget = target;
    state.optResult = null;
    $("btn-opt-cancel").disabled = false;
    $("btn-opt-apply").disabled = true;
    pollOptimizer();
  } catch (e) {
    toast(`Could not start optimization: ${e.message}`);
    $("btn-opt-run").disabled = false;
  }
}

function pollOptimizer() {
  clearInterval(state.optPoll);
  let misses = 0;
  state.optPoll = setInterval(async () => {
    try {
      const s = await api.optimizeStatus(state.optJob);
      misses = 0;
      renderOptimizer(s);
      // "finalizing" (best design re-analyzing) still counts as running
      if (["done", "failed", "cancelled"].includes(s.state)) {
        clearInterval(state.optPoll);
        state.optJob = null;   // else reattachOptimizer is dead forever
        $("btn-opt-run").disabled = false;
        $("btn-opt-cancel").disabled = true;
        if (s.state === "failed") toast(`Optimization failed: ${s.error}`);
        if (s.best_config) {
          state.optResult = s;
          $("btn-opt-apply").disabled = false;
          // the shortlist exists now — offer the RANS re-rank
          if (s.candidates && s.candidates.length) {
            $("rerank-title").hidden = false;
            $("rerank-block").hidden = false;
          }
        } else if (s.state === "done") {
          $("opt-best-body").textContent =
            "The run finished without a usable design — nothing to apply. " +
            "Raise the effort or loosen the constraints and retry.";
        }
        renderOptHints(s);
        renderCandidates(s);
      }
    } catch (e) {
      // a single failed poll (network blip, server hiccup) must not orphan
      // a still-running CPU-bound search: ride out transient errors; only
      // a definitive 404 or a persistent outage detaches
      misses++;
      if (e.status !== 404 && misses < 5) return;
      clearInterval(state.optPoll);
      state.optJob = null;   // the promised tab re-attach needs this clear
      $("btn-opt-run").disabled = false;
      $("btn-opt-cancel").disabled = true;
      toast(`Lost the optimization job: ${e.message} — reopen the ` +
            `Optimizer tab to re-attach if it is still running.`);
    }
  }, 700);
}

function renderOptimizer(s) {
  const pct = Math.round((s.progress || 0) * 100);
  $("opt-progress").style.width = pct + "%";
  $("opt-progress").classList.toggle("done", s.state === "done");
  const active = s.state === "running" || s.state === "finalizing";
  const bits = [active ? (s.phase || s.state) : s.state,
                `${pct}%`, `${s.n_eval} evaluations`, `${s.elapsed_s}s`];
  if (s.best) bits.push(`best ${s.best.downforce_n} N`);
  if (active && s.eta_s != null && s.eta_s > 2) {
    bits.push(`~${Math.round(s.eta_s)}s left`);
  }
  $("opt-status").textContent = bits.join(" · ");
  if (s.history && s.history.length) {
    const target = state.optTarget ?? state.target;
    const isMax = s.objective === "max_downforce";
    lineChart($("opt-conv"), {
      series: [{ name: "downforce", color: SERIES[0],
                 x: s.history.map(h => h.eval),
                 y: s.history.map(h => h.downforce_n) }],
      xLabel: "evaluation", yLabel: "downforce [N]",
      // max mode has no set-point; the measured floor/ceiling line (D*)
      // replaces the target line when the run reported one
      targetY: isMax ? null : (s.target_note ? s.dstar_n : target),
      targetLabel: isMax ? null
        : (s.target_note ? `achievable ${s.dstar_n} N`
                         : `target ${target} N`),
      height: 128,
    });
    lineChart($("opt-obj"), {
      series: [{ name: "objective", color: SERIES[2],
                 x: s.history.map(h => h.eval),
                 y: s.history.map(h => h.J) }],
      xLabel: "evaluation", yLabel: "objective (log)", logY: true,
      height: 128,
    });
  }
  renderPareto(s);
  if (s.best && s.variables) {
    const NAMES = { deflection_deg: "deflection", chord_ratio: "chord",
                    slot_gap_pct: "slot gap", slot_overlap_pct: "overlap",
                    dx: "slot dx", dy: "slot dy",
                    shape_b25: "camber @25%", shape_b55: "camber @55%",
                    shape_b80: "camber @80%", shape_ts: "thickness" };
    const afRows = (s.best.airfoils || []).map((a, i) =>
      `<div class="kv"><span>E${i + 1} airfoil</span><b>${a}</b></div>`).join("");
    const rows = afRows + s.variables.map((v, i) => {
      if (v.key === "airfoil_idx") return "";   // shown by name above
      const label = v.elem == null ? "stack angle"
        : `E${v.elem + 1} ${NAMES[v.key] || v.key}`;
      const val = s.best.x[i];
      const unit =
        v.key === "slot_gap_pct" || v.key === "slot_overlap_pct"
          ? `${val.toFixed(1)} %c`
        : v.key === "dx" || v.key === "dy" ? `${(val * 100).toFixed(1)} %c`
        : v.key === "chord_ratio" ? `${(val * 100).toFixed(0)} %`
        : v.key === "shape_ts" ? `×${val.toFixed(2)}`
        : v.key && v.key.startsWith("shape_b")
          ? `${val >= 0 ? "+" : ""}${(val * 100).toFixed(2)} %c`
        : `${val.toFixed(1)}°`;
      return `<div class="kv"><span>${label}</span><b>${unit}</b></div>`;
    }).join("");
    $("opt-best-body").innerHTML =
      `<div class="kv"><span>downforce</span><b>${s.best.downforce_n} N</b></div>` +
      `<div class="kv"><span>drag estimate</span><b>${s.best.drag_n} N</b></div>` +
      rows;
  }
}

function renderPareto(s) {
  const host = $("opt-pareto");
  const note = $("opt-pareto-note");
  const flagged = (p) => {
    const f = p.summary || {};
    return f.low_confidence || f.near_stall || f.slot_signature
      || (f.frac_max ?? 0) > 0.9;
  };
  if (s.pareto && s.pareto.length) {
    // finalized front: full-fidelity numbers, clickable, trust-colored
    const pts = s.pareto.slice().sort((a, b) => a.drag_n - b.drag_n);
    lineChart(host, {
      series: [{
        name: "front", color: SERIES[0], markers: "only",
        x: pts.map(p => p.drag_n), y: pts.map(p => p.downforce_n),
        pointColors: pts.map(p => (flagged(p) ? "#fab219" : SERIES[0])),
        onPointClick: (i) => {
          applyDesign(pts[i].config);
          toast(`Pareto design applied — ${fmtN(pts[i].downforce_n, 0)} N ` +
                `at ${fmtN(pts[i].drag_n)} N drag. Re-analyzing.`, "good");
        },
      }],
      xLabel: "drag [N]", yLabel: "downforce [N]", height: 148,
    });
    note.hidden = false;
  } else if (s.cloud && s.cloud.length
             && (s.state === "running" || s.state === "finalizing")) {
    // live evaluation cloud while the search runs (not clickable — these
    // are search-fidelity numbers)
    lineChart(host, {
      series: [{ name: "evaluations", color: "#5a6a8a", markers: "only",
                 x: s.cloud.map(c => c[1]), y: s.cloud.map(c => c[0]),
                 pointColors: s.cloud.map(c => (c[2] ? "#fab219"
                                                     : "#5a6a8a")) }],
      xLabel: "drag [N]", yLabel: "downforce [N]", height: 148,
    });
    note.hidden = true;
  }
}

/* ---------------- RANS re-rank of the shortlist ---------------- */

function rerankItems() {
  const s = state.optResult;
  if (!s || !s.candidates || !s.candidates.length) return [];
  const items = s.candidates.map((c) => ({
    label: `candidate #${c.rank} — ` +
           `${fmtN(c.summary?.downforce_n ?? c.downforce_n, 0)} N`,
    config: c.config,
    _x: JSON.stringify(c.x),
  }));
  // add the Pareto knee: the front point farthest from the line between
  // the extremes (normalized axes) — the classic best-trade-off pick
  const front = s.pareto || [];
  if (front.length >= 3) {
    const dn = front.map(p => p.downforce_n);
    const dr = front.map(p => p.drag_n);
    const dnS = Math.max(...dn) - Math.min(...dn) || 1;
    const drS = Math.max(...dr) - Math.min(...dr) || 1;
    const a = front[0], b = front[front.length - 1];
    const ax = a.drag_n / drS, ay = a.downforce_n / dnS;
    const bx = b.drag_n / drS, by = b.downforce_n / dnS;
    const len = Math.hypot(bx - ax, by - ay) || 1;
    let knee = null, kd = 0;
    for (const p of front) {
      const px = p.drag_n / drS, py = p.downforce_n / dnS;
      const d = Math.abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / len;
      if (d > kd) { kd = d; knee = p; }
    }
    if (knee && !items.some(it => it._x === JSON.stringify(knee.x))) {
      items.push({ label: `pareto knee — ${fmtN(knee.downforce_n, 0)} N`,
                   config: knee.config, _x: "" });
    }
  }
  return items.slice(0, 8).map(({ label, config }) => ({ label, config }));
}

async function startRerank() {
  const items = rerankItems();
  if (!items.length) {
    toast("Run the optimizer first — the queue verifies its shortlist.");
    return;
  }
  busy($("btn-rerank"), true);
  try {
    await api.ransQueueStart(items, $("rr-mesh").value);
    $("btn-rerank-cancel").disabled = false;
    pollRerank();
  } catch (e) {
    toast(`Could not start the verification queue: ${e.message}`);
    busy($("btn-rerank"), false);
  }
}

function pollRerank() {
  clearInterval(state.rerankPoll);
  state.rerankPoll = setInterval(async () => {
    try {
      const { queue } = await api.ransQueueCurrent();
      if (!queue) return;
      renderRerank(queue);
      if (["done", "failed", "cancelled"].includes(queue.state)) {
        clearInterval(state.rerankPoll);
        busy($("btn-rerank"), false);
        $("btn-rerank-cancel").disabled = true;
        if (queue.state === "failed") {
          toast(`Verification queue failed: ${queue.error}`);
        }
        state.ransRerank = queue.rows;
        persistSession();
      }
    } catch { /* transient poll miss — the next tick retries */ }
  }, 2000);
}

function renderRerank(q) {
  const rows = q.rows || [];
  const status = $("rerank-status");
  if (q.state) {
    const act = (q.active != null && rows[q.active])
      ? ` — solving ${rows[q.active].label}` : "";
    status.textContent = `${q.state}${act} · ${q.mesh_size || ""} mesh · ` +
                         `${Math.round(q.elapsed_s || 0)}s`;
  } else {
    status.textContent = "last verification (restored with the session)";
  }
  const host = $("rerank-table");
  host.innerHTML = "";
  if (!rows.length) return;
  const tbl = document.createElement("table");
  tbl.className = "rr-table";
  const head = tbl.insertRow();
  for (const h of ["#", "design", "panel N", "RANS N", "Δ%", "verdict",
                   "state", ""]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  for (const r of rows) {
    const tr = tbl.insertRow();
    if (!r.converged) tr.className = "dim";
    const cells = [
      r.rank ?? "–", r.label,
      r.panel_downforce_n != null ? fmtN(r.panel_downforce_n, 0) : "–",
      r.rans_downforce_n != null ? fmtN(r.rans_downforce_n, 0) : "–",
      r.delta_cl_pct != null
        ? `${r.delta_cl_pct > 0 ? "+" : ""}${fmtN(r.delta_cl_pct, 1)}` : "–",
      (r.verdict || "–") + (r.mesh_caution ? " · coarse mesh" : ""),
      r.state + (r.error ? ` (${String(r.error).slice(0, 60)})` : ""),
    ];
    for (const c of cells) {
      const td = tr.insertCell();
      td.textContent = String(c);   // labels/errors are data, not markup
    }
    const td = tr.insertCell();
    if (r.config) {
      const b = document.createElement("button");
      b.className = "btn tiny";
      b.textContent = "Apply";
      b.addEventListener("click", () => applyDesign(r.config));
      td.appendChild(b);
    }
    const vd = tr.cells[5];
    if (r.verdict === "over-claims") vd.style.color = "var(--warning)";
    if (r.verdict === "healthy band") vd.style.color = "var(--good)";
  }
  host.appendChild(tbl);
}

$("btn-rerank").addEventListener("click", startRerank);
$("btn-rerank-cancel").addEventListener("click", async () => {
  try { await api.ransQueueCancel(); } catch { /* already gone */ }
});

function renderOptHints(s) {
  const host = $("opt-hints");
  host.innerHTML = "";
  if (s.state === "failed") {
    const d = document.createElement("div");
    d.className = "warning-item crit";
    d.textContent = s.error || "Optimization failed.";
    host.appendChild(d);
    return;
  }
  if (!s.best || !s.variables) return;
  const pinnedLo = [], pinnedHi = [];
  s.variables.forEach((v, i) => {
    const x = s.best.x[i], span = v.hi - v.lo;
    const isLoad = v.key === "stack_aoa_deg" || v.key === "deflection_deg";
    if (!isLoad || span <= 0) return;
    const nm = v.elem == null ? "stack angle" : `E${v.elem + 1} deflection`;
    if (x - v.lo < 0.03 * span) pinnedLo.push(nm);
    else if (v.hi - x < 0.03 * span) pinnedHi.push(nm);
  });
  const target = state.optTarget ?? state.target;
  // same on-target tolerance the optimizer applies to candidates (3%, min 1 N)
  const hit = Math.abs(s.best.downforce_n - target) <= Math.max(0.03 * target, 1);
  let msg = null;
  if (s.target_note === "unreachable_low") {
    // the optimizer measured the clean floor — quote it instead of guessing
    msg = `The lowest clean downforce this stack can make is about ` +
          `${s.dstar_n} N — more than the ${target} N asked. The winner ` +
          `delivers that floor at minimum drag (no slot tricks to fake the ` +
          `number). To genuinely reach ${target} N: drop an element, ` +
          `enable flap chords, shrink the chord, or reduce speed.`;
  } else if (s.target_note === "unreachable_high") {
    msg = `${target} N is beyond this stack at these conditions — the ` +
          `clean ceiling measured about ${s.dstar_n} N, and the winner ` +
          `delivers it at minimum drag. Add an element, enlarge the ` +
          `chord, or lower the target.`;
  } else if (pinnedLo.length && hit) {
    msg = `Target reached, but ${pinnedLo.join(", ")} sat at the minimum — ` +
          `this stack can make far more than ${target} N. For a cleaner ` +
          `design, raise the target, drop an element, or shrink the chord.`;
  } else if (pinnedHi.length && !hit && s.best.downforce_n < target) {
    msg = `${pinnedHi.join(", ")} hit the maximum and the target was still ` +
          `missed — ${target} N is beyond this stack at these conditions. ` +
          `Add an element, enlarge the chord, or lower the target.`;
  }
  if (s.objective === "max_downforce") {
    // target-mode heuristics don't apply; say what the mode guaranteed
    msg = null;
    const w0 = ((s.candidates || [])[0] || {}).summary;
    if (s.state === "done" && w0 && w0.frac_max != null) {
      msg = `Maximum trusted downforce: every element held inside the 90% ` +
            `free-air loading line (winner peaks at ` +
            `${Math.round(w0.frac_max * 100)}%) — the regime where clean ` +
            `designs measured ~14% optimistic against fine-mesh RANS, not ` +
            `the 23–42% over-claim zone past the line.`;
    }
  }
  if (s.load_cap_note === "baseline_exceeds_cap") {
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = "The starting design already loads past the 90% trust " +
      "line. Maximize mode will not follow it there — the winner can sit " +
      "below the start's (over-claimed) number by design.";
    host.appendChild(d);
  }
  if (s.conf_note === "baseline_below_floor") {
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = "The starting design itself sits below the confidence " +
      "floor; the search is only charged for leaning harder on distrusted " +
      "data, but if nothing passes the floor the run will fail and say so.";
    host.appendChild(d);
  }
  if (msg) {
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = msg;
    host.appendChild(d);
  }
  // the winner is what gets applied — if IT carries trust flags, say so
  // here, not only on the candidate card
  const f1 = ((s.candidates || [])[0] || {}).summary;
  if (f1 && (f1.low_confidence || f1.near_stall)) {
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = (f1.low_confidence
      ? "The winning design leans on low-confidence viscous data (NeuralFoil " +
        `confidence ${Math.round((f1.confidence_min ?? 0) * 100)}%)`
      : "The winning design loads sections at the edge of their stall data") +
      " — treat its numbers as optimistic and verify with RANS before building.";
    host.appendChild(d);
  }
}

async function applyDesign(cfgIn) {
  const cfg = structuredClone(cfgIn);
  // shaped sections get a readable display name derived from their base
  for (const e of cfg.elements) {
    const a = e.airfoil || "";
    if (a.startsWith("shape:") && !state.airfoilNames[a]) {
      const base = a.split(":").slice(5).join(":");
      state.airfoilNames[a] = `${state.airfoilNames[base] || base} (shaped)`;
    }
  }
  cfg.stack_aoa_deg = Math.round(cfg.stack_aoa_deg * 100) / 100;
  for (const e of cfg.elements) {
    e.deflection_deg = Math.round(e.deflection_deg * 10) / 10;
    e.chord_ratio = Math.round(e.chord_ratio * 1000) / 1000;
    if (e.slot_gap_pct != null) e.slot_gap_pct = Math.round(e.slot_gap_pct * 10) / 10;
    if (e.slot_overlap_pct != null) e.slot_overlap_pct = Math.round(e.slot_overlap_pct * 10) / 10;
    if (e.dx != null) e.dx = Math.round(e.dx * 1000) / 1000;
    if (e.dy != null) e.dy = Math.round(e.dy * 1000) / 1000;
  }
  // cfg is a snapshot of the configuration from when the run STARTED —
  // adopt only the optimizer-owned fields so edits made since (speed, ride
  // height, manufacturing, …) survive the apply
  if (cfg.elements.length === state.config.elements.length) {
    state.config.stack_aoa_deg = cfg.stack_aoa_deg;
    cfg.elements.forEach((e, i) => {
      const cur = state.config.elements[i];
      cur.airfoil = e.airfoil;
      cur.deflection_deg = e.deflection_deg;
      cur.chord_ratio = e.chord_ratio;
      if (e.slot_gap_pct != null) cur.slot_gap_pct = e.slot_gap_pct;
      if (e.slot_overlap_pct != null) cur.slot_overlap_pct = e.slot_overlap_pct;
    });
  } else {
    state.config = cfg;
    toast("Element count changed since the run started — the whole " +
          "configuration was restored from the optimizer run.", "info", 7000);
  }
  writeConfigToForm();
  persistSession();
  await refreshGeometry();
  runAnalysis();
}

async function applyBestDesign() {
  const s = state.optResult;
  if (!s || !s.best_config) return;
  await applyDesign(s.best_config);
  toast("Optimized design applied.", "good");
}

function renderCandidates(s) {
  const host = $("opt-candidates");
  host.innerHTML = "";
  const cands = s.candidates || [];
  $("opt-cand-title").hidden = cands.length === 0;
  cands.forEach((c) => {
    const card = document.createElement("div");
    card.className = "cand-card";
    const f = c.summary || {};
    const dn = f.downforce_n ?? c.downforce_n;
    const drag = f.drag_total_n ?? c.drag_n;
    const ld = f.efficiency_ld;
    const badges =
      // "off target" is meaningless while maximizing — there is no target
      (c.on_target || s.objective === "max_downforce"
        ? "" : `<span class="badge warn">off target</span>`) +
      (f.slot_signature ? `<span class="badge warn" title="Slot at the ` +
        `workable floor with no overlap tuck — the corner the inviscid ` +
        `model over-rates; no recorded RANS point supports it. See the ` +
        `analysis warning.">slot corner</span>` : "") +
      (f.low_confidence ? `<span class="badge warn" title="NeuralFoil ` +
        `confidence is below 50% on at least one section — the viscous ` +
        `data behind this design is an extrapolation, not a prediction. ` +
        `Verify with RANS before trusting it.">low confidence</span>` : "") +
      (f.near_stall ? `<span class="badge warn" title="A loaded element ` +
        `runs at the edge of its viscous data: its drag lookup is capped ` +
        `at the pre-stall polar (drag understated), or its polar never ` +
        `stalled in the analyzed range (CL_max is a lower bound, not a ` +
        `stall). Verify with RANS.">near stall</span>` : "") +
      (f.warnings ? `<span class="badge warn">${f.warnings} warning` +
                    `${f.warnings > 1 ? "s" : ""}</span>` : "");
    const head = document.createElement("div");
    head.className = "cand-head";
    head.innerHTML =
      `<span><b>#${c.rank}</b> ${fmtN(dn, 0)} N · drag ${fmtN(drag, 1)} N` +
      (ld != null ? ` · L/D ${fmtN(ld, 1)}` : "") +
      (f.confidence_min != null
        ? ` · conf ${Math.round(f.confidence_min * 100)}%` : "") +
      `</span><span>${badges}</span>`;
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "cand-body";
    const bits = [];
    (c.airfoils || []).forEach((a, i) =>
      bits.push(`E${i + 1} ${state.airfoilNames[a] || a}`));
    const shapeByElem = {};
    (s.variables || []).forEach((v, i) => {
      if (v.key === "airfoil_idx") return;
      const val = c.x[i];
      if (v.key && v.key.startsWith("shape_")) {
        (shapeByElem[v.elem] = shapeByElem[v.elem] || {})[v.key] = +val;
        return;
      }
      if (v.key === "stack_aoa_deg") bits.push(`aoa ${(+val).toFixed(1)}°`);
      else if (v.key === "deflection_deg")
        bits.push(`E${v.elem + 1} defl ${(+val).toFixed(1)}°`);
      else if (v.key === "chord_ratio")
        bits.push(`E${v.elem + 1} chord ${(val * 100).toFixed(0)}%`);
      else if (v.key === "slot_gap_pct")
        bits.push(`E${v.elem + 1} gap ${(+val).toFixed(1)}%`);
      else if (v.key === "slot_overlap_pct")
        bits.push(`E${v.elem + 1} ovl ${(+val).toFixed(1)}%`);
    });
    for (const [e, p] of Object.entries(shapeByElem)) {
      const b = ["shape_b25", "shape_b55", "shape_b80"].map(k => {
        const v = (p[k] || 0) * 100;
        return `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
      });
      bits.push(`E${+e + 1} Δcam ${b.join("/")}%c t×${(p.shape_ts ?? 1).toFixed(2)}`);
    }
    body.textContent = bits.join(" · ");
    card.appendChild(body);

    const btn = document.createElement("button");
    btn.className = "btn small";
    btn.textContent = "Apply";
    btn.addEventListener("click", async () => {
      busy(btn, true);
      await applyDesign(c.config);
      busy(btn, false);
      toast(`Candidate #${c.rank} applied.`, "good");
    });
    card.appendChild(btn);
    host.appendChild(card);
  });
}

/* ---------------- screener tab ---------------- */

function buildScreenerTargets() {
  const sel = $("scr-elem");
  const prev = sel.value;   // rebuilding must not silently reset to E1
  sel.innerHTML = "";
  state.config.elements.forEach((e, i) => {
    const o = document.createElement("option");
    o.value = i;
    o.textContent = `E${i + 1} ${roleName(i)}`;
    sel.appendChild(o);
  });
  if (prev !== "" && +prev < state.config.elements.length) sel.value = prev;
}

// prefill must not clobber hand-edited values on every tab visit; switching
// the target element is an explicit request to prefill again
const screenerDirty = { re: false, tmin: false };
$("scr-re").addEventListener("input", () => { screenerDirty.re = true; });
$("scr-tmin").addEventListener("input", () => { screenerDirty.tmin = true; });

function prefillScreener() {
  const i = parseInt($("scr-elem").value || "0", 10);
  if (!screenerDirty.re) $("scr-re").value = Math.round(elementRe(i));
  // manufacturing constraints -> %c floor at this element's chord: both the
  // explicit buildable minimum and the TE thickness (a section thinner than
  // its own TE is a plate, not an airfoil)
  const m = state.config.manufacturing;
  if (m && !screenerDirty.tmin) {
    const floorMm = Math.max(m.min_thickness_mm || 0,
                             1.5 * (m.te_gap_mm || 0));
    if (floorMm > 0) {
      const cMm = (i === 0 ? 1 : state.config.elements[i].chord_ratio)
                  * state.config.chord_mm;
      const floor = Math.ceil((floorMm / cMm) * 1000) / 10;
      $("scr-tmin").value = Math.max(parseFloat($("scr-tmin").value) || 0, floor);
    }
  }
}
$("scr-elem").addEventListener("change", () => {
  screenerDirty.re = screenerDirty.tmin = false;
  prefillScreener();
});

$("btn-screen").addEventListener("click", async () => {
  const btn = $("btn-screen");
  // a cleared field must not send NaN (serialized as null -> 422 with a
  // pydantic error blob in the toast): fall back to sensible values
  let re = parseFloat($("scr-re").value);
  if (!Number.isFinite(re)) {
    re = Math.round(elementRe(parseInt($("scr-elem").value || "0", 10)));
  }
  // the server requires Re > 1000 — clamp even the element fallback (a
  // tiny/slow test section can sit below it)
  re = Math.max(re, 1001);
  $("scr-re").value = re;
  let clRef = parseFloat($("scr-cl").value);
  if (!Number.isFinite(clRef)) { clRef = 1.5; $("scr-cl").value = clRef; }
  busy(btn, true);
  $("scr-note").textContent = "screening the library…";
  try {
    // always fetch low-confidence rows too — the 50% cut is applied
    // client-side so hidden sections (often uploads) stay discoverable
    const res = await api.screen({
      re,
      ncrit: state.config.ncrit,
      cl_ref: clRef,
      thickness_pct_min: parseFloat($("scr-tmin").value) || 0,
      thickness_pct_max: parseFloat($("scr-tmax").value) || 25,
      include_low_confidence: true,
    });
    state.screenRows = res.rows;
    $("scr-note").textContent =
      `${res.count} sections passed the filters — click a column to sort.`;
    renderScreenTable();
  } catch (e) {
    $("scr-note").textContent = "";
    toast(`Screening failed: ${e.message}`);
  } finally {
    busy(btn, false);
  }
});

const SCR_COLS = [
  ["spec", "airfoil"], ["CL_max", "CLmax"], ["alpha_CL_max", "α@CLmax"],
  ["LD_max", "L/D max"], ["CL_at_LD_max", "CL@L/D"],
  ["CD_at_CL_ref", "CD@CLref"], ["thickness_pct", "t %"],
  ["camber_pct", "camber %"], ["confidence", "conf"],
];

function renderScreenTable() {
  const all = state.screenRows || [];
  const rows = state.screenShowLowConf ? [...all]
    : all.filter(r => (r.confidence ?? 1) >= 0.5);
  const nLow = all.length
    - all.filter(r => (r.confidence ?? 1) >= 0.5).length;
  const { key, dir } = state.screenSort;
  rows.sort((a, b) => {
    const av = a[key], bv = b[key];
    if (av == null) return 1;
    if (bv == null) return -1;
    return (av < bv ? -1 : av > bv ? 1 : 0) * dir;
  });
  const top = rows.slice(0, 60);
  const host = $("scr-table");
  const th = SCR_COLS.map(([k, label]) =>
    `<th data-k="${k}" class="${k === key ? "sorted" : ""}">${label}</th>`).join("");
  const trs = top.map(r => {
    const tds = SCR_COLS.map(([k]) => {
      let v = r[k];
      if (v == null) v = "–";
      if (k === "CL_max" && r.CL_max_lower_bound) {
        return `<td title="polar had not stalled by the last analyzed ` +
               `angle — CL_max is a lower bound">≥ ${v}</td>`;
      }
      return `<td>${v}</td>`;
    }).join("");
    return `<tr>${tds}<td><button class="btn ghost" data-use="${r.spec}">Use</button></td></tr>`;
  }).join("");
  host.innerHTML =
    `<table class="data-table"><thead><tr>${th}<th></th></tr></thead>` +
    `<tbody>${trs}</tbody></table>`;
  if (nLow > 0) {
    const note = document.createElement("div");
    note.className = "note conf-note";
    note.textContent = state.screenShowLowConf
      ? `showing all ${all.length}, including ${nLow} with NeuralFoil ` +
        `confidence below 50% — `
      : `showing ${rows.length} of ${all.length} — ${nLow} hidden ` +
        `(low NeuralFoil confidence): `;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "link-btn";
    b.textContent = state.screenShowLowConf ? "hide" : "show";
    b.addEventListener("click", () => {
      state.screenShowLowConf = !state.screenShowLowConf;
      renderScreenTable();
    });
    note.appendChild(b);
    host.appendChild(note);
  }
  host.querySelectorAll("th[data-k]").forEach((h) => {
    h.addEventListener("click", () => {
      const k = h.dataset.k;
      state.screenSort = {
        key: k,
        dir: state.screenSort.key === k ? -state.screenSort.dir
          : (k === "spec" || k === "CD_at_CL_ref" ? 1 : -1),
      };
      renderScreenTable();
    });
  });
  host.querySelectorAll("button[data-use]").forEach((b) => {
    b.addEventListener("click", () => {
      const idx = parseInt($("scr-elem").value || "0", 10);
      selectAirfoil(idx, b.dataset.use, b.dataset.use);
      toast(`E${idx + 1} set to ${b.dataset.use}.`, "good");
    });
  });
}

/* ---------------- maps tab ---------------- */

const MAP_DEFAULTS = {
  ride_height_mm: { from: 10, to: 120, steps: 23 },
  speed_ms: { from: 5, to: 30, steps: 26 },
};
const MAP_LABELS = { ride_height_mm: "ride height [mm]", speed_ms: "speed [m/s]" };
const MAP_UNITS = { ride_height_mm: "mm", speed_ms: "m/s" };

$("map-var").addEventListener("change", () => {
  const d = MAP_DEFAULTS[$("map-var").value];
  $("map-from").value = d.from;
  $("map-to").value = d.to;
  $("map-steps").value = d.steps;
});

$("btn-sweep").addEventListener("click", runSweep);

async function runSweep() {
  const btn = $("btn-sweep");
  const variable = $("map-var").value;
  const from = parseFloat($("map-from").value);
  const to = parseFloat($("map-to").value);
  const steps = Math.round(parseFloat($("map-steps").value));
  if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) {
    toast("Give the sweep two distinct from/to values.", "info");
    return;
  }
  if (!Number.isFinite(steps) || steps < 2 || steps > 40) {
    toast("Steps must be between 2 and 40.", "info");
    return;
  }
  const values = Array.from({ length: steps }, (_, i) =>
    +(from + ((to - from) * i) / (steps - 1)).toFixed(4));
  busy(btn, true);
  $("map-note").textContent = "sweeping…";
  try {
    const res = await api.sweep(state.config, variable, values);
    renderSweep(res);
  } catch (e) {
    $("map-note").textContent = "";
    toast(`Sweep failed: ${e.message}`);
  } finally {
    busy(btn, false);
  }
}

function renderSweep(res) {
  const variable = res.variable;
  const xLabel = MAP_LABELS[variable] || variable;
  const unit = MAP_UNITS[variable] || "";
  const pts = res.points.filter(p => !p.error && Number.isFinite(p.downforce_n));
  const skipped = res.points.length - pts.length;
  $("map-allowance").hidden = true;
  if (!pts.length) {
    $("map-note").textContent =
      "no valid sweep points — every value failed validation";
    return;
  }
  const xs = pts.map(p => p.value);
  const dn = pts.map(p => p.downforce_n);
  let ipk = 0;
  dn.forEach((v, i) => { if (v > dn[ipk]) ipk = i; });

  // current operating point, interpolated onto the sweep
  const series = [
    { name: "downforce", color: SERIES[0], x: xs, y: dn, markers: true },
    { name: "peak", color: "#fab219", markers: "only",
      x: [xs[ipk]], y: [dn[ipk]] },
  ];
  const cur = state.config[variable];
  for (let i = 0; i + 1 < xs.length; i++) {
    if ((cur - xs[i]) * (cur - xs[i + 1]) <= 0 && xs[i] !== xs[i + 1]) {
      const t = (cur - xs[i]) / (xs[i + 1] - xs[i]);
      series.push({ name: "current", color: "#e9edf6", markers: "only",
                    x: [cur], y: [dn[i] + t * (dn[i + 1] - dn[i])] });
      break;
    }
  }
  // measured model-validity bands (RANS cross-referencing campaign,
  // docs/calibration): h/c thresholds from the server, converted with this
  // config's chord. Ride-height sweeps only — they don't apply to speed.
  let xBands;
  if (variable === "ride_height_mm" && res.trust_bands) {
    const c_mm = state.config.chord_mm;
    const tb = res.trust_bands;
    xBands = [
      { from: 0, to: tb.optimistic_below_hc * c_mm, color: "#e0645c",
        label: "estimate optimistic" },
      { from: tb.conservative_hc[0] * c_mm, to: tb.conservative_hc[1] * c_mm,
        color: "#57b6f0", label: "estimate conservative" },
    ];
  }
  lineChart($("map-downforce"), {
    series, xLabel, yLabel: "downforce [N]", height: 205, xBands,
  });
  lineChart($("map-ld"), {
    series: [{ name: "L/D", color: SERIES[2], x: xs,
               y: pts.map(p => p.efficiency_ld), markers: true }],
    xLabel, yLabel: "L/D estimate", height: 205, xBands,
  });

  // points past the ground-loading allowance carry an optimistic estimate
  const hot = pts.filter(p => p.loading_fraction_ground_max > 2);
  if (hot.length) {
    const hx = hot.map(p => p.value);
    $("map-allowance").textContent =
      `${hot.length} of ${pts.length} points (${Math.min(...hx)}–` +
      `${Math.max(...hx)} ${unit}) push element loading past 2× the ` +
      `isolated stall limit — the model is optimistic there; treat those ` +
      `downforce values as upper bounds.`;
    $("map-allowance").hidden = false;
  }
  $("map-note").textContent =
    `peak ${fmtN(dn[ipk], 0)} N at ${fmtN(xs[ipk], 1)} ${unit}` +
    (skipped ? ` · ${skipped} point${skipped > 1 ? "s" : ""} skipped` : "") +
    ` · ${res.n_panels_per_side_used} panels/side` +
    (xBands ? " · shaded bands: where RANS cross-referencing measured the " +
              "estimate optimistic (red) or conservative (blue) for designs " +
              "inside the loading budget — loading warnings trump the bands" : "");
}

/* ---------------- RANS verify tab ---------------- */

let ransChecked = false;

async function refreshRansAvailability() {
  // a job the page does not know about (reload, second window, dropped
  // poll) is re-attached first — its id is server-side state. A finished
  // one renders its result; a live one resumes polling.
  if (!state.ransJob && !$("rans-result").querySelector(".kv")) {
    try {
      const cur = await api.ransCurrent();
      if (cur.job_id && ["pending", "running"].includes(cur.state)) {
        state.ransJob = cur.job_id;
        $("btn-rans-run").disabled = true;
        $("btn-rans-cancel").disabled = false;
        pollRans();
      } else if (cur.job_id && cur.state === "done") {
        const s = await api.ransStatus(cur.job_id);
        renderRans(s);
        renderRansResult(s);
        showRansFlow(s.id);
      }
    } catch { /* rediscovery is best-effort */ }
  }
  // the good-news probe latches (the server caches it anyway); the
  // unavailable state must NOT latch — the note tells the user to start
  // Docker Desktop and reopen the tab, and reopening has to re-check
  if (ransChecked) return;
  const note = $("rans-note");
  try {
    const a = await api.ransAvailability();
    if (a.available) {
      ransChecked = true;
      note.textContent = a.image_present
        ? `Docker ${a.docker} ready · ${a.image}`
        : `Docker ${a.docker} ready — the OpenFOAM image downloads on the ` +
          `first run (~1 GB, one time)`;
      if (!state.ransJob) $("btn-rans-run").disabled = false;
    } else {
      note.textContent = `Docker unavailable (${a.detail || "not running"}) — ` +
        `start Docker Desktop and reopen this tab, or generate the case in ` +
        `the Export tab and run it in WSL.`;
      $("btn-rans-run").disabled = true;
    }
  } catch (e) {
    note.textContent = `Could not check Docker: ${e.message}`;
  }
}

$("btn-rans-run").addEventListener("click", startRansVerify);
$("btn-rans-cancel").addEventListener("click", async () => {
  if (state.ransJob) { try { await api.ransCancel(state.ransJob); } catch {} }
});
$("btn-rans-stop").addEventListener("click", async () => {
  if (!state.ransJob) return;
  try {
    await api.ransStop(state.ransJob);
    $("btn-rans-stop").disabled = true;   // one request is enough
  } catch (e) {
    toast(`Could not stop gracefully: ${e.message}`);
  }
});

async function startRansVerify() {
  const iters = parseInt($("rans-iters").value, 10);
  try {
    const { job_id } = await api.ransStart(
      state.config, $("rans-mesh").value,
      Number.isFinite(iters) ? Math.min(Math.max(iters, 100), 20000) : 10000);
    state.ransJob = job_id;
    // the target line must describe the config THIS run solves, not
    // whatever the form says later — snapshot the estimate at start
    state.ransCEst = state.analysis?.coefficients?.C_downforce_estimated
      ?? null;
    $("btn-rans-run").disabled = true;
    $("btn-rans-cancel").disabled = false;
    $("rans-conv").innerHTML = "";   // previous run's chart is not this run
    $("rans-flow").hidden = true;
    $("rans-result").innerHTML =
      '<div class="empty-note">Verification running…</div>';
    pollRans();
  } catch (e) {
    toast(`Could not start the RANS run: ${e.message}`);
  }
}

function pollRans() {
  clearInterval(state.ransPoll);
  let misses = 0;
  state.ransPoll = setInterval(async () => {
    try {
      const s = await api.ransStatus(state.ransJob);
      misses = 0;
      renderRans(s);
      // graceful stop is meaningful only while the solver iterates and no
      // writeNow is already pending (progress pins at 0.97 once one is)
      $("btn-rans-stop").disabled = !(state.ransJob && s.state === "running"
                                      && s.iteration > 0
                                      && s.progress < 0.97);
      if (["done", "failed", "cancelled"].includes(s.state)) {
        clearInterval(state.ransPoll);
        state.ransJob = null;
        $("btn-rans-run").disabled = false;
        $("btn-rans-cancel").disabled = true;
        $("btn-rans-stop").disabled = true;
        if (s.state === "failed") {
          toast("RANS verification failed — details in the RANS tab.", "err");
          $("rans-result").innerHTML = "";
          const d = document.createElement("div");
          d.className = "warning-item crit";
          d.style.whiteSpace = "pre-wrap";
          d.textContent = s.error || "run failed";
          $("rans-result").appendChild(d);
        }
        if (s.state === "cancelled") {
          $("rans-result").innerHTML =
            '<div class="empty-note">Run cancelled — the partial case was ' +
            'kept for inspection.</div>';
        }
        if (s.state === "done") {
          renderRansResult(s);
          showRansFlow(s.id);
        }
      }
    } catch (e) {
      // a lost poll must not orphan the run: the job keeps solving
      // server-side, so ride out transient failures; only a definitive
      // "job unknown" (404) or a persistent outage detaches — and the tab
      // re-attaches through /api/rans/current either way
      misses++;
      if (e.status !== 404 && misses < 5) return;
      clearInterval(state.ransPoll);
      state.ransJob = null;
      $("btn-rans-run").disabled = false;
      $("btn-rans-cancel").disabled = true;
      $("btn-rans-stop").disabled = true;
      toast(`Lost the RANS job: ${e.message} — reopen this tab to ` +
            `re-attach if it is still running.`);
    }
  }, 1000);
}

function renderRans(s) {
  const pct = Math.round((s.progress || 0) * 100);
  $("rans-progress").style.width = pct + "%";
  $("rans-progress").classList.toggle("done", s.state === "done");
  const bits = [s.state === "running" ? (s.phase || "running") : s.state];
  if (s.mesh) bits.push(`${s.mesh.n_cells.toLocaleString()} cells`);
  if (s.iteration) bits.push(`iteration ${s.iteration} / ${s.n_iters}`);
  if (s.latest) bits.push(`Cl ${s.latest.cl.toFixed(3)}`,
                          `Cd ${s.latest.cd.toFixed(4)}`);
  bits.push(`${Math.round(s.elapsed_s)}s`);
  $("rans-status").textContent = bits.join(" · ");
  if (s.history && s.history.length > 1) {
    const cEst = s.result?.panel?.c_est ?? state.ransCEst;
    lineChart($("rans-conv"), {
      series: [{ name: "Cl (RANS)", color: SERIES[0],
                 x: s.history.map(h => h.iter),
                 y: s.history.map(h => h.cl) }],
      xLabel: "iteration", yLabel: "Cl (downforce +)",
      targetY: cEst ?? undefined,
      targetLabel: cEst != null ? `panel C_est ${(+cEst).toFixed(2)}` : undefined,
      height: 180,
    });
  }
}

function renderRansResult(s) {
  const r = s.result;
  if (!r) return;
  const host = $("rans-result");
  const p = r.panel;
  const pct = (v) => v == null ? "–"
    : `${v > 0 ? "+" : ""}${(+v).toFixed(1)}%`;
  const rows = [
    ["Sectional Cl — RANS", `${r.cl_rans.toFixed(3)} ± ${r.cl_rans_std.toFixed(3)}`],
    ["Sectional Cl — panel C_est", p ? (+p.c_est).toFixed(3) : "–"],
    ["Cl delta (RANS vs estimate)", pct(r.delta_cl_pct)],
    ["Profile Cd — RANS", r.cd_rans.toFixed(4)],
    ["Profile Cd — panel stack", p ? (+p.cd_profile).toFixed(4) : "–"],
    ["Downforce at RANS Cl", `${r.downforce_n_at_rans_cl} N`],
    ["Downforce — panel estimate", p ? `${p.downforce_n} N` : "–"],
    ["Iterations", `${r.n_iters_run} (${r.stop_reason}; tail mean of ${r.tail_rows})`],
  ];
  host.innerHTML = rows.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");
  if (!r.converged) {
    const w = document.createElement("div");
    w.className = "warning-item crit";
    w.textContent = `NOT CONVERGED — the lift history was still trending ` +
      `when the iteration cap ended the run` +
      (r.cl_drift != null ? ` (Cl drift ${(r.cl_drift * 100).toFixed(1)}% ` +
        `per window)` : ``) +
      `. The numbers above are a mid-transient snapshot, not a result: ` +
      `raise Max iterations and rerun. No k_g calibration is offered from ` +
      `an unconverged run.`;
    host.appendChild(w);
  }
  if (!p && r.panel_error) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = `The panel-model comparison could not be computed ` +
      `(${r.panel_error}) — the RANS numbers above stand on their own.`;
    host.appendChild(w);
  }
  if (r.mesh_caution) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = "Coarse-mesh caution: calibration testing measured the " +
      "coarse mesh reading validated operating points 22–35% below " +
      "fine-mesh truth at racing and mid ride heights (it under-resolves " +
      "the venturi gap). Treat this run as screening; re-run on medium or " +
      "fine before trusting the delta or pinning k_g from it.";
    host.appendChild(w);
  }
  if (r.suggested_k_g != null) {
    const d = document.createElement("div");
    d.className = "kv";
    d.innerHTML = `<span title="Pinning the ground-gain factor to this value
      makes the studio estimate reproduce the RANS sectional load at this
      operating point.">suggested k<sub>g</sub></span>
      <b>${r.suggested_k_g}
      <button id="btn-rans-apply-kg" class="btn tiny">Apply</button></b>`;
    host.appendChild(d);
    $("btn-rans-apply-kg").addEventListener("click", () => {
      state.config.k_g = r.suggested_k_g;
      writeConfigToForm();
      onConfigChanged();
      toast(`k_g pinned to ${r.suggested_k_g} — re-analyze to see the ` +
            `calibrated estimate.`, "good", 6000);
    });
  }
  const note = document.createElement("p");
  note.className = "note";
  note.style.marginTop = "6px";
  note.innerHTML = `2D section truth check: RANS Cd is profile drag only —
    induced drag is a 3D effect and is compared in the studio's totals, not
    here. Case retained at <code>${r.case_dir}</code> (fields, logs,
    ParaView-openable <code>case.foam</code>).`;
  host.appendChild(note);
}

/* flow-field view: the solved section rendered server-side from the final
   OpenFOAM fields, in the same as-driven orientation as the drawing */
let ransFlowId = null;
let ransFlowUrl = null;   // objectURL of the currently shown image
let ransFlowSeq = 0;

async function showRansFlow(jobId, field = "umag") {
  ransFlowId = jobId;
  const seq = ++ransFlowSeq;
  $("rans-flow").hidden = false;
  $("rans-flow-umag").classList.toggle("active", field === "umag");
  $("rans-flow-cp").classList.toggle("active", field === "cp");
  const img = $("rans-flow-img");
  const note = $("rans-flow-note");
  note.textContent = "rendering the flow field…";
  img.style.opacity = "0.4";
  // fetched (not img.src) so a failure can show the server's actual reason
  try {
    const res = await fetch(`/api/rans/${jobId}/flow?field=${field}`);
    if (seq !== ransFlowSeq) return;   // a newer request owns the panel
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      note.textContent = `flow field unavailable: ${detail}`;
      img.style.opacity = "";
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    if (ransFlowUrl) URL.revokeObjectURL(ransFlowUrl);
    ransFlowUrl = url;
    img.src = url;
    img.style.opacity = "";
    note.textContent = "same view as the drawing: as driven, ground at " +
      "the bottom, flow left to right";
  } catch (e) {
    if (seq === ransFlowSeq) {
      note.textContent = `flow field unavailable: ${e.message}`;
      img.style.opacity = "";
    }
  }
}

$("rans-flow-umag").addEventListener("click", () =>
  ransFlowId && showRansFlow(ransFlowId, "umag"));
$("rans-flow-cp").addEventListener("click", () =>
  ransFlowId && showRansFlow(ransFlowId, "cp"));

/* ---------------- export tab ---------------- */

let lastExport = null;   // {path, dir, filename, fmt}

document.querySelectorAll("[data-export]").forEach((b) => {
  b.addEventListener("click", async () => {
    busy(b, true);
    const fmt = b.dataset.export;
    const options = { frame: $("exp-frame").value, entity: $("exp-entity").value,
                      include_hitbox: $("exp-hitbox").checked };
    try {
      const saved = await api.exportSave(fmt, state.config, options);
      lastExport = { ...saved, fmt, options };
      $("exp-dlg-name").textContent = saved.filename;
      $("exp-dlg-size").textContent = `${(saved.size_bytes / 1024).toFixed(1)} kB`;
      $("exp-dlg-path").textContent = saved.path;
      $("exp-dlg-download").hidden = false;
      $("export-dialog").showModal();
    } catch (e) {
      toast(`Export failed: ${e.message}`);
    } finally {
      busy(b, false);
    }
  });
});

$("btn-export-cfd").addEventListener("click", async () => {
  const b = $("btn-export-cfd");
  busy(b, true);
  try {
    const saved = await api.exportCfd(state.config, $("exp-cfd-mesh").value);
    // a case is a folder, not a downloadable file: fmt null hides Download
    lastExport = { ...saved, fmt: null };
    const s = saved.summary;
    $("exp-dlg-name").textContent = saved.filename;
    $("exp-dlg-size").textContent =
      `${s.n_cells.toLocaleString()} cells, y+ ≈ ${s.y_plus_est}`;
    $("exp-dlg-path").textContent = saved.path;
    $("exp-dlg-download").hidden = true;
    $("export-dialog").showModal();
  } catch (e) {
    toast(`Case generation failed: ${e.message}`);
  } finally {
    busy(b, false);
  }
});

$("exp-dlg-reveal").addEventListener("click", async () => {
  if (!lastExport) return;
  try {
    await api.exportReveal(lastExport.path);
  } catch (e) {
    toast(`Could not open the folder: ${e.message}`);
  }
});
$("exp-dlg-download").addEventListener("click", async () => {
  if (!lastExport || !lastExport.fmt) return;
  busy($("exp-dlg-download"), true);
  try {
    await downloadExport(lastExport.fmt, state.config, lastExport.options);
  } catch (e) {
    toast(`Download failed: ${e.message}`);
  } finally {
    busy($("exp-dlg-download"), false);
  }
});
$("exp-dlg-close").addEventListener("click", () => $("export-dialog").close());

/* ---------------- presets + project files ---------------- */

async function loadPresets() {
  try {
    const { presets } = await api.presets();
    const sel = $("preset-select");
    presets.forEach((p, i) => {
      const o = document.createElement("option");
      o.value = i;
      o.textContent = p.name;
      o.title = p.description;
      sel.appendChild(o);
    });
    sel.addEventListener("change", () => {
      if (sel.value === "") return;
      const p = presets[+sel.value];
      state.config = withDefaults(structuredClone(p.config));
      state.target = p.target_downforce_n ?? state.target;
      writeConfigToForm();
      persistSession();
      refreshGeometry().then(runAnalysis);
      sel.value = "";
    });
  } catch { /* presets are optional */ }
}

$("btn-save").addEventListener("click", async () => {
  const custom = {};
  for (const spec of usedCustomSpecs()) {
    if (state.customDat[spec]) {
      custom[spec] = state.customDat[spec];
    } else {
      // uploaded in an earlier session: rebuild the .dat from server coords
      try {
        const g = await api.airfoil(spec);
        custom[spec] = g.name + "\n" +
          g.coords.map(p => ` ${p[0].toFixed(6)} ${p[1].toFixed(6)}`).join("\n") + "\n";
        state.customDat[spec] = custom[spec];
      } catch {
        toast(`Could not embed ${spec} in the project file — re-upload it ` +
              `before saving.`, "err", 8000);
        return;
      }
    }
  }
  const blob = new Blob([JSON.stringify({
    app: "wing-section-studio", version: 1,
    config: state.config, target_downforce_n: state.target,
    airfoil_names: state.airfoilNames, custom_airfoils: custom,
  }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "wing_section_project.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
});

$("btn-open").addEventListener("click", () => $("file-project").click());
$("file-project").addEventListener("change", async () => {
  const f = $("file-project").files[0];
  $("file-project").value = "";
  if (!f) return;
  try {
    const p = JSON.parse(await f.text());
    if (!Array.isArray(p.config?.elements) || !p.config.elements.length) {
      throw new Error("not a project file (no elements)");
    }
    // re-register embedded custom airfoils; server may assign new ids
    const remap = {};
    for (const [spec, dat] of Object.entries(p.custom_airfoils || {})) {
      const up = await api.uploadAirfoil(spec.split(":")[1], dat);
      remap[spec] = up.spec;
      state.customDat[up.spec] = dat;
      state.airfoilNames[up.spec] = up.name;
    }
    for (const e of p.config.elements) {
      if (remap[e.airfoil]) { e.airfoil = remap[e.airfoil]; continue; }
      // custom base wrapped in shape:/mfg: — remap the embedded slug
      const m = String(e.airfoil || "").match(/custom:[a-z0-9_-]+$/);
      if (m && remap[m[0]]) e.airfoil = e.airfoil.replace(m[0], remap[m[0]]);
    }
    Object.assign(state.airfoilNames, p.airfoil_names || {});
    state.config = withDefaults(p.config);
    state.target = p.target_downforce_n ?? 250;
    writeConfigToForm();
    persistSession();
    await refreshGeometry();
    runAnalysis();
    toast("Project loaded.", "good");
  } catch (e) {
    toast(`Could not open project: ${e.message}`);
  }
});

/* ---------------- dialogs, health ---------------- */

$("btn-model-notes").addEventListener("click", () =>
  $("model-dialog").showModal());

async function checkHealth() {
  try {
    await api.health();
    $("server-dot").classList.add("ok");
    $("server-dot").classList.remove("err");
  } catch {
    $("server-dot").classList.add("err");
    $("server-dot").classList.remove("ok");
  }
}

/* ---------------- boot ---------------- */

async function restoreSession() {
  let saved = null;
  // the server copy first: it is written by every session regardless of
  // origin, so it is always at least as fresh as this origin's localStorage
  try { saved = (await api.session()).state; } catch {}
  if (!saved?.config?.elements?.length) {
    try { saved = JSON.parse(localStorage.getItem("wss-session")); } catch {}
  }
  if (!saved?.config?.elements?.length) return false;
  try {
    // uploaded airfoils live in server memory: re-register any the session used
    const remap = {};
    for (const [spec, dat] of Object.entries(saved.custom_airfoils || {})) {
      try {
        const up = await api.uploadAirfoil(spec.split(":")[1], dat);
        remap[spec] = up.spec;
        state.customDat[up.spec] = dat;
        state.airfoilNames[up.spec] = up.name;
      } catch { /* leave the old spec; geometry will report it clearly */ }
    }
    for (const e of saved.config.elements) {
      if (remap[e.airfoil]) { e.airfoil = remap[e.airfoil]; continue; }
      const m = String(e.airfoil || "").match(/custom:[a-z0-9_-]+$/);
      if (m && remap[m[0]]) e.airfoil = e.airfoil.replace(m[0], remap[m[0]]);
    }
    Object.assign(state.airfoilNames, saved.airfoil_names || {});
    state.config = withDefaults(saved.config);
    if (Number.isFinite(saved.target)) state.target = saved.target;
    if (Array.isArray(saved.rans_rerank)) {
      state.ransRerank = saved.rans_rerank;
    }
    return true;
  } catch {
    return false;
  }
}

async function boot() {
  bindConfigInputs();
  bindManufacturing();
  bindRules();
  loadPresets();
  loadRulePresets();
  checkHealth();
  setInterval(checkHealth, 20000);
  // the desktop launcher's browser fallback reads request activity as "the
  // tab is still open" — keep a heartbeat independent of the status dot
  setInterval(() => { fetch("/api/health").catch(() => {}); }, 60000);
  viewport.setFrame("installed");

  const restored = await restoreSession();
  sessionReady = true;   // flushes may persist state from here on
  writeConfigToForm();
  // draw the section, but leave analysis to the user — the results panel
  // explains the two actions
  refreshGeometry();
  reattachOptimizer();   // a reload must not orphan a running search

  // re-attach a running verification queue, or restore the last table
  try {
    const { queue } = await api.ransQueueCurrent();
    if (queue && ["pending", "running"].includes(queue.state)) {
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      busy($("btn-rerank"), true);
      $("btn-rerank-cancel").disabled = false;
      pollRerank();
    } else if (state.ransRerank && state.ransRerank.length) {
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      renderRerank({ rows: state.ransRerank });
    }
  } catch { /* rediscovery is best-effort */ }

  if (restored) {
    toast("Continuing where you left off — use Load preset… to start fresh.",
          "info", 6500);
  } else if (!localStorage.getItem("wss-hint-dismissed")) {
    $("vp-hint").hidden = false;
  }
  $("vp-hint-close").addEventListener("click", () => {
    $("vp-hint").hidden = true;
    localStorage.setItem("wss-hint-dismissed", "1");
  });
  $("hero-info").addEventListener("click", () => $("model-dialog").showModal());
}

boot();
