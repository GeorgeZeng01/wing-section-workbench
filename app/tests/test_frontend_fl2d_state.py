"""Frontend contract checks for Fluent 2D state handling (no server needed).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_frontend_fl2d_state.py

There is no JS harness, so these are static contract checks in the same
spirit as the manufacturing-guard block: they pin the load-bearing pieces
of the Fluent 2D persistence/gating round so a refactor cannot silently
drop them. Covered contracts:
  * Fluent 2D results survive project save/restore (resultsSnapshot /
    restoreResults / captureFl2dFlow / fl2d_flow embedding);
  * restored RANS flow images are never discarded by a theme or
    view-settings change (only a live job may drop the capture cache);
  * cross-engine solver-mutex reflection (solverForeignBusy) so start
    buttons never 409 on click after a reload;
  * a run adopted mid-flight completes as "reattached", never "fresh";
  * a re-attached run's live chart reads conventions off every snapshot;
  * the flow view is ONE factory instantiated per engine tab (prefix
    "rans" / "fl2d") — the quirks the shipped RANS path depends on, the
    animation's lifecycle at every teardown site, and the DOM ids both
    instances address by prefix;
  * every path that swaps what the panel shows drops the images, the
    mount and the zoom that belonged to the old one;
  * the level-of-detail guard measures the BASE grid, so a detail window
    survives a pan;
  * both instances describe the animation with the same honest words;
  * the ANSYS settings panel: every field's DOM id, blank-means-inherit
    placeholders, the preset mechanism mirroring the rules one, the run
    lock covering the new controls, and the start request carrying the
    overrides;
  * the resolved settings are read BACK onto the result card, so a coarse
    confined run cannot render identically to a recipe-parity one;
  * a box the browser cannot parse, or a fraction in a whole-number box,
    refuses by name instead of silently inheriting or truncating — and a
    range violation reads as a sentence, not as a validator array;
  * the export bundle meshes on the same overrides a run would use;
  * the conventions/sizing vocabulary is "default", never the old value —
    the frontend and the MCP-facing chain must not disagree about it;
  * the documentation claims a round has falsified stay falsified.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def body(src, marker):
    """Slice from a marker to the next top-level closing brace."""
    i = src.index(marker)
    j = src.index("\n}", i)
    return src[i:j]


def member(src, marker):
    """Slice a factory member (one indent level in) to its own brace."""
    i = src.index(marker)
    j = src.index("\n  }", i)
    return src[i:j]


def block(src, marker, end):
    i = src.index(marker)
    return src[i:src.index(end, i)]


# ---- restored flow images survive theme/view changes -------------------
refresh = member(js, "  function refresh() {")
check("refresh drops the capture cache only for a live job",
      "if (view.cache && view.id)" in refresh)
check("refresh no longer keys the drop on the result snapshot",
      "state.ransResult?.id" not in refresh
      and 'state.ransResult.state === "done"' not in refresh)

# ---- Fluent 2D results persist like RANS results -----------------------
snap = body(js, "function resultsSnapshot()")
check("resultsSnapshot carries the Fluent 2D result",
      "fl2d: fl2dResult" in snap)
check("resultsSnapshot carries fl2d staleness alongside it",
      "fl2d_stale:" in snap and "configRevision > fl2dRev" in snap)

restore = body(js, "function restoreResults(r)")
check("restoreResults has an isolated Fluent 2D attempt block",
      'attempt("Fluent 2D result"' in restore)
check("restore renders the fl2d card with restored provenance",
      'renderFl2dResult(r.fl2d, { provenance: "restored" })' in restore)
check("restore re-draws the fl2d convergence chart",
      "renderFl2d(r.fl2d)" in restore)
check("restore shows saved flow images, else probes the live job quietly",
      "fl2dFlow.show(null)" in restore
      and "encodeURIComponent(r.fl2d.id)" in restore)

check("fl2d flow images are captured into the project file",
      "async function captureFl2dFlow()" in js
      and "fl2d_flow: fl2dImages," in js)
check("project open restores the fl2d images through the same sanitizer",
      "fl2dFlow.cache = safeFlow(p.fl2d_flow)" in js)
show = member(js, "  async function show(jobId")
check("the static view renders from the cache when there is no live job",
      "if (!jobId && view.cache)" in show)
check("flow buttons stay usable on cached images (three per instance)",
      js.count("(view.id || view.cache) && show(view.id,") == 3)
reset = body(js, "function resetWorkspaceResults()")
check("workspace reset clears the fl2d flow state and adoption flag",
      "fl2dFlow.reset();" in reset and "fl2dAdoptedLive = false;" in reset)
check("renderFl2dResult explains restored provenance in its own words",
      'provenance === "restored"' in body(js, "function renderFl2dResult(s"))
check("export gate on restored ids is intact (a saved id would only 404)",
      'if (s.id && provenance !== "restored")' in js)

# ---- cross-engine mutex reflection --------------------------------------
check("shared foreign-busy flag exists",
      "let solverForeignBusy = null;" in js)
check("RANS-side probe records a live Fluent 2D run without adopting it",
      '? "fluent2d" : null;' in js)
check("fl2d-side probe records a live foreign run without adopting it",
      '? "rans" : null;' in js)
check("updateSolverButtons reflects the foreign run on the RANS button",
      "else if (solverForeignBusy)"
      in body(js, "function updateSolverButtons()"))
check("gateFl2dRun reflects the foreign run",
      "if (solverForeignBusy)" in body(js, "function gateFl2dRun()"))
gr = body(js, "async function gateRerank()")
check("gateRerank reflects the foreign run before and after its await",
      "if (solverForeignBusy)" in gr
      and "|| fl2dJob || polishJob || solverForeignBusy)" in gr)
check("click-time single-flight guards include the foreign run "
      "(and the adjoint polish)",
      "|| state.queueActive || fl2dJob || polishJob\n"
      "      || solverForeignBusy)" in js
      and "fl2dJob || polishJob || state.ransJob || state.queueActive\n"
      "      || solverForeignBusy)" in js)

# ---- adopted-live runs never complete as fresh --------------------------
check("adoption flag declared beside the fl2d state",
      "let fl2dAdoptedLive = false;" in js)
check("live adoption marks the start revision unverifiable",
      "fl2dAdoptedLive = true;" in js and js.count("fl2dRev = null;") >= 2)
check("a page-started run clears the adoption flag",
      "fl2dAdoptedLive = false;   // this page started the run" in js)
check("completion of an adopted run renders as reattached, never fresh",
      'fl2dAdoptedLive ? "reattached"' in js)
check("adoption and start paths invalidate saved images of older runs",
      js.count("// saved images predate this run") >= 3)

# ---- conventions read off every snapshot --------------------------------
check("live chart prefers the snapshot's own conventions",
      "s.conventions || s.result?.conventions || fl2dConventions" in js)
check("adoption seeds conventions from the snapshot",
      "fl2dConventions = s.conventions ?? null;" in js)

# ---- one flow-view factory, one instance per engine tab -----------------
check("the flow view is a factory, not a per-tab copy",
      "function makeFlowView(cfg) {" in js
      and js.count("= makeFlowView({") == 2
      and 'prefix: "rans", tab: "rans"' in js
      and 'prefix: "fl2d", tab: "fluent2d"' in js)
check("the per-tab flow functions the factory replaced are gone",
      "function showRansFlow(" not in js
      and "function showFl2dFlow(" not in js
      and "function refreshFlowView(" not in js
      and "function teardownRansAnim(" not in js)
check("every element is addressed through the instance's id prefix",
      "const el = (part) => $(`${cfg.prefix}-${part}`);" in js)
check("each instance owns its job id, cache, sequence and mount",
      "let seq = 0, lodSeq = 0;" in js
      and "FLOW_VIEWS.push(view);" in js)

# ---- RANS quirks the shipped path depends on ----------------------------
anim = member(js, "  async function showAnim(jobId) {")
check("streamlines stay a static-view overlay, hidden in animated mode",
      'el("flow-streams-wrap").hidden = true;' in anim
      and 'el("flow-streams-wrap").hidden = false;' in show)
check("the animation refuses without a live job and says why",
      "if (!jobId) {" in anim
      and "not saved with a project" in anim
      and "to animate" in anim)
check("styleKey mount reuse survives (no refetch on an unchanged view)",
      "view.anim.styleKey === styleKey" in anim
      and "view.anim.ctrl.start();" in anim)
check("stale-response guards survive on both fetch paths",
      js.count("if (mySeq !== seq) return;") >= 3
      and "if (mySeq !== lodSeq || view.anim?.ctrl !== ctrl) return;" in js)
check("level-of-detail refetch is per instance and keeps its Cp backdrop",
      'if (view.anim.bg === "cp") q += "&fields=umag,cp";' in js)
check("the RANS caption states the same thing the Fluent one does",
      'animLead: "particles ride the solved field"' not in js
      and js.count("animLead: FLOW_ANIM_LEAD,") == 2)
check("the Fluent 2D caption states it is not time-accurate",
      "rather than a time-accurate simulation" in js
      and "converged steady field" in js)
check("captured project images bake in each panel's own view settings",
      "${ransFlow.query(field)}" in js and "${fl2dFlow.query(field)}" in js)

# ---- animation lifecycle: destroy at every teardown site ----------------
check("teardown destroys the mount (rAF loop, observer, field arrays)",
      "if (view.anim?.ctrl) view.anim.ctrl.destroy();" in js)
check("reset() tears the animation down and hides the panel",
      "teardownAnim();" in member(js, "  function reset() {"))
check("workspace reset resets both instances",
      "ransFlow.reset();" in reset and "fl2dFlow.reset();" in reset)
tabs = body(js, 'document.querySelectorAll(".tab").forEach((t) => {')
check("leaving a tab suspends its flow view, entering resumes it",
      "v.tab === t.dataset.tab ? v.resume() : v.suspend()" in tabs)
check("suspend destroys rather than merely stopping",
      "suspend() { seq++; teardownAnim(); }," in js)
check("a start or adoption drops the previous run's panel and animation",
      "ransFlow.reset();" in body(js, "async function startRansVerify()")
      and js.count("fl2dFlow.reset();") >= 4)
check("a settings change on a hidden tab defers instead of mounting blind",
      "if (!onActiveTab()) { deferred = true; return; }" in js)
check("the theme remount hook is wired inside the factory",
      'window.addEventListener("wss-themechange", refresh);' in js)

# ---- DOM contract: everything referenced above actually exists ----------
for eid in ["fl2d-stale", "fl2d-result", "fl2d-conv",
            "btn-fl2d-run", "btn-rans-run", "btn-rerank"]:
    check(f"#{eid} present in index.html", f'id="{eid}"' in html)
# both panels carry the identical control set the factory addresses
for part in ["flow", "flow-img", "flow-note", "flow-viewport", "flow-canvas",
             "flow-umag", "flow-cp", "flow-anim", "flow-cmap", "flow-vmax",
             "flow-streams", "flow-streams-wrap", "flow-extent",
             "anim-speed", "anim-speed-wrap", "anim-trail", "anim-trail-wrap",
             "anim-density", "anim-density-wrap", "anim-bg", "anim-bg-wrap"]:
    for prefix in ["rans", "fl2d"]:
        check(f"#{prefix}-{part} present in index.html",
              f'id="{prefix}-{part}"' in html)
check("both flow viewports host the animation's two canvases",
      html.count('<canvas data-role="bg"></canvas>') == 2
      and html.count('<canvas data-role="pt"></canvas>') == 2)

# ---- a re-attached RANS run carries none of the previous run's state ----
avail = body(js, "async function refreshRansAvailability()")
check("adopting a live run drops the panel, its mount and its images",
      '$("rans-flow").hidden = true;' not in avail
      and avail.count("ransFlow.reset();") == 2)
check("adopting a finished run resets before it shows the new one",
      "ransFlow.reset();   // saved images predate the run being adopted"
      in avail
      and avail.index("// saved images predate the run being adopted")
          < avail.index("ransFlow.show(s.id);"))

# ---- nothing on screen may outlive the picture it belonged to -----------
show = member(js, "  async function show(jobId")
check("a different run or quantity clears the static zoom and pan",
      "if (jobId !== view.id || field !== view.field) resetTransform();" in show)
check("reset clears the transform along with the id, cache and mount",
      "resetTransform();" in member(js, "  function reset() {"))
check("the transform reset is owned by the zoom block, one per instance",
      "let resetTransform = () => {};" in js
      and "resetTransform = () => { s = 1; tx = 0; ty = 0; apply(); };" in js)
check("a field missing from a restored project blanks the raster",
      'img.removeAttribute("src");' in show)
check("a slow static body cannot clobber a newer field",
      "const blob = await res.blob();" in show
      and show.count("if (mySeq !== seq) return;") >= 2)

# ---- the animation is never mounted into a hidden panel -----------------
check("suspend invalidates a fetch still in flight",
      "suspend() { seq++; teardownAnim(); }," in js)
anim = member(js, "  async function showAnim(jobId)")
check("a restored project shows no animation chrome it cannot drive",
      anim.index("if (!jobId) {")
      < anim.index('el("anim-speed-wrap").hidden = false;')
      and anim.index("if (!jobId) {") < anim.index("host.hidden = false;"))

# ---- level of detail: measured on the base grid, with hysteresis --------
lod = member(js, "  async function lod(v) {")
check("a settle landing on a static view refines nothing",
      'if (!el("flow-anim").classList.contains("active")) return;' in lod)
check("coarseness is judged on the base grid, not the installed window",
      "const win = view.anim.detail;" in lod
      and "((base.x1 - base.x0) / base.nx)" in lod)
check("refine and revert cannot alternate on consecutive settles",
      "(win ? 4 : 7) * dpr" in lod)
check("a pan past the window's edge refetches instead of reverting to base",
      "bx0 >= win.x0 && bx1 <= win.x1" in lod)
check("the installed window is recorded so the next settle can judge it",
      "view.anim.detail = data;" in lod
      and "view.anim.detail = null;" in lod
      and "detail: null };" in js)

# ---- both animations describe themselves the same honest way -----------
check("the caption lead is one shared string, not a per-tab wording",
      "const FLOW_ANIM_LEAD =" in js
      and js.count("animLead: FLOW_ANIM_LEAD,") == 2
      and "path picture rather than a time-accurate simulation" in js)
check("the RANS button says the field is steady and the motion is not",
      "converged steady velocity field" in block(
          html, 'id="rans-flow-anim"', "</button>")
      and "rather than a time-accurate simulation" in block(
          html, 'id="rans-flow-anim"', "</button>"))

# ---- select-inlines outside an .f-wrap box read as controls ------------
css = (ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")
check("the re-rank concurrency select is boxed like the mesh select",
      "#rr-mesh, #rr-parallel {" in css)
check("both share the action row instead of taking a line each",
      "width: auto" in block(css, "#rr-mesh, #rr-parallel {", "}"))

# ---- ANSYS settings panel: DOM contract --------------------------------
apijs = (ROOT / "app" / "static" / "js" / "api.js").read_text(encoding="utf-8")
fl2d_tab = block(html, '<div id="tab-fluent2d"', '<div id="tab-export"')

for eid in ["fl2d-settings", "fl2d-sizing", "fl2d-conventions", "fl2d-iters",
            "fl2d-ranks", "fl2d-edge-mm", "fl2d-first-mm", "fl2d-layers",
            "fl2d-growth", "fl2d-front-l", "fl2d-back-l", "fl2d-top-h",
            "fl2d-sc-budget", "fl2d-wb-budget", "fl2d-preset",
            "fl2d-preset-name", "fl2d-preset-save", "fl2d-preset-del"]:
    check(f"#{eid} present in the Fluent 2D tab", f'id="{eid}"' in fl2d_tab)

check("the panel is a details disclosure titled ANSYS settings",
      '<details class="adv fl2d-adv" id="fl2d-settings">' in fl2d_tab
      and "<summary>ANSYS settings</summary>" in fl2d_tab)
check("it sits between the one-line run row and the progress bar",
      fl2d_tab.index('id="btn-fl2d-run"')
      < fl2d_tab.index('id="fl2d-settings"')
      < fl2d_tab.index('id="fl2d-progress"'))
check("the quick-start row still carries Run, Cancel and the note",
      all(f'id="{i}"' in block(fl2d_tab, '<div class="tab-controls">',
                               "</div>")
          for i in ["btn-fl2d-run", "btn-fl2d-cancel", "fl2d-note"]))
check("the fields use the optimizer idiom (two-col label.field blocks)",
      block(fl2d_tab, 'id="fl2d-settings"', "</details>").count(
          '<div class="fields two-col">') >= 4
      and '<span class="f-label">Edge size</span>' in fl2d_tab
      and '<span class="f-wrap"><input id="fl2d-edge-mm"' in fl2d_tab)
check("the panel is grouped so it reads (mesh / domain / solve / limits)",
      all(f"<legend>{g}</legend>" in fl2d_tab
          for g in ["Mesh", "Domain", "Solve", "Limits"]))
check("controls that need the width span both columns",
      fl2d_tab.count('class="field full"') >= 3)
ranks = block(fl2d_tab, 'id="fl2d-ranks"', "</label>")
check("solver cores is a number input over the whole 1..32 range",
      '<input id="fl2d-ranks" type="number"' in fl2d_tab
      and 'min="1"' in ranks and 'max="32"' in ranks
      and '<option value="2">2 ranks</option>' not in html)
check("no override field ships a value — blank is what means inherit",
      all(f'value="' not in block(fl2d_tab, f'id="{i}"', "</label>")
          for i in ["fl2d-edge-mm", "fl2d-first-mm", "fl2d-layers",
                    "fl2d-growth", "fl2d-front-l", "fl2d-back-l",
                    "fl2d-top-h", "fl2d-sc-budget", "fl2d-wb-budget"]))

# ---- the panel's wiring -------------------------------------------------
check("one table pairs every override with its control and parser",
      "const FL2D_OVERRIDES = [" in js
      and all(f'["{key}", "{eid}"' in js for key, eid in [
          ("edge_size_mm", "fl2d-edge-mm"),
          ("first_layer_mm", "fl2d-first-mm"),
          ("n_layers", "fl2d-layers"),
          ("growth", "fl2d-growth"),
          ("front_l", "fl2d-front-l"),
          ("back_l", "fl2d-back-l"),
          ("top_h", "fl2d-top-h"),
          ("sc_budget_s", "fl2d-sc-budget"),
          ("wb_budget_s", "fl2d-wb-budget")]))
check("the run lock covers every new control, presets included",
      "const FL2D_SETTING_IDS = [" in js
      and "...FL2D_OVERRIDES.map(([, id]) => id)" in js
      and "const FL2D_CONTROLS = [...FL2D_SETTING_IDS," in js
      and all(i in block(js, "const FL2D_CONTROLS = [", "];")
              for i in ['"fl2d-preset"', '"fl2d-preset-name"',
                        '"fl2d-preset-save"', '"fl2d-preset-del"']))
lock = body(js, "function fl2dLockControls(on)")
check("the lock re-derives the preset buttons' own disabled reasons",
      "syncAnsysPresetButtons();" in lock)
check("a blank box states the value it would inherit",
      "function syncAnsysPlaceholders()" in js
      and "$(id).placeholder = fl2dNum(src[key]);" in js
      and "...fl2dRecipe($(\"fl2d-sizing\").value, state.config)," in js
      and "...FL2D_FALLBACKS };" in js)
check("the documented fallbacks are the domain and the stage budgets",
      "const FL2D_FALLBACKS = { front_l: 3, back_l: 7, top_h: 3," in js
      and "sc_budget_s: 300, wb_budget_s: 900 };" in js)
recipe = body(js, "function fl2dRecipe(mode, cfg)")
check("the default recipe placeholder is the documented sizing",
      "edge_size_mm: 0.1, first_layer_mm: 1, n_layers: 10, growth: 1.2"
      in recipe)
check("the resolved-wall recipe follows the operating point",
      "0.002 * chordM * 1000" in recipe
      and "(2 * nu / uTau) * 1000" in recipe
      and "n_layers: 30" in recipe)
check("a config with no usable operating point shows no NaN placeholder",
      'Number.isFinite(v) ? String(+(+v).toPrecision(4)) : "recipe value"'
      in js)
check("the placeholders follow the recipe and the operating point",
      '$("fl2d-sizing").addEventListener("change", syncAnsysPlaceholders);'
      in js
      and "syncAnsysPlaceholders();" in body(js, "function onConfigChanged()"))
ov = body(js, "function readAnsysOverrides()")
check("a blank override is left OUT of the request, never sent as zero",
      "if (!raw) continue;" in ov and "out[key] = v;" in ov)
settings = body(js, "function readAnsysSettings()")
check("a recipe number the panel only DISPLAYS can never reach a request",
      "fl2dRecipe(" in js
      and "fl2dRecipe" not in ov and "fl2dRecipe" not in settings)
check("the settings object carries the base recipe the contract names",
      all(k in settings for k in ['sizing:', 'conventions:', 'n_iters,',
                                  'n_ranks,'])
      and '"studio-yplus1" : "default"' in settings
      and '? "studio" : "default"' in settings)
check("an untouched override is stored as null (inherit), not as a number",
      "for (const [key] of FL2D_OVERRIDES) s[key] = null;" in settings)
fill = body(js, "function fillAnsysSettings(s)")
check("loading a preset clears the boxes it leaves empty",
      'for (const [key, id] of FL2D_OVERRIDES) $(id).value = s?.[key] ?? "";'
      in fill
      and "syncAnsysPlaceholders();" in fill)

# ---- presets mirror the rules mechanism ---------------------------------
for fn in ["function setAnsysPreset(name)",
           "function syncAnsysPresetButtons()",
           "function renderAnsysPresetOptions()",
           "async function loadAnsysPresets()",
           "async function saveAnsysPreset()",
           "async function deleteAnsysPreset()",
           "function bindAnsysSettings()"]:
    check(f"{fn.split('(')[0].split()[-1]} exists", fn in js)
check("the preset panel is wired at boot like the rules one",
      "bindAnsysSettings();" in js and "loadAnsysPresets();" in js)
sync = body(js, "function syncAnsysPresetButtons()")
check("Save and Delete disable with the reason in their title",
      '$("fl2d-preset-save").disabled' in sync
      and '$("fl2d-preset-save").title' in sync
      and '$("fl2d-preset-del").disabled' in sync
      and '$("fl2d-preset-del").title' in sync
      and "Give the preset a name first" in sync
      and "Select a preset to delete." in sync)
opts = body(js, "function renderAnsysPresetOptions()")
check("preset names are written as text, never as markup",
      "o.textContent = p.name;" in opts)
check("a selection that no longer exists falls back to none",
      "sel.value = state.ansysPresets.some((p) => p.name === cur) ? cur : \"\";"
      in opts)
load = body(js, "async function loadAnsysPresets()")
check("an unreachable endpoint is an empty library, not a broken panel",
      "state.ansysPresets = [];" in load)
save = body(js, "async function saveAnsysPreset()")
check("saving overwrites case-insensitively, as the server compares",
      "p.name.toLowerCase() !== name.toLowerCase()" in save
      and "const settings = readAnsysSettings();" in save
      and "{ name, settings }" in save)
dele = body(js, "async function deleteAnsysPreset()")
check("deleting leaves the values on screen and drops only the selection",
      'setAnsysPreset("");' in dele)
bind = body(js, "function bindAnsysSettings()")
check("selecting a preset fills every field",
      "fillAnsysSettings(p.settings);" in bind)
check("a hand edit clears the selection",
      'for (const id of FL2D_SETTING_IDS) {' in bind
      and 'setAnsysPreset("")' in bind)
check("the preset library lives in state beside the rule one",
      "ansysPresets: [],           // machine-level ANSYS 2D settings library"
      in js)
check("api.js reaches the ANSYS preset endpoints",
      'ansysPresets: () => request("GET", "/api/ansys-presets")' in apijs
      and 'request("PUT", "/api/ansys-presets", { presets })' in apijs)

# ---- the start request carries the panel --------------------------------
start = body(js, "async function startFl2d()")
check("the start request sends the overrides alongside the recipe",
      "const overrides = readAnsysOverrides();" in start
      and "conventions, overrides);" in start
      and '$("fl2d-sizing").value' in start)
check("solver cores is clamped to the range the server accepts",
      'readAnsysInt("fl2d-ranks", 1, 32, 1)' in start
      and "clampInt(Math.round(v), lo, hi, fb)" in js)
check("the overrides ride into the request body",
      "conventions, ...settings }" in apijs)
check("the mesh export still sends the sizing it needs",
      'api.exportFluent2dMesh(state.config,' in js
      and '$("exp-fl2d-sizing").value' in js)

# ---- the resolved settings are read BACK, not only written -------------
res = body(js, "function renderFl2dResult(s")
check("the card states the resolved set the run carried, not the panel",
      "const st = s.settings ?? r.settings;" in res
      and '["Mesh as run"' in res
      and '["Domain / stage budgets"' in res)
check("every resolved number the panel can set is stated on the card",
      all(f'v("{k}"' in res for k in
          ["edge_size_mm", "first_layer_mm", "n_layers", "growth",
           "front_l", "back_l", "top_h", "sc_budget_s", "wb_budget_s"]))
check("a result saved before the settings surface still renders",
      "predates the settings record" in res)
check("a hand-set number is marked, and only where the recipe is a constant",
      '"†"' in res
      and 'sizing === "default"' in res
      and "...FL2D_FALLBACKS };" in res
      and "set by hand for this run, overriding the sizing " in res)
check("the layer stack the mesher actually cut is reported when it moved",
      "const infl = s.mesh?.inflation;" in res
      and "if (infl && infl.note)" in res
      and 'infl.degraded ? "warning-item crit"' in res)
check("the panel unlocking after a run is why the card must state its own",
      "The panel unlocks the moment a run ends" in res)

# ---- an unreadable box never becomes a silently different mesh ---------
check("a box the browser cannot parse is refused, not read as blank",
      "el.validity?.badInput" in ov
      and "that is not a number" in ov
      and "return null;" in ov)
check("a fractional value in a whole-number box is refused, not truncated",
      'kind === "int" && !Number.isInteger(v)' in ov
      and "must be a whole number" in ov
      and "parseInt(" not in ov and "parseFloat(" not in ov)
check("the message names the box on screen, never the request key",
      "const fl2dLabel = (id) =>" in js
      and '$(id).closest(".field")?.querySelector(".f-label")' in js)
check("the start path aborts before anything is locked or started",
      start.index("const overrides = readAnsysOverrides();")
      < start.index('$("btn-fl2d-run").disabled = true;')
      and "if (!overrides) return;" in start)
check("the always-populated integer controls refuse the same way",
      "function readAnsysInt(id, lo, hi, fb)" in js
      and "if (el.validity?.badInput) return null;"
      in body(js, "function readAnsysInt(id, lo, hi, fb)")
      and "Iterations and solver cores must be whole numbers." in start)
check("a preset stores nothing rather than dropping the box it cannot read",
      "if (!settings) return;" in save
      and "return null;" in settings)

# ---- a range violation reads as a sentence, not as an array ------------
check("api.js folds the validator's error list into named-field sentences",
      "function detailOf(j, fallback)" in apijs
      and "Array.isArray(d)" in apijs
      and "`${loc}: ${e.msg}`" in apijs)
check("both response paths use the same folding",
      apijs.count("detailOf(await res.json(), res.statusText)") == 2
      and "JSON.stringify(j.detail)" not in apijs)

# ---- the export bundle meshes on the same numbers as a run -------------
check("the bundle forwards the panel's overrides",
      "exportFluent2dMesh: (config, sizing, overrides = {})" in apijs
      and "{ config, sizing, ...overrides }" in apijs)
exp = block(js, '$("btn-export-fluent2d").addEventListener', "\n});")
check("the export click reads the panel and aborts on an unreadable box",
      "const overrides = readAnsysOverrides();" in exp
      and "if (!overrides) return;" in exp
      and "overrides);" in exp)
check("the export card says the tab's overrides ride on the chosen sizing",
      "ANSYS settings</b>" in block(html, '<h3>ANSYS 2D mesh bundle</h3>',
                                    "</div>"))
check("a second card paragraph cannot take the description's slack",
      ".export-card p.group-note { flex: 0 0 auto;" in css)

# ---- placeholders follow a REPLACED configuration too ------------------
wcf = body(js, "function writeConfigToForm()")
check("the one funnel every config-replacing path shares syncs them",
      "syncAnsysPlaceholders();" in wcf)

# ---- the run-holds-the-settings reason is reachable --------------------
check("the preset buttons re-derive their reason in both lock directions",
      "syncAnsysPresetButtons();" in lock
      and "if (!on) syncAnsysPresetButtons();" not in lock)
check("locking happens after the job id is known, so the reason is true",
      start.index("fl2dJob = job_id;")
      < start.index("fl2dLockControls(true);"))

# ---- L is the stack's streamwise extent, not the chord -----------------
check("the domain labels agree with their own tooltips",
      '<span class="f-label">Lengths ahead</span>' in fl2d_tab
      and '<span class="f-label">Lengths behind</span>' in fl2d_tab
      and "Chords ahead" not in html and "Chords behind" not in html)
check("the full-domain view no longer promises a fixed rectangle",
      "the whole box this run actually meshed" in html
      and "the whole meshed box the documented manual ANSYS workflow"
      not in html)

# ---- the renamed vocabulary --------------------------------------------
check("the frontend default convention is 'default' on both sides",
      'conventions = "default"' in apijs
      and '$("fl2d-conventions").value === "studio" ? "studio" : "default"'
      in start)
check("both sizing selects offer the renamed value",
      html.count('<option value="default" selected>Default '
                 '(0.1 mm profile sizing)</option>') == 2)
check("the conventions select offers the renamed value",
      '<option value="default" selected>Default</option>' in html)
check("the result table names the convention, not who runs it",
      '["Default conventions — Fluent\'s own report",' in js)
for name, src in [("index.html", html), ("app.js", js), ("api.js", apijs)]:
    check(f"no 'team' string survives in {name}", "team" not in src.lower())

# ---- documentation claims that were measured, not asserted -------------
# The docs are the only statement of these contracts a reader outside the
# code ever sees, so the ones a round has already falsified are pinned here.
top_readme = (ROOT / "README.md").read_text(encoding="utf-8")
app_readme = (ROOT / "app" / "README.md").read_text(encoding="utf-8")
decisions = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
check("the LE-radius claim quotes the shipped fit, not the rejected one",
      "better than 1 %" not in app_readme
      and "under-reads by about 6 %" in app_readme)
check("solver cores is described as the bounded field it now is",
      "*Solver cores*\nfield (any count from 1 to 32)" in app_readme
      and "*Solver cores*\nselect" not in app_readme)
check("the domain proportions are described in stack lengths, not chords",
      "chords ahead, chords behind" not in app_readme
      and "streamwise extent rather than the reference chord" in app_readme)
check("the flow view's Fluent 2D extent is described as run-dependent",
      "the walkthrough's own\ndomain rectangle on the Fluent 2D tab"
      not in app_readme
      and "the rectangle the run actually" in app_readme)
check("the install row names the components the 2D tab actually gates on",
      "native-meshing path needs no Docker" not in top_readme
      and "SpaceClaim/Discovery and Workbench for the Fluent 2D tab"
      in top_readme)
check("the mesh claim admits the clearance cap and the degrade retry",
      "exactly as the walkthrough prescribes" not in top_readme
      and "capped to the tightest slot or ground clearance" in top_readme
      and "returns an empty mesh" in top_readme)
check("the audit table is scoped as the slab route's on both columns",
      "the defaults column as well as the knob names" in decisions
      and "true-2D sizing `default`: the literal 0.1 mm" in decisions
      and "inlet / outlet / upper_bound / ground / profile" in decisions)
check("the settings subsection names the knobs it is pointed at for",
      all(f"`{k}`" in decisions for k in
          ["edge_size_mm", "first_layer_mm", "n_layers", "growth",
           "front_l", "back_l", "top_h", "sc_budget_s", "wb_budget_s"]))
check("the 'every number is a control' claim is scoped to the tab's runs",
      "Every number the 2D chain runs on is now a control" not in decisions
      and "Every number the Fluent 2D tab's runs use is now a control"
      in decisions)

print(f"\n{sum(results)}/{len(results)} fl2d frontend-contract checks passed")
sys.exit(0 if all(results) else 1)
