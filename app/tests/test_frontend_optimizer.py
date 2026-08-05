"""Optimizer-UI contract: credible newtons, honest marker, measured drag.

Static source checks in the test_frontend_rules.py spirit, pinning the
2026-08 trust round:
  * candidate cards show the expected-after-RANS value beside the claim and
    carry the >= / <= bound marks when the drag lookup was capped;
  * the green Pareto marker reads the RUN'S echoed drag_weight, never the
    live k field, and target mode marks the lowest-drag on-level point;
  * the re-rank table shows the measured RANS drag column (the number
    target-mode rows are ranked by), with measured L/D in its tooltip and
    the delta-cd caveat kept OUT of the columns (the negative controls
    showed it grades the estimate, not the design);
  * the exchange-rate tooltip says the rate applies in both modes;
  * the report's optimization table carries the Expected column and marks;
  * the max-mode done hint quotes the winner's own expectation;
  * user-facing strings carry no repo paths.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_frontend_optimizer.py
"""
import re
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
    i = src.index(marker)
    j = src.index("\n}", i)
    return src[i:j]


# ---- candidate cards ----------------------------------------------------
cands = body(js, "function renderCandidates")
check("cards read the expected-after-RANS value",
      "credible_downforce_n" in cands and "credible_gain" in cands)
check("cards render the under-claim annotation instead of a raised number",
      "credible_underclaim_possible" in cands and "may under-claim" in cands)
check("cards carry the bound marks driven by the capped-drag flag",
      "drag_is_lower_bound" in cands
      and '"≥ "' in cands and '"≤ "' in cands)
check("the expectation is labelled advisory (shared tooltip constant)",
      "EXPECTED_TIP" in cands and "const EXPECTED_TIP" in js
      and "Advisory" in js)

# ---- the Pareto marker --------------------------------------------------
par = body(js, "function renderPareto")
check("the marker reads the run's echoed drag_weight",
      "s.drag_weight" in par)
check("the marker never reads the live k field",
      'opt-drag-k' not in par)
check("max mode marks the exchange-rate pick on the EXPECTED downforce",
      "credible_downforce_n" in par)
check("target mode marks the lowest-drag on-level point (dstar first)",
      "s.dstar_n ?? s.target_downforce_n" in par)
check("the host title states the marker is the run's own pick",
      "run's own exchange rate" in par and "re-run after changing" in par)

# ---- the re-rank table --------------------------------------------------
rr = body(js, "function renderRerank")
check("the measured-drag column exists and renders drag_rans_n",
      '"RANS drag N"' in rr and "drag_rans_n" in rr)
check("measured L/D lives in the drag cell's tooltip",
      "measured L/D" in rr)
check("the delta-cd caveat is tooltip-only and names the documented bias",
      "delta_cd_pct" in rr and "4–6× low" in rr
      and '"Δcd' not in rr)
check("the flow and verdict cell indices survived the widened header",
      "tr.cells[6]" in rr and "tr.cells[7]" in rr
      and "tr.cells[5];" not in rr)

# ---- launch + persistence ----------------------------------------------
check("launch sends the true 6k weight in both modes",
      "6 * dragK" in js
      and ": 0.1;" not in js.split("const dragK")[1][:400])
check("the k field persists with the rest of the panel",
      '"opt-drag-k", "opt-minld"' in js)

# ---- copy ---------------------------------------------------------------
check("the exchange-rate tooltip names both modes",
      "Applies in both modes" in html)
check("the Pareto note explains the green point",
      "Green = the run's own pick." in html)
check("the re-rank tooltip names the drag column's role",
      "the RANS drag column shows the number those rows are ranked by"
      in html)

# ---- the report ---------------------------------------------------------
rep_i = js.index("<h2>Optimization</h2>")
rep = js[rep_i:rep_i + 2400]
check("the report table gained the Expected column",
      "<th>Expected</th>" in rep and "credible_downforce_n" in rep)
check("the report table carries the bound marks",
      '"≥ "' in rep and '"≤ "' in rep)

# ---- the max-mode done hint --------------------------------------------
hints = body(js, "function renderOptHints")
check("the done hint quotes the winner's own expectation and says the "
      "ranking uses it",
      "expects ≈" in hints and "ranked by that expectation" in hints)

# ---- no repo paths in user-facing strings ------------------------------
new_strings = [m for m in re.findall(r'"([^"\n]{20,})"', js)
               if "expected" in m.lower() or "Advisory" in m]
check("no repo paths leak into the new user-facing strings",
      all("app/" not in s and "docs/" not in s and ".py" not in s
          for s in new_strings))

# ---- the re-rank's ANSYS engine option ----
for i in ("rr-engine", "rr-mesh", "rr-sizing", "rr-parallel"):
    check(f"re-rank: #{i} present once",
          len(re.findall(f'id="{i}"', html)) == 1)
check("re-rank: the engine select offers OpenFOAM and ANSYS 2D",
      'value="openfoam" selected' in html
      and 'value="fluent2d">ANSYS Fluent 2D (reference)' in html)
check("re-rank: choosing ANSYS swaps the mesh control and hides the "
      "parallel opt-in",
      '$("rr-engine").addEventListener("change"' in js
      and '$("rr-mesh").hidden = ansys;' in js
      and '$("rr-sizing").hidden = !ansys;' in js
      and '$("rr-parallel").hidden = ansys;' in js)
check("re-rank: the ANSYS start is serial by construction and sends "
      "the sizing",
      'engine === "fluent2d" ? [1, 1]' in js
      and '? $("rr-sizing").value : $("rr-mesh").value' in js)
check("re-rank: the status line names the ANSYS engine",
      '"ANSYS 2D · "' in js)
check("re-rank: every lock/unlock site covers the new controls",
      js.count('$("rr-engine").disabled = true;') == 2
      and js.count('$("rr-engine").disabled = false;') == 1
      and js.count('$("rr-sizing").disabled = true;') == 2)

print(f"\n{sum(results)}/{len(results)} optimizer frontend checks passed")
sys.exit(0 if all(results) else 1)
