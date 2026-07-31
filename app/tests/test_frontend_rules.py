"""Frontend contract checks for the section-level rule checks (no server).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_frontend_rules.py

There is no JS harness, so these are static contract checks in the same
spirit as test_frontend_fl2d_state.py: they pin the load-bearing pieces of
the Rules-UI round so a refactor cannot silently drop them. Covered
contracts:
  * every new control exists, is rule-prefixed, and no existing id moved;
  * the new fields are in the id list bindRules iterates and in the object
    readRulesForm emits, using exactly the field names RuleEnvelopeSpec
    accepts (its from_dict is strict: a typo'd key 422s);
  * one writer fills the form, so a preset that omits a limit CLEARS it;
  * the preset save guard accepts a preset made only of the new limits;
  * the built-in FSAE presets carry the draft wording, the right numbers,
    and a note saying where each number comes from;
  * built-ins are not deletable or overwritable and sit in their own group;
  * the per-element verdicts render as badges, with the radius-cost
    advisory distinct from a violation;
  * the draft disclaimer is on screen and travels into the report,
    and survives a hand edit or a save-as of a built-in's numbers;
  * one writer owns the preset selection, and a library entry can never
    shadow a built-in;
  * the height caps are drawn in the frame they are judged in;
  * _drawRules cannot break on a per-element violation.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
vp = (ROOT / "app" / "static" / "js" / "viewport.js").read_text(encoding="utf-8")

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def body(src, marker):
    """Slice from a marker to the next top-level closing brace."""
    i = src.index(marker)
    j = src.index("\n}", i)
    return src[i:j]


def block(src, marker, end):
    i = src.index(marker)
    return src[i:src.index(end, i)]


# every field name RuleEnvelopeSpec.from_dict accepts; it rejects anything
# else, so the UI may never invent a key
ENV_KEYS = {"max_length_mm", "max_height_mm", "min_ground_clearance_mm",
            "x_offset_mm", "preset_name", "min_le_radius_mm",
            "le_radius_scope", "min_te_thickness_mm",
            "measure_ride_height_mm"}
NEW_IDS = ["rule-le-radius", "rule-le-scope", "rule-te-thick",
           "rule-measure-height"]

# ---- the controls exist, and nothing that existed was renamed -----------
for eid in NEW_IDS + ["rule-preset-note"]:
    check(f"#{eid} present in index.html", f'id="{eid}"' in html)
for eid in ["rules-on", "rules-body", "rule-len", "rule-height", "rule-clear",
            "rule-xoff", "rule-preset", "rule-preset-name", "rule-preset-save",
            "rule-preset-del"]:
    check(f"existing #{eid} still present", f'id="{eid}"' in html)
check("every new control id is rule-prefixed",
      all(i.startswith("rule-") for i in NEW_IDS))
rules_ui = block(html, '<div id="rules-body"', "</section>")
check("the new controls live inside the Rules body",
      all(f'id="{i}"' in rules_ui for i in NEW_IDS))
check("the new controls use the existing .field/two-col idiom",
      rules_ui.count('class="fields two-col"') >= 3
      and all(rules_ui.count(f'id="{i}"') == 1 for i in NEW_IDS))
def field_of(src, eid):
    """The .field label wrapping a control, from its tag to the control."""
    i = src.index(f'id="{eid}"')
    return src[src.rindex('<label class="field"', 0, i):i]


check("every new control sits in a labelled field with a tooltip",
      all('title="' in field_of(rules_ui, i)
          and '<span class="f-label">' in field_of(rules_ui, i)
          for i in NEW_IDS))

# ---- tooltips paraphrase and cite, and state what the rules do NOT say ---
check("the LE-radius tooltip cites T.7.1.4 and offers the rule's 5 mm",
      "T.7.1.4" in rules_ui and 'id="rule-le-radius"' in rules_ui
      and "at least 5 mm on horizontal edges" in rules_ui)
check("no blank-means-off field greys in a rule number as its placeholder",
      'placeholder="5.0"' not in html)
check("the LE-radius field defaults to blank (the check is opt-in)",
      not re.search(r'id="rule-le-radius"[^>]*\svalue=', rules_ui))
check("the TE tooltip cites T.7.1.5 and says the rules give no number",
      "T.7.1.5" in rules_ui
      and re.search(r"gives?\s+NO number|no number", rules_ui))
check("the ride-height tooltip cites T.7.3.1 and names both load cases",
      "T.7.3.1" in rules_ui and "no driver" in rules_ui
      and "laden" in rules_ui)
check("the scope select offers exactly the backend's two values",
      '<option value="frontmost">' in rules_ui
      and '<option value="all">' in rules_ui
      and rules_ui.count("<option value=", rules_ui.index('id="rule-le-scope"'),
                         rules_ui.index('id="rule-te-thick"')) == 2)

# ---- readRulesForm emits the contract, bindRules binds it ---------------
rd = body(js, "function readRulesForm()")
env_lit = block(rd, "const env = {", "\n  };")
emitted = set(re.findall(r"^\s+(\w+):", env_lit, re.M))
check("readRulesForm emits every new envelope field",
      {"min_le_radius_mm", "le_radius_scope", "min_te_thickness_mm",
       "measure_ride_height_mm"} <= emitted)
check("readRulesForm invents no field RuleEnvelopeSpec would reject",
      emitted <= ENV_KEYS, sorted(emitted - ENV_KEYS))
check("readRulesForm reads the new fields from the new ids",
      all(f'"{i}"' in rd for i in NEW_IDS))
bind = body(js, "function bindRules()")
id_list = block(bind, "for (const id of [", "]) {")
check("the new ids are in the list bindRules iterates",
      all(f'"{i}"' in id_list for i in NEW_IDS))
check("the old ids are still in that list",
      all(f'"{i}"' in id_list
          for i in ["rule-len", "rule-height", "rule-clear", "rule-xoff"]))

# ---- one writer for the form -------------------------------------------
fill = body(js, "function fillRulesForm(env)")
check("fillRulesForm writes every envelope field",
      all(f'$("{i}").value' in fill
          for i in NEW_IDS + ["rule-len", "rule-height", "rule-clear",
                              "rule-xoff"]))
check("a preset that omits a limit clears the field instead of inheriting",
      fill.count('?? ""') >= 5)
check("the scope falls back to the backend's default",
      'env.le_radius_scope || "frontmost"' in fill)
check("writeConfigToForm restores through the same writer",
      "fillRulesForm(env);" in body(js, "function writeConfigToForm()"))
check("selecting a preset applies it through the same writer",
      "fillRulesForm(p.envelope);" in bind)

# ---- the save guard accepts a preset of only the new limits -------------
save = body(js, "async function saveRulePreset()")
check("the guard is driven by a limit list, not three hardcoded names",
      "RULE_LIMIT_KEYS.some((k) => env[k] != null)" in save
      and "env.max_length_mm == null && env.max_height_mm == null" not in save)
limits = block(js, "const RULE_LIMIT_KEYS = [", "];")
check("an LE-radius-only or TE-only preset is savable",
      '"min_le_radius_mm"' in limits and '"min_te_thickness_mm"' in limits)
check("the rule ride height alone is not a rule set (it caps nothing)",
      '"measure_ride_height_mm"' not in limits)
check("the drawing anchor and the radius scope alone are not a rule set",
      '"x_offset_mm"' not in limits and '"le_radius_scope"' not in limits)
check("the three original limits are still guarded",
      all(f'"{k}"' in limits for k in ("max_length_mm", "max_height_mm",
                                       "min_ground_clearance_mm")))

# ---- built-in FSAE presets ----------------------------------------------
bi = block(js, "const BUILTIN_RULE_PRESETS = [", "\n];")
names = re.findall(r'name: "([^"]+)"', bi)
check("both built-in presets exist", len(names) == 2, str(names))
check("both labels carry the draft wording",
      all("2027 draft" in n for n in names))
check("the outboard/tip and centre stations are the two offered",
      any("outboard/tip station" in n for n in names)
      and any("centre station" in n for n in names))
blocks = re.findall(r"envelope: \{(.*?)\n    \}", bi, re.S)
check("both presets carry an envelope", len(blocks) == 2)
out_env, ctr_env = (blocks + ["", ""])[:2]
check("outboard/tip: 625 mm length, 250 mm height, 5 mm nose radius",
      "max_length_mm: 625" in out_env and "max_height_mm: 250" in out_env
      and "min_le_radius_mm: 5.0" in out_env)
check("centre: 500 mm height, 5 mm nose radius",
      "max_height_mm: 500" in ctr_env and "min_le_radius_mm: 5.0" in ctr_env)
check("centre leaves the length BLANK (the 700 mm is off the tires)",
      "max_length_mm" not in ctr_env)
for i, b in enumerate(blocks):
    keys = set(re.findall(r"^\s+(\w+):", b, re.M))
    check(f"built-in {i + 1} invents no field the backend would reject",
          keys <= ENV_KEYS, sorted(keys - ENV_KEYS))
notes = re.findall(r'note: "(.*?)",\n  \}', bi, re.S)
check("both presets carry a note", len(notes) == 2)
all_notes = " ".join(notes)
check("the notes cite the rule numbers rather than quoting the rules",
      all(n in all_notes for n in ("T.7.7.1.c", "T.7.7.1.b", "T.7.5.a",
                                   "V.1.1.c", "T.7.1.4")))
check("the note explains 625 as the 700 minus the 75 mm keep-out",
      "= 625 mm of chordwise" in all_notes and "700 mm ahead" in all_notes)
check("the note says the centre length depends on where the nose sits",
      "left blank" in all_notes
      and "fronts of the front tires, not from the nose" in all_notes)
check("the built-in list is documented as a draft quotation",
      "PUBLIC-COMMENT DRAFT" in js and "not valid" in js
      and "before V1" in js)
check("the built-in list says what a 2D section cannot decide",
      "plan-view keep-outs" in js and "cannot decide" in js)

# ---- built-ins are not the user's to overwrite or delete ----------------
check("a built-in name is matched case-insensitively",
      "function builtinRulePreset(name)" in js
      and "p.name.toLowerCase() === key" in js)
check("saving over a built-in name is refused",
      "if (builtinRulePreset(name)) {" in save)
check("deleting a built-in is refused",
      "if (builtinRulePreset(name)) {"
      in body(js, "async function deleteRulePreset()"))
sync = body(js, "function syncRulePresetButtons()")
check("the buttons disable with the reason instead of toasting on click",
      "$(\"rule-preset-save\").disabled = !name || clash;" in sync
      and '$("rule-preset-del").disabled = !sel || isBuiltin;' in sync)
opts = body(js, "function renderRulePresetOptions()")
check("built-ins render in their own optgroup, labelled as a draft",
      'document.createElement("optgroup")' in opts
      and "FSAE 2027 draft (not valid for competition)" in opts)
check("the user's own presets keep a separate group",
      '"Saved on this machine"' in opts)
check("a saved envelope naming a built-in still selects it",
      "builtinRulePreset(cur)" in opts)
check("preset names stay textContent (they are user data)",
      "o.textContent = p.name;" in opts and "innerHTML = p.name" not in opts)

# ---- the built-in note is shown, and follows the selection --------------
note = body(js, "function renderRulePresetNote()")
check("the note renders from the selection, then from its provenance",
      "builtinRulePreset($(\"rule-preset\").value)" in note
      and "ruleDraftSource(state.config.rule_envelope)" in note
      and "n.hidden = !p;" in note)
check("the note is textContent, never innerHTML",
      "n.textContent = !p" in note and "innerHTML" not in note)
check("a hand edit drops the preset and re-reads the note and the buttons",
      '$("rule-preset").value = "";' in bind
      and 'setRulePreset("");' in bind)

# ---- the draft disclaimer is on screen ----------------------------------
check("the Rules section states the source is a public-comment draft",
      "public-comment draft" in rules_ui)
check("it states the draft is not valid for competition",
      "not valid for competition" in rules_ui)
check("it dates the draft and warns the numbers will move",
      "version 0.0, 21 July 2026" in rules_ui and "before V1" in rules_ui)
check("it names the rules a 2D section cannot decide",
      "plan-view keep-outs" in rules_ui and "span limits" in rules_ui)

# ---- per-element verdicts render as badges ------------------------------
badges = body(js, "function updateCardBadges(card, i)")
check("badges read the per-element rule verdicts",
      "const ru = ge?.rules;" in badges)
check("the LE-radius verdict is a badge, critical when violated",
      '"le_radius_ok" in ru' in badges
      and 'ru.le_radius_ok === false ? "crit"' in badges)
check("an unmeasurable nose is amber-unknown, never a green pass",
      'ru.le_radius_mm == null\n            ? "LE radius ?"' in badges
      and 'ru.le_radius_ok == null ? "warn"' in badges)
check("the radius-cost advisory is distinct from a violation",
      "if (ru.radius_cost_flag) {" in badges
      and 'add("nose cost", "warn",' in badges)
check("the TE-thickness verdict is a badge, critical when violated",
      "ru.te_thickness_mm != null" in badges
      and 'add(`TE rule ${ru.te_thickness_mm} mm`, ru.te_ok ? "" : "crit");'
      in badges)
check("the rule badges are named apart from the manufacturing TE badge",
      "TE rule ${ru.te_thickness_mm}" in badges
      and "add(`TE ${ge.mfg.te_gap_mm} mm`" in badges)
check("absent keys badge nothing (a scoped-out element is not a pass)",
      '"le_radius_ok" in ru' in badges and "ru.te_thickness_mm != null" in badges)

# ---- warning surfaces carry the new prose -------------------------------
check("the analysis warning list still renders every server warning",
      "for (const msg of res.warnings || []) {" in js)
check("a rule violation renders critical, advisories do not",
      'msg.startsWith("Rule \'")' in js)
check("the viewport status line still surfaces the first warning",
      "geo.warnings[0]" in js)

# ---- the report carries the limits and the caveat -----------------------
rep = body(js, "async function buildReport()")
check("the report prints the new limits",
      "LE radius" in rep and "TE thickness" in rep
      and "heights measured at" in rep)
check("the report names the radius scope it was checked with",
      'env.le_radius_scope === "all" ? "every element"' in rep)
check("a draft-derived envelope drags its caveat into the report",
      '["Rule source", env && ruleDraftSource(env)' in rep
      and "PUBLIC-COMMENT DRAFT" in rep)

# ---- the envelope travels with the project ------------------------------
check("project save writes the whole config, envelope included",
      "config: state.config," in js)
check("project open merges the saved config without dropping keys",
      "const merged = { ...CONFIG_DEFAULTS, ...cfg };" in js
      and "state.config = withDefaults(saved.config);" in js)

# ---- viewport tolerates per-element violations --------------------------
draw = block(vp, "  _drawRules(svg) {", "\n  }")
check("_drawRules keeps per-element violations out of the box-edge map",
      'if (v.edge !== "element") viol[v.edge] = v;' in draw)
check("_drawRules only ever colours the three box edges",
      set(re.findall(r"viol\.(\w+)", draw)) <= {"length", "top", "bottom"})
check("_drawRules reads by_mm only off box-edge records",
      draw.count("by_mm") == 3)

# ---- draft provenance survives a hand edit and a save-as ----------------
src_fn = body(js, "function ruleDraftSource(env)")
check("provenance rides in the one display-only string the schema allows",
      'const RULE_DRAFT_MARK = " (edited)";' in js
      and "preset_name" in src_fn)
check("an edited copy and a preset saved from one both resolve to the draft",
      "builtinRulePreset(name) || marked(name)" in src_fn
      and "state.rulePresets.find((p) => p.name === name)" in src_fn)
check("a hand edit keeps where the numbers came from",
      "ruleDraftSource(state.config.rule_envelope)" in rd
      and "src.name + RULE_DRAFT_MARK" in rd)
check("turning Rules off does not drop the provenance with the envelope",
      "let ruleDraftLast = null;" in js and "ruleDraftLast" in rd
      and "ruleDraftLast = env ?" in body(js, "function writeConfigToForm()"))
check("a saved copy keeps the draft, not the built-in's identity",
      "const src = ruleDraftSource(env);" in save
      and "delete env.preset_name;" in save
      and "src.name + RULE_DRAFT_MARK" in save)
check("the report says a derived envelope may have been edited since",
      "derived from that draft and may have" in rep)
check("the on-screen note marks a derived copy as derived",
      "Derived from a built-in draft preset" in note)

# ---- one writer for the preset selection --------------------------------
setp = body(js, "function setRulePreset(name)")
check("the selection writer re-renders the note and the buttons",
      "renderRulePresetNote();" in setp and "syncRulePresetButtons();" in setp)
check("a name with no matching option falls back to none",
      '[...sel.options].some((o) => o.value === name) ? name : ""' in setp)
check("no programmatic selection bypasses that writer",
      '$("rule-preset").value = name;' not in js
      and "setRulePreset(name);" in save
      and 'setRulePreset(env.preset_name ?? "");'
          in body(js, "function writeConfigToForm()"))

# ---- a library entry may not shadow a built-in --------------------------
check("a user preset named like a built-in is never offered",
      "state.rulePresets.filter((p) => !builtinRulePreset(p.name))" in opts)
check("the restored selection is checked against what is offered",
      "own.some((p) => p.name === cur)" in opts)

# ---- 0 is not a floor: the inputs cannot silently disable a check -------
le_tag = block(html, '<input id="rule-le-radius"', ">")
te_tag = block(html, '<input id="rule-te-thick"', ">")
check("Min LE radius cannot be set to a check-disabling 0",
      'min="0.1"' in le_tag and 'placeholder="off"' in le_tag)
check("Min TE thickness cannot be set to a check-disabling 0",
      'min="0.1"' in te_tag and 'placeholder="off"' in te_tag)

# ---- the height caps' load case is stated wherever it applies ----------
check("the Rules tooltip names the height-cap exception",
      "except the height caps, when a Rule ride height is set below" in html)
check("the cap line is drawn in the frame the check judges it in",
      "r.extents_mm.bottom - env.measure_ride_height_mm" in draw)
check("the cap line never drops below the ground line",
      "Math.max(env.max_height_mm / mm + hShift, 0)" in draw)
check("the cap label states the load case, violated or not",
      "rule ride height" in draw and draw.count("atRh") == 3)

# ---- the advisory badge explains itself ---------------------------------
check("the badge helper can carry a tooltip",
      'const add = (txt, cls = "", title = "") => {' in badges
      and "if (title) b.title = title;" in badges)
check("the nose-cost badge says it is advisory and what it costs",
      "Advisory, not a violation" in badges
      and "suction peak and stall margin" in badges)

print(f"\n{sum(results)}/{len(results)} rules-UI frontend-contract checks passed")
sys.exit(0 if all(results) else 1)
