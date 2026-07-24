/* Wing Section Studio — application wiring. */

import { api, downloadExport } from "./api.js";
import { lineChart, airfoilPreview } from "./charts.js";
import { Viewport, SERIES } from "./viewport.js";

const $ = (id) => document.getElementById(id);
const NU = 1.5e-5;

/* Resolve a chart color from the active theme at render time, so both
   themes ink the same chart correctly (fallback = dark-theme value). */
const ink = (name, fb) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fb;

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
  optResult: null,            // terminal optimizer status (session-persisted, trimmed)
  ransResult: null,           // terminal RANS verify status (session-persisted)
  ransRev: null,              // configRevision when the RANS run started
  screenMeta: null,           // {elem, re, ncrit, count} the table was screened at
  lastSweep: null,            // last operating-map response
  pins: [],                   // pinned designs [{t, label, config, target, headline}]
  geoValid: true,             // last geometry refresh succeeded
  queueActive: false,         // a re-rank queue owns the RANS solver
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

// values interpolated into innerHTML templates can originate from a project
// file — escape them so a crafted file cannot inject markup
function esc(v) {
  return String(v).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// fixed-point formatter that survives file-controlled junk: null, strings
// and objects render as an en-dash instead of throwing
function numf(v, d) {
  return v != null && Number.isFinite(+v) ? (+v).toFixed(d) : "–";
}

// restored flow-field images must be inline raster data, nothing else — an
// http(s) URL would phone home the moment a shared project opens, and a
// crafted string could break out of the report's <img src="..."> attribute
function safeFlow(o) {
  if (!o || typeof o !== "object") return null;
  const out = {};
  for (const k of ["umag", "cp"]) {
    if (typeof o[k] === "string"
        && /^data:image\/(png|jpe?g|webp);base64,[A-Za-z0-9+/=]+$/.test(o[k])) {
      out[k] = o[k];
    }
  }
  return Object.keys(out).length ? out : null;
}

// any job that owns server-side compute right now
function jobsRunning() {
  return !!(state.optJob || state.ransJob || state.queueActive);
}

/* unsaved-work indicator: the Save button carries a dot whenever the
   workspace (config or results) has changed since the last save/open */
let workspaceDirty = false;
function markDirty() {
  workspaceDirty = true;
  syncSaveIndicator();
}
function clearDirty() {
  workspaceDirty = false;
  syncSaveIndicator();
}
function syncSaveIndicator() {
  const b = $("btn-save");
  b.classList.toggle("dirty", workspaceDirty);
  b.title = workspaceDirty
    ? "Save the project to a file — unsaved changes (Ctrl+S)"
    : "Save the project to a file (Ctrl+S)";
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
  syncVpRulesToggle();
  buildElementCards();
  updateTargetC();
}

/* the viewport's Rules checkbox draws the envelope box — meaningless while
   no envelope is applied, so say so instead of a silent no-op */
function syncVpRulesToggle() {
  const has = !!state.config.rule_envelope;
  $("vp-rules").disabled = !has;
  $("vp-rules").closest("label").title = has
    ? "Show the rule envelope box"
    : "Enable Rules in the left panel first — there is no envelope to draw.";
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
    syncVpRulesToggle();
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
  $("rule-preset-name").addEventListener("input", syncRulePresetButtons);
  $("rule-preset").addEventListener("change", syncRulePresetButtons);
  syncRulePresetButtons();
}

/* boundary states disable with the reason instead of toasting on click */
function syncRulePresetButtons() {
  const name = ($("rule-preset-name").value || "").trim();
  $("rule-preset-save").disabled = !name;
  $("rule-preset-save").title = name
    ? "Save the current limits as a named preset"
    : "Give the preset a name first (the Save as field).";
  const sel = $("rule-preset").value;
  $("rule-preset-del").disabled = !sel;
  $("rule-preset-del").title = sel
    ? `Delete the "${sel}" preset from this machine`
    : "Select a preset to delete.";
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
  syncRulePresetButtons();
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
  // case-insensitive overwrite, matching the server's duplicate check —
  // else saving "fsae" over "FSAE" 422s as a duplicate
  const presets = [...state.rulePresets.filter(
                     (p) => p.name.toLowerCase() !== name.toLowerCase()),
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
    // the active envelope's values stay (they are the design's rules) but
    // they no longer come from a library preset
    if (state.config.rule_envelope?.preset_name === name) {
      delete state.config.rule_envelope.preset_name;
      onConfigChanged();
    }
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
  const n = state.config.elements.length;
  $("el-count").textContent = n;
  // boundary states disable with the reason instead of toasting on click
  $("el-add").disabled = n >= 4;
  $("el-add").title = n >= 4 ? "Maximum of 4 elements." : "Add a flap";
  $("el-remove").disabled = n <= 1;
  $("el-remove").title = n <= 1
    ? "The main element cannot be removed." : "Remove last flap";
  state.config.elements.forEach((e, i) => host.appendChild(elementCard(e, i)));
  state.polarElem = Math.min(state.polarElem, n - 1);
  buildViewportLegend();
  buildPolarChips();
  buildScreenerTargets();
  syncOptimizerVars();
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

// the optimizer status carries fields only the live poll needs — drop the
// evaluation cloud and the winner's full re-analysis before persisting
function trimOptResult(s) {
  if (!s) return null;
  const { cloud, result, ...rest } = s;
  return rest;
}

// everything computed for the current workspace, in exactly the shapes the
// render functions consume — restored without recomputation
function resultsSnapshot() {
  return {
    analysis: state.analysis,
    analysis_stale: !!state.analysis && !$("results-stale").hidden,
    optimizer: trimOptResult(state.optResult),
    opt_target: state.optTarget,
    rans: state.ransResult,
    rans_stale: !!state.ransResult && state.ransRev != null
                && configRevision > state.ransRev,
    sweep: state.lastSweep,
    sweep_stale: !!state.lastSweep && !$("map-stale").hidden,
    // the full library screen is ~500 KB of JSON — persist the top slice
    // (the table renders 60 rows; 150 keeps re-sorting useful)
    screen: state.screenRows
      ? { rows: state.screenRows.slice(0, 150), meta: state.screenMeta } : null,
  };
}

function sessionSnapshot() {
  const custom = {};
  for (const spec of usedCustomSpecs()) {
    if (state.customDat[spec]) {
      custom[spec] = state.customDat[spec];
    }
  }
  return { t: Date.now(),
           config: state.config, target: state.target,
           airfoil_names: state.airfoilNames, custom_airfoils: custom,
           rans_rerank: state.ransRerank,
           pins: state.pins,
           results: resultsSnapshot() };
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
function beacon(state) {
  try {
    return navigator.sendBeacon("/api/session",
      new Blob([JSON.stringify({ state })], { type: "application/json" }));
  } catch { return false; }
}
function flushSession() {
  if (!sessionReady) return;
  try {
    const snap = sessionSnapshot();
    try { localStorage.setItem("wss-session", JSON.stringify(snap)); } catch {}
    // the beacon quota is ~64 KB — a results-bearing snapshot can exceed it
    // and be dropped. On a same-origin reload localStorage covers the gap,
    // but the desktop shell serves a fresh origin each launch, so a dropped
    // beacon at close would lose the last edits. If the full beacon is
    // refused, retry with a results-stripped snapshot: the design, target
    // and pins are tiny and always fit, so the configuration never gets
    // lost even when the heavy results do (the debounced POST carried those
    // moments earlier).
    if (!beacon(snap)) {
      const { results, ...lean } = snap;
      beacon(lean);
    }
  } catch { /* best effort */ }
}
window.addEventListener("pagehide", flushSession);
window.addEventListener("beforeunload", flushSession);

// editing speed/chord/nu/ncrit changes each element's Re — keep the polar
// chips honest, and re-render the charts when the tab is being watched
const schedulePolarSync = debounce(() => {
  buildPolarChips();
  if (document.querySelector('.tab[data-tab="polars"]').classList
        .contains("active")) {
    renderPolars();
  }
}, 600);

function onConfigChanged() {
  configRevision++;
  markStale();
  // analysis overlays (CP marker) describe the previous geometry — clear
  // them until the next analyze completes
  if (viewport.xcp != null) viewport.setCp(null);
  updateTargetC();
  scheduleGeometry();
  schedulePolarSync();
  markDirty();
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
    state.geoValid = true;
    applyGeoValidity();
  } catch (e) {
    if (seq !== geoReqSeq) return;
    $("vp-status").textContent = e.message;
    $("vp-status").style.color = "var(--critical)";
    state.geoValid = false;
    applyGeoValidity();
  }
}

/* while the configuration has no valid geometry, every action that consumes
   it can only fail — disable them with the reason instead of five toasts.
   Buttons that are busy with an in-flight run keep their own lock: forcing
   them back on would allow concurrent duplicate runs. */
function applyGeoValidity() {
  const bad = !state.geoValid;
  const reason = "The current configuration has no valid geometry — fix it " +
    "first (the drawing panel's status line has the error).";
  const setBtn = (el) => {
    if (bad) {
      el.disabled = true;
      el.title = reason;
    } else if (!el.classList.contains("busy")) {
      el.disabled = false;
      el.title = "";
    }
  };
  setBtn($("btn-analyze"));
  setBtn($("btn-sweep"));
  document.querySelectorAll("[data-export]").forEach(setBtn);
  setBtn($("btn-export-cfd"));
  setBtn($("btn-report"));
  // the optimizer start button additionally answers to its own job state —
  // including the launch window before state.optJob is assigned
  if (bad) {
    $("btn-opt-run").disabled = true;
    $("btn-opt-run").title = reason;
  } else {
    if (!state.optJob && !optLaunching) $("btn-opt-run").disabled = false;
    $("btn-opt-run").title = "";
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
  if (state.analysis) {
    $("results-stale").hidden = false;
    // the Cp chart in the dock shows the same outdated analysis — say so
    // where the user is actually looking
    if (!$("cp-chart").hidden) $("cp-stale").hidden = false;
  }
  // a RANS verdict describes the config it solved, not the edited one.
  // Reset the text: a provenance message ("restored result…") may still be
  // in the element from an earlier render and would mislabel a fresh run.
  if (state.ransResult && state.ransRev != null
      && configRevision > state.ransRev) {
    $("rans-stale").textContent = "configuration changed since this " +
      "verification — the verdict below describes the earlier design; " +
      "re-verify";
    $("rans-stale").hidden = false;
    const kg = document.getElementById("btn-rans-apply-kg");
    if (kg) {
      kg.disabled = true;
      kg.title = "Calibrated on an earlier configuration — re-verify first.";
    }
  }
  // operating maps were swept for the previous configuration
  if (state.lastSweep) $("map-stale").hidden = false;
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
      $("cp-stale").hidden = true;
      viewport.setCp(res.coefficients.x_cp_c);
    } else {
      $("results-stale").hidden = false;
    }
    syncPinButton();
    markDirty();
    persistSession();
  } catch (e) {
    if (seq === analyzeSeq) toast(`Analysis failed: ${e.message}`);
  } finally {
    if (seq === analyzeSeq) {
      busy(btn, false);
      // if the config went geometry-invalid mid-run, busy(false) would have
      // re-enabled a button applyGeoValidity meant to keep disabled
      applyGeoValidity();
    }
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
  if (!res?.coefficients || !res?.forces) return;   // file-controlled shape
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
  (res.elements || []).forEach((e, i) => {
    const frac = +e.loading_fraction || 0;
    const gf = e.loading_fraction_ground;
    const row = document.createElement("div");
    row.className = "load-row";
    const scale = 1.3; // track spans 0..130% of CL_max
    // abbreviated tokens keep the subline on one line at the results
    // column's ~297px even with double-digit values (mono font)
    const gload = gf == null ? "" :
      ` · <span class="gload${gf > 2.0 ? " warn" : ""}" title="realized ` +
      `ground-effect operating point: Cl ${fmtN(e.Cl_operating, 2)} — ` +
      `${fmtN(gf, 2)}× the isolated CLmax">g-load ${fmtN(gf, 2)}</span>`;
    // the element label + loading % on one clean line; the detailed
    // coefficients on a muted subline below so nothing wraps mid-metric
    row.innerHTML =
      `<div class="load-head"><span>E${i + 1} ${esc(e.role)}</span>` +
      `<span>${(frac * 100).toFixed(0)}%</span></div>` +
      `<div class="load-sub">Cl ${fmtN(e.Cl_checked, 2)}/` +
      `${fmtN(e.CL_max_isolated, 2)} · gnd ×${fmtN(e.ground_multiplier, 1)}` +
      `${gload}</div>` +
      `<div class="load-track">` +
      `<div class="load-fill" style="width:${Math.min(frac / scale, 1) * 100}%;` +
      `background:${SERIES[i]}"></div>` +
      `<div class="load-limit" style="left:${(1 / scale) * 100}%"></div></div>`;
    host.appendChild(row);
  });

  const w = $("r-warnings");
  w.innerHTML = "";
  for (const msg of res.warnings || []) {
    const d = document.createElement("div");
    const crit = /separation|intersect|choke/.test(msg);
    d.className = "warning-item" + (crit ? " crit" : "");
    d.textContent = msg;
    w.appendChild(d);
  }
}

$("btn-analyze").addEventListener("click", runAnalysis);

/* Everything computed belongs to ONE workspace. Loading another project or
   preset must clear it all — otherwise the old workspace's results keep
   rendering under the new configuration, get pinned against it, and are
   persisted and exported as if they described it. */
function resetWorkspaceResults() {
  state.analysis = null;
  state.optResult = null;
  state.optTarget = null;
  state.ransResult = null;
  state.ransRev = null;
  state.ransCEst = null;
  state.lastSweep = null;
  state.screenRows = null;
  state.screenMeta = null;
  state.ransRerank = null;
  ransFlowId = null;
  ransFlowCache = null;
  if (ransFlowUrl) { URL.revokeObjectURL(ransFlowUrl); ransFlowUrl = null; }
  // results panel
  $("results-body").hidden = true;
  $("results-empty").hidden = false;
  $("results-stale").hidden = true;
  viewport.setCp(null);
  // pressure
  $("cp-chart").hidden = true;
  $("cp-chart").innerHTML = "";
  $("cp-empty").hidden = false;
  $("cp-stale").hidden = true;
  // optimizer
  $("opt-best-body").textContent = "No run yet.";
  $("opt-hints").innerHTML = "";
  $("opt-candidates").innerHTML = "";
  $("opt-cand-title").hidden = true;
  $("opt-conv").innerHTML = "";
  $("opt-obj").innerHTML = "";
  $("opt-pareto").innerHTML = "";
  $("opt-pareto-note").hidden = true;
  $("opt-charts").classList.add("empty");   // back to the placeholder
  $("opt-progress").style.width = "0%";
  $("opt-progress").classList.remove("done");
  $("btn-opt-apply").disabled = true;
  $("rerank-title").hidden = true;
  $("rerank-block").hidden = true;
  $("rerank-table").innerHTML = "";
  $("rerank-status").textContent = "";
  $("opt-status").textContent = "idle";
  syncOptObjectiveUI();   // expands "idle" into the mode's idle line
  // RANS verify
  $("rans-conv").innerHTML = "";
  $("rans-charts").classList.add("empty");   // hide the empty conv half
  $("rans-result").innerHTML =
    '<div class="empty-note">No verification run yet. Set up a section, ' +
    'then Verify with RANS — a coarse run typically takes a few minutes.</div>';
  $("rans-flow").hidden = true;
  $("rans-stale").hidden = true;
  $("rans-progress").style.width = "0%";
  $("rans-progress").classList.remove("done");
  // maps
  $("map-downforce").innerHTML = "";
  $("map-ld").innerHTML = "";
  $("map-charts").classList.add("empty");   // back to the placeholder
  $("map-allowance").hidden = true;
  $("map-stale").hidden = true;
  $("map-note").textContent = "Sweep ride height or speed to map how the " +
    "current design behaves away from its operating point.";
  // screener
  $("scr-table").innerHTML =
    '<div class="empty-note">Run a screen to rank candidate airfoils for ' +
    'an element.</div>';
  $("scr-note").textContent =
    "Ranks all 2,174 bundled sections at your Reynolds number.";
  syncPinButton();
}

/* ---------------- pinned designs ----------------
   Snapshot the current design + its analyzed numbers; restore or compare
   any pin later. Pins travel with the session and the project file — the
   iterate-and-justify record for a design report. */

const MAX_PINS = 12;

function syncPinButton() {
  const b = $("btn-pin");
  if (!state.analysis) {
    b.disabled = true;
    b.title = "Analyze the section first — a pin records the design with " +
              "its analyzed numbers.";
  } else if (state.pins.length >= MAX_PINS) {
    b.disabled = true;
    b.title = `Pin limit reached (${MAX_PINS}) — remove one to pin again.`;
  } else {
    b.disabled = false;
    b.title = !$("results-stale").hidden
      ? "The analysis is older than the current edits — the pin records " +
        "the analyzed design, not the edited form."
      : "Pin the current design and its results for later comparison.";
  }
}

function pinCurrent() {
  if (!state.analysis) return;
  const f = state.analysis.forces;
  state.pins.push({
    t: new Date().toISOString(),
    label: `#${state.pins.length + 1}`,
    config: structuredClone(state.config),
    target: state.target,
    headline: {
      downforce_n: f.downforce_n,
      drag_n: f.drag_total_n,
      ld: f.efficiency_ld,
      warnings: (state.analysis.warnings || []).length,
      elements: state.config.elements.length,
    },
  });
  renderPins();
  syncPinButton();
  markDirty();
  persistSession();
  toast(`Design pinned (${fmtN(f.downforce_n, 0)} N).`, "good", 3000);
}

function renderPins() {
  const host = $("pins-list");
  host.innerHTML = "";
  $("pins-empty").hidden = state.pins.length > 0;
  state.pins.forEach((p, i) => {
    const h = p.headline || {};   // pins can come from an edited file
    const row = document.createElement("div");
    row.className = "pin-row";
    const head = document.createElement("div");
    head.className = "pin-head";
    const name = document.createElement("span");
    name.className = "pin-name";
    name.textContent = `${p.label} · ${fmtN(h.downforce_n, 0)} N`;
    name.title = new Date(p.t).toLocaleString();
    head.appendChild(name);
    const stats = document.createElement("span");
    stats.className = "pin-stats";
    stats.textContent =
      `drag ${fmtN(h.drag_n, 1)} N · L/D ${fmtN(h.ld, 1)}` +
      ` · ${h.elements ?? "?"} el` +
      (h.warnings ? ` · ${h.warnings} warn` : "");
    head.appendChild(stats);
    row.appendChild(head);
    const actions = document.createElement("div");
    actions.className = "pin-actions";
    const apply = document.createElement("button");
    apply.className = "btn tiny";
    apply.textContent = "Restore";
    apply.title = "Restore this pinned design (replaces the configuration " +
                  "and re-analyzes)";
    apply.addEventListener("click", async () => {
      // restoring a pin is a full context switch, exactly like open/preset:
      // it must not swap the config out from under a running job, and the
      // previous design's results must not survive to render under, be
      // pinned against, or persist/export with the restored one
      if (jobsRunning()) {
        toast("A run is still using the solver — wait for it to finish or " +
              "cancel it before restoring a pin.", "info", 6000);
        return;
      }
      busy(apply, true);
      state.config = withDefaults(structuredClone(p.config));
      state.target = p.target;
      configRevision++;
      resetWorkspaceResults();
      writeConfigToForm();
      persistSession();
      await refreshGeometry();
      await runAnalysis();
      busy(apply, false);
      toast(`Pinned design ${p.label} restored.`, "good", 3000);
    });
    actions.appendChild(apply);
    const del = document.createElement("button");
    del.className = "btn tiny ghost";
    del.textContent = "✕";
    del.title = "Remove this pin";
    del.addEventListener("click", () => {
      state.pins.splice(i, 1);
      renderPins();
      syncPinButton();
      markDirty();
      persistSession();
    });
    actions.appendChild(del);
    row.appendChild(actions);
    host.appendChild(row);
  });
}

$("btn-pin").addEventListener("click", pinCurrent);

/* ---------------- pressure tab ---------------- */

function renderCp(res) {
  if (!Array.isArray(res?.cp_distributions)) return;
  const host = $("cp-chart");
  $("cp-empty").hidden = true;
  host.hidden = false;
  const mm = state.config.chord_mm;
  lineChart(host, {
    series: res.cp_distributions.map((d, i) => ({
      // role can arrive from a project file — charts.js escapes series
      // names at its innerHTML sink, so pass it through as plain text
      name: `E${i + 1} ${d.role}`,
      color: SERIES[i],
      x: (d.x || []).map(v => v * mm),
      y: d.cp || [],
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

/* Theme switch: everything drawn with resolved colors is re-inked from
   the new theme's tokens (CSS-classed chrome re-inks by itself). */
window.addEventListener("wss-themechange", () => {
  viewport.render();
  buildViewportLegend();
  if (state.analysis) { renderResults(state.analysis); renderCp(state.analysis); }
  if (state.lastSweep) renderSweep(state.lastSweep);
  if (state.screenRows) renderScreenTable();
  const active = document.querySelector(".tab.active");
  if (!active) return;
  if (active.dataset.tab === "polars") renderPolars();
  if (active.dataset.tab === "optimizer") reattachOptimizer();
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
    let xfJustFetched = false;
    if (withXfoil && !xf) {
      note.textContent = "running XFOIL — up to a minute…";
      xf = await api.polar({ spec, re, ncrit: state.config.ncrit,
                             engine: "xfoil" });
      state.xfoilCache[key] = xf;
      xfJustFetched = true;
    }
    // element chip switched (or config changed) while we were fetching —
    // a newer render owns the charts now
    if (seq !== polarReqSeq) {
      if (xfJustFetched) {
        const stillSelected =
          Math.min(state.polarElem, state.config.elements.length - 1) === i;
        if (stillSelected) {
          // a config nudge/theme flip re-rendered without the overlay while
          // XFOIL ran — re-render now that the cache has it
          renderPolars();
        } else {
          // the minute-long run DID finish — it sits in the cache; say so
          // instead of silently discarding the wait
          toast(`XFOIL polar for ${state.airfoilNames[spec] || spec} is ` +
                `ready — it shows when that element is selected again.`,
                "info", 6000);
        }
      }
      return;
    }
    const series = (xk, yk) => {
      const s = [{ name: "NeuralFoil", color: SERIES[i],
                   x: nf[xk], y: nf[yk] }];
      if (xf) s.push({ name: "XFOIL", color: ink("--viz-ref", "#e9edf6"), markers: "only",
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
    syncXfoilButton();
  } catch (err) {
    if (seq !== polarReqSeq) return;
    note.textContent = `polar failed: ${err.message}`;
  }
}

/* the button reflects the overlay state for the CURRENT selection: a cached
   overlay renders automatically, so clicking again would be a silent no-op */
function syncXfoilButton() {
  const b = $("btn-xfoil");
  if (b.classList.contains("busy")) return;
  const i = Math.min(state.polarElem, state.config.elements.length - 1);
  const spec = state.geo?.design?.[i]?.airfoil_eff
    || state.config.elements[i].airfoil;
  const has = !!state.xfoilCache[
    `${spec}|${Math.round(elementRe(i))}|${state.config.ncrit}`];
  b.disabled = has;
  b.textContent = has ? "XFOIL shown" : "Add XFOIL reference";
  b.title = has
    ? "The XFOIL overlay for this element at this Re is displayed."
    : "Overlay an XFOIL run for the selected element (takes up to a minute)";
}

$("btn-xfoil").addEventListener("click", async () => {
  busy($("btn-xfoil"), true);
  await renderPolars(true);
  const b = $("btn-xfoil");
  b.classList.remove("busy");
  b.disabled = false;
  syncXfoilButton();
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
      setOptControlsLocked(true);
      pollOptimizer();
    }
  } catch { /* rediscovery is best-effort */ }
}
$("btn-opt-cancel").addEventListener("click", async () => {
  if (state.optJob) { try { await api.optimizeCancel(state.optJob); } catch {} }
});
$("btn-opt-apply").addEventListener("click", applyBestDesign);
$("ov-af").addEventListener("change", syncAfPool);

function syncAfPool() {
  const on = $("ov-af").checked;
  const pool = $("ov-af-pool");
  pool.disabled = !on || pool.dataset.locked === "1";
  pool.title = on ? ""
    : "Enable Airfoil selection above to choose where candidates come from.";
}

/* the whole form follows the objective, not just the target field: the idle
   status line, the Effort tooltip and the target pill all describe
   target-mode behavior that max mode does not have */
function syncOptObjectiveUI({ statusToo = true } = {}) {
  const isMax = $("opt-objective").value === "max_downforce";
  // maximize mode has no target — hide the field entirely (not just grey
  // it), so the control set matches what the run actually uses
  $("opt-target-field").hidden = isMax;
  $("opt-target").disabled = isMax;
  // only the idle line follows the mode — a finished run's summary stays
  if (statusToo && !state.optJob
      && $("opt-status").textContent.startsWith("idle")) {
    $("opt-status").textContent = isMax
      ? "idle — maximize searches up to the 90% loading trust line; no " +
        "early stop at any target"
      : "idle — Fast/Standard stop early at the target; Thorough searches " +
        "everything";
  }
  $("opt-budget").title = isMax
    ? "Effort budget. In maximize mode every level searches the trusted " +
      "ceiling — there is no early stop; Thorough is the reproducible search."
    : "Fast/Standard stop as soon as the target is reached — quick, but " +
      "repeated runs can land in different drag basins. Thorough explores " +
      "the whole search space before refining (takes minutes) and " +
      "converges to the same lowest-drag design on every run.";
  $("r-target-pill").title = isMax
    ? "Grades the analysis against the downforce target — informational " +
      "only while the optimizer maximizes (max mode ignores the target)."
    : "";
}
$("opt-objective").addEventListener("change", syncOptObjectiveUI);

/* "Thorough (reproducible)" is a global-mode contract: local refinement
   just gets a larger budget from it */
function syncOptModeUI() {
  const local = $("opt-mode").value === "local";
  const thorough = $("opt-budget").querySelector('option[value="4000"]');
  thorough.textContent = local
    ? "Thorough (larger refine budget)" : "Thorough (reproducible)";
  thorough.title = local
    ? "Refine current design keeps the search in the same basin — a bigger " +
      "budget refines further, but the reproducible full-space search " +
      "needs Search = Global."
    : "Explores the whole search space before refining; converges to the " +
      "same lowest-drag design on every run.";
}
$("opt-mode").addEventListener("change", syncOptModeUI);

/* flap-only free variables need a flap to exist */
function syncOptimizerVars() {
  const single = state.config.elements.length === 1;
  for (const id of ["ov-defl", "ov-pos", "ov-chord"]) {
    const el = $(id);
    el.disabled = single || el.dataset.locked === "1";
    el.closest("label").title = single
      ? "Needs a flap — add an element (+) to free this variable." : "";
  }
}

/* a running job snapshots its options at launch — lock the controls so
   mid-run edits are not silently ignored */
const OPT_CONTROLS = ["opt-objective", "opt-target", "opt-mode", "opt-budget",
  "opt-drag-w", "opt-minld", "opt-minconf", "ov-aoa", "ov-defl", "ov-pos",
  "ov-chord", "ov-af", "ov-shape", "ov-af-pool"];

function setOptControlsLocked(on) {
  for (const id of OPT_CONTROLS) {
    const el = $(id);
    el.dataset.locked = on ? "1" : "";
    el.disabled = on;
    if (on) el.title = "Locked while the search runs — settings apply to " +
                       "the next run.";
  }
  if (!on) {
    // restore the state-dependent gating the blanket unlock would lose
    for (const id of OPT_CONTROLS) $(id).title = "";
    syncOptObjectiveUI({ statusToo: false });
    syncOptModeUI();
    syncOptimizerVars();
    syncAfPool();
    // the config may have become geometry-invalid during the run — Run must
    // stay disabled with its reason rather than being blanket re-enabled
    applyGeoValidity();
  }
}

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

// covers the window between the pre-request disable and state.optJob
// assignment, during which a geometry refresh could re-enable the button
let optLaunching = false;

async function launchOptimization() {
  // disable BEFORE the request: a double-click on the button must not
  // spawn two concurrent server-side search jobs
  if ($("btn-opt-run").disabled || optLaunching) return;
  optLaunching = true;
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
    optLaunching = false;
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
    optLaunching = false;
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
    setOptControlsLocked(true);
    pollOptimizer();
  } catch (e) {
    toast(`Could not start optimization: ${e.message}`);
    $("btn-opt-run").disabled = false;
  } finally {
    optLaunching = false;
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
        setOptControlsLocked(false);
        if (s.state === "failed") toast(`Optimization failed: ${s.error}`);
        if (s.best_config) {
          state.optResult = s;
          $("btn-opt-apply").disabled = false;
          // the shortlist exists now — offer the RANS re-rank
          if (s.candidates && s.candidates.length) {
            $("rerank-title").hidden = false;
            $("rerank-block").hidden = false;
            gateRerank();
          }
        } else if (s.state === "done") {
          $("opt-best-body").textContent =
            "The run finished without a usable design — nothing to apply. " +
            "Raise the effort or loosen the constraints and retry.";
        }
        renderOptHints(s);
        renderCandidates(s);
        // finished results are workspace state now — they survive restarts
        markDirty();
        persistSession();
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
      setOptControlsLocked(false);
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
    $("opt-charts").classList.remove("empty");   // real charts now
    // the job's own set-point first: after a reload the client snapshot is
    // gone and the live form value may have been edited since
    const target = s.target_downforce_n ?? state.optTarget ?? state.target;
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
    // airfoil names, variable keys and values can arrive from a project
    // file — escape strings and coerce numerics before innerHTML
    const afRows = (s.best.airfoils || []).map((a, i) =>
      `<div class="kv"><span>E${i + 1} airfoil</span><b>${esc(a)}</b></div>`).join("");
    const rows = afRows + s.variables.map((v, i) => {
      if (v.key === "airfoil_idx") return "";   // shown by name above
      // v.elem is file-controlled; only a real integer becomes an "E#"
      // prefix, so a crafted string can neither inject markup nor print NaN
      const eLbl = Number.isInteger(v.elem) ? `E${v.elem + 1} ` : "";
      const label = v.elem == null ? "stack angle"
        : `${eLbl}${NAMES[v.key] || esc(v.key)}`;
      const val = +(s.best.x?.[i]);
      if (!Number.isFinite(val)) return "";
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
      `<div class="kv"><span>downforce</span><b>${esc(s.best.downforce_n)} N</b></div>` +
      `<div class="kv"><span>drag estimate</span><b>${esc(s.best.drag_n)} N</b></div>` +
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
  // if anything is about to draw, the placeholder must be gone first — a
  // host that is display:none (under .empty) can't measure its own width
  if ((s.pareto && s.pareto.length) || (s.cloud && s.cloud.length)) {
    $("opt-charts").classList.remove("empty");
  }
  if (s.pareto && s.pareto.length) {
    // finalized front: full-fidelity numbers, clickable, trust-colored
    const pts = s.pareto.slice().sort((a, b) => a.drag_n - b.drag_n);
    lineChart(host, {
      series: [{
        name: "front", color: SERIES[0], markers: "only",
        x: pts.map(p => p.drag_n), y: pts.map(p => p.downforce_n),
        pointColors: pts.map(p => (flagged(p) ? ink("--viz-flag", "#f2b544") : SERIES[0])),
        onPointClick: (i) => {
          applyDesign(pts[i].config);
          toast(`Pareto design applied — ${fmtN(pts[i].downforce_n, 0)} N ` +
                `at ${fmtN(pts[i].drag_n)} N drag. Re-analyzing.`, "good");
        },
      }],
      xLabel: "drag [N]", yLabel: "downforce [N]", height: 148,
    });
    note.hidden = false;
    host.title = "Every clean design the search evaluated, as the " +
      "downforce–drag trade-off. Blue points are trusted; amber points " +
      "carry a trust flag. Click a front point to apply that design.";
  } else if (s.cloud && s.cloud.length
             && (s.state === "running" || s.state === "finalizing")) {
    // live evaluation cloud while the search runs (not clickable — these
    // are search-fidelity numbers)
    lineChart(host, {
      series: [{ name: "evaluations", color: ink("--viz-cloud", "#5a6a8a"), markers: "only",
                 x: s.cloud.map(c => c[1]), y: s.cloud.map(c => c[0]),
                 pointColors: s.cloud.map(c => (c[2] ? ink("--viz-flag", "#f2b544")
                                                     : ink("--viz-cloud", "#5a6a8a"))) }],
      xLabel: "drag [N]", yLabel: "downforce [N]", height: 148,
    });
    note.hidden = true;
    host.title = "Search-fidelity evaluation cloud (not clickable) — the " +
      "finalized front replaces it, with clickable points, when the run " +
      "finishes.";
  }
}

/* ---------------- RANS re-rank of the shortlist ---------------- */

function rerankItems() {
  const s = state.optResult;
  if (!s || !s.candidates || !s.candidates.length) {
    // no run in this page session — fall back to the restored table so a
    // reloaded shortlist can still be re-verified (e.g. on a finer mesh)
    return (state.ransRerank || [])
      .filter((r) => r.config)
      .slice(0, 8)
      .map((r) => ({ label: r.label, config: r.config }));
  }
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
    $("rr-mesh").disabled = true;
    state.queueActive = true;
    updateSolverButtons();
    pollRerank();
  } catch (e) {
    toast(`Could not start the verification queue: ${e.message}`);
    busy($("btn-rerank"), false);
    gateRerank();
  }
}

function pollRerank() {
  clearInterval(state.rerankPoll);
  let misses = 0;
  const detach = (msg) => {
    clearInterval(state.rerankPoll);
    busy($("btn-rerank"), false);
    $("btn-rerank-cancel").disabled = true;
    $("rr-mesh").disabled = false;
    state.queueActive = false;
    updateSolverButtons();
    if (msg) toast(msg);
  };
  state.rerankPoll = setInterval(async () => {
    try {
      const { queue } = await api.ransQueueCurrent();
      if (!queue) {
        // a restarted server has no queue at all — do not spin forever
        if (++misses >= 5) {
          detach("Lost the verification queue (server restarted?) — " +
                 "its runs did not survive.");
        }
        return;
      }
      misses = 0;
      state.queueActive = ["pending", "running"].includes(queue.state);
      renderRerank(queue);
      if (["done", "failed", "cancelled"].includes(queue.state)) {
        detach(queue.state === "failed"
          ? `Verification queue failed: ${queue.error}` : null);
        state.ransRerank = queue.rows;
        markDirty();
        persistSession();
      }
    } catch {
      if (++misses >= 5) {
        detach("Lost contact with the verification queue — reopen the " +
               "tab to re-attach if the server is back.");
      }
    }
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
      // a restored row can reference an uploaded airfoil the server no
      // longer holds (uploads live in server memory) — applying it would
      // just error, so say why instead
      const lost = (r.config.elements || []).some((e) => {
        const m = String(e.airfoil || "").match(/custom:[a-z0-9_-]+/);
        return m && !state.customDat[m[0]];
      });
      const b = document.createElement("button");
      b.className = "btn tiny";
      b.textContent = "Apply";
      if (lost) {
        b.disabled = true;
        b.title = "References an uploaded airfoil that is not present in " +
                  "this session — re-upload it to apply this design.";
      } else {
        b.addEventListener("click", () => applyDesign(r.config));
      }
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
  const target = s.target_downforce_n ?? state.optTarget ?? state.target;
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
            `${Math.round(w0.frac_max * 100)}%). Fine-mesh RANS measured ` +
            `panel optimism rising along this axis — about −14% near 85% ` +
            `loading, −24% right at the line — versus the −37…−42% ` +
            `collapse past it that this mode exists to exclude. Use the ` +
            `RANS re-rank below to measure your actual winner.`;
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
  // the applied design is a different configuration: verdicts, maps and
  // pressure plots computed for the previous one are stale from here on
  configRevision++;
  markStale();
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
      (f.warnings ? `<span class="badge warn">${esc(f.warnings)} warning` +
                    `${+f.warnings > 1 ? "s" : ""}</span>` : "");
    const head = document.createElement("div");
    head.className = "cand-head";
    head.innerHTML =
      `<span><b>#${esc(c.rank)}</b> ${fmtN(dn, 0)} N · drag ${fmtN(drag, 1)} N` +
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
      const val = +(c.x?.[i]);
      if (!Number.isFinite(val)) return;
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
  if (prev !== "" && +prev < state.config.elements.length) {
    sel.value = prev;
  } else if (prev !== "" && state.screenRows) {
    // the element the table was screened for no longer exists — say so
    // instead of silently retargeting E1
    $("scr-note").textContent = `E${+prev + 1} was removed — the table ` +
      `below was screened for it; re-screen for the selected element.`;
  }
  syncScreenerProvenance();
}

/* the results table is tied to the element/Re it was screened at — warn
   when the selection or the config has drifted away from that */
function syncScreenerProvenance() {
  const meta = state.screenMeta;
  if (!meta || !state.screenRows) return;
  const idx = parseInt($("scr-elem").value || "0", 10);
  const reNow = Math.round(elementRe(idx));
  const drift = Math.abs(reNow - meta.re) / Math.max(meta.re, 1);
  if (idx !== meta.elem || drift > 0.15) {
    $("scr-note").textContent =
      `table screened for E${meta.elem + 1} at Re ${fmtRe(meta.re)} — ` +
      `E${idx + 1} runs at Re ${fmtRe(reNow)}; re-screen before using ` +
      `these rankings.`;
  }
}
$("scr-elem").addEventListener("change", syncScreenerProvenance);

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
    state.screenMeta = {
      elem: parseInt($("scr-elem").value || "0", 10),
      re, ncrit: state.config.ncrit, count: res.count,
    };
    $("scr-note").textContent =
      `${res.count} sections passed the filters — click a column to sort.`;
    renderScreenTable();
    markDirty();
    persistSession();
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
  // rows can be restored from a project file — escape every interpolation
  const trs = top.map(r => {
    const tds = SCR_COLS.map(([k]) => {
      let v = r[k];
      if (v == null) v = "–";
      if (k === "CL_max" && r.CL_max_lower_bound) {
        return `<td title="polar had not stalled by the last analyzed ` +
               `angle — CL_max is a lower bound">≥ ${esc(v)}</td>`;
      }
      return `<td>${esc(v)}</td>`;
    }).join("");
    return `<tr>${tds}<td><button class="btn ghost" data-use="${esc(r.spec)}">Use</button></td></tr>`;
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
      const meta = state.screenMeta;
      const reNow = Math.round(elementRe(idx));
      if (meta && Math.abs(reNow - meta.re) / Math.max(meta.re, 1) > 0.15) {
        toast(`E${idx + 1} set to ${b.dataset.use} — note: it was ranked ` +
              `at Re ${fmtRe(meta.re)}, but E${idx + 1} runs at ` +
              `Re ${fmtRe(reNow)}. Re-screen to rank at the right Reynolds ` +
              `number.`, "info", 8000);
      } else {
        toast(`E${idx + 1} set to ${b.dataset.use}.`, "good");
      }
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

// hand-edited ranges are remembered per variable — switching ride height →
// speed → ride height must not destroy the range the user set up
const mapRanges = structuredClone(MAP_DEFAULTS);
let mapVarPrev = $("map-var").value;

$("map-var").addEventListener("change", () => {
  mapRanges[mapVarPrev] = {
    from: parseFloat($("map-from").value),
    to: parseFloat($("map-to").value),
    steps: parseInt($("map-steps").value, 10),
  };
  const v = $("map-var").value;
  const d = mapRanges[v] || MAP_DEFAULTS[v];
  $("map-from").value = d.from;
  $("map-to").value = d.to;
  $("map-steps").value = d.steps;
  mapVarPrev = v;
  // the charts still show the previous variable's sweep — flag them
  if (state.lastSweep && state.lastSweep.variable !== v) {
    $("map-stale").textContent = `charts show the last ` +
      `${MAP_LABELS[state.lastSweep.variable] || "sweep"} — re-sweep for ` +
      `${MAP_LABELS[v]}`;
    $("map-stale").hidden = false;
  }
});

$("btn-sweep").addEventListener("click", runSweep);

let sweepSeq = 0;

async function runSweep() {
  const btn = $("btn-sweep");
  if (btn.classList.contains("busy")) return;   // one sweep at a time
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
  const seq = ++sweepSeq;
  const revAtStart = configRevision;
  busy(btn, true);
  $("map-note").textContent = "sweeping…";
  try {
    const res = await api.sweep(state.config, variable, values);
    if (seq !== sweepSeq) return;   // a newer sweep owns the charts
    renderSweep(res);
    // only claim freshness if the form wasn't edited mid-flight
    if (configRevision === revAtStart) {
      $("map-stale").hidden = true;
      $("map-stale").textContent = "configuration changed since this " +
        "sweep — re-sweep to update the maps";
    } else {
      $("map-stale").hidden = false;
    }
    markDirty();
    persistSession();
  } catch (e) {
    $("map-note").textContent = "";
    toast(`Sweep failed: ${e.message}`);
  } finally {
    busy(btn, false);
    applyGeoValidity();   // re-gate if the config went invalid mid-sweep
  }
}

function renderSweep(res) {
  state.lastSweep = res;   // kept for theme re-inking + workspace persistence
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
  $("map-charts").classList.remove("empty");   // real charts now
  const xs = pts.map(p => p.value);
  const dn = pts.map(p => p.downforce_n);
  let ipk = 0;
  dn.forEach((v, i) => { if (v > dn[ipk]) ipk = i; });

  // current operating point, interpolated onto the sweep
  const series = [
    { name: "downforce", color: SERIES[0], x: xs, y: dn, markers: true },
    { name: "peak", color: ink("--viz-flag", "#f2b544"), markers: "only",
      x: [xs[ipk]], y: [dn[ipk]] },
  ];
  const cur = state.config[variable];
  for (let i = 0; i + 1 < xs.length; i++) {
    if ((cur - xs[i]) * (cur - xs[i + 1]) <= 0 && xs[i] !== xs[i + 1]) {
      const t = (cur - xs[i]) / (xs[i + 1] - xs[i]);
      series.push({ name: "current", color: ink("--viz-ref", "#e9edf6"), markers: "only",
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
      { from: 0, to: tb.optimistic_below_hc * c_mm, color: ink("--viz-optimistic", "#ec6a60"),
        label: "estimate optimistic" },
      { from: tb.conservative_hc[0] * c_mm, to: tb.conservative_hc[1] * c_mm,
        color: ink("--viz-conservative", "#57b6f0"), label: "estimate conservative" },
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

/* Docker availability, shared by the verify tab and the re-rank queue.
   Good news latches (the server caches the probe); unavailability re-checks
   on every ask so starting Docker Desktop is picked up. */
let ransAvail = null;

async function checkRansAvailable() {
  if (ransAvail?.available) return ransAvail;
  try {
    ransAvail = await api.ransAvailability();
  } catch (e) {
    ransAvail = { available: false, probe_error: e.message };
  }
  return ransAvail;
}

/* the single verify run and the re-rank queue share one solver — reflect
   the mutex on both start buttons instead of letting a click 409 */
function updateSolverButtons() {
  const runBtn = $("btn-rans-run");
  if (state.queueActive) {
    runBtn.disabled = true;
    runBtn.title = "The optimizer's re-rank queue is using the solver — " +
                   "wait for it or cancel it from the Optimizer tab.";
  } else if (ransAvail?.available && !state.ransJob) {
    runBtn.disabled = false;
    runBtn.title = "";
  }
  gateRerank();
}

/* Verify-shortlist gating: needs candidates, Docker, and a free solver */
async function gateRerank() {
  const btn = $("btn-rerank");
  if (btn.classList.contains("busy")) return;   // the queue itself runs
  if (!rerankItems().length) {
    btn.disabled = true;
    btn.title = "Run the optimizer first — the queue verifies its shortlist.";
    return;
  }
  if (state.queueActive) {
    btn.disabled = true;
    btn.title = "A re-rank queue is already using the solver.";
    return;
  }
  if (state.ransJob) {
    btn.disabled = true;
    btn.title = "A RANS verify run is using the solver — wait for it or " +
                "cancel it in the RANS verify tab.";
    return;
  }
  const a = await checkRansAvailable();
  // the world may have moved while the probe ran — never enable against
  // stale pre-await state
  if (btn.classList.contains("busy") || state.queueActive || state.ransJob) {
    return;
  }
  if (!a.available) {
    btn.disabled = true;
    btn.title = `Docker unavailable (${a.detail || a.probe_error ||
      "not running"}) — start Docker Desktop first; the queue runs OpenFOAM ` +
      `in a container.`;
    return;
  }
  btn.disabled = false;
  btn.title = "Runs the shortlist through the 2D RANS truth case, one at a " +
              "time, then re-ranks by measured downforce.";
}

async function refreshRansAvailability() {
  // the re-rank queue owns the solver while it runs: do not adopt its
  // active row as "our" verify job — cancelling it would silently kill the
  // queue, and its result describes a shortlist candidate, not the current
  // configuration
  let queue = null, queueProbeOk = false;
  try { ({ queue } = await api.ransQueueCurrent()); queueProbeOk = true; } catch {}
  const queueLive = queue && ["pending", "running"].includes(queue.state);
  state.queueActive = !!queueLive;
  if (queueLive) {
    const act = (queue.active != null && queue.rows?.[queue.active])
      ? ` — solving ${queue.rows[queue.active].label}` : "";
    $("rans-status").textContent =
      `the optimizer's re-rank queue is using the solver${act}. Its ` +
      `progress and results live in the Optimizer tab.`;
    updateSolverButtons();
  } else if (!state.ransJob && queueProbeOk) {
    // only re-attach an unknown solver job when we actually KNOW there is no
    // queue: if the queue probe failed we cannot tell whether the active job
    // is a queue row, and adopting it as "our" verify run would let Cancel
    // silently kill the queue
    // a job the page does not know about (reload, second window, dropped
    // poll) is re-attached — unless it was a queue row, whose result
    // belongs to the Optimizer tab, not to the current config. A LIVE run
    // always wins over a restored display; a done run is adopted only when
    // it is not the one already shown (id check — a session-restored result
    // must not block re-attaching a newer run).
    try {
      const cur = await api.ransCurrent();
      const queueOwned = !!queue?.rows?.some((r) => r.job_id === cur.job_id);
      if (cur.job_id && !queueOwned
          && ["pending", "running"].includes(cur.state)) {
        state.ransJob = cur.job_id;
        $("btn-rans-run").disabled = true;
        $("btn-rans-cancel").disabled = false;
        $("rans-stale").hidden = true;
        $("rans-flow").hidden = true;
        $("rans-result").innerHTML =
          '<div class="empty-note">Re-attached to a running verification…</div>';
        pollRans();
      } else if (cur.job_id && !queueOwned && cur.state === "done"
                 && state.ransResult?.id !== cur.job_id) {
        const s = await api.ransStatus(cur.job_id);
        state.ransResult = s;
        state.ransRev = null;   // solved before this page session
        renderRans(s);
        renderRansResult(s, { provenance: "reattached" });
        showRansFlow(s.id);
      } else if (cur.job_id && queueOwned && cur.state === "done"
                 && !state.ransResult) {
        $("rans-status").textContent = "idle — the last solver run belonged " +
          "to the optimizer's re-rank queue (see the Optimizer tab). Verify " +
          "the current configuration with the button above.";
      }
    } catch { /* rediscovery is best-effort */ }
  }
  const note = $("rans-note");
  const a = await checkRansAvailable();
  if (a.available) {
    note.textContent = a.image_present
      ? `Docker ${a.docker} ready · ${a.image}`
      : `Docker ${a.docker} ready — the OpenFOAM image downloads on the ` +
        `first run (~1 GB, one time)`;
  } else if (a.probe_error) {
    note.textContent = `Could not check Docker: ${a.probe_error}`;
  } else {
    note.textContent = `Docker unavailable (${a.detail || "not running"}) — ` +
      `start Docker Desktop and reopen this tab, or generate the case in ` +
      `the Export tab and run it in WSL.`;
  }
  if (!a.available) $("btn-rans-run").disabled = true;
  updateSolverButtons();
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
  // click-time single-flight: the mutex is otherwise only on button.disabled,
  // which two near-simultaneous triggers (a click plus Enter) can both pass
  // before the async ransStart lands and the server 409s
  if (state.ransJob || state.queueActive) {
    toast("The solver is already busy — wait for the current run or the " +
          "re-rank queue to finish.", "info");
    return;
  }
  const iters = parseInt($("rans-iters").value, 10);
  $("btn-rans-run").disabled = true;   // close the window before the await
  try {
    const { job_id } = await api.ransStart(
      state.config, $("rans-mesh").value,
      Number.isFinite(iters) ? Math.min(Math.max(iters, 100), 20000) : 10000);
    state.ransJob = job_id;
    // the target line must describe the config THIS run solves, not
    // whatever the form says later — snapshot the estimate at start
    state.ransCEst = state.analysis?.coefficients?.C_downforce_estimated
      ?? null;
    state.ransRev = configRevision;   // ties the verdict to this config
    $("btn-rans-run").disabled = true;
    $("btn-rans-cancel").disabled = false;
    $("rans-mesh").disabled = true;   // snapshotted at start — lock mid-run
    $("rans-iters").disabled = true;
    $("rans-stale").hidden = true;
    $("rans-conv").innerHTML = "";   // previous run's chart is not this run
    $("rans-charts").classList.add("empty");   // until this run's history draws
    $("rans-flow").hidden = true;
    ransFlowCache = null;
    $("rans-result").innerHTML =
      '<div class="empty-note">Verification running…</div>';
    updateSolverButtons();
    pollRans();
  } catch (e) {
    toast(`Could not start the RANS run: ${e.message}`);
    // the run never started — restore the mesh/iter controls and re-gate
    // the button (it was disabled to close the double-start window)
    $("rans-mesh").disabled = false;
    $("rans-iters").disabled = false;
    updateSolverButtons();
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
        $("rans-mesh").disabled = false;
        $("rans-iters").disabled = false;
        updateSolverButtons();
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
          state.ransResult = s;
          // a mid-run config edit means this verdict describes the design
          // at start, not the current form — say so and hold back the
          // one-click k_g calibration (same guard runAnalysis has)
          const edited = state.ransRev != null
            && configRevision !== state.ransRev;
          renderRansResult(s, { provenance: edited ? "edited" : "fresh" });
          if (edited) $("rans-stale").hidden = false;
          showRansFlow(s.id);
          markDirty();
          persistSession();
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
      $("rans-mesh").disabled = false;
      $("rans-iters").disabled = false;
      updateSolverButtons();
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
  if (s.mesh && Number.isFinite(+s.mesh.n_cells)) {
    bits.push(`${(+s.mesh.n_cells).toLocaleString()} cells`);
  }
  if (s.iteration) bits.push(`iteration ${s.iteration} / ${s.n_iters}`);
  if (s.latest) bits.push(`Cl ${numf(s.latest.cl, 3)}`,
                          `Cd ${numf(s.latest.cd, 4)}`);
  bits.push(`${Math.round(+s.elapsed_s || 0)}s`);
  $("rans-status").textContent = bits.join(" · ");
  if (s.history && s.history.length > 1) {
    // reveal the convergence half BEFORE lineChart measures its width, or
    // it reads 0 and falls back to a fixed 480px (letterboxed chart)
    $("rans-charts").classList.remove("empty");
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

function renderRansResult(s, { provenance = "fresh" } = {}) {
  const r = s.result;
  if (!r) return;
  const host = $("rans-result");
  // a verdict that predates this page session (re-attached or restored)
  // cannot be tied to the current form — say which config it describes
  if (provenance !== "fresh") {
    $("rans-stale").textContent = provenance === "restored"
      ? "restored result — it describes the configuration it was saved " +
        "with; re-verify to check the current one"
      : provenance === "edited"
      ? "the configuration was edited while this run solved — the verdict " +
        "describes the design at start; re-verify"
      : "re-attached result from an earlier run — it may describe an " +
        "earlier configuration; re-verify to be sure";
    $("rans-stale").hidden = false;
  }
  const p = r.panel;
  const pct = (v) => v == null ? "–"
    : `${v > 0 ? "+" : ""}${(+v).toFixed(1)}%`;
  // every value below can arrive from a project file: numerics through
  // numf (no .toFixed on junk), free strings through esc (innerHTML sink)
  const rows = [
    ["Sectional Cl — RANS", `${numf(r.cl_rans, 3)} ± ${numf(r.cl_rans_std, 3)}`],
    ["Sectional Cl — panel C_est", p ? numf(p.c_est, 3) : "–"],
    ["Cl delta (RANS vs estimate)", pct(r.delta_cl_pct)],
    ["Profile Cd — RANS", numf(r.cd_rans, 4)],
    ["Profile Cd — panel stack", p ? numf(p.cd_profile, 4) : "–"],
    ["Downforce at RANS Cl", `${esc(r.downforce_n_at_rans_cl)} N`],
    ["Downforce — panel estimate", p ? `${esc(p.downforce_n)} N` : "–"],
    ["Iterations", `${esc(r.n_iters_run)} (${esc(r.stop_reason)}; ` +
      `tail mean of ${esc(r.tail_rows)})`],
  ];
  host.innerHTML = rows.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");
  if (r.user_stopped) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = `Stopped at your request — the fields were written, ` +
      `so the flow views below show the partial solution. The force ` +
      `numbers are a preview, not a result` +
      (r.cl_drift != null ? ` (Cl drift ${(r.cl_drift * 100).toFixed(1)}% ` +
        `per window)` : ``) +
      `; rerun without stopping for a verdict. No k_g calibration is ` +
      `offered from a hand-stopped run.`;
    host.appendChild(w);
  } else if (!r.converged) {
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
      <b>${esc(r.suggested_k_g)}
      <button id="btn-rans-apply-kg" class="btn tiny">Apply</button></b>`;
    host.appendChild(d);
    const kg = $("btn-rans-apply-kg");
    if (provenance !== "fresh") {
      // k_g calibrated on another (or unknown) config must not be pinned
      // onto this one with one click
      kg.disabled = true;
      kg.title = "Calibrated on the configuration this run verified — " +
                 "re-verify the current configuration to calibrate k_g.";
    } else {
      kg.addEventListener("click", () => {
        state.config.k_g = r.suggested_k_g;
        writeConfigToForm();
        onConfigChanged();
        toast(`k_g pinned to ${r.suggested_k_g} — re-analyze to see the ` +
              `calibrated estimate.`, "good", 6000);
      });
    }
  }
  const note = document.createElement("p");
  note.className = "note";
  note.style.marginTop = "6px";
  note.innerHTML = `2D section truth check: RANS Cd is profile drag only —
    induced drag is a 3D effect and is compared in the studio's totals, not
    here. Case retained at <code>${esc(r.case_dir)}</code> (fields, logs,
    ParaView-openable <code>case.foam</code>).`;
  host.appendChild(note);
}

/* flow-field view: the solved section rendered server-side from the final
   OpenFOAM fields, in the same as-driven orientation as the drawing */
let ransFlowId = null;
let ransFlowUrl = null;   // objectURL of the currently shown image
let ransFlowSeq = 0;
let ransFlowCache = null; // {umag?, cp?} dataURLs restored from a project file

async function showRansFlow(jobId, field = "umag") {
  ransFlowId = jobId;
  const seq = ++ransFlowSeq;
  $("rans-flow").hidden = false;
  $("rans-flow-umag").classList.toggle("active", field === "umag");
  $("rans-flow-cp").classList.toggle("active", field === "cp");
  const img = $("rans-flow-img");
  const note = $("rans-flow-note");
  // a restored workspace carries the rendered images, not a live job
  if (!jobId && ransFlowCache) {
    if (ransFlowCache[field]) {
      img.src = ransFlowCache[field];
      img.style.opacity = "";
      note.textContent = "restored from the project file — same view as " +
        "the drawing: as driven, ground at the bottom, flow left to right";
    } else {
      note.textContent = "this field was not saved with the project — " +
        "re-verify to render it";
    }
    return;
  }
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
  (ransFlowId || ransFlowCache) && showRansFlow(ransFlowId, "umag"));
$("rans-flow-cp").addEventListener("click", () =>
  (ransFlowId || ransFlowCache) && showRansFlow(ransFlowId, "cp"));

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

/* ---------------- design report (self-contained HTML) ---------------- */

/* the live drawing, serialized standalone: computed styles are inlined so
   the SVG renders identically outside the app's stylesheets */
function svgSnapshot() {
  const src = $("viewport");
  if (!src.childNodes.length) return "";
  const clone = src.cloneNode(true);
  const srcEls = [src, ...src.querySelectorAll("*")];
  const dstEls = [clone, ...clone.querySelectorAll("*")];
  const PROPS = ["fill", "stroke", "stroke-width", "stroke-dasharray",
                 "stroke-linejoin", "opacity", "font-family", "font-size",
                 "font-weight", "letter-spacing", "text-anchor"];
  srcEls.forEach((sEl, i) => {
    const d = dstEls[i];
    if (!(d instanceof Element) || d === clone) return;
    const cs = getComputedStyle(sEl);
    let style = "";
    for (const p of PROPS) {
      const v = cs.getPropertyValue(p);
      if (v) style += `${p}:${v};`;
    }
    d.setAttribute("style", style);
    d.removeAttribute("class");
  });
  const W = src.clientWidth || 800, H = src.clientHeight || 400;
  clone.removeAttribute("class");
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("viewBox", `0 0 ${W} ${H}`);
  clone.setAttribute("width", "100%");
  clone.removeAttribute("height");
  // the sheet itself is a CSS background — bake it in as the first rect
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("width", W);
  bg.setAttribute("height", H);
  bg.setAttribute("fill", getComputedStyle(document.documentElement)
    .getPropertyValue("--sheet").trim() || "#ffffff");
  clone.insertBefore(bg, clone.firstChild);
  return clone.outerHTML;
}

function reportKv(rows) {
  return `<table class="kv">` + rows
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`)
    .join("") + `</table>`;
}

async function buildReport() {
  const c = state.config;
  const now = new Date();
  const sec = [];

  sec.push(`<h1>Wing section design report</h1>
    <p class="meta">${esc(now.toLocaleString())} · Wing Section Studio</p>`);

  // configuration
  const elRows = c.elements.map((e, i) =>
    `<tr><td>E${i + 1} ${roleName(i)}</td>` +
    `<td>${esc(state.airfoilNames[e.airfoil] || e.airfoil)}</td>` +
    `<td>${i === 0 ? "100" : (e.chord_ratio * 100).toFixed(1)}%</td>` +
    `<td>${i === 0 ? "–" : fmtN(e.deflection_deg, 1) + "°"}</td>` +
    `<td>${e.slot_gap_pct != null ? fmtN(e.slot_gap_pct, 1) + "%c" : "–"}</td>` +
    `<td>${e.slot_overlap_pct != null ? fmtN(e.slot_overlap_pct, 1) + "%c" : "–"}</td></tr>`
  ).join("");
  const m = c.manufacturing;
  const env = c.rule_envelope;
  sec.push(`<h2>Configuration</h2>` + reportKv([
    ["Speed", `${c.speed_ms} m/s`],
    ["Ride height", `${c.ride_height_mm} mm`],
    ["Main chord", `${c.chord_mm} mm`],
    ["Span", `${c.span_mm} mm`],
    ["Stack angle", `${c.stack_aoa_deg}°`],
    ["N_crit", c.ncrit],
    ["Air density", `${c.rho} kg/m³`],
    ["Viscous efficiency", c.viscous_efficiency],
    ["3D efficiency", c.efficiency_3d],
    ["Span efficiency", c.span_efficiency],
    ["Ground gain k_g", c.k_g != null ? c.k_g : "auto"],
    ["Target downforce", `${state.target} N`],
    ["Manufacturing prep", m
      ? `TE ${m.te_gap_mm} mm (${m.te_mode})` +
        (m.min_thickness_mm ? `, min thickness ${m.min_thickness_mm} mm` : "")
      : "off"],
    ["Rule envelope", env
      ? [env.max_length_mm != null ? `length ≤ ${env.max_length_mm} mm` : "",
         env.max_height_mm != null ? `height ≤ ${env.max_height_mm} mm` : "",
         env.min_ground_clearance_mm != null
           ? `clearance ≥ ${env.min_ground_clearance_mm} mm` : ""]
          .filter(Boolean).join(", ") + (env.preset_name ? ` (${env.preset_name})` : "")
      : "off"],
  ]) +
  `<table class="grid"><tr><th>Element</th><th>Airfoil</th><th>Chord</th>` +
  `<th>Deflection</th><th>Slot gap</th><th>Overlap</th></tr>${elRows}</table>`);

  // drawing
  const svg = svgSnapshot();
  if (svg) sec.push(`<h2>Section drawing</h2><div class="fig">${svg}</div>`);

  // analysis results
  const a = state.analysis;
  if (a) {
    const f = a.forces, co = a.coefficients;
    sec.push(`<h2>Analysis</h2>` + reportKv([
      ["Estimated downforce", `${fmtN(f.downforce_n, 0)} N`],
      ["Drag estimate", `${fmtN(f.drag_total_n, 1)} N ` +
        `(${fmtN(f.drag_induced_n, 1)} induced · ${fmtN(f.drag_profile_n, 1)} profile)`],
      ["L/D estimate", fmtN(f.efficiency_ld, 1)],
      ["C_ΔF estimate", fmtN(co.C_downforce_estimated, 2)],
      ["C_ΔF inviscid gnd / free", `${fmtN(co.C_downforce_inviscid_ground, 2)} / ` +
        `${fmtN(co.C_downforce_inviscid_free, 2)}`],
      ["Centre of pressure", `${(co.x_cp_c * c.chord_mm).toFixed(0)} mm`],
      ["k_g used", co.k_ground_realization != null
        ? `${fmtN(co.k_ground_realization, 2)} (${co.k_ground_source || "auto"})` : "–"],
    ]) +
    `<table class="grid"><tr><th>Element</th><th>Cl</th><th>CL_max</th>` +
    `<th>Loading</th><th>Ground ×</th></tr>` +
    (a.elements || []).map((e, i) =>
      `<tr><td>E${i + 1} ${esc(e.role)}</td><td>${fmtN(e.Cl_checked, 2)}</td>` +
      `<td>${fmtN(e.CL_max_isolated, 2)}</td>` +
      `<td>${numf(e.loading_fraction != null ? e.loading_fraction * 100 : null, 0)}%</td>` +
      `<td>${fmtN(e.ground_multiplier, 1)}</td></tr>`).join("") +
    `</table>` +
    ((a.warnings || []).length
      ? `<div class="warn"><b>Warnings</b><ul>` +
        (a.warnings || []).map(w => `<li>${esc(w)}</li>`).join("") + `</ul></div>`
      : ""));
  }

  // optimizer summary
  const o = state.optResult;
  if (o?.best) {
    sec.push(`<h2>Optimization</h2>` + reportKv([
      ["Objective", o.objective === "max_downforce"
        ? "Maximize downforce (trusted)" : "Hit a downforce target"],
      ["Best downforce", `${o.best.downforce_n} N`],
      ["Best drag", `${o.best.drag_n} N`],
      ["Evaluations", o.n_eval],
    ]) + ((o.candidates || []).length
      ? `<table class="grid"><tr><th>#</th><th>Downforce</th><th>Drag</th>` +
        `<th>L/D</th><th>Flags</th></tr>` +
        o.candidates.map(cd => {
          const s = cd.summary || {};
          const flags = [s.low_confidence ? "low conf" : "",
                         s.near_stall ? "near stall" : "",
                         s.slot_signature ? "slot corner" : ""]
            .filter(Boolean).join(", ") || "clean";
          return `<tr><td>${esc(cd.rank)}</td>` +
            `<td>${fmtN(s.downforce_n ?? cd.downforce_n, 0)} N</td>` +
            `<td>${fmtN(s.drag_total_n ?? cd.drag_n, 1)} N</td>` +
            `<td>${s.efficiency_ld != null ? fmtN(s.efficiency_ld, 1) : "–"}</td>` +
            `<td>${esc(flags)}</td></tr>`;
        }).join("") + `</table>` : ""));
  }

  // RANS verification
  const rs = state.ransResult?.result;
  if (rs) {
    sec.push(`<h2>RANS verification</h2>` + reportKv([
      ["Sectional Cl — RANS", `${numf(rs.cl_rans, 3)} ± ${numf(rs.cl_rans_std, 3)}`],
      ["Sectional Cl — panel estimate", rs.panel ? numf(rs.panel.c_est, 3) : "–"],
      ["Cl delta", rs.delta_cl_pct != null
        ? `${rs.delta_cl_pct > 0 ? "+" : ""}${fmtN(rs.delta_cl_pct, 1)}%` : "–"],
      ["Downforce at RANS Cl", `${rs.downforce_n_at_rans_cl} N`],
      ["Converged", rs.converged ? "yes" : (rs.user_stopped
        ? "stopped by user (preview)" : "NO — mid-transient snapshot")],
      ["Iterations", `${rs.n_iters_run} (${rs.stop_reason})`],
      ["Suggested k_g", rs.suggested_k_g != null ? rs.suggested_k_g : "–"],
      ["Mesh caution", rs.mesh_caution ? "coarse mesh — screening only" : ""],
    ]));
    // re-validated: a restored cache must stay inline image data
    const flow = safeFlow(await captureRansFlow());
    if (flow?.umag) {
      sec.push(`<div class="fig"><img src="${flow.umag}" ` +
               `alt="RANS velocity field"><p class="meta">Velocity field — ` +
               `as driven, ground at the bottom, flow left to right</p></div>`);
    }
  }

  // re-rank table
  if (state.ransRerank?.length) {
    sec.push(`<h2>RANS re-rank of the shortlist</h2>` +
      `<table class="grid"><tr><th>#</th><th>Design</th><th>Panel N</th>` +
      `<th>RANS N</th><th>Δ%</th><th>Verdict</th></tr>` +
      state.ransRerank.map(r =>
        `<tr><td>${esc(r.rank ?? "–")}</td><td>${esc(r.label)}</td>` +
        `<td>${r.panel_downforce_n != null ? fmtN(r.panel_downforce_n, 0) : "–"}</td>` +
        `<td>${r.rans_downforce_n != null ? fmtN(r.rans_downforce_n, 0) : "–"}</td>` +
        `<td>${r.delta_cl_pct != null
          ? (r.delta_cl_pct > 0 ? "+" : "") + fmtN(r.delta_cl_pct, 1) : "–"}</td>` +
        `<td>${esc(r.verdict || r.state || "–")}</td></tr>`).join("") +
      `</table>`);
  }

  // operating map summary
  if (state.lastSweep) {
    const pts = state.lastSweep.points.filter(
      p => !p.error && Number.isFinite(p.downforce_n));
    if (pts.length) {
      let pk = pts[0];
      for (const p of pts) if (p.downforce_n > pk.downforce_n) pk = p;
      sec.push(`<h2>Operating map</h2>` + reportKv([
        ["Swept variable", MAP_LABELS[state.lastSweep.variable]
          || state.lastSweep.variable],
        ["Range", `${pts[0].value} – ${pts[pts.length - 1].value} ` +
          `${MAP_UNITS[state.lastSweep.variable] || ""}`],
        ["Peak downforce", `${fmtN(pk.downforce_n, 0)} N at ${pk.value} ` +
          `${MAP_UNITS[state.lastSweep.variable] || ""}`],
      ]));
    }
  }

  // pinned designs
  if (state.pins.length) {
    sec.push(`<h2>Design iterations (pinned)</h2>` +
      `<table class="grid"><tr><th>Pin</th><th>Date</th><th>Elements</th>` +
      `<th>Downforce</th><th>Drag</th><th>L/D</th><th>Warnings</th></tr>` +
      state.pins.map(p => {
        const h = p.headline || {};
        return `<tr><td>${esc(p.label)}</td>` +
          `<td>${esc(new Date(p.t).toLocaleDateString())}</td>` +
          `<td>${esc(h.elements ?? "–")}</td>` +
          `<td>${fmtN(h.downforce_n, 0)} N</td>` +
          `<td>${fmtN(h.drag_n, 1)} N</td>` +
          `<td>${fmtN(h.ld, 1)}</td>` +
          `<td>${esc(h.warnings || "–")}</td></tr>`;
      }).join("") +
      `</table>`);
  }

  sec.push(`<p class="meta">Estimates from the studio's panel + viscous
    model; RANS rows are OpenFOAM (simpleFoam, k-ω SST, moving ground)
    section truth checks. See the workflow guide for model limits.</p>`);

  const html = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Wing section design report — ${esc(now.toISOString().slice(0, 10))}</title>
<style>
  body { font: 14px/1.5 "Segoe UI", system-ui, sans-serif; color: #1c2433;
         max-width: 860px; margin: 32px auto; padding: 0 20px; }
  h1 { font-size: 24px; margin: 0 0 2px; }
  h2 { font-size: 16px; margin: 26px 0 8px; border-bottom: 1px solid #c3cbda;
       padding-bottom: 4px; }
  .meta { color: #5c6980; font-size: 12px; }
  table { border-collapse: collapse; margin: 8px 0; font-size: 13px; }
  table.kv td { padding: 3px 14px 3px 0; vertical-align: top; }
  table.kv td:first-child { color: #5c6980; white-space: nowrap; }
  table.grid { width: 100%; }
  table.grid th, table.grid td { border: 1px solid #d7dce6; padding: 4px 8px;
       text-align: left; }
  table.grid th { background: #f0f2f6; font-weight: 600; }
  .fig { margin: 12px 0; border: 1px solid #d7dce6; border-radius: 4px;
         overflow: hidden; }
  .fig img { max-width: 100%; display: block; }
  .fig svg { display: block; }
  .warn { background: #fdf6e7; border: 1px solid #e5cf9a; border-radius: 4px;
          padding: 8px 12px; margin: 10px 0; font-size: 13px; }
  .warn ul { margin: 6px 0 0; padding-left: 20px; }
  @media print { body { margin: 10mm auto; } h2 { break-after: avoid; } }
</style></head><body>${sec.join("\n")}</body></html>`;

  const blob = new Blob([html], { type: "text/html" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `wing_design_report_${now.toISOString().slice(0, 10)}.html`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 5000);
}

$("btn-report").addEventListener("click", async () => {
  const b = $("btn-report");
  busy(b, true);
  try {
    await buildReport();
    toast("Design report saved — open it in a browser or print to PDF.",
          "good", 5000);
  } catch (e) {
    toast(`Report failed: ${e.message}`);
  } finally {
    busy(b, false);
  }
});

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
      if (jobsRunning()) {
        toast("A run is still using the solver — wait for it to finish or " +
              "cancel it before loading a preset.", "info", 6000);
        sel.value = "";
        return;
      }
      const p = presets[+sel.value];
      // a preset is a fresh project: the old workspace's results AND its
      // pins (which belonged to the previous design context) must not carry
      // over — file-open replaces pins with the file's, a preset has none
      resetWorkspaceResults();
      state.pins = [];
      configRevision++;
      state.config = withDefaults(structuredClone(p.config));
      state.target = p.target_downforce_n ?? state.target;
      writeConfigToForm();
      renderPins();
      markDirty();
      persistSession();
      refreshGeometry().then(runAnalysis);
      sel.value = "";
    });
  } catch { /* presets are optional */ }
}

function blobToDataURL(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

// the flow-field renders live in the server's job registry, which forgets
// them on restart — embed them in the project file while they are fetchable
async function captureRansFlow() {
  if (ransFlowCache) return ransFlowCache;   // restored images stay valid
  const id = state.ransResult?.id;
  if (!id || state.ransResult.state !== "done") return null;
  const grab = async (field) => {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 5000);
      const res = await fetch(`/api/rans/${id}/flow?field=${field}`,
                              { signal: ctl.signal });
      clearTimeout(t);
      if (res.ok) return await blobToDataURL(await res.blob());
    } catch { /* best effort — the verdict saves either way */ }
    return null;
  };
  const [umag, cp] = await Promise.all([grab("umag"), grab("cp")]);
  const out = {};
  if (umag) out.umag = umag;
  if (cp) out.cp = cp;
  if (!Object.keys(out).length) return null;
  ransFlowCache = out;   // later saves skip the refetch
  return out;
}

$("btn-save").addEventListener("click", saveProject);

async function saveProject() {
  // capturing flow images is async — one save at a time, with the spinner
  const saveBtn = $("btn-save");
  if (saveBtn.classList.contains("busy")) return;
  busy(saveBtn, true);
  try {
    await saveProjectInner();
  } finally {
    busy(saveBtn, false);
    syncSaveIndicator();
  }
}

async function saveProjectInner() {
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
  const flow = await captureRansFlow();
  const blob = new Blob([JSON.stringify({
    app: "wing-section-studio", version: 2,
    saved_at: new Date().toISOString(),
    config: state.config, target_downforce_n: state.target,
    airfoil_names: state.airfoilNames, custom_airfoils: custom,
    pins: state.pins,
    rans_rerank: state.ransRerank,
    results: resultsSnapshot(),
    rans_flow: flow,
  }, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  const stamp = new Date().toISOString().slice(0, 10);
  a.download = `wing_project_${stamp}.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  clearDirty();
  toast("Project saved — configuration, pins and all computed results.",
        "good", 4000);
}

/* Restore every saved result into the exact render paths the live flows
   use — no recomputation. Shapes come from resultsSnapshot(), but the
   payload may be a hand-edited file: each section is isolated so one
   corrupt block skips itself instead of aborting the whole restore. */
function restoreResults(r) {
  if (!r || typeof r !== "object") return;
  const attempt = (what, fn) => {
    try { fn(); } catch (e) {
      console.warn(`workspace restore: ${what} skipped —`, e);
    }
  };
  attempt("analysis", () => {
    if (!r.analysis) return;
    state.analysis = r.analysis;
    renderResults(r.analysis);
    renderCp(r.analysis);
    if (r.analysis_stale) {
      $("results-stale").hidden = false;
      $("cp-stale").hidden = false;
    } else {
      $("results-stale").hidden = true;
      $("cp-stale").hidden = true;
      const xcp = +r.analysis?.coefficients?.x_cp_c;
      if (Number.isFinite(xcp)) viewport.setCp(xcp);
    }
  });
  attempt("optimizer results", () => {
    if (!r.optimizer || !r.optimizer.best_config) return;
    state.optResult = r.optimizer;
    state.optTarget = Number.isFinite(r.opt_target) ? r.opt_target : null;
    renderOptimizer(r.optimizer);
    renderOptHints(r.optimizer);
    renderCandidates(r.optimizer);
    $("btn-opt-apply").disabled = false;
    if (r.optimizer.candidates?.length) {
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      gateRerank();
    }
  });
  attempt("RANS result", () => {
    if (!r.rans || !r.rans.result) return;
    state.ransResult = r.rans;
    state.ransRev = r.rans_stale ? -1 : null;
    renderRans(r.rans);
    renderRansResult(r.rans, { provenance: "restored" });
    if (ransFlowCache) {
      showRansFlow(null);   // rendered from the project file's images
    } else if (r.rans.id) {
      // the server may still hold the job (same-process reload) — probe
      // quietly and only surface the flow view if it is actually there
      $("rans-flow").hidden = true;
      fetch(`/api/rans/${encodeURIComponent(r.rans.id)}/flow?field=umag`)
        .then((res) => { if (res.ok) showRansFlow(r.rans.id); })
        .catch(() => {});
    } else {
      $("rans-flow").hidden = true;
    }
  });
  attempt("operating map", () => {
    if (!r.sweep) return;
    renderSweep(r.sweep);
    if (r.sweep_stale) $("map-stale").hidden = false;
  });
  attempt("screener table", () => {
    if (!r.screen || !Array.isArray(r.screen.rows)) return;
    state.screenRows = r.screen.rows;
    state.screenMeta = r.screen.meta || null;
    renderScreenTable();
    if (state.screenMeta?.count != null) {
      $("scr-note").textContent = `${state.screenMeta.count} sections ` +
        `passed the filters (restored) — click a column to sort.`;
    }
    syncScreenerProvenance();
  });
  syncPinButton();
}

$("btn-open").addEventListener("click", () => {
  if (jobsRunning()) {
    toast("A run is still using the solver — wait for it to finish or " +
          "cancel it before opening another project.", "info", 6000);
    return;
  }
  $("file-project").click();
});
$("file-project").addEventListener("change", async () => {
  const f = $("file-project").files[0];
  $("file-project").value = "";
  if (!f) return;
  if (jobsRunning()) {
    toast("A run is still using the solver — wait for it to finish or " +
          "cancel it before opening another project.", "info", 6000);
    return;
  }
  try {
    const p = JSON.parse(await f.text());
    if (!Array.isArray(p.config?.elements) || !p.config.elements.length) {
      throw new Error("not a project file (no elements)");
    }
    // the old workspace's computations end here — nothing from it may
    // render under, be pinned against, or persist with the new project
    resetWorkspaceResults();
    configRevision++;
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
    state.pins = Array.isArray(p.pins) ? p.pins : [];
    if (Array.isArray(p.rans_rerank)) {
      state.ransRerank = p.rans_rerank;
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      renderRerank({ rows: state.ransRerank });
      gateRerank();
    }
    ransFlowCache = safeFlow(p.rans_flow);
    writeConfigToForm();
    renderPins();
    await refreshGeometry();
    if (p.version >= 2 && p.results) {
      // a full workspace: restore the saved results instead of recomputing
      restoreResults(p.results);
      toast("Project loaded — configuration, pins and saved results.",
            "good", 5000);
    } else {
      runAnalysis();
      toast("Project loaded.", "good");
    }
    persistSession();
    clearDirty();
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
  // both copies carry a timestamp: the pagehide beacon can silently drop a
  // large payload (64 KB quota), so the server copy is NOT guaranteed
  // fresher — take whichever snapshot is newer
  let server = null, local = null;
  try { server = (await api.session()).state; } catch {}
  try { local = JSON.parse(localStorage.getItem("wss-session")); } catch {}
  const valid = (s) => !!s?.config?.elements?.length;
  let saved = null;
  if (valid(server) && valid(local)) {
    // prefer the newer snapshot; on a tie prefer localStorage, which always
    // holds the FULL snapshot — at close both copies share a timestamp but
    // the server one may be the results-stripped beacon fallback
    saved = (+local.t || 0) >= (+server.t || 0) ? local : server;
  } else {
    saved = valid(server) ? server : (valid(local) ? local : null);
  }
  if (!saved) return false;
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
    if (Array.isArray(saved.pins)) state.pins = saved.pins;
    return saved.results && typeof saved.results === "object"
      ? saved.results : true;
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
  renderPins();
  syncPinButton();
  syncOptObjectiveUI();
  syncOptModeUI();
  syncAfPool();
  syncSaveIndicator();
  // draw the section first, then re-render every result the session carried
  // — the workspace comes back exactly as it was left, computations
  // included. A corrupt snapshot must not abort boot: the rest of the app
  // (re-attach, shortcuts, dialogs) still has to wire up.
  await refreshGeometry();
  if (restored && typeof restored === "object") {
    try { restoreResults(restored); } catch (e) {
      console.warn("session results restore skipped —", e);
    }
  }
  reattachOptimizer();   // a reload must not orphan a running search

  // re-attach a running verification queue, or restore the last table
  try {
    const { queue } = await api.ransQueueCurrent();
    if (queue && ["pending", "running"].includes(queue.state)) {
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      busy($("btn-rerank"), true);
      $("btn-rerank-cancel").disabled = false;
      $("rr-mesh").disabled = true;
      state.queueActive = true;
      updateSolverButtons();
      pollRerank();
    } else if (state.ransRerank && state.ransRerank.length) {
      $("rerank-title").hidden = false;
      $("rerank-block").hidden = false;
      renderRerank({ rows: state.ransRerank });
      gateRerank();
    }
  } catch { /* rediscovery is best-effort */ }

  // keyboard shortcuts: the three highest-traffic actions. Never inside a
  // modal dialog (the dialog's own controls answer there) and never on key
  // auto-repeat (a held Ctrl+S must not queue a stack of saves).
  window.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    if (!mod || e.repeat) return;
    if (document.querySelector("dialog[open]")) return;
    if (e.key === "s" || e.key === "S") {
      e.preventDefault();
      saveProject();   // internally single-flight
    } else if (e.key === "o" || e.key === "O") {
      e.preventDefault();
      $("btn-open").click();   // carries the jobs-running guard
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (!$("btn-analyze").disabled
          && !$("btn-analyze").classList.contains("busy")) {
        runAnalysis();
      }
    }
  });

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
