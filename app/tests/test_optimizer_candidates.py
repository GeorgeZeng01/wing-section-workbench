"""Optimizer candidate-set regression (no server needed).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_optimizer_candidates.py

The optimizer must return several genuinely different on-target designs,
not just the single optimum (and not jitter around it).
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import optimizer  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}
TARGET = 250.0

job = optimizer.Job(CFG, {"target_downforce_n": TARGET,
                          "budget": 400, "mode": "global"})
job.run()
snap = job.snapshot()

check("job finishes", snap["state"] == "done",
      f"({snap['state']}, {snap['n_eval']} evals)")

cands = snap["candidates"] or []
check("multiple candidates returned", 2 <= len(cands) <= optimizer.CAND_MAX,
      f"({len(cands)})")

check("candidate #1 is the best design",
      cands and cands[0]["J"] == min(c["J"] for c in cands),
      f"(J {[c['J'] for c in cands]})")

tol = max(optimizer.CAND_TARGET_TOL * TARGET, 1.0)
check("every candidate is on target (search had on-target designs)",
      all(abs(c["downforce_n"] - TARGET) <= tol and c["on_target"]
          for c in cands),
      f"({[c['downforce_n'] for c in cands]} N)")

# diversity: pairwise worst-variable distance above the selection threshold
spans = [v["hi"] - v["lo"] for v in snap["variables"]]
dmin = min(
    max(abs(a - b) / s for a, b, s in zip(ci["x"], cj["x"], spans))
    for i, ci in enumerate(cands) for cj in cands[i + 1:]
) if len(cands) > 1 else 0.0
check("candidates are genuinely different designs",
      dmin >= optimizer.CAND_DIVERSITY - 1e-9,
      f"(min pairwise distance {dmin:.3f}, threshold {optimizer.CAND_DIVERSITY})")

check("each candidate carries an applicable config + analysis summary",
      all(c["config"].get("elements") and c["summary"] is not None
          and np.isfinite(c["summary"]["downforce_n"]) for c in cands))

# configs must round-trip through the analysis pipeline (what Apply does)
from app.core import analysis, geometry  # noqa: E402
ok_apply = True
for c in cands:
    try:
        r = analysis.analyze(geometry.StackConfig.from_dict(c["config"]),
                             include_geometry=False)
        if abs(r["forces"]["downforce_n"] - c["summary"]["downforce_n"]) > 2.0:
            ok_apply = False
    except Exception:
        ok_apply = False
check("candidate configs re-analyze to their claimed numbers", ok_apply)

# hot-baseline regression: a design already loaded far past the absolute
# 105%-of-CL_max band must still be matchable — the run may never come back
# with LESS downforce than the start when the target asks for more. (The
# absolute band did exactly that: every optimization of an aggressive design
# lost downforce, because the search refused the baseline's loading level.)
HOT = {**CFG, "stack_aoa_deg": 6.0}
hot_base = analysis.analyze(geometry.StackConfig.from_dict(HOT),
                            include_geometry=False)
hot_dn = hot_base["forces"]["downforce_n"]
hot_frac = max(e["loading_fraction"] for e in hot_base["elements"])
hot_target = round(hot_dn * 1.05)
job_h = optimizer.Job(HOT, {"target_downforce_n": hot_target,
                            "budget": 600, "mode": "global"})
job_h.run()
snap_h = job_h.snapshot()
full_h = (snap_h["result"] or {}).get("forces", {}).get("downforce_n")
check("hot baseline is genuinely past the absolute load band",
      hot_frac > 1.20, f"(max loading fraction {hot_frac})")
check("optimizing a hot design does not lose downforce",
      snap_h["state"] == "done" and full_h is not None
      and full_h >= hot_dn - 2.0,
      f"(baseline {hot_dn} N -> optimized {full_h} N)")
check("hot design reaches an above-baseline target",
      full_h is not None and abs(full_h - hot_target) <= 0.03 * hot_target,
      f"(target {hot_target} N, got {full_h} N)")

# explicit API bounds stay hard even when they exclude the baseline: the
# widened search bounds only apply to DEFAULT bounds. Values OUTSIDE the
# defaults exercise the widening for real (13 deg > default hi of 12,
# 70 deg > deflection hi of 60).
WIDE = {**CFG, "stack_aoa_deg": 13.0,
        "elements": [CFG["elements"][0],
                     {**CFG["elements"][1], "deflection_deg": 70.0}]}
vars_hard = optimizer.build_variables(
    WIDE, {"bounds": {"stack_aoa_deg": (-4.0, 4.0)}})
aoa_hard = next(v for v in vars_hard if v["key"] == "stack_aoa_deg")
vars_soft = optimizer.build_variables(WIDE, {})
aoa_soft = next(v for v in vars_soft if v["key"] == "stack_aoa_deg")
dfl_soft = next(v for v in vars_soft if v["key"] == "deflection_deg")
check("explicit bounds stay hard, default bounds widen to the baseline",
      aoa_hard["hi"] == 4.0 and aoa_soft["hi"] == 13.0
      and dfl_soft["hi"] == 70.0,
      f"(hard hi {aoa_hard['hi']}, widened aoa {aoa_soft['hi']}, "
      f"defl {dfl_soft['hi']})")

# an already-shaped design being re-optimized: its unwrapped shape seed
# must be representable even when it sits outside the generic shape bounds
SHAPED = {**CFG, "elements": [
    {**CFG["elements"][0], "_shape_x0": [0.05, 0.0, -0.05, 0.62]},
    CFG["elements"][1]]}
vars_sh = optimizer.build_variables(SHAPED, {"opt_shape": True})
b25 = next(v for v in vars_sh if v["key"] == "shape_b25" and v["elem"] == 0)
ts = next(v for v in vars_sh if v["key"] == "shape_ts" and v["elem"] == 0)
check("shape seeds outside generic bounds stay representable",
      b25["hi"] >= 0.05 and ts["lo"] <= 0.62,
      f"(b25 hi {b25['hi']}, ts lo {ts['lo']})")

# determinism: identical inputs must give the identical result
job2 = optimizer.Job(CFG, {"target_downforce_n": TARGET,
                           "budget": 400, "mode": "global"})
job2.run()
b1, b2 = snap["best"], job2.snapshot()["best"]
check("identical runs are identical (seeded search)",
      b1["J"] == b2["J"] and b1["x"] == b2["x"],
      f"(J {b1['J']:.5f} vs {b2['J']:.5f})")

# thorough consistency: jittered starting configs must converge to the same
# lowest-drag basin (full exploration + multi-start full-res polish).
# Coarse paneling keeps this check affordable; it is the slowest one here.
drags = []
for defl, gap in ((24.0, 1.5), (24.7, 1.4)):
    cfg_j = {**CFG, "n_panels_per_side": 40,
             "elements": [CFG["elements"][0],
                          {**CFG["elements"][1], "deflection_deg": defl,
                           "slot_gap_pct": gap}]}
    job_t = optimizer.Job(cfg_j, {"target_downforce_n": TARGET,
                                  "budget": 3000, "mode": "global"})
    job_t.run()
    c1 = (job_t.snapshot()["candidates"] or [{}])[0]
    drags.append((c1.get("summary") or {}).get("drag_total_n"))
spread = (max(drags) - min(drags)) if all(d is not None for d in drags) else 99
check("thorough mode: jittered starts converge to the same drag",
      spread < 1.0, f"(drags {drags}, spread {spread:.2f} N)")

print(f"\n{sum(results)}/{len(results)} optimizer-candidate checks passed")
sys.exit(0 if all(results) else 1)
