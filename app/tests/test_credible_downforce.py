"""Credible newtons: the calibration-informed expectation beside the claim.

The fine-mesh RANS record maps panel optimism against peak free-air loading
(-14% near 0.85, -24.4% at 0.90, -37..-42% past the line, ~0% for the clean
cluster). credible_gain() carries that gradient as a piecewise-linear derate;
analyze() reports credible_downforce_n beside downforce_n; the optimizer's
max mode RANKS survivors by the derated expectation (J_credible) while
target mode keeps its rank (candidates sit on one level by construction and
differentiate by drag). The derate is advisory-grade (the record predates
the 2026-07-24 domain fix) — it must read as an expectation, never as a
correction, and mid-ride-height designs are annotated "may under-claim"
instead of raised (the measured +35..+67% conservative class).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_credible_downforce.py
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import analysis, optimizer  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


g = analysis.credible_gain

# ---- the knots, pinned to the measured record --------------------------
check("clean cluster keeps its claim: g(<=0.80) = 1.00",
      g(0.80) == 1.0 and g(0.5) == 1.0 and g(0.0) == 1.0)
check("g(0.85) = 0.86 (stage2_clean -14.2%, stage2b_clean -14.4%)",
      abs(g(0.85) - 0.86) < 1e-12)
check("g(0.90) = 0.76 (stage3b_maxdf_winner -24.4% at 0.900)",
      abs(g(0.90) - 0.76) < 1e-12)
check("past the line the floor clamps: g(0.95) = g(0.99) = g(99) = 0.60",
      g(0.95) == 0.60 and g(0.99) == 0.60 and g(99.0) == 0.60)
check("no loading information -> no derate: g(None) = g(nan) = 1.0",
      g(None) == 1.0 and g(float("nan")) == 1.0)

# held-out validation: the record's third gradient point was NOT a knot —
# the 0.85->0.90 segment must reproduce it, or linear is the wrong shape
check("held-out point: g(0.876) within 0.005 of the measured 0.808",
      abs(g(0.876) - 0.808) < 0.005, f"({g(0.876):.4f})")

grid = np.linspace(0.0, 1.2, 241)
vals = [g(x) for x in grid]
check("monotone non-increasing over the whole axis",
      all(a >= b - 1e-12 for a, b in zip(vals, vals[1:])))
check("bounded to [0.60, 1.00]",
      all(0.60 <= v <= 1.00 for v in vals))

# ---- analyze() plumbing ------------------------------------------------
CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15,
}
r = analysis.analyze(StackConfig.from_dict(CFG), include_geometry=False)
f = r["forces"]
frac_max = max(e["loading_fraction"] for e in r["elements"])
check("forces carry the expectation consistent with the element loading",
      abs(f["credible_downforce_n"]
          - round(f["downforce_n"] * g(frac_max), 1)) <= 0.2,
      f"(claim {f['downforce_n']}, expected {f['credible_downforce_n']}, "
      f"frac_max {frac_max})")
check("credible_gain rides beside it", f["credible_gain"] == round(g(frac_max), 3))
check("h/c 0.086 is NOT the conservative class",
      f["credible_underclaim_possible"] is False)
r60 = analysis.analyze(StackConfig.from_dict({**CFG, "ride_height_mm": 60}),
                       include_geometry=False)
check("h/c 0.171 IS annotated as possibly under-claiming (+35..+67% class)",
      r60["forces"]["credible_underclaim_possible"] is True)

# c_free is recorded per evaluation (the conservative-class predictor
# c_ground/c_free needs it in archives for a future calibration leg)
ev = analysis.quick_objective_eval(StackConfig.from_dict(CFG))
check("quick eval records c_free consistent with the analyze coefficient",
      abs(ev["c_free"]
          - r["coefficients"]["C_downforce_inviscid_free"]) < 5e-4,
      f"({ev['c_free']:.4f} vs "
      f"{r['coefficients']['C_downforce_inviscid_free']})")

# ---- max-mode ranking: expectation beats claim -------------------------
mk = optimizer.Job(dict(CFG), {"target_downforce_n": 250.0,
                               "objective": "max_downforce",
                               "budget": 400, "mode": "global"})
mk.dscale = 300.0


def entry(rank, j, dn, frac):
    return {"rank": rank, "J": j, "downforce_n": dn, "drag_n": 10.0,
            "x": [], "config": dict(CFG),
            "summary": {"downforce_n": dn, "credible_downforce_n":
                        round(dn * g(frac), 1),
                        "credible_gain": round(g(frac), 3),
                        "frac_max": frac, "confidence_min": 0.9,
                        "low_confidence": False, "near_stall": False,
                        "shadow_collapse": False, "slot_signature": False,
                        "efficiency_ld": 20.0, "warnings": 0}}


# A: bigger paper claim riding the line (300 N at 0.90 -> expects 228);
# B: smaller claim, clean (270 N at 0.80 -> expects 270). Raw J says A.
kept = mk._apply_output_filters([entry(1, 10.0, 300.0, 0.90),
                                 entry(2, 11.0, 270.0, 0.80)])
check("max mode: the clean 270 N out-ranks the paper 300 N riding the line",
      kept and kept[0]["downforce_n"] == 270.0
      and kept[0]["rank"] == 1 and kept[1]["rank"] == 2,
      f"(order {[k['downforce_n'] for k in kept]})")
check("every max-mode survivor carries its J_credible",
      all("J_credible" in k for k in kept))
check("clean survivors rank exactly as raw J (g = 1 changes nothing)",
      [k["downforce_n"] for k in
       mk._apply_output_filters([entry(1, 10.0, 300.0, 0.70),
                                 entry(2, 11.0, 270.0, 0.75)])]
      == [300.0, 270.0])

tg = optimizer.Job(dict(CFG), {"target_downforce_n": 250.0,
                               "budget": 400, "mode": "global"})
tg.dscale = 300.0
kept_t = tg._apply_output_filters([entry(1, 10.0, 300.0, 0.90),
                                   entry(2, 11.0, 270.0, 0.80)])
check("target mode keeps its rank (levels differentiate by drag, the "
      "expectation is display-only)",
      [k["downforce_n"] for k in kept_t] == [300.0, 270.0])

# ---- a real max run: every surface carries the fields ------------------
mj = optimizer.Job(dict(CFG), {"target_downforce_n": 250.0,
                               "objective": "max_downforce",
                               "budget": 500, "mode": "global",
                               "drag_weight": 1.5})
mj.run()
s = mj.snapshot()
cands = s["candidates"] or []
check("max run completes with candidates", s["state"] == "done"
      and len(cands) >= 1, f"({s['state']}, {len(cands)})")
NEED = ("credible_downforce_n", "credible_gain",
        "credible_underclaim_possible", "drag_is_lower_bound")
check("every candidate summary carries the credible + bound fields",
      all(all(k in (c["summary"] or {}) for k in NEED) for c in cands))
check("candidate order equals the J_credible order",
      [c["rank"] for c in cands]
      == [i + 1 for i in range(len(cands))]
      and all(cands[i]["J_credible"] <= cands[i + 1]["J_credible"] + 1e-9
              for i in range(len(cands) - 1)))
front = s["pareto"] or []
check("every finalized Pareto point carries the fields (front stays raw "
      "newtons on its axes; the expectation rides in the summary)",
      front and all(all(k in (p["summary"] or {}) for k in NEED)
                    for p in front))
check("the front is still non-dominated on RAW axes",
      all(front[i]["downforce_n"] > front[i + 1]["downforce_n"]
          and front[i]["drag_n"] > front[i + 1]["drag_n"]
          for i in range(len(front) - 1)))
check("the snapshot echoes drag_weight for the marker",
      "drag_weight" in s and s["drag_weight"] == 1.5,
      f"({s.get('drag_weight')})")

print(f"\n{sum(results)}/{len(results)} credible-downforce checks passed")
sys.exit(0 if all(results) else 1)
