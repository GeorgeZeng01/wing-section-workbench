/* Wing Section Studio — application wiring. */

import { api, downloadExport } from "./api.js";
import { lineChart, airfoilPreview, redrawStaleCharts } from "./charts.js";
import { Viewport, SERIES } from "./viewport.js";
import { mountFlowAnim } from "./flowanim.js";

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
  ansysPresets: [],           // machine-level ANSYS 2D settings library
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
  return !!(state.optJob || state.ransJob || state.queueActive || fl2dJob);
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
  // another project's provenance may not survive into this one
  ruleDraftLast = env ? ruleDraftSource(env)?.name ?? null : null;
  if (env) {
    fillRulesForm(env);
    setRulePreset(env.preset_name ?? "");
  }
  syncVpRulesToggle();
  buildElementCards();
  updateTargetC();
  // every path that REPLACES the configuration (project open, preset load,
  // pin restore, optimizer apply) funnels through here without passing
  // onConfigChanged — the resolved-wall placeholders are derived from the
  // operating point, so they would otherwise describe the previous design
  syncAnsysPlaceholders();
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

/* every field that is a LIMIT: x_offset_mm anchors the drawn box,
   le_radius_scope only qualifies the radius limit and measure_ride_height_mm
   only says which load case the height caps are read in, so none of those on
   its own is a rule set worth saving — an envelope holding nothing but a
   rule ride height can never produce a violation */
const RULE_LIMIT_KEYS = ["max_length_mm", "max_height_mm",
                         "min_ground_clearance_mm", "min_le_radius_mm",
                         "min_te_thickness_mm"];

/* Built-in envelopes taken from the FSAE 2027 rules PUBLIC-COMMENT DRAFT
   (version 0.0, 21 July 2026), which states on its face that it is not valid
   for competition — the numbers will move before V1. They live outside the
   user's preset library and cannot be overwritten or deleted, so the offered
   numbers stay traceable to a document. Only section-computable limits are
   here: endplate edge radii, plan-view keep-outs, span limits and mount rules
   are real rules a 2D section cannot decide. */
const BUILTIN_RULE_PRESETS = [
  {
    name: "FSAE 2027 draft - outboard/tip station",
    envelope: {
      max_length_mm: 625, max_height_mm: 250,
      min_le_radius_mm: 5.0, le_radius_scope: "frontmost",
    },
    note: "Outboard/tip station, FSAE 2027 draft. Height is capped at 250 mm "
        + "forward of the front axle centreline and outboard of the front "
        + "tires (T.7.7.1.c). The 625 mm length is a rules constant, not a "
        + "guess: nothing may sit more than 700 mm ahead of the front tires "
        + "(T.7.5.a) and a 75 mm keep-out runs forward of the tire outer "
        + "diameter in side view (V.1.1.c), so 700 − 75 = 625 mm of chordwise "
        + "room is left whatever tire you run. The 5 mm nose radius is the "
        + "horizontal-edge figure from T.7.1.4.",
  },
  {
    name: "FSAE 2027 draft - centre station",
    envelope: {
      max_height_mm: 500,
      min_le_radius_mm: 5.0, le_radius_scope: "frontmost",
    },
    note: "Centre station, FSAE 2027 draft. Height is capped at 500 mm "
        + "outside the rear aero zone (T.7.7.1.b). Length is deliberately "
        + "left blank: the 700 mm forward limit (T.7.5.a) is measured from "
        + "the fronts of the front tires, not from the nose, so how much "
        + "chord the centre section gets depends on where your nose sits "
        + "relative to the front axle — measure it on your car and enter it. "
        + "The 5 mm nose radius is the horizontal-edge figure from T.7.1.4.",
  },
];

/* name match is case-insensitive, matching the server's duplicate check —
   else "fsae 2027 draft - centre station" would shadow a built-in */
function builtinRulePreset(name) {
  const key = String(name || "").trim().toLowerCase();
  return key ? BUILTIN_RULE_PRESETS.find(
    (p) => p.name.toLowerCase() === key) || null : null;
}

/* A hand edit clears the preset selection, but the numbers on screen are
   still the draft's until they are replaced — and the report is where a
   number gets quoted from, so the caveat has to survive the edit. Provenance
   rides in preset_name (the envelope's only display-only string; the schema
   is strict, so no new key may be invented) with this marker appended. */
const RULE_DRAFT_MARK = " (edited)";

/* the built-in the last applied envelope came from — turning Rules off
   deletes the envelope while the numbers stay in the form, and the caveat
   must not be what the toggle drops */
let ruleDraftLast = null;

/* Which built-in draft an envelope's numbers came from, if any: the preset
   itself, an edited copy of it, or a user preset saved from one. */
function ruleDraftSource(env) {
  const name = String(env?.preset_name || "");
  const marked = (n) => (n.endsWith(RULE_DRAFT_MARK)
    ? builtinRulePreset(n.slice(0, -RULE_DRAFT_MARK.length)) : null);
  const direct = builtinRulePreset(name) || marked(name);
  if (direct) return direct;
  const own = state.rulePresets.find((p) => p.name === name);
  return own ? marked(String(own.envelope?.preset_name || "")) : null;
}

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
    min_le_radius_mm: num("rule-le-radius"),
    le_radius_scope: $("rule-le-scope").value,
    min_te_thickness_mm: num("rule-te-thick"),
    measure_ride_height_mm: num("rule-measure-height"),
  };
  const preset = $("rule-preset").value;
  if (preset) {
    env.preset_name = preset;
  } else {
    // no selection: keep the draft the previous envelope's numbers came from
    const src = ruleDraftSource(state.config.rule_envelope)
      || (ruleDraftLast ? builtinRulePreset(ruleDraftLast) : null);
    if (src) env.preset_name = src.name + RULE_DRAFT_MARK;
  }
  ruleDraftLast = ruleDraftSource(env)?.name ?? null;
  return env;
}

/* one writer for the envelope fields: a preset that leaves a limit out has to
   CLEAR that field, not inherit whatever was typed before it */
function fillRulesForm(env) {
  $("rule-len").value = env.max_length_mm ?? "";
  $("rule-height").value = env.max_height_mm ?? "";
  $("rule-clear").value = env.min_ground_clearance_mm ?? "";
  $("rule-xoff").value = env.x_offset_mm ?? 0;
  $("rule-le-radius").value = env.min_le_radius_mm ?? "";
  $("rule-le-scope").value = env.le_radius_scope || "frontmost";
  $("rule-te-thick").value = env.min_te_thickness_mm ?? "";
  $("rule-measure-height").value = env.measure_ride_height_mm ?? "";
}

/* a built-in preset's note says where its numbers come from — the centre
   station's blank length is a fact about how the rule is measured, not an
   omission, and the user has to see that before trusting the preset */
function renderRulePresetNote() {
  const sel = builtinRulePreset($("rule-preset").value);
  // an edited or re-saved copy still shows the sourcing, marked as derived:
  // the numbers may have moved away from the ones the note describes
  const p = sel || ruleDraftSource(state.config.rule_envelope);
  const n = $("rule-preset-note");
  n.textContent = !p ? ""
    : sel ? p.note
      : `Derived from a built-in draft preset — these limits may have been `
        + `edited since. ${p.note}`;
  n.hidden = !p;
}

/* one writer for the selection: assigning .value fires no change event, so
   the note and the Save/Delete buttons would otherwise describe a preset
   that is no longer selected. A name with no matching option (an edited
   copy's marker) selects "— none —". */
function setRulePreset(name) {
  const sel = $("rule-preset");
  sel.value = [...sel.options].some((o) => o.value === name) ? name : "";
  renderRulePresetNote();
  syncRulePresetButtons();
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
  for (const id of ["rule-len", "rule-height", "rule-clear", "rule-xoff",
                    "rule-le-radius", "rule-le-scope", "rule-te-thick",
                    "rule-measure-height"]) {
    $(id).addEventListener("input", () => {
      if (!$("rules-on").checked) return;
      // hand edits leave the preset behind, but not where its numbers came
      // from — readRulesForm carries that over from the applied envelope
      $("rule-preset").value = "";
      state.config.rule_envelope = readRulesForm();
      setRulePreset("");
      onConfigChanged();
    });
  }
  $("rule-preset").addEventListener("change", () => {
    const name = $("rule-preset").value;
    const builtin = builtinRulePreset(name);
    const p = builtin || state.rulePresets.find((x) => x.name === name);
    if (p) {
      fillRulesForm(p.envelope);
      // a built-in name may not be saved over, so don't pre-load it into the
      // Save as field — the user names their own copy
      $("rule-preset-name").value = builtin ? "" : name;
    }
    // the note reads the applied envelope, so apply first
    if ($("rules-on").checked) state.config.rule_envelope = readRulesForm();
    renderRulePresetNote();
    if (!$("rules-on").checked) return;
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
  const clash = !!builtinRulePreset(name);
  $("rule-preset-save").disabled = !name || clash;
  $("rule-preset-save").title = clash
    ? "That name belongs to a built-in draft preset — those stay as the "
      + "document wrote them. Save your version under a different name."
    : name
      ? "Save the current limits as a named preset"
      : "Give the preset a name first (the Save as field).";
  const sel = $("rule-preset").value;
  const isBuiltin = !!builtinRulePreset(sel);
  $("rule-preset-del").disabled = !sel || isBuiltin;
  $("rule-preset-del").title = isBuiltin
    ? "Built-in draft presets can't be deleted."
    : sel
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
  const group = (label) => {
    const g = document.createElement("optgroup");
    g.label = label;
    sel.appendChild(g);
    return g;
  };
  // built-ins in their own group: the user's library is theirs, these are
  // quotations from a draft document and are labelled as such
  const gb = group("Built in — FSAE 2027 draft (not valid for competition)");
  for (const p of BUILTIN_RULE_PRESETS) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.name;
    gb.appendChild(o);
  }
  // a library entry carrying a built-in's name (written by an older build or
  // copied between machines) would emit the same option value twice, and the
  // built-in always wins the lookup — so it would load numbers other than the
  // ones saved under that name, and could not be deleted. Never offer it.
  const own = state.rulePresets.filter((p) => !builtinRulePreset(p.name));
  if (own.length) {
    const gu = group("Saved on this machine");
    for (const p of own) {
      const o = document.createElement("option");
      o.value = p.name;
      o.textContent = p.name;   // textContent: preset names are user data
      gu.appendChild(o);
    }
  }
  sel.value = (builtinRulePreset(cur)
               || own.some((p) => p.name === cur)) ? cur : "";
  renderRulePresetNote();
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
  if (builtinRulePreset(name)) {
    toast(`"${name}" is a built-in draft preset — save your version under a `
          + `different name.`);
    return;
  }
  const env = readRulesForm();
  // a preset's identity is its own name; the only thing worth keeping out of
  // preset_name is which built-in draft the numbers came from, so a copy
  // still carries the draft caveat into the report
  const src = ruleDraftSource(env);
  delete env.preset_name;
  if (src) env.preset_name = src.name + RULE_DRAFT_MARK;
  // any one limit makes a rule set: an envelope of nothing but an edge rule
  // (a nose radius, a trailing-edge floor) is a perfectly real preset
  if (!RULE_LIMIT_KEYS.some((k) => env[k] != null)) {
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
    setRulePreset(name);
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
  if (builtinRulePreset(name)) {
    toast("Built-in draft presets can't be deleted.");
    return;
  }
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
  const add = (txt, cls = "", title = "") => {
    const b = document.createElement("span");
    b.className = `badge ${cls}`;
    b.textContent = txt;
    if (title) b.title = title;
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
  // per-element rule verdicts: a missing key means the check does not reach
  // this element (radius scope "frontmost"), which is not a pass to show
  const ru = ge?.rules;
  if (ru) {
    if ("le_radius_ok" in ru) {
      // an unmeasurable nose is unknown, not compliant — amber, never green
      add(ru.le_radius_mm == null
            ? "LE radius ?" : `LE radius ${ru.le_radius_mm} mm`,
          ru.le_radius_ok === false ? "crit"
            : ru.le_radius_ok == null ? "warn" : "");
    }
    // advisory, not a violation: the rule is met, the section pays for it.
    // The badge is always on screen while the prose is only in the analysis
    // warnings, so it carries its own explanation
    if (ru.radius_cost_flag) {
      add("nose cost", "warn",
          "Advisory, not a violation: the required leading-edge radius is "
          + "more than 5% of this element's chord, so meeting it costs "
          + "suction peak and stall margin on this element.");
    }
    if (ru.te_thickness_mm != null) {
      add(`TE rule ${ru.te_thickness_mm} mm`, ru.te_ok ? "" : "crit");
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
    fl2d: fl2dResult,
    fl2d_stale: !!fl2dResult && fl2dRev != null && configRevision > fl2dRev,
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
  // the y+ ~ 1 recipe is derived from the operating point — what a blank
  // ANSYS override would inherit moves with the configuration
  syncAnsysPlaceholders();
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
  setBtn($("btn-export-fluent2d"));
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
  // same contract for a Fluent 2D result: it describes the design it solved
  if (fl2dResult && fl2dRev != null && configRevision > fl2dRev) {
    $("fl2d-stale").textContent = "configuration changed since this run — " +
      "the numbers below describe the earlier design; re-run";
    $("fl2d-stale").hidden = false;
    const kg2 = document.getElementById("btn-fl2d-apply-kg");
    if (kg2) {
      kg2.disabled = true;
      kg2.title = "Calibrated on an earlier configuration — re-run first.";
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
  // a "≥" profile figure means some element operates past its pre-stall
  // polar, so the lookup is a floor — the number is honest, not precise
  const dragLB = !!f.drag_profile_is_lower_bound;
  $("r-drag").textContent = `${dragLB ? "≥ " : ""}${fmtN(f.drag_total_n, 1)} N`;
  $("r-drag-split").textContent =
    `${fmtN(f.drag_induced_n, 1)} induced · ` +
    `${dragLB ? "≥" : ""}${fmtN(f.drag_profile_n, 1)} profile`;
  $("drag-stat").title = (f.induced_model
    ? `CDi ${f.induced_model.CDi} at wing CL ${f.induced_model.CL_wing} · ` +
      `AR ${f.induced_model.AR} · ground factor ` +
      `${f.induced_model.ground_factor_phi}`
    : "") + (dragLB
    ? `\nProfile drag is a lower bound: ` +
      `${(f.drag_capped_roles || []).join(", ")} operate past the ` +
      `pre-stall polar the drag lookup reads from — verify drag with RANS ` +
      `before trading on it.`
    : "");
  $("r-ld").textContent = (dragLB ? "≤ " : "") + fmtN(f.efficiency_ld, 1);
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
    // wake-shadow line (downstream elements only) — it does NOT fit on
    // the loading subline: that line clears the 297px column by ~6 chars
    // (the round-4 wrap fix), so the shadow gets its own muted subline
    // in the same voice. Measured separation line 0.50·V∞, caution band
    // to 0.53; the analysis warning carries the full story when hot.
    const shadow = e.shadow_min == null ? "" :
      `<div class="load-sub"><span class="gload` +
      `${e.shadow_status !== "ok" ? " warn" : ""}" ` +
      `title="wake-shadow: the stream over this element's upper side ` +
      `bottoms at ${fmtN(e.shadow_min, 2)}·V∞ (measured separation line ` +
      `0.50, caution band to 0.53)` +
      (e.shadow_knife_edge ? ` — knife-edge: within ±0.03 of the ` +
        `cutoffs, so which side of the line it lands on is ` +
        `calibration-band luck` : ``) +
      `">wake-shadow ${fmtN(e.shadow_min, 2)}` +
      (e.shadow_status === "collapse" ? " — collapse"
        : e.shadow_status === "warn" ? " — gray band" : "") +
      (e.shadow_knife_edge ? " · knife-edge" : "") +
      `</span></div>`;
    // the element label + loading % on one clean line; the detailed
    // coefficients on a muted subline below so nothing wraps mid-metric
    row.innerHTML =
      `<div class="load-head"><span>E${i + 1} ${esc(e.role)}</span>` +
      `<span>${(frac * 100).toFixed(0)}%</span></div>` +
      `<div class="load-sub">Cl ${fmtN(e.Cl_checked, 2)}/` +
      `${fmtN(e.CL_max_isolated, 2)} · gnd ×${fmtN(e.ground_multiplier, 1)}` +
      `${gload}</div>${shadow}` +
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
    // a rule violation is not a soft note — the part is not legal as drawn,
    // while the radius-cost and unmeasurable-nose lines are advisories
    const crit = /separation|intersect|choke/.test(msg) || msg.startsWith("Rule '");
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
  ransFlow.reset();   // id, cached images, objectURL and the animation
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
  // Fluent 2D
  fl2dResult = null;
  fl2dRev = null;
  fl2dCEst = null;
  fl2dLast = null;
  fl2dAdoptedLive = false;
  fl2dFlow.reset();   // id, cached images, objectURL and the animation
  $("fl2d-conv").innerHTML = "";
  $("fl2d-charts").classList.add("empty");
  $("fl2d-result").innerHTML =
    '<div class="empty-note">No run yet. Set up a section, then Run ' +
    'Fluent 2D — meshing in ANSYS Workbench takes several minutes before ' +
    'the solve starts.</div>';
  $("fl2d-flow").hidden = true;
  $("fl2d-stale").hidden = true;
  $("fl2d-progress").style.width = "0%";
  $("fl2d-progress").classList.remove("done");
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
    // charts drawn while this tab was hidden baked the 480px fallback into
    // their viewBox; replay them now that the hosts have real widths
    redrawStaleCharts();
    if (t.dataset.tab === "polars") renderPolars();
    if (t.dataset.tab === "screener") prefillScreener();
    if (t.dataset.tab === "rans") refreshRansAvailability();
    if (t.dataset.tab === "fluent2d") refreshFl2dAvailability();
    if (t.dataset.tab === "optimizer") reattachOptimizer();
    // a flow animation owns a rAF loop, a resize observer and megabytes of
    // field arrays, none of which a merely hidden panel releases — leaving
    // the tab destroys the mount, returning rebuilds it
    FLOW_VIEWS.forEach(v =>
      v.tab === t.dataset.tab ? v.resume() : v.suspend());
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
  // finished results carry resolved colors too; a live optimizer poll rewrites
  // its charts every tick, so only a settled result needs the re-ink here
  if (state.optResult && !state.optJob) renderOptimizer(state.optResult);
  if (state.ransResult) renderRans(state.ransResult);
  const active = document.querySelector(".tab.active");
  if (!active) return;
  if (active.dataset.tab === "polars") renderPolars();
  if (active.dataset.tab === "optimizer") reattachOptimizer();
  if (active.dataset.tab === "fluent2d" && fl2dLast) renderFl2d(fl2dLast);
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
  "opt-drag-k", "opt-minld", "opt-minconf", "ov-aoa", "ov-defl", "ov-pos",
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
  // the panel asks for the physical exchange rate k (newtons of downforce
  // traded per newton of drag); the objective's stored weight is w = 6k,
  // because its terms are 60*(1 - D/scale) and 10w*drag/scale, so a neutral
  // trade sits at dD/ddrag = 10w/60. drag_weight stays the wire field, so
  // saved automations and older sessions keep working unchanged.
  //
  // The rate drives MAX mode, where balance was missing — the old 0.10
  // default made drag cost 1/60 N of downforce, pure maximization in
  // practice. TARGET mode keeps the legacy weight: it balances by
  // construction (attain the level, then the descend phase spends the
  // remaining freedom on drag at that level), and rate-scale drag pressure
  // there fights the target spring and pulls designs off the level the
  // user explicitly asked for (measured: the target suites miss).
  const dragK = parseFloat($("opt-drag-k").value);
  const isMax = $("opt-objective").value === "max_downforce";
  const dragW = isMax ? (Number.isFinite(dragK) ? 6 * dragK : 1.5) : 0.1;
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
    // 0 is a legal rate ("ignore drag") — don't || it away
    drag_weight: dragW,
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
      || f.shadow_collapse || (f.frac_max ?? 0) > 0.9;
  };
  // if anything is about to draw, the placeholder must be gone first — a
  // host that is display:none (under .empty) can't measure its own width
  if ((s.pareto && s.pareto.length) || (s.cloud && s.cloud.length)) {
    $("opt-charts").classList.remove("empty");
  }
  if (s.pareto && s.pareto.length) {
    // finalized front: full-fidelity numbers, clickable, trust-colored
    const pts = s.pareto.slice().sort((a, b) => a.drag_n - b.drag_n);
    // where the CHOSEN exchange rate lands on the front: the point
    // maximizing D − k·drag. This is the picture of what the rate means —
    // moving the k field moves this marker along the front, so the trade
    // is chosen by eye instead of by faith in a weight.
    const kNow = parseFloat($("opt-drag-k").value);
    let kBest = -1;
    if (Number.isFinite(kNow)) {
      let bestV = -Infinity;
      pts.forEach((p, i) => {
        const v = p.downforce_n - kNow * p.drag_n;
        if (v > bestV) { bestV = v; kBest = i; }
      });
    }
    lineChart(host, {
      series: [{
        name: "front", color: SERIES[0], markers: "only",
        x: pts.map(p => p.drag_n), y: pts.map(p => p.downforce_n),
        pointColors: pts.map((p, i) => (i === kBest
          ? ink("--viz-pick", "#3ecf8e")
          : flagged(p) ? ink("--viz-flag", "#f2b544") : SERIES[0])),
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
      "carry a trust flag; the green point is where your drag exchange " +
      "rate lands on this front (max of D − k·drag). Click a front point " +
      "to apply that design.";
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
    // "MCxRANKS" -> concurrent solves x MPI ranks per solve; the serial
    // default 1x1 reproduces the old sequential queue exactly
    const [mc, ranks] = ($("rr-parallel").value || "1x1")
      .split("x").map((v) => parseInt(v, 10) || 1);
    await api.ransQueueStart(items, $("rr-mesh").value, 10000, ranks, mc);
    $("btn-rerank-cancel").disabled = false;
    $("rr-mesh").disabled = true;
    $("rr-parallel").disabled = true;
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
    $("rr-parallel").disabled = false;
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
  for (const h of ["#", "design", "panel N", "RANS N", "Δ%", "flow",
                   "verdict", "state", ""]) {
    const th = document.createElement("th");
    th.textContent = h;
    head.appendChild(th);
  }
  for (const r of rows) {
    const tr = tbl.insertRow();
    if (!r.converged) tr.className = "dim";
    // measured attachment, compact (full per-element verdict as tooltip);
    // a separated row is demoted in rank by the server for the same reason
    // wall channel first; the adopted field verdict covers engines that
    // write no wall shear ("*" marks the field channel, which under-reads)
    const fv = r.field_verdict;
    const flow = r.worst_reversed != null
      ? (r.worst_reversed > 0.20 ? "separated"
        : r.worst_reversed > 0.10 ? "partial" : "attached")
      : fv ? (fv.separated ? "separated*"
        : fv.knife_edge ? "knife*" : "no evidence*")
      : "–";
    const cells = [
      r.rank ?? "–", r.label,
      r.panel_downforce_n != null ? fmtN(r.panel_downforce_n, 0) : "–",
      r.rans_downforce_n != null ? fmtN(r.rans_downforce_n, 0) : "–",
      r.delta_cl_pct != null
        ? `${r.delta_cl_pct > 0 ? "+" : ""}${fmtN(r.delta_cl_pct, 1)}` : "–",
      flow,
      // the caution covers every mesh below fine, so name the one the
      // queue actually ran rather than assuming coarse
      (r.verdict || "–") + (r.mesh_caution
        ? (q.mesh_size ? ` · ${q.mesh_size} mesh` : " · below calibration grade")
        : ""),
      r.state + (r.error ? ` (${String(r.error).slice(0, 60)})` : ""),
    ];
    for (const c of cells) {
      const td = tr.insertCell();
      td.textContent = String(c);   // labels/errors are data, not markup
    }
    const fd = tr.cells[5];
    if (r.wall_verdict) fd.title = r.wall_verdict;
    else if (fv) fd.title = fv.verdict;
    if (flow.startsWith("separated")) fd.style.color = "var(--warning)";
    else if (flow === "attached") fd.style.color = "var(--good)";
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
    const vd = tr.cells[6];
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
  if (s.shadow_note === "baseline_below_sep") {
    const d = document.createElement("div");
    d.className = "warning-item";
    d.textContent = "The starting design already collapses the wake-shadow " +
      "screen (an element's top-side stream below the measured 0.50 " +
      "separation line — the flow state behind the recorded 22–40% " +
      "reversed-flow RANS cases). " +
      (s.objective === "max_downforce"
        ? "Maximize mode will not follow it there, and candidates that " +
          "stay collapsed are dropped at full fidelity."
        : "The search is only charged for pushing deeper; expect the " +
          "candidates to carry the separation-risk badge until the " +
          "shadowing is opened up.");
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
        `workable floor with no overlap tuck — the corner the optimizer ` +
        `gravitates to, and the geometry every observed detachment ` +
        `failure wore. Fine-mesh truth: essentially exact at moderate ` +
        `loading, but optimism climbs steeply with loading. Watch the ` +
        `loading meters. See the analysis warning.">slot corner</span>`
        : "") +
      (f.past_load_band ? `<span class="badge warn" title="This design ` +
        `sits outside the loading band the search treats as clean — it is ` +
        `carried past the band rather than inside it, so its estimate is ` +
        `the optimistic end of the model. Verify with RANS before ` +
        `committing to it.">past load band</span>` : "") +
      (f.low_confidence ? `<span class="badge warn" title="NeuralFoil ` +
        `confidence is below 50% on at least one section — the viscous ` +
        `data behind this design is an extrapolation, not a prediction. ` +
        `Verify with RANS before trusting it.">low confidence</span>` : "") +
      (f.near_stall ? `<span class="badge warn" title="A loaded element ` +
        `runs at the edge of its viscous data: its drag lookup is capped ` +
        `at the pre-stall polar (drag understated), or its polar never ` +
        `stalled in the analyzed range (CL_max is a lower bound, not a ` +
        `stall). Verify with RANS.">near stall</span>` : "") +
      (f.shadow_collapse ? `<span class="badge warn" title="Wake-shadow ` +
        `collapse: the stream over an element's upper side bottoms below ` +
        `0.50·V∞ — every wall-shear-graded element on record at this ` +
        `level measured 22–40% reversed flow in RANS. The flow will not ` +
        `reach that element's trailing edge; see the analysis warning.` +
        `">separation risk</span>`
        : f.shadow_warn ? `<span class="badge warn" title="Wake-shadow ` +
        `gray band (0.50–0.53·V∞): between the measured attached and ` +
        `separated classes — the recorded flagged-class flaps sit here. ` +
        `Verify with RANS.">shadow gray band</span>` : "") +
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

/* the single verify run, the re-rank queue and the Fluent 2D tab share one
   solver registry — reflect the mutex on every start button instead of
   letting a click 409 */

/* a live run discovered in the registry but owned by the OTHER engine's
   tab: rediscovery deliberately never adopts across engines (Cancel would
   kill a job under its own tab), yet the start buttons must still show
   the busy solver ("fluent2d" | "rans" | null). Each tab's availability
   probe overwrites this from what the registry actually holds. */
let solverForeignBusy = null;

function foreignBusyTitle() {
  return solverForeignBusy === "fluent2d"
    ? "A Fluent 2D run started in another window or session is using the " +
      "solver — the Fluent 2D tab re-attaches to it."
    : "A solver run started in another window or session is using the " +
      "solver — the RANS verify tab re-attaches to it.";
}

function updateSolverButtons() {
  const runBtn = $("btn-rans-run");
  if (state.queueActive) {
    runBtn.disabled = true;
    runBtn.title = "The optimizer's re-rank queue is using the solver — " +
                   "wait for it or cancel it from the Optimizer tab.";
  } else if (fl2dJob) {
    runBtn.disabled = true;
    runBtn.title = "A Fluent 2D run is using the solver — wait for it or " +
                   "cancel it in the Fluent 2D tab.";
  } else if (solverForeignBusy) {
    runBtn.disabled = true;
    runBtn.title = foreignBusyTitle();
  } else if (ransAvail?.available && !state.ransJob) {
    runBtn.disabled = false;
    runBtn.title = "";
  }
  gateRerank();
  gateFl2dRun();
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
  if (fl2dJob) {
    btn.disabled = true;
    btn.title = "A Fluent 2D run is using the solver — wait for it or " +
                "cancel it in the Fluent 2D tab.";
    return;
  }
  if (solverForeignBusy) {
    btn.disabled = true;
    btn.title = foreignBusyTitle();
    return;
  }
  const a = await checkRansAvailable();
  // the world may have moved while the probe ran — never enable against
  // stale pre-await state
  if (btn.classList.contains("busy") || state.queueActive || state.ransJob
      || fl2dJob || solverForeignBusy) {
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
  btn.title = "Runs the shortlist through the 2D RANS truth case — one at " +
              "a time by default, or several solves at once with a " +
              "parallel option — then re-ranks on the measured numbers: " +
              "by drag among the designs that hit the target, or by " +
              "downforce in maximum-downforce mode.";
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
      // the Fluent 2D tab's jobs live in the same registry — adopting one
      // here would let this tab's Cancel kill it under that tab, so probe
      // the engine before claiming anything
      const s = cur.job_id && !queueOwned
        ? await api.ransStatus(cur.job_id) : null;
      const fl2dOwned = s?.engine === "fluent2d";
      // a live Fluent 2D run is not adopted here, but the Run button must
      // still reflect the busy solver instead of 409ing on click
      solverForeignBusy = fl2dOwned
        && ["pending", "running"].includes(s.state) ? "fluent2d" : null;
      if (s && !fl2dOwned && ["pending", "running"].includes(cur.state)) {
        state.ransJob = cur.job_id;
        $("btn-rans-run").disabled = true;
        $("btn-rans-cancel").disabled = false;
        $("rans-stale").hidden = true;
        // hiding the panel does not stop the animation's rAF loop, and the
        // saved images predate this run
        ransFlow.reset();
        $("rans-result").innerHTML =
          '<div class="empty-note">Re-attached to a running verification…</div>';
        pollRans();
      } else if (s && !fl2dOwned && cur.state === "done"
                 && state.ransResult?.id !== cur.job_id) {
        state.ransResult = s;
        state.ransRev = null;   // solved before this page session
        renderRans(s);
        renderRansResult(s, { provenance: "reattached" });
        ransFlow.reset();   // saved images predate the run being adopted
        ransFlow.show(s.id);
      } else if (cur.job_id && queueOwned && cur.state === "done"
                 && !state.ransResult) {
        $("rans-status").textContent = "idle — the last solver run belonged " +
          "to the optimizer's re-rank queue (see the Optimizer tab). Verify " +
          "the current configuration with the button above.";
      }
    } catch { /* rediscovery is best-effort */ }
  }
  await checkRansAvailable();
  applyRansAvailabilityNote();
}

/* the OpenFOAM truth case runs in a container — this tab needs Docker */
function applyRansAvailabilityNote() {
  const note = $("rans-note");
  const a = ransAvail || { available: false };
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
  if (state.ransJob || state.queueActive || fl2dJob || solverForeignBusy) {
    toast("The solver is already busy — wait for the current run or the " +
          "re-rank queue to finish.", "info");
    return;
  }
  const iters = parseInt($("rans-iters").value, 10);
  $("btn-rans-run").disabled = true;   // close the window before the await
  try {
    const { job_id } = await api.ransStart(
      state.config, $("rans-mesh").value,
      Number.isFinite(iters) ? Math.min(Math.max(iters, 100), 20000) : 10000,
      parseInt($("rans-ranks").value, 10) || 1,
      "openfoam");
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
    $("rans-ranks").disabled = true;
    $("rans-stale").hidden = true;
    $("rans-conv").innerHTML = "";   // previous run's chart is not this run
    $("rans-charts").classList.add("empty");   // until this run's history draws
    // hiding the panel does not stop the animation's rAF loop, and the
    // saved images predate this run
    ransFlow.reset();
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
    $("rans-ranks").disabled = false;
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
      const stopBtn = $("btn-rans-stop");
      stopBtn.disabled = !(state.ransJob && s.state === "running"
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
        $("rans-ranks").disabled = false;
        updateSolverButtons();
        if (s.state === "failed") {
          toast("RANS verification failed — details in the RANS verify tab.", "err");
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
          ransFlow.show(s.id);
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
      $("rans-ranks").disabled = false;
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
  // n_cells can legitimately be null (count not parsed yet) — +null
  // coerces to 0 and a phantom "0 cells" reads as a stuck mesher
  if (s.mesh && s.mesh.n_cells != null
      && Number.isFinite(+s.mesh.n_cells) && +s.mesh.n_cells > 0) {
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
    ["Sectional Cl — RANS", `${numf(r.cl_rans, 3)} ± ${numf(r.cl_rans_std, 3)}`
      + (r.delta_cl_provisional ? " (provisional)" : "")],
    ["Sectional Cl — panel C_est", p ? numf(p.c_est, 3) : "–"],
    ["Cl delta (RANS vs estimate)", pct(r.delta_cl_pct)
      + (r.delta_cl_provisional && r.delta_cl_pct != null ? " *" : "")],
    ["Profile Cd — RANS", numf(r.cd_rans, 4)],
    ["Profile Cd — panel stack", p ? numf(p.cd_profile, 4) : "–"],
    ["Downforce at RANS Cl", `${esc(r.downforce_n_at_rans_cl)} N`],
    ["Downforce — panel estimate", p ? `${esc(p.downforce_n)} N` : "–"],
    ["Iterations", `${esc(r.n_iters_run)} (${esc(r.stop_reason)}; ` +
      `tail mean of ${esc(r.tail_rows)})`],
  ];
  // measured wall state: the sanity check a bare Cl cannot give — an
  // attached solution's high Cl is the model's answer, a separated one's
  // steady verdict is a band, and either way the user should see which
  if (r.wall_verdict) {
    rows.push(["Attachment (wall shear)", esc(r.wall_verdict)]);
  }
  // the field channel beside the wall channel; wall outranks it, but a
  // census-mode hit can fire where wall faces read clean (off-body flow)
  if (r.field_verdict) {
    rows.push(["Attachment (field)", esc(r.field_verdict.verdict)]);
  }
  if (r.wall_report && typeof r.wall_report.yplus === "object"
      && r.wall_report.yplus) {
    const yp = Object.entries(r.wall_report.yplus)
      .filter(([k, v]) => k.startsWith("wing_") && v
        && typeof v === "object")
      .map(([k, v]) => `${esc(k.replace("wing_", ""))} ${numf(v.avg, 1)}` +
        ` (max ${numf(v.max, 0)})`);
    if (yp.length) rows.push(["Measured y+ (avg)", yp.join(" · ")]);
  }
  host.innerHTML = rows.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><b>${v}</b></div>`).join("");
  if (r.cl_trend_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = r.cl_trend_note;
    host.appendChild(w);
  }
  if (r.estimate_scope_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = `Estimate scope: ${r.estimate_scope_note}`;
    host.appendChild(w);
  }
  if (r.engine_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = r.engine_note;
    host.appendChild(w);
  }
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
    // the solver also exits below the cap without settling, and only the
    // cap case is cured by a bigger iteration budget
    const capped = String(r.stop_reason || "").startsWith("iteration cap");
    w.textContent = `NOT CONVERGED — ` +
      `${r.stop_reason || "the force history had not settled when the run ended"}` +
      (r.cl_drift != null ? ` (Cl drift ${(r.cl_drift * 100).toFixed(1)}% ` +
        `per window)` : ``) +
      `. The numbers above are a mid-transient snapshot, not a result: ` +
      (capped
        ? `raise Max iterations and rerun.`
        : `the solver quit before the iteration cap, so a bigger cap alone ` +
          `will not fix it — rerun, and step up the mesh if it exits early ` +
          `again.`) +
      ` No k_g calibration is offered from an unconverged run.`;
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
    w.textContent = "Below calibration grade: only the fine mesh is " +
      "calibrated against the campaign's truth cases. On the two-element " +
      "baseline the coarse mesh read validated operating points 22–35% " +
      "below fine-mesh truth at racing and mid ride heights (it " +
      "under-resolved the venturi gap), and medium scattered -2 to -20% " +
      "across tiny geometry changes at racing height. That bias is " +
      "configuration-dependent — it did not reproduce on a three-element " +
      "case — and predates the slot-throat refinement, so its size here is " +
      "unknown rather than known. Treat this run as screening; re-run on " +
      "fine before trusting the delta or pinning k_g from it.";
    host.appendChild(w);
  }
  if (r.suggested_k_g != null) {
    const d = document.createElement("div");
    d.className = "kv";
    d.innerHTML = `<span title="Pinning the ground-gain factor to this ` +
      `value makes the studio estimate reproduce the RANS sectional load ` +
      `at this operating point.">suggested k<sub>g</sub></span>
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
    here. Case retained at <code>${esc(r.case_dir)}</code>
    (fields, logs, ParaView-openable <code>case.foam</code>).`;
  host.appendChild(note);
}

/* flow-field view: the solved section rendered server-side from the final
   fields, in the same as-driven orientation as the drawing.

   ONE factory drives both engines' panels. /api/rans/{id}/flow and its
   flowfield sibling read the finished job's case directory, not whatever
   engine filled it, and a Fluent 2D run writes its solved field out in
   the same OpenFOAM form — so the two panels differ in their id prefix
   and the wording of their captions, nothing else. Each instance owns
   its own job id, image cache, request sequence and animation mount. */
const FLOW_VIEWS = [];

/* Both engines solve a STEADY field and both animations trace paths through
   it — neither steps a flow forward in time, and a run stopped by hand is a
   partial field, not a moment in one. One string, so the two captions cannot
   drift apart on how honest they are. */
const FLOW_ANIM_LEAD = "particles traced through the converged steady field, "
                     + "a path picture rather than a time-accurate simulation";

function makeFlowView(cfg) {
  const el = (part) => $(`${cfg.prefix}-${part}`);
  const view = {
    tab: cfg.tab,
    id: null,        // the live job behind the panel; null when restored
    field: "umag",   // which view is currently shown
    url: null,       // objectURL of the currently shown image
    cache: null,     // {umag?, cp?} dataURLs restored from a project file
    anim: null,      // {ctrl, jobId, styleKey, bg, detail} — the animation
                     // and the detail window (if any) it is sampling
  };
  let seq = 0, lodSeq = 0;
  // assigned by the static zoom/pan block below; a zoom belongs to the
  // picture it was made on, so anything that swaps the picture clears it
  let resetTransform = () => {};

  /* the contour-dialog knobs (theme, colormap, range clamp, streamlines)
     as fetch params — the same settings the manual GUI offers */
  function settings() {
    const theme = document.documentElement.dataset.theme === "light"
      ? "light" : "dark";
    const cmap = el("flow-cmap").value || "auto";
    const vmaxRaw = parseFloat(el("flow-vmax").value);
    const vmax = Number.isFinite(vmaxRaw) && vmaxRaw > 0 ? vmaxRaw : null;
    return { theme, cmap, vmax,
             streamlines: el("flow-streams").checked,
             extent: el("flow-extent").value || "section" };
  }

  function query(field) {
    const s = settings();
    let q = `&theme=${s.theme}&cmap=${s.cmap}` +
            `&streamlines=${s.streamlines}&extent=${s.extent}`;
    if (s.vmax != null) {
      q += `&vmax=${s.vmax}`;
      if (field === "cp") q += `&vmin=${-s.vmax}`;   // symmetric clamp
    }
    return q;
  }

  function teardownAnim() {
    if (view.anim?.ctrl) view.anim.ctrl.destroy();
    view.anim = null;
  }

  /* the caption states what the particles are and how far playback is
     slowed — cfg.animLead carries the engine's own honest phrasing */
  const animNote = () =>
    `${cfg.animLead} — playback 1/${el("anim-speed").value}× real ` +
    `time · Ctrl+wheel zooms, drag pans, double-click resets`;

  /* level-of-detail: once the visible window is a fraction of the base
     field AND the grid is visibly coarse on screen, refetch just that
     window at full grid resolution — zooming never runs out of pixels */
  async function lod(v) {
    if (!view.anim) return;
    // a settle scheduled by the last gesture still fires after a switch to a
    // static view — refining (and re-captioning) a hidden animation is waste
    if (!el("flow-anim").classList.contains("active")) return;
    const { ctrl, jobId } = view.anim;
    if (!jobId) return;
    const base = ctrl.baseData;
    const [bx0, by0, bx1, by1] = v.bbox;
    const dpr = window.devicePixelRatio || 1;
    // pxPerCell arrives measured on the ACTIVE grid, and a detail window is
    // finer than the base at the same zoom — judging coarseness on it would
    // revert every window on the settle right after it was installed. Rescale
    // to the base grid, and keep a window (4) below the level that earns one
    // (7) so refine and revert cannot alternate on consecutive settles.
    const win = view.anim.detail;
    const perBase = win
      ? v.pxPerCell * (win.nx / (win.x1 - win.x0))
                    * ((base.x1 - base.x0) / base.nx)
      : v.pxPerCell;
    const needDetail =
      perBase > (win ? 4 : 7) * dpr
      && (bx1 - bx0) < (base.x1 - base.x0) * 0.8;
    const mySeq = ++lodSeq;
    if (!needDetail) {
      if (!ctrl.isBaseField()) { ctrl.setField(base); view.anim.detail = null; }
      return;
    }
    // the installed window still covers the view at full sharpness: panning
    // inside it needs nothing, and only a deeper zoom or a pan past its edge
    // asks for a new one
    if (win && v.pxPerCell <= 7 * dpr
        && bx0 >= win.x0 && bx1 <= win.x1
        && by0 >= win.y0 && by1 <= win.y1) return;
    const mx = (bx1 - bx0) * 0.25, my = (by1 - by0) * 0.25;
    let q = `x0=${(bx0 - mx).toFixed(5)}&y0=${(by0 - my).toFixed(5)}` +
            `&x1=${(bx1 + mx).toFixed(5)}&y1=${(by1 + my).toFixed(5)}`;
    // a Cp backdrop needs the Cp field in every detail window too
    if (view.anim.bg === "cp") q += "&fields=umag,cp";
    const note = el("flow-note");
    note.textContent = animNote() + " · refining the zoomed view…";
    try {
      const res = await fetch(`/api/rans/${jobId}/flowfield?${q}`);
      if (mySeq !== lodSeq || view.anim?.ctrl !== ctrl) return;
      if (res.ok) {
        // the body is MBs of arrays — a newer window can land while it
        // parses, and a stale detail field must not clobber it
        const data = await res.json();
        if (mySeq !== lodSeq || view.anim?.ctrl !== ctrl) return;
        ctrl.setField(data);
        view.anim.detail = data;   // what pxPerCell now measures
      }
    } catch { /* keep the coarse field — zooming still works */ }
    if (mySeq === lodSeq && view.anim?.ctrl === ctrl) {
      note.textContent = animNote();
    }
  }

  async function showAnim(jobId) {
    const mySeq = ++seq;
    const note = el("flow-note");
    const host = el("flow-canvas");
    // nothing to play: leave the static raster and every animation control
    // where they are, so no chrome implies a playable view
    if (!jobId) {
      note.textContent = "the animation reads the live run's velocity " +
        `field, which is not saved with a project — ${cfg.rerun} to animate`;
      return;
    }
    el("flow-img").style.display = "none";
    host.hidden = false;
    el("anim-speed-wrap").hidden = false;
    // streamlines are a static-view overlay — the animation ignores them
    el("flow-streams-wrap").hidden = true;
    el("anim-trail-wrap").hidden = false;
    el("anim-density-wrap").hidden = false;
    el("anim-bg-wrap").hidden = false;
    const s = settings();
    const bg = el("anim-bg").value || "field";
    const styleKey = `${s.theme}|${s.cmap}|${s.vmax}|${s.extent}|${bg}`;
    if (view.anim && view.anim.jobId === jobId
        && view.anim.styleKey === styleKey) {
      view.anim.ctrl.start();
      note.textContent = animNote();
      return;
    }
    teardownAnim();
    note.textContent = "extracting the velocity field…";
    try {
      const fields = bg === "cp" ? "&fields=umag,cp" : "";
      const res = await fetch(
        `/api/rans/${jobId}/flowfield?extent=${s.extent}${fields}`);
      if (mySeq !== seq) return;   // a newer request owns the panel
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        note.textContent = `animation unavailable: ${detail}`;
        return;
      }
      const data = await res.json();
      if (mySeq !== seq) return;
      const ctrl = mountFlowAnim(host, data, {
        slowdown: +el("anim-speed").value,
        theme: s.theme,
        cmap: s.cmap === "auto" ? null : s.cmap,
        vmax: s.vmax,
        trail: el("anim-trail").value,
        density: +el("anim-density").value,
        background: bg,
        onViewSettled: lod,
      });
      view.anim = { ctrl, jobId, styleKey, bg, detail: null };
      ctrl.start();
      note.textContent = animNote();
    } catch (e) {
      if (mySeq === seq) {
        note.textContent = `animation unavailable: ${e.message}`;
      }
    }
  }

  async function show(jobId, field = "umag") {
    // a re-render of the same picture (theme, view settings) keeps the zoom;
    // a different run or a different quantity does not
    if (jobId !== view.id || field !== view.field) resetTransform();
    view.id = jobId;
    view.field = field;
    el("flow").hidden = false;
    el("flow-umag").classList.toggle("active", field === "umag");
    el("flow-cp").classList.toggle("active", field === "cp");
    el("flow-anim").classList.toggle("active", field === "anim");
    if (view.anim && view.anim.jobId !== jobId) teardownAnim();
    if (field === "anim") {
      return showAnim(jobId);
    }
    view.anim?.ctrl.stop();
    el("flow-canvas").hidden = true;
    el("anim-speed-wrap").hidden = true;
    el("anim-trail-wrap").hidden = true;
    el("anim-density-wrap").hidden = true;
    el("anim-bg-wrap").hidden = true;
    el("flow-streams-wrap").hidden = false;
    const mySeq = ++seq;
    const img = el("flow-img");
    img.style.display = "";
    const note = el("flow-note");
    // a restored workspace carries the rendered images, not a live job
    if (!jobId && view.cache) {
      if (view.cache[field]) {
        img.src = view.cache[field];
        img.style.opacity = "";
        note.textContent = "restored from the project file — same view as " +
          "the drawing: as driven, ground at the bottom, flow left to right";
      } else {
        // nothing may stay on screen under the field button just activated
        img.removeAttribute("src");
        img.style.opacity = "";
        resetTransform();
        note.textContent = "this field was not saved with the project — " +
          `${cfg.rerun} to render it`;
      }
      return;
    }
    note.textContent = "rendering the flow field…";
    img.style.opacity = "0.4";
    // fetched (not img.src) so a failure can show the server's actual reason
    try {
      const res = await fetch(
        `/api/rans/${jobId}/flow?field=${field}${query(field)}`);
      if (mySeq !== seq) return;   // a newer request owns the panel
      if (!res.ok) {
        let detail = res.statusText;
        try { detail = (await res.json()).detail || detail; } catch {}
        note.textContent = `flow field unavailable: ${detail}`;
        img.style.opacity = "";
        return;
      }
      // the body still has to stream — a newer field can be requested,
      // land and paint while it does, and must not be clobbered here
      const blob = await res.blob();
      if (mySeq !== seq) return;
      const url = URL.createObjectURL(blob);
      if (view.url) URL.revokeObjectURL(view.url);
      view.url = url;
      img.src = url;
      img.style.opacity = "";
      note.textContent = "same view as the drawing: as driven, ground at " +
        "the bottom, flow left to right · Ctrl+wheel zooms, drag pans, " +
        "double-click resets";
    } catch (e) {
      if (mySeq === seq) {
        note.textContent = `flow field unavailable: ${e.message}`;
        img.style.opacity = "";
      }
    }
  }

  const onActiveTab = () =>
    document.querySelector(".tab.active")?.dataset.tab === cfg.tab;
  let deferred = false;   // a settings change landed while the tab was away

  /* view-settings changes re-render whichever view is showing (the
     animation remounts with the new style; the static views refetch) */
  function refresh() {
    // captured images bake in the view settings — drop the capture cache so
    // the next save re-grabs under the new settings, but only when a live
    // job rendered this panel (view.id): a restored project's images are
    // the only copy, and its result snapshot's id cannot be refetched
    if (view.cache && view.id) {
      view.cache = null;
    }
    if (el("flow").hidden || !(view.id || view.cache)) return;
    // an unlaid-out panel measures zero — the animation would mount into a
    // canvas of no size, so a hidden tab re-renders when it comes back
    if (!onActiveTab()) { deferred = true; return; }
    const field = el("flow-anim").classList.contains("active")
      ? "anim" : view.field;
    show(view.id, field);
  }

  /* one workspace's results — dropping them drops the panel, and the
     animation holds a rAF loop, observers and the field arrays that a
     merely hidden panel would keep alive */
  function reset() {
    view.id = null;
    view.cache = null;
    if (view.url) { URL.revokeObjectURL(view.url); view.url = null; }
    teardownAnim();
    resetTransform();
    el("flow").hidden = true;
  }

  el("flow-umag").addEventListener("click", () =>
    (view.id || view.cache) && show(view.id, "umag"));
  el("flow-cp").addEventListener("click", () =>
    (view.id || view.cache) && show(view.id, "cp"));
  el("flow-anim").addEventListener("click", () =>
    (view.id || view.cache) && show(view.id, "anim"));
  el("anim-speed").addEventListener("change", () => {
    view.anim?.ctrl.setSlowdown(+el("anim-speed").value);
    if (view.anim) el("flow-note").textContent = animNote();
  });
  el("anim-trail").addEventListener("change", () => {
    view.anim?.ctrl.setTrail(el("anim-trail").value);
  });
  el("anim-density").addEventListener("change", () => {
    view.anim?.ctrl.setDensity(+el("anim-density").value);
  });
  // the backdrop changes what the fetch must carry (Cp) — remount
  el("anim-bg").addEventListener("change", refresh);
  el("flow-cmap").addEventListener("change", refresh);
  el("flow-vmax").addEventListener("change", refresh);
  el("flow-streams").addEventListener("change", refresh);
  el("flow-extent").addEventListener("change", refresh);
  // the rendered raster and the animation's chrome both bake the theme
  // in — the animation re-inks by remounting under the new styleKey
  window.addEventListener("wss-themechange", refresh);

  /* static-image zoom/pan: CSS transform on the raster (the animated view
     draws its own transform and handles these gestures itself) */
  {
    const vp = el("flow-viewport"), img = el("flow-img");
    let s = 1, tx = 0, ty = 0, drag = null;
    const apply = () => {
      img.style.transform =
        s === 1 ? "" : `translate(${tx}px,${ty}px) scale(${s})`;
      // capture touch gestures only while zoomed — at s === 1 a finger drag
      // must keep scrolling the page (same contract as the wheel handler)
      vp.style.touchAction = s === 1 ? "" : "none";
    };
    resetTransform = () => { s = 1; tx = 0; ty = 0; apply(); };
    const imgMode = () => el("flow-canvas").hidden && img.src;
    vp.addEventListener("wheel", (e) => {
      if (!imgMode()) return;
      // plain wheel keeps scrolling the page; zoom is Ctrl+wheel so the
      // panel never traps the scroll position
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const r = vp.getBoundingClientRect();
      const ns = Math.min(Math.max(s * Math.exp(-e.deltaY * 0.0016), 1), 12);
      const f = ns / s;
      tx = (e.clientX - r.left) - ((e.clientX - r.left) - tx) * f;
      ty = (e.clientY - r.top) - ((e.clientY - r.top) - ty) * f;
      s = ns;
      if (s === 1) { tx = 0; ty = 0; }
      apply();
    }, { passive: false });
    vp.addEventListener("pointerdown", (e) => {
      if (!imgMode() || s === 1) return;
      drag = { x: e.clientX, y: e.clientY };
      vp.setPointerCapture?.(e.pointerId);
    });
    vp.addEventListener("pointermove", (e) => {
      if (!drag) return;
      tx += e.clientX - drag.x;
      ty += e.clientY - drag.y;
      drag = { x: e.clientX, y: e.clientY };
      apply();
    });
    const end = () => { drag = null; };
    vp.addEventListener("pointerup", end);
    vp.addEventListener("pointercancel", end);
    vp.addEventListener("dblclick", () => {
      if (!imgMode()) return;
      s = 1; tx = 0; ty = 0; apply();
    });
  }

  Object.assign(view, {
    query, show, refresh, reset,
    // leaving the tab destroys the mount: hiding a panel does not stop
    // its rAF loop, release its observer or free the field arrays. The
    // sequence bump discards a fetch still in flight — it would otherwise
    // mount into a display:none host, which measures zero, and start the
    // very loop this releases; resume() then does a real mount instead.
    suspend() { seq++; teardownAnim(); },
    // returning rebuilds the animation from the same fetch that first
    // drew it, and picks up any settings change made while away
    resume() {
      if (el("flow").hidden || !(view.id || view.cache)) return;
      const anim = el("flow-anim").classList.contains("active");
      if (!anim && !deferred) return;
      deferred = false;
      show(view.id, anim ? "anim" : view.field);
    },
  });
  FLOW_VIEWS.push(view);
  return view;
}

const ransFlow = makeFlowView({
  prefix: "rans", tab: "rans", rerun: "re-verify",
  animLead: FLOW_ANIM_LEAD,
});

/* ---------------- Fluent 2D tab ---------------- */

/* The documented manual 2D workflow, automated end to end: profile+domain
   DXF, ANSYS Workbench/Mechanical mesh, true-2D Fluent solve. State lives in
   its own variables — the server registry serializes the actual runs, but
   this tab's job and the RANS verify poll must never cross-wire. */
let fl2dJob = null;
let fl2dPoll = null;
let fl2dAvail = null;
let fl2dCEst = null;          // panel estimate snapshotted at start
let fl2dRev = null;           // configRevision when the run started
let fl2dResult = null;        // terminal status of the last finished run
let fl2dLast = null;          // last rendered snapshot (theme re-ink)
let fl2dConventions = null;   // conventions of the run being polled
let fl2dAdoptedLive = false;  // run adopted mid-flight — start rev unknown

/* ---- the ANSYS settings panel ----
   One table for the override fields: the request key the server
   range-checks, the control that carries it, and how the value parses.
   Readers, writers, the run lock and the preset round-trip all walk this
   list, so a new knob cannot land in one of them and miss another.
   BLANK means "inherit" everywhere — never zero, never the placeholder. */
const FL2D_OVERRIDES = [
  ["edge_size_mm", "fl2d-edge-mm", "float"],
  ["first_layer_mm", "fl2d-first-mm", "float"],
  ["n_layers", "fl2d-layers", "int"],
  ["growth", "fl2d-growth", "float"],
  ["front_l", "fl2d-front-l", "float"],
  ["back_l", "fl2d-back-l", "float"],
  ["top_h", "fl2d-top-h", "float"],
  ["sc_budget_s", "fl2d-sc-budget", "float"],
  ["wb_budget_s", "fl2d-wb-budget", "float"],
];
/* the base recipe plus every override — what a preset stores and what a
   run is started from */
const FL2D_SETTING_IDS = ["fl2d-sizing", "fl2d-conventions", "fl2d-iters",
                          "fl2d-ranks",
                          ...FL2D_OVERRIDES.map(([, id]) => id)];
const FL2D_CONTROLS = [...FL2D_SETTING_IDS, "fl2d-preset", "fl2d-preset-name",
                       "fl2d-preset-save", "fl2d-preset-del"];
function fl2dLockControls(on) {
  for (const id of FL2D_CONTROLS) $(id).disabled = on;
  // the preset buttons carry their own disabled-with-a-reason state, which
  // has to be re-derived in BOTH directions: locking must replace the
  // "name it first" reason with the run holding the settings, unlocking
  // must restore it rather than enabling them unconditionally
  syncAnsysPresetButtons();
}

/* what the chain falls back to when a domain or budget box is left blank —
   the documented rectangle and the per-stage wall budgets */
const FL2D_FALLBACKS = { front_l: 3, back_l: 7, top_h: 3,
                         sc_budget_s: 300, wb_budget_s: 900 };

/* The four mesh controls a sizing recipe resolves to, for DISPLAY only:
   they fill the blank boxes' placeholders so the panel states the numbers
   a run would inherit. "default" is the documented manual workflow's fixed
   sizing; "studio-yplus1" derives the wall spacing from the operating point
   the way the mesher does (y+ = 1 off the flat-plate correlation, edge size
   at the fine preset's wall fraction). The run resolves its own values
   server-side — nothing computed here is ever sent. */
function fl2dRecipe(mode, cfg) {
  if (mode !== "studio-yplus1") {
    return { edge_size_mm: 0.1, first_layer_mm: 1, n_layers: 10, growth: 1.2 };
  }
  const chordM = (cfg.chord_mm || 0) / 1000;
  const nu = cfg.nu || NU;
  const re = (cfg.speed_ms * chordM) / nu;
  const uTau = cfg.speed_ms * Math.sqrt(0.5 * 0.058 * Math.pow(re, -0.2));
  return { edge_size_mm: 0.002 * chordM * 1000,
           first_layer_mm: (2 * nu / uTau) * 1000,   // y+ = 1
           n_layers: 30, growth: 1.2 };
}

/* a placeholder is a number the user may retype, so show it at the
   precision it is used at — and never show NaN for a config that has no
   usable operating point yet */
const fl2dNum = (v) =>
  Number.isFinite(v) ? String(+(+v).toPrecision(4)) : "recipe value";

/* blank means "inherit", which is only readable if the box says WHAT it
   would inherit — re-run whenever the recipe or the operating point moves */
function syncAnsysPlaceholders() {
  if (!$("fl2d-sizing")) return;
  const src = { ...fl2dRecipe($("fl2d-sizing").value, state.config),
                ...FL2D_FALLBACKS };
  for (const [key, id] of FL2D_OVERRIDES) {
    $(id).placeholder = fl2dNum(src[key]);
  }
}

/* the label the tooltips and the panel use, for naming the box a message
   is about — never the request key, which is not on screen */
const fl2dLabel = (id) =>
  $(id).closest(".field")?.querySelector(".f-label")?.textContent || id;

/* the overrides actually typed — a blank box is left OUT of the request so
   the chain resolves it against the recipe instead of freezing a number.
   NULL means the panel is unreadable and nothing may be started from it: a
   value the browser cannot parse (a decimal comma, a stray character)
   reports .value === "" and would read as blank, and a truncated or
   dropped number is not recoverable once a licensed multi-minute run has
   meshed on it. Say which box instead. */
function readAnsysOverrides() {
  const out = {};
  for (const [key, id, kind] of FL2D_OVERRIDES) {
    const el = $(id);
    if (el.validity?.badInput) {
      toast(`${fl2dLabel(id)}: that is not a number — clear the box to ` +
            `use the recipe's own value.`);
      return null;
    }
    const raw = (el.value || "").trim();
    if (!raw) continue;
    const v = Number(raw);   // Number, not parseFloat: "0.05abc" is an error
    if (!Number.isFinite(v)) {
      toast(`${fl2dLabel(id)}: that is not a number.`);
      return null;
    }
    if (kind === "int" && !Number.isInteger(v)) {
      toast(`${fl2dLabel(id)}: must be a whole number.`);
      return null;
    }
    out[key] = v;
  }
  return out;
}

const clampInt = (v, lo, hi, fb) =>
  Number.isFinite(v) ? Math.min(Math.max(v, lo), hi) : fb;

/* the two integer controls that always carry a value: an unparseable box
   also reads "" here, which would silently start the run on the fallback
   instead of the number on screen — null says so rather than substituting */
function readAnsysInt(id, lo, hi, fb) {
  const el = $(id);
  if (el.validity?.badInput) return null;
  const raw = (el.value || "").trim();
  if (!raw) return fb;
  const v = Number(raw);
  return Number.isFinite(v) ? clampInt(Math.round(v), lo, hi, fb) : null;
}

/* the whole panel as the settings object the preset endpoint stores: the
   base recipe plus every override, an untouched one explicitly null. Null
   for an unreadable panel — a preset that quietly dropped the box it could
   not parse would hand the same wrong mesh back on every later load. */
function readAnsysSettings() {
  const over = readAnsysOverrides();
  const n_iters = readAnsysInt("fl2d-iters", 50, 20000, 500);
  const n_ranks = readAnsysInt("fl2d-ranks", 1, 32, 1);
  if (!over || n_iters == null || n_ranks == null) {
    if (over) toast("Iterations and solver cores must be whole numbers.");
    return null;
  }
  const s = {
    sizing: $("fl2d-sizing").value === "studio-yplus1"
      ? "studio-yplus1" : "default",
    conventions: $("fl2d-conventions").value === "studio"
      ? "studio" : "default",
    n_iters, n_ranks,
  };
  for (const [key] of FL2D_OVERRIDES) s[key] = null;
  return Object.assign(s, over);
}

/* one writer for the panel: a preset that leaves an override null has to
   CLEAR that box, not inherit whatever was typed before it */
function fillAnsysSettings(s) {
  $("fl2d-sizing").value = s?.sizing === "studio-yplus1"
    ? "studio-yplus1" : "default";
  $("fl2d-conventions").value = s?.conventions === "studio"
    ? "studio" : "default";
  $("fl2d-iters").value = s?.n_iters ?? 500;
  $("fl2d-ranks").value = s?.n_ranks ?? 1;
  for (const [key, id] of FL2D_OVERRIDES) $(id).value = s?.[key] ?? "";
  syncAnsysPlaceholders();
}

/* one writer for the selection: assigning .value fires no change event, so
   the Save/Delete buttons would otherwise describe a preset that is no
   longer selected */
function setAnsysPreset(name) {
  const sel = $("fl2d-preset");
  sel.value = [...sel.options].some((o) => o.value === name) ? name : "";
  syncAnsysPresetButtons();
}

/* boundary states disable with the reason instead of toasting on click */
function syncAnsysPresetButtons() {
  const running = !!fl2dJob;
  const name = ($("fl2d-preset-name").value || "").trim();
  $("fl2d-preset-save").disabled = running || !name;
  $("fl2d-preset-save").title = running
    ? "A Fluent 2D run is using these settings — wait for it to finish."
    : name
      ? "Save the current ANSYS settings as a named preset"
      : "Give the preset a name first (the Save as field).";
  const sel = $("fl2d-preset").value;
  $("fl2d-preset-del").disabled = running || !sel;
  $("fl2d-preset-del").title = running
    ? "A Fluent 2D run is using these settings — wait for it to finish."
    : sel
      ? `Delete the "${sel}" preset from this machine`
      : "Select a preset to delete.";
}

function renderAnsysPresetOptions() {
  const sel = $("fl2d-preset");
  const cur = sel.value;
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "— none —";
  sel.appendChild(none);
  for (const p of state.ansysPresets) {
    const o = document.createElement("option");
    o.value = p.name;
    o.textContent = p.name;   // textContent: preset names are user data
    sel.appendChild(o);
  }
  sel.value = state.ansysPresets.some((p) => p.name === cur) ? cur : "";
  syncAnsysPresetButtons();
}

async function loadAnsysPresets() {
  try {
    const res = await api.ansysPresets();
    state.ansysPresets = res.presets || [];
  } catch {
    state.ansysPresets = [];   // endpoint unreachable: an empty library
  }
  renderAnsysPresetOptions();
}

async function saveAnsysPreset() {
  const name = ($("fl2d-preset-name").value || "").trim();
  if (!name) { toast("Give the preset a name first."); return; }
  const settings = readAnsysSettings();
  if (!settings) return;   // the panel said which box; nothing is stored
  // case-insensitive overwrite, matching the server's duplicate check —
  // else saving "parity" over "Parity" 422s as a duplicate
  const presets = [...state.ansysPresets.filter(
                     (p) => p.name.toLowerCase() !== name.toLowerCase()),
                   { name, settings }]
    .sort((a, b) => a.name.localeCompare(b.name));
  try {
    await api.ansysPresetsSave(presets);
    state.ansysPresets = presets;
    renderAnsysPresetOptions();
    setAnsysPreset(name);
    toast(`ANSYS preset "${name}" saved.`, "good");
  } catch (e) {
    toast(`Preset not saved: ${e.message}`);
  }
}

async function deleteAnsysPreset() {
  const name = $("fl2d-preset").value;
  if (!name) { toast("Select a preset to delete."); return; }
  const presets = state.ansysPresets.filter((p) => p.name !== name);
  try {
    await api.ansysPresetsSave(presets);
    state.ansysPresets = presets;
    // the values on screen stay (they are what the next run would use) but
    // they no longer come from a library preset
    renderAnsysPresetOptions();
    setAnsysPreset("");
    toast(`ANSYS preset "${name}" deleted.`, "good");
  } catch (e) {
    toast(`Preset not deleted: ${e.message}`);
  }
}

function bindAnsysSettings() {
  // the recipe drives what a blank box inherits, so its placeholders move
  // with the selection
  $("fl2d-sizing").addEventListener("change", syncAnsysPlaceholders);
  for (const id of FL2D_SETTING_IDS) {
    // a hand edit is no longer the preset that was selected — but the
    // values stay, they are what the next run would use
    $(id).addEventListener("input", () => setAnsysPreset(""));
  }
  $("fl2d-preset").addEventListener("change", () => {
    const p = state.ansysPresets.find(
      (x) => x.name === $("fl2d-preset").value);
    if (p) {
      fillAnsysSettings(p.settings);
      $("fl2d-preset-name").value = p.name;
    }
    syncAnsysPresetButtons();
  });
  $("fl2d-preset-save").addEventListener("click", saveAnsysPreset);
  $("fl2d-preset-del").addEventListener("click", deleteAnsysPreset);
  $("fl2d-preset-name").addEventListener("input", syncAnsysPresetButtons);
  syncAnsysPlaceholders();
  syncAnsysPresetButtons();
}

function gateFl2dRun() {
  const btn = $("btn-fl2d-run");
  if (fl2dJob) {
    btn.disabled = true;
    btn.title = "";
    return;
  }
  if (state.ransJob || state.queueActive) {
    btn.disabled = true;
    btn.title = "The solver is busy (a RANS verify run or the re-rank " +
                "queue) — wait for it to finish or cancel it first.";
    return;
  }
  if (solverForeignBusy) {
    btn.disabled = true;
    btn.title = foreignBusyTitle();
    return;
  }
  if (!fl2dAvail?.available) {
    btn.disabled = true;
    btn.title = "Needs a local ANSYS installation — the note beside this " +
                "button says what is missing.";
    return;
  }
  btn.disabled = false;
  btn.title = "";
}

function applyFl2dAvailabilityNote() {
  const note = $("fl2d-note");
  const a = fl2dAvail || { available: false };
  if (a.available) {
    const ver = a.workbench?.version ? ` ${a.workbench.version}` : "";
    note.textContent = `ANSYS Workbench${ver} and Fluent found — a run ` +
      `holds a license seat from meshing through the solve`;
  } else if (a.probe_error) {
    note.textContent = `Could not check the ANSYS toolchain: ${a.probe_error}`;
  } else {
    const missing = [
      a.workbench && !a.workbench.available ? a.workbench.detail : null,
      a.fluent && !a.fluent.available ? a.fluent.detail : null,
    ].filter(Boolean).join(" · ")
      || a.detail || "no local ANSYS installation found";
    note.textContent = `ANSYS toolchain unavailable — ${missing}`;
  }
  gateFl2dRun();
}

async function refreshFl2dAvailability() {
  // a theme change while this tab was hidden left the chart in old ink —
  // redraw now that the host is visible and measurable
  if (fl2dLast) renderFl2d(fl2dLast);
  // re-attach a Fluent 2D run this page lost (reload, second window). A
  // LIVE run always wins over the shown result; a done run is adopted only
  // when it is not the one already displayed. Jobs of any other engine
  // belong to the RANS verify tab (or the re-rank queue) — leave them alone.
  if (!fl2dJob) {
    try {
      const cur = await api.ransCurrent();
      const s = cur.job_id ? await api.ransStatus(cur.job_id) : null;
      const live = !!s && ["pending", "running"].includes(s.state);
      // a live run of another engine is not adopted, but the Run button
      // must still reflect the busy solver instead of 409ing on click
      solverForeignBusy = live && s.engine !== "fluent2d" ? "rans" : null;
      if (live && s.engine === "fluent2d") {
        fl2dJob = cur.job_id;
        fl2dConventions = s.conventions ?? null;
        fl2dRev = null;             // start revision is unverifiable
        fl2dAdoptedLive = true;     // completion renders as re-attached
        fl2dFlow.reset();           // saved images predate this run
        $("btn-fl2d-cancel").disabled = false;
        fl2dLockControls(true);
        $("fl2d-stale").hidden = true;
        $("fl2d-flow").hidden = true;
        $("fl2d-result").innerHTML =
          '<div class="empty-note">Re-attached to a running Fluent 2D ' +
          'run…</div>';
        updateSolverButtons();
        pollFl2d();
      } else if (s && s.engine === "fluent2d" && s.state === "done"
                 && fl2dResult?.id !== cur.job_id) {
        fl2dResult = s;
        fl2dRev = null;   // solved before this page session
        fl2dFlow.reset();   // saved images predate this run
        renderFl2d(s);
        renderFl2dResult(s, { provenance: "reattached" });
        fl2dFlow.show(s.id);
      }
    } catch { /* rediscovery is best-effort */ }
  }
  try {
    fl2dAvail = await api.fluent2dAvailability();
  } catch (e) {
    fl2dAvail = { available: false, probe_error: e.message };
  }
  applyFl2dAvailabilityNote();
}

$("btn-fl2d-run").addEventListener("click", startFl2d);
$("btn-fl2d-cancel").addEventListener("click", async () => {
  if (fl2dJob) { try { await api.ransCancel(fl2dJob); } catch {} }
});

async function startFl2d() {
  // click-time single-flight, same reasoning as startRansVerify: the
  // disabled flag alone cannot stop a click+Enter pair racing the await
  if (fl2dJob || state.ransJob || state.queueActive || solverForeignBusy) {
    toast("The solver is already busy — wait for the current run or the " +
          "re-rank queue to finish.", "info");
    return;
  }
  // a box the panel cannot read aborts BEFORE anything is locked or
  // started — a run must never mesh on a number other than the one shown
  const overrides = readAnsysOverrides();
  if (!overrides) return;
  const iters = readAnsysInt("fl2d-iters", 50, 20000, 500);
  const ranks = readAnsysInt("fl2d-ranks", 1, 32, 1);
  if (iters == null || ranks == null) {
    toast("Iterations and solver cores must be whole numbers.");
    return;
  }
  $("btn-fl2d-run").disabled = true;   // close the window before the await
  try {
    const conventions =
      $("fl2d-conventions").value === "studio" ? "studio" : "default";
    // a blank override box is absent from the request, not zero — the
    // chain resolves it against the sizing recipe
    const { job_id } = await api.ransStart(
      state.config, $("fl2d-sizing").value, iters, ranks,
      "fluent2d", "fluent", conventions, overrides);
    fl2dJob = job_id;
    fl2dConventions = conventions;
    // the comparison must describe the config THIS run solves — snapshot
    // the estimate and the revision at start
    fl2dCEst = state.analysis?.coefficients?.C_downforce_estimated ?? null;
    fl2dRev = configRevision;
    fl2dAdoptedLive = false;   // this page started the run — rev is known
    fl2dFlow.reset();          // saved images predate this run
    $("btn-fl2d-cancel").disabled = false;
    fl2dLockControls(true);   // snapshotted at start — lock mid-run
    $("fl2d-stale").hidden = true;
    $("fl2d-conv").innerHTML = "";   // previous run's chart is not this run
    $("fl2d-charts").classList.add("empty");
    $("fl2d-flow").hidden = true;
    $("fl2d-result").innerHTML =
      '<div class="empty-note">Run started — geometry to DXF, then ANSYS ' +
      'Workbench meshing (several minutes), then the 2D solve…</div>';
    updateSolverButtons();
    pollFl2d();
  } catch (e) {
    toast(`Could not start the Fluent 2D run: ${e.message}`);
    // the run never started — restore the controls and re-gate the button
    fl2dLockControls(false);
    updateSolverButtons();
  }
}

function pollFl2d() {
  clearInterval(fl2dPoll);
  let misses = 0;
  fl2dPoll = setInterval(async () => {
    try {
      const s = await api.ransStatus(fl2dJob);
      misses = 0;
      renderFl2d(s);
      if (["done", "failed", "cancelled"].includes(s.state)) {
        clearInterval(fl2dPoll);
        fl2dJob = null;
        $("btn-fl2d-cancel").disabled = true;
        fl2dLockControls(false);
        updateSolverButtons();
        if (s.state === "failed") {
          toast("Fluent 2D run failed — details in the Fluent 2D tab.",
                "err");
          $("fl2d-result").innerHTML = "";
          const d = document.createElement("div");
          d.className = "warning-item crit";
          d.style.whiteSpace = "pre-wrap";
          d.textContent = s.error || "run failed";
          $("fl2d-result").appendChild(d);
        }
        if (s.state === "cancelled") {
          $("fl2d-result").innerHTML =
            '<div class="empty-note">Run cancelled.</div>';
        }
        if (s.state === "done") {
          fl2dResult = s;
          // a mid-run config edit means these numbers describe the design
          // at start, not the current form — say so and hold back the
          // one-click k_g calibration (same guard the RANS tab has). A run
          // adopted mid-flight has no verifiable start revision, so it
          // must not present as fresh either — it keeps the re-attached
          // wording, which also disables the one-click k_g
          const prov = fl2dAdoptedLive ? "reattached"
            : (fl2dRev != null && configRevision !== fl2dRev
               ? "edited" : "fresh");
          renderFl2dResult(s, { provenance: prov });
          fl2dFlow.show(s.id);
        }
      }
    } catch (e) {
      // a lost poll must not orphan the run — it keeps solving server-side;
      // ride out transient failures, detach only on 404 or a persistent
      // outage (the tab re-attaches through /api/rans/current either way)
      misses++;
      if (e.status !== 404 && misses < 5) return;
      clearInterval(fl2dPoll);
      fl2dJob = null;
      $("btn-fl2d-cancel").disabled = true;
      fl2dLockControls(false);
      updateSolverButtons();
      toast(`Lost the Fluent 2D job: ${e.message} — reopen this tab to ` +
            `re-attach if it is still running.`);
    }
  }, 1000);
}

function renderFl2d(s) {
  fl2dLast = s;
  const pct = Math.round((s.progress || 0) * 100);
  $("fl2d-progress").style.width = pct + "%";
  $("fl2d-progress").classList.toggle("done", s.state === "done");
  const bits = [s.state === "running" ? (s.phase || "running") : s.state];
  if (s.mesh && s.mesh.n_cells != null
      && Number.isFinite(+s.mesh.n_cells) && +s.mesh.n_cells > 0) {
    bits.push(`${(+s.mesh.n_cells).toLocaleString()} cells`);
  }
  if (s.iteration) bits.push(`iteration ${s.iteration} / ${s.n_iters}`);
  if (s.latest) bits.push(`Cl ${numf(s.latest.cl, 3)}`,
                          `Cd ${numf(s.latest.cd, 4)}`);
  bits.push(`${Math.round(+s.elapsed_s || 0)}s`);
  $("fl2d-status").textContent = bits.join(" · ");
  if (s.history && s.history.length > 1) {
    // reveal the convergence half BEFORE lineChart measures its width
    $("fl2d-charts").classList.remove("empty");
    // the two conventions chart different quantities: the default
    // reports Fluent's raw lift_coef (reference area 1 m², lift-positive),
    // which
    // the panel C_est (chord-referenced, downforce-positive) cannot be
    // drawn against — the estimate line appears only under studio
    // conventions, where the report definition matches
    const conv = s.conventions || s.result?.conventions || fl2dConventions;
    const cEst = conv === "studio"
      ? (s.result?.panel?.c_est ?? fl2dCEst) : null;
    lineChart($("fl2d-conv"), {
      series: [{ name: "Cl (Fluent)", color: SERIES[0],
                 x: s.history.map(h => h.iter),
                 y: s.history.map(h => h.cl) }],
      xLabel: "iteration",
      yLabel: conv === "studio" ? "Cl (downforce +)" : "Cl (Fluent report)",
      targetY: cEst ?? undefined,
      targetLabel: cEst != null
        ? `panel C_est ${(+cEst).toFixed(2)}` : undefined,
      height: 180,
    });
  }
}

function renderFl2dResult(s, { provenance = "fresh" } = {}) {
  const r = s.result;
  if (!r) return;
  const host = $("fl2d-result");
  // numbers that predate this page session cannot be tied to the current
  // form — say which configuration they describe
  if (provenance !== "fresh") {
    $("fl2d-stale").textContent = provenance === "restored"
      ? "restored result — it describes the configuration it was saved " +
        "with; re-run to check the current one"
      : provenance === "edited"
      ? "the configuration was edited while this run solved — the numbers " +
        "below describe the design at start; re-run"
      : "re-attached result from an earlier run — it may describe an " +
        "earlier configuration; re-run to be sure";
    $("fl2d-stale").hidden = false;
  }
  const p = r.panel;
  const pct = (v) => v == null ? "–"
    : `${v > 0 ? "+" : ""}${(+v).toFixed(1)}%`;
  const kv = ([k, v]) => `<div class="kv"><span>${k}</span><b>${v}</b></div>`;
  // headline is the chord-referenced downforce-positive coefficient; the
  // raw Fluent numbers (the ones the GUI shows) stay visible as the
  // default-convention line with the reference note right under it
  const head = [
    ["C<sub>ΔF</sub> — Fluent 2D (downforce +, chord-referenced)",
      numf(r.cl_chord, 3)],
    ["Cd — Fluent 2D (chord-referenced)", numf(r.cd_chord, 4)],
    ["Default conventions — Fluent's own report",
      `Cl ${numf(r.cl_raw, 4)} · Cd ${numf(r.cd_raw, 5)}`],
  ];
  const rest = [
    ["Sectional Cl — panel C_est", p ? numf(p.c_est, 3) : "–"],
    ["Cl delta (Fluent vs estimate)", pct(r.delta_cl_pct)
      + (r.delta_cl_provisional && r.delta_cl_pct != null ? " *" : "")],
    ["Downforce at measured Cl", `${esc(r.downforce_n_at_rans_cl)} N`],
    ["Downforce — panel estimate", p ? `${esc(p.downforce_n)} N` : "–"],
    ["Iterations", `${esc(r.n_iters_run)} (${esc(r.stop_reason)}; ` +
      `tail mean of ${esc(r.tail_rows)})`],
  ];
  // attachment, both channels: the wall verdict reads this run's own
  // exported wall shear (since 2026-08), the field verdict grades the
  // solved flow by the adopted lines — wall outranks field
  if (r.wall_verdict) {
    head.push(["Attachment (wall shear)", esc(r.wall_verdict)]);
  }
  if (r.field_verdict) {
    head.push(["Attachment (field)", esc(r.field_verdict.verdict)]);
  }
  // The panel unlocks the moment a run ends, so the boxes on screen need
  // not describe the run beside them — the card states the RESOLVED set the
  // run carried. A result saved before this surface existed has no settings
  // key at all and must still render.
  const st = s.settings ?? r.settings;
  const sizing = r.sizing ?? s.mesh_size ?? "?";
  let overridden = false;
  if (st) {
    // "†" marks a number that is not the recipe's own. Computable only
    // where the recipe is a constant: the resolved-wall sizing follows the
    // operating point the run started from, which is not recoverable here.
    const base = { ...(sizing === "default"
                       ? fl2dRecipe("default", state.config) : {}),
                   ...FL2D_FALLBACKS };
    const v = (k, unit = "") => {
      const off = base[k] != null && +base[k] !== +st[k];
      overridden = overridden || off;
      return `${esc(st[k])}${unit}${off ? "†" : ""}`;
    };
    rest.push(
      ["Mesh as run", `${esc(sizing)} · edge ${v("edge_size_mm", " mm")} · ` +
        `first layer ${v("first_layer_mm", " mm")} × ` +
        `${v("n_layers")} layers at growth ${v("growth")}`],
      ["Domain / stage budgets",
        `${v("front_l")} L ahead, ${v("back_l")} L behind, ` +
        `${v("top_h")} H above · ${v("sc_budget_s", " s")} + ` +
        `${v("wb_budget_s", " s")}`]);
  } else {
    rest.push(["Mesh as run",
      `${esc(sizing)} — this run predates the settings record, so the ` +
      `numbers it used are not stated`]);
  }
  host.innerHTML = head.map(kv).join("")
    + (r.ref_note
       ? `<p class="note" style="margin:2px 0 6px">${esc(r.ref_note)}</p>`
       : "")
    + rest.map(kv).join("");
  if (overridden) {
    const n = document.createElement("p");
    n.className = "note";
    n.style.margin = "2px 0 6px";
    n.textContent = "† set by hand for this run, overriding the sizing " +
      "recipe's own number — the recipe label is unchanged by design.";
    host.appendChild(n);
  }
  // what the mesher actually built off the wall: the layer stack is capped
  // to the slot and ground clearances it faces, and dropped outright if
  // Mechanical still fails, so the requested count above is not always the
  // count that was cut
  const infl = s.mesh?.inflation;
  if (infl && infl.note) {
    const w = document.createElement("div");
    w.className = infl.degraded ? "warning-item crit" : "warning-item";
    w.textContent = `Inflation: ${infl.note}`;
    host.appendChild(w);
  }
  if (r.residual_stop) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = "Fluent's default residual criteria ended the run " +
      "before the requested iteration count — the documented manual " +
      "ANSYS workflow reads that as converged; the drift figures here " +
      "still say how flat the " +
      "force history actually was at the stop.";
    host.appendChild(w);
  }
  if (r.cl_trend_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = r.cl_trend_note;
    host.appendChild(w);
  }
  if (r.estimate_scope_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = `Estimate scope: ${r.estimate_scope_note}`;
    host.appendChild(w);
  }
  if (r.engine_note) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = r.engine_note;
    host.appendChild(w);
  }
  if (!r.converged) {
    const w = document.createElement("div");
    w.className = "warning-item crit";
    const capped = String(r.stop_reason || "").startsWith("iteration cap");
    w.textContent = `NOT CONVERGED — ` +
      `${r.stop_reason || "the force history had not settled when the run ended"}` +
      (r.cl_drift != null ? ` (Cl drift ${(r.cl_drift * 100).toFixed(1)}% ` +
        `per window)` : ``) +
      `. The numbers above are a mid-transient snapshot, not a result: ` +
      (capped
        ? `raise Iterations and re-run.`
        : `re-run; if it ends early again, step up the sizing.`) +
      ` No k_g calibration is offered from an unconverged run.`;
    host.appendChild(w);
  }
  if (!p && r.panel_error) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = `The panel-model comparison could not be computed ` +
      `(${r.panel_error}) — the Fluent numbers above stand on their own.`;
    host.appendChild(w);
  }
  if (r.mesh_caution) {
    const w = document.createElement("div");
    w.className = "warning-item";
    w.textContent = typeof r.mesh_caution === "string" ? r.mesh_caution
      : "This sizing is not calibrated against the campaign's truth " +
        "cases — treat the delta as screening and pin k_g only from a " +
        "run you trust.";
    host.appendChild(w);
  }
  if (r.suggested_k_g != null) {
    const d = document.createElement("div");
    d.className = "kv";
    d.innerHTML = `<span title="Pinning the ground-gain factor to this ` +
      `value makes the studio estimate reproduce the measured sectional ` +
      `load at this operating point.">suggested k<sub>g</sub></span>
      <b>${esc(r.suggested_k_g)}
      <button id="btn-fl2d-apply-kg" class="btn tiny">Apply</button></b>`;
    host.appendChild(d);
    const kg = $("btn-fl2d-apply-kg");
    if (provenance !== "fresh") {
      // k_g calibrated on another (or unknown) config must not be pinned
      // onto this one with one click
      kg.disabled = true;
      kg.title = "Calibrated on the configuration this run solved — " +
                 "re-run the current configuration to calibrate k_g.";
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
  // the solved case+data live beside the run — offer the durable copy
  // under exports/ (same provenance gate as the RANS tab: an id from a
  // restored snapshot would only 404)
  if (s.id && provenance !== "restored") {
    const b = document.createElement("button");
    b.className = "btn small";
    b.style.marginTop = "8px";
    b.textContent = "Export case for ANSYS…";
    b.title = "Copies the solved case+data (case.cas.h5 / case.dat.h5) " +
      "and a README into exports/ — open in Fluent via File > Read > " +
      "Case & Data.";
    b.addEventListener("click", async () => {
      busy(b, true);
      try {
        const saved = await api.ransExportFluent(s.id);
        lastExport = { ...saved, fmt: null };
        $("exp-dlg-name").textContent = saved.filename;
        $("exp-dlg-size").textContent =
          `${saved.files.join(" · ")} — ` +
          `${(saved.size_bytes / 1048576).toFixed(1)} MB` +
          (saved.data_included ? "" : " (no .dat.h5 — the data write " +
                                      "did not land; re-run to export " +
                                      "solved fields)");
        $("exp-dlg-path").textContent = saved.path;
        $("exp-dlg-download").hidden = true;
        $("export-dialog").showModal();
      } catch (e) {
        toast(`ANSYS export failed: ${e.message}`);
      } finally {
        busy(b, false);
      }
    });
    host.appendChild(b);
  }
  const note = document.createElement("p");
  note.className = "note";
  note.style.marginTop = "6px";
  note.innerHTML = `True-2D case: Cd is profile drag only — induced drag
    is a 3D effect and is compared in the studio's totals, not here.
    <b>Use the Export button above</b> before opening this run in Fluent:
    it writes <code>case.cas.h5</code> / <code>case.dat.h5</code> into
    exports/, which is never cleaned up. Open that copy with File &gt;
    Read &gt; Case &amp; Data. The working copy under
    <code>${esc(r.case_dir)}</code> is scratch space — only the last few
    runs are kept, so a path saved from there stops resolving once older
    runs age out.`;
  host.appendChild(note);
}

/* the same panel as the RANS verify tab, one instance further: the
   Fluent 2D run's exported field reads through the identical endpoints,
   so the only differences are the id prefix and the caption's wording —
   the animation traces a converged STEADY solution, it does not step a
   flow forward in time */
const fl2dFlow = makeFlowView({
  prefix: "fl2d", tab: "fluent2d", rerun: "re-run",
  animLead: FLOW_ANIM_LEAD,
});

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

$("btn-export-fluent2d").addEventListener("click", async () => {
  const b = $("btn-export-fluent2d");
  // the bundle drives the same chain as a run, so it must mesh on the same
  // numbers: the Fluent 2D tab's overrides ride on top of the sizing picked
  // here, and an untouched panel sends nothing at all
  const overrides = readAnsysOverrides();
  if (!overrides) return;
  busy(b, true);
  toast("Meshing in ANSYS Workbench — the geometry and mesh stages " +
        "typically take several minutes…", "info");
  try {
    const saved = await api.exportFluent2dMesh(state.config,
                                               $("exp-fl2d-sizing").value,
                                               overrides);
    lastExport = { ...saved, fmt: null };   // a folder — no Download
    $("exp-dlg-name").textContent = saved.filename;
    $("exp-dlg-size").textContent =
      (saved.n_cells != null
        ? `${(+saved.n_cells).toLocaleString()} cells — `
        : "") + (Array.isArray(saved.files) ? saved.files.join(" · ") : "");
    $("exp-dlg-path").textContent = saved.path;
    $("exp-dlg-download").hidden = true;
    $("export-dialog").showModal();
  } catch (e) {
    toast(`ANSYS 2D mesh export failed: ${e.message}`);
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
           ? `clearance ≥ ${env.min_ground_clearance_mm} mm` : "",
         env.min_le_radius_mm != null
           ? `LE radius ≥ ${env.min_le_radius_mm} mm (` +
             `${env.le_radius_scope === "all" ? "every element"
                                              : "frontmost element"})` : "",
         env.min_te_thickness_mm != null
           ? `TE thickness ≥ ${env.min_te_thickness_mm} mm` : "",
         env.measure_ride_height_mm != null
           ? `heights measured at ${env.measure_ride_height_mm} mm ride height`
           : ""]
          .filter(Boolean).join(", ") + (env.preset_name ? ` (${env.preset_name})` : "")
      : "off"],
    // the report is where a number gets quoted from, so a draft's caveat has
    // to travel with it
    ["Rule source", env && ruleDraftSource(env)
      ? "FSAE 2027 rules PUBLIC-COMMENT DRAFT (version 0.0, 21 July 2026) — "
        + "that document states on its face it is not valid for competition "
        + "and the numbers will move before V1. Verify against the rulebook "
        + "the event actually runs."
        + (builtinRulePreset(env.preset_name) ? ""
           : " The limits above were derived from that draft and may have "
             + "been edited since.")
      : ""],
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
    // the printed report is the surface a number gets quoted from, so the
    // bound marks the live panel shows have to travel with it — the cap
    // raises no warning of its own
    const dragLB = !!f.drag_profile_is_lower_bound;
    sec.push(`<h2>Analysis</h2>` + reportKv([
      ["Estimated downforce", `${fmtN(f.downforce_n, 0)} N`],
      ["Drag estimate", `${dragLB ? "≥ " : ""}${fmtN(f.drag_total_n, 1)} N ` +
        `(${fmtN(f.drag_induced_n, 1)} induced · ` +
        `${dragLB ? "≥" : ""}${fmtN(f.drag_profile_n, 1)} profile)`],
      ["L/D estimate", `${dragLB ? "≤ " : ""}${fmtN(f.efficiency_ld, 1)}`],
      ["Drag bound", dragLB
        ? `profile drag is a floor and L/D a ceiling: ` +
          `${(f.drag_capped_roles || []).join(", ") || "some elements"} ` +
          `operate past the pre-stall polar the drag lookup reads from — ` +
          `verify drag with RANS before trading on it`
        : ""],
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
                         s.slot_signature ? "slot corner" : "",
                         s.shadow_collapse ? "sep risk" : ""]
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
    // a printed delta outlives the screen that qualified it — carry the
    // same caveats the RANS card shows
    const prov = rs.delta_cl_provisional ? " (provisional)" : "";
    sec.push(`<h2>RANS verification</h2>` + reportKv([
      ["Sectional Cl — RANS",
        `${numf(rs.cl_rans, 3)} ± ${numf(rs.cl_rans_std, 3)}${prov}`],
      ["Sectional Cl — panel estimate", rs.panel ? numf(rs.panel.c_est, 3) : "–"],
      ["Cl delta", rs.delta_cl_pct != null
        ? `${rs.delta_cl_pct > 0 ? "+" : ""}${fmtN(rs.delta_cl_pct, 1)}%${prov}` : "–"],
      ["Downforce at RANS Cl", `${rs.downforce_n_at_rans_cl} N`],
      ["Attachment (wall shear)", rs.wall_verdict || ""],
      ["Attachment (field)", rs.field_verdict
        ? rs.field_verdict.verdict : ""],
      ["Converged", rs.converged ? "yes" : (rs.user_stopped
        ? "stopped by user (preview)" : "NO — mid-transient snapshot")],
      ["Iterations", `${rs.n_iters_run} (${rs.stop_reason})`],
      ["Lift trend", rs.cl_trend_note || ""],
      ["Estimate scope", rs.estimate_scope_note || ""],
      ["Suggested k_g", rs.suggested_k_g != null ? rs.suggested_k_g : "–"],
      ["Mesh caution", rs.mesh_caution
        ? "mesh below calibration grade — screening only; re-run on fine " +
          "before trusting the delta"
        : ""],
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
    model; RANS rows are k-ω SST section truth checks over a moving ground,
    solved by OpenFOAM (simpleFoam) or ANSYS Fluent — whichever engine ran
    the verification. See the workflow guide for model limits.</p>`);

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
  if (ransFlow.cache) return ransFlow.cache;   // restored images stay valid
  const id = state.ransResult?.id;
  if (!id || state.ransResult.state !== "done") return null;
  const grab = async (field) => {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 5000);
      // captured under the SAME view settings as the live panel — a bare
      // fetch would bake the server's dark/magma defaults into the file
      const res = await fetch(
        `/api/rans/${id}/flow?field=${field}${ransFlow.query(field)}`,
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
  ransFlow.cache = out;   // later saves skip the refetch
  return out;
}

// the Fluent 2D renders live in the same forget-on-restart registry —
// embed them in the project file too, while they are fetchable
async function captureFl2dFlow() {
  if (fl2dFlow.cache) return fl2dFlow.cache;   // restored images stay valid
  const id = fl2dResult?.id;
  if (!id || fl2dResult.state !== "done") return null;
  const grab = async (field) => {
    try {
      const ctl = new AbortController();
      const t = setTimeout(() => ctl.abort(), 5000);
      // captured under the SAME view settings as the live panel — a bare
      // fetch would bake the server's dark/magma defaults into the file
      const res = await fetch(
        `/api/rans/${id}/flow?field=${field}${fl2dFlow.query(field)}`,
        { signal: ctl.signal });
      clearTimeout(t);
      if (res.ok) return await blobToDataURL(await res.blob());
    } catch { /* best effort — the result saves either way */ }
    return null;
  };
  const [umag, cp] = await Promise.all([grab("umag"), grab("cp")]);
  const out = {};
  if (umag) out.umag = umag;
  if (cp) out.cp = cp;
  if (!Object.keys(out).length) return null;
  fl2dFlow.cache = out;   // later saves skip the refetch
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
  const fl2dImages = await captureFl2dFlow();
  const blob = new Blob([JSON.stringify({
    app: "wing-section-studio", version: 2,
    saved_at: new Date().toISOString(),
    config: state.config, target_downforce_n: state.target,
    airfoil_names: state.airfoilNames, custom_airfoils: custom,
    pins: state.pins,
    rans_rerank: state.ransRerank,
    results: resultsSnapshot(),
    rans_flow: flow,
    fl2d_flow: fl2dImages,
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
    if (ransFlow.cache) {
      ransFlow.show(null);   // rendered from the project file's images
    } else if (r.rans.id) {
      // the server may still hold the job (same-process reload) — probe
      // quietly and only surface the flow view if it is actually there
      $("rans-flow").hidden = true;
      fetch(`/api/rans/${encodeURIComponent(r.rans.id)}/flow?field=umag`)
        .then((res) => { if (res.ok) ransFlow.show(r.rans.id); })
        .catch(() => {});
    } else {
      $("rans-flow").hidden = true;
    }
  });
  attempt("Fluent 2D result", () => {
    if (!r.fl2d || !r.fl2d.result) return;
    fl2dResult = r.fl2d;
    fl2dRev = r.fl2d_stale ? -1 : null;
    renderFl2d(r.fl2d);
    renderFl2dResult(r.fl2d, { provenance: "restored" });
    if (fl2dFlow.cache) {
      fl2dFlow.show(null);   // rendered from the project file's images
    } else if (r.fl2d.id) {
      // the server may still hold the job (same-process reload) — probe
      // quietly and only surface the flow view if it is actually there
      $("fl2d-flow").hidden = true;
      fetch(`/api/rans/${encodeURIComponent(r.fl2d.id)}/flow?field=umag`)
        .then((res) => { if (res.ok) fl2dFlow.show(r.fl2d.id); })
        .catch(() => {});
    } else {
      $("fl2d-flow").hidden = true;
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
    ransFlow.cache = safeFlow(p.rans_flow);
    fl2dFlow.cache = safeFlow(p.fl2d_flow);
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
  bindAnsysSettings();
  loadPresets();
  loadRulePresets();
  loadAnsysPresets();
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
      $("rr-parallel").disabled = true;
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
