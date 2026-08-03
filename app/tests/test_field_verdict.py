"""The ADOPTED two-mode field attachment verdict (2026-08-03).

Pins cfd_run.field_verdict: the grading of a recirculation report by the
wall-verified lines — worst near-wall probe >= 0.28 (confluence mode) OR
open-census cells >= 1000 (main/off-body mode), each with a knife band
sitting in the measured gap of the record (probes: separated >= 0.318 vs
quiet <= 0.22; census: failing tier 3459-8272 vs everything else <= 377).

Contracts protected here:
  * an unmeasurable report grades None, never a pass;
  * "clean" is worded as absence of evidence, never as "attached" — the
    probe under-reads the wall everywhere (measured same-solve);
  * both modes fire independently and the knife bands are honest;
  * the verdict rides on both engines' results and the queue demotes on
    it exactly like wall separation, with wall outranking field.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_field_verdict.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def rec(probes=(0.0, 0.0), open_regions=(), status="measured"):
    return {"status": status,
            "elements": [{"near_wall_reversed_frac": p} for p in probes],
            "regions": [{"n_cells": n, "reattaches": False}
                        for n in open_regions]}


FV = cfd_run.field_verdict

# ---- absence is absence ------------------------------------------------
check("no report grades None, never a pass", FV(None) is None)
check("an unknown-status report grades None",
      FV({"status": "unknown"}) is None)
check("an element with no reading does not fake a zero",
      FV({"status": "measured",
          "elements": [{"near_wall_reversed_frac": None}],
          "regions": []})["worst_probe"] is None)

# ---- mode A: the probe line -------------------------------------------
v = FV(rec(probes=(0.02, 0.39)))
check("probe 0.39 fires mode A separated",
      v["separated"] is True and v["mode"] == "probe"
      and "separated" in v["verdict"], f"({v['verdict'][:60]})")
check("the golden collapse reading (0.3848) is separated, no knife",
      FV(rec(probes=(0.02, 0.3848)))["separated"] is True
      and FV(rec(probes=(0.02, 0.3848)))["knife_edge"] is False)
v = FV(rec(probes=(0.27,)))
check("probe 0.27 is knife-edge, not a stable side",
      v["separated"] is False and v["knife_edge"] is True
      and "knife" in v["verdict"])
v = FV(rec(probes=(0.29,)))
check("probe 0.29 is separated AND knife-flagged",
      v["separated"] is True and v["knife_edge"] is True)
check("probe 0.22 (the quiet-side maximum observed) is clean of mode A",
      FV(rec(probes=(0.22,)))["separated"] is False)

# ---- mode B: the census line ------------------------------------------
v = FV(rec(open_regions=(3459,)))
check("3459 open cells fires mode B (the measured failing tier's floor)",
      v["separated"] is True and v["mode"] == "census"
      and "non-reattaching" in v["verdict"])
check("377 open cells (the benign tier's ceiling) does not fire",
      FV(rec(open_regions=(377,)))["separated"] is False)
v = FV(rec(open_regions=(800,)))
check("800 open cells is knife-edge", v["separated"] is False
      and v["knife_edge"] is True)
check("open cells sum across regions",
      FV(rec(open_regions=(600, 600)))["separated"] is True)
check("reattaching regions never count",
      FV({"status": "measured", "elements": [],
          "regions": [{"n_cells": 9000, "reattaches": True}]})
      ["separated"] is False)

# ---- both modes --------------------------------------------------------
v = FV(rec(probes=(0.40,), open_regions=(5000,)))
check("both modes firing reads mode 'both'", v["mode"] == "both"
      and v["separated"] is True)

# ---- honesty of the clean wording -------------------------------------
v = FV(rec(probes=(0.01,)))
check("a clean grading never says 'attached' — the field under-reads "
      "the wall", "attached" not in v["verdict"].split("not 'attached'")[0]
      and "no separation evidence" in v["verdict"])

# ---- wiring ------------------------------------------------------------
for name in ("cfd_run", "fluent2d_run"):
    src = (ROOT / "app" / "core" / f"{name}.py").read_text(encoding="utf-8")
    check(f"{name} publishes field_verdict on the result",
          '"field_verdict"' in src)
f2 = (ROOT / "app" / "core" / "fluent2d_run.py").read_text(encoding="utf-8")
check("fluent2d grades AFTER the flow export (the field exists only then)",
      f2.find("cfd_run.field_verdict(") > f2.find(
          "self._export_flow_fields(fm"))
q = (ROOT / "app" / "core" / "rans_queue.py").read_text(encoding="utf-8")
check("the queue demotes on the field verdict where wall shear is absent",
      'r.get("field_verdict")' in q
      and 'fv.get("separated")' in q)
tier = q[q.find("def sep_tier"):q.find("def sep_tier") + 700]
check("wall outranks field in the tier (wall checked first, inside "
      "sep_tier itself)",
      0 < tier.find('r.get("worst_reversed")')
      < tier.find('r.get("field_verdict")'))
js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("both result cards surface the field verdict",
      js.count('Attachment (field)') >= 2)
check("the queue's flow cell marks the field channel with '*' and "
      "tooltips the full verdict",
      '"separated*"' in js and "fv.verdict" in js)
check("the harvest row carries the verdict",
      '"field_verdict": result.get("field_verdict")'
      in (ROOT / "app" / "core" / "cfd_run.py").read_text(encoding="utf-8"))

print(f"\n{sum(results)}/{len(results)} field-verdict checks passed")
sys.exit(0 if all(results) else 1)
