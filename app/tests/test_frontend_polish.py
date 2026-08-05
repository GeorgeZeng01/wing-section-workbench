"""Adjoint-polish frontend contract — static source checks.

The polish panel's DOM ids, the JS state machine's presence and its
integration with the shared solver gates, the API wrappers, the pin
channel, the chart registration and the theme re-ink — asserted against
the shipped sources, so a rename or a dropped wire fails here before a
user ever clicks it.

Run directly:  python app/tests/test_frontend_polish.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
API = (ROOT / "app" / "static" / "js" / "api.js").read_text(encoding="utf-8")
SRV = (ROOT / "app" / "server.py").read_text(encoding="utf-8")

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# ---- DOM contract: every id the JS drives exists exactly once ----

IDS = ["polish-block", "btn-polish-run", "btn-polish-cancel",
       "polish-note", "polish-settings", "polish-k", "polish-iters",
       "polish-flow-iters", "polish-adjoint-iters", "polish-margin",
       "polish-settle", "polish-step", "polish-auto",
       "polish-progress", "polish-status",
       "polish-charts", "polish-conv", "polish-result", "polish-actions",
       "btn-polish-reverify", "btn-polish-dxf", "btn-polish-case"]
for i in IDS:
    n = len(re.findall(f'id="{i}"', HTML))
    check(f"html: #{i} present once", n == 1, f"(x{n})")

check("html: the polish block lives inside the Fluent 2D tab",
      HTML.index('id="tab-fluent2d"') < HTML.index('id="polish-block"')
      < HTML.index('id="tab-pinned"'))
check("html: settings defaults mirror the server body's",
      'id="polish-k" type="number"\n                  step="0.05" '
      'min="0" max="10" value="0.25"' in HTML
      and 'id="polish-iters" type="number"\n                  step="1" '
      'min="1" max="60" value="12"' in HTML)
check("html: the group note states the doctrine (bounded morph, "
      "re-checks, provisional numbers)",
      "clipped to the rule envelope" in HTML
      and "aft-thickness floor" in HTML
      and "Re-verify runs the polished profiles" in HTML)

# ---- app.js: the state machine and its wiring ----

for fn in ["function gatePolish()", "async function startPolish()",
           "function pollPolish()", "function renderPolish(",
           "function renderPolishResult(",
           "function renderPolishReverify(",
           "async function capturePolishFlow()"]:
    check(f"app.js: {fn.split('(')[0].split()[-1]} defined", fn in APP)

check("app.js: polish state uses var (shared gates may run during "
      "module evaluation)",
      "var polishJob = null" in APP and "var polishResult = null" in APP
      and "var fl2dReverifyOf = null" in APP)
check("app.js: every start guard knows the polish job",
      len(re.findall(r"polishJob", APP)) >= 12)
gates = APP[APP.index("function updateSolverButtons"):]
gates = gates[:gates.index("\n}")]
check("app.js: updateSolverButtons gates on polish and re-gates it",
      "polishJob" in gates and "gatePolish()" in gates)
rerank = APP[APP.index("async function gateRerank"):]
rerank = rerank[:rerank.index("\n}")]
check("app.js: gateRerank refuses while a polish runs",
      "polishJob" in rerank)
check("app.js: startFl2d and startRansVerify refuse while a polish runs",
      re.search(r"function startFl2d\(\).{0,400}polishJob", APP,
                re.S) is not None
      and re.search(r"function startRansVerify\(\).{0,400}polishJob",
                    APP, re.S) is not None)
check("app.js: polish history charts through the self-healing lineChart",
      'lineChart($("polish-conv")' in APP)
check("app.js: theme flip re-inks the polish chart",
      "polishLast) {\n    renderPolish(polishLast);" in APP)
check("app.js: pin capture carries the polish channel",
      "out.polish = await runSnapshot(polishResult" in APP)
check("app.js: reverify hands the run to the fl2d machinery with "
      "polish provenance",
      "function attachReverifyRun(jobId, polishId)" in APP
      and '? "polish"' in APP
      and 'renderPolishReverify(s)' in APP)
check("app.js: the server-chained re-verify auto-attaches on completion",
      "async function autoAttachReverify(s)" in APP
      and "autoAttachReverify(s);" in APP
      and "reverify_job_id" in APP and "reverify_error" in APP)
check("app.js: the start body carries the step request and the "
      "auto-reverify contract",
      "step_pct: stepPct" in APP
      and 'auto_reverify: $("polish-auto").checked' in APP)
check("app.js: the chart starts at the baseline (iteration 0)",
      "const hist = base ? [{ ...base, iter: 0 }, ...s.history]" in APP)
check("app.js: a floor-tight no-gain run points at the step knob",
      "A smaller " in APP and "Step request morphs more gently" in APP)
check("app.js: the polish provenance names the free-form contour",
      "re-verification of the POLISHED shape" in APP)
check("app.js: a live polish from another window is re-attached",
      'live && s.engine === "polish" && !polishJob' in APP)
check("app.js: newtons on the chart axis, baseline as the target line",
      '"newtons (credible, span-scaled)"' in APP
      and "baseline J" in APP)

# ---- api.js: the wrappers and their endpoints ----

check("api.js: polishStart posts /api/polish/start",
      "polishStart:" in API and '"/api/polish/start"' in API)
check("api.js: polishReverify posts the job-scoped path",
      "polishReverify:" in API
      and "`/api/polish/${id}/reverify`" in API)
check("api.js: polishExportDxf posts the job-scoped path",
      "polishExportDxf:" in API
      and "`/api/polish/${id}/export/dxf`" in API)

# ---- server: the pin channel and the endpoint set ----

check("server: pins admit the polish run channel",
      '_PIN_RUN_KEYS = ("rans", "fl2d", "polish")' in SRV)
for route in ['@app.post("/api/polish/start")',
              '@app.post("/api/polish/{job_id}/reverify")',
              '@app.post("/api/polish/{job_id}/export/dxf")']:
    check(f"server: {route.split(chr(34))[1]} declared", route in SRV)
check("server: the ANSYS export and the stop refusal know the polish "
      "engine",
      '("fluent", "fluent2d", "polish")' in SRV
      and SRV.count('"polish"') >= 4)

print(f"\n{sum(results)}/{len(results)} frontend-polish checks passed")
sys.exit(0 if all(results) else 1)
