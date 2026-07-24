"""Two-phase target search: attain then descend. The run must end on the
lowest-drag design at the achievable level — not on the first design that
hit the target, and never on a design that faked a low target by
sabotaging its own slots (penalized entries cannot define the level).

Run directly:  python app/tests/test_optimizer_target_modes.py
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
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15,
}


def run_job(options):
    job = optimizer.Job(dict(CFG), dict(options))
    job.run()
    return job


def main():
    # ---- reachable target: descend must beat the first on-target hit ----
    job = run_job({"target_downforce_n": 250, "mode": "global",
                   "budget": 500})
    s = job.snapshot()
    check("reachable target: run completes", s["state"] == "done")
    check("reachable target: D* = target, no note",
          s["dstar_n"] == 250.0 and s["target_note"] is None,
          f"(dstar {s['dstar_n']}, note {s['target_note']})")
    cands = s["candidates"] or []
    w = cands[0] if cands else {}
    ws = w.get("summary") or {}
    check("winner lands on target at full fidelity",
          ws and abs(ws["downforce_n"] - 250) <= 0.03 * 250,
          f"(got {ws.get('downforce_n')})")
    check("candidate #1 carries the lowest final-objective J",
          cands and all(w["J"] <= c["J"] + 1e-9 for c in cands))
    tol = max(optimizer.CAND_TARGET_TOL * 250, 1.0)
    # ATTAIN-phase entries only: the regression this guards is "the early
    # stop kept the first tracker hit" — comparing against descend-phase
    # entries would let the check pass vacuously
    on_target = [a for a in job.archive
                 if a["phase"] == "attain"
                 and abs(a["downforce_n"] - 250) <= tol
                 and a["penalty"] < optimizer.PEN_OK]
    check("descend improves drag over the first on-target ATTAIN design",
          on_target and w.get("drag_n") is not None
          and w["drag_n"] <= on_target[0]["drag_n"] + 0.05,
          f"(first attain hit "
          f"{on_target[0]['drag_n'] if on_target else None} N "
          f"-> winner {w.get('drag_n')} N)")
    check("archive tags phases and loading",
          {a["phase"] for a in job.archive} == {"attain", "descend"}
          and all(a["frac_max"] is not None for a in job.archive))

    # ---- determinism: the restructure must stay reproducible ----
    job_b = run_job({"target_downforce_n": 250, "mode": "global",
                     "budget": 500})
    wb = (job_b.snapshot()["candidates"] or [{}])[0]
    check("identical runs are identical (regression sentinel)",
          w.get("x") == wb.get("x"),
          f"(dn {w.get('downforce_n')} vs {wb.get('downforce_n')})")

    # ---- unreachable-low target: deliver the clean floor, say so ----
    job2 = run_job({"target_downforce_n": 5, "mode": "global",
                    "budget": 500})
    s2 = job2.snapshot()
    w2 = (s2["candidates"] or [{}])[0]
    check("low target: run completes", s2["state"] == "done")
    check("low target: floor measured and note set",
          s2["target_note"] == "unreachable_low"
          and s2["dstar_n"] is not None and s2["dstar_n"] > 5 + 1.0,
          f"(dstar {s2['dstar_n']}, note {s2['target_note']})")
    check("low target: winner delivers the floor, not the fantasy",
          w2 and abs(w2["downforce_n"] - s2["dstar_n"])
          <= max(0.03 * s2["dstar_n"], 1.0),
          f"(winner {w2.get('downforce_n')} vs floor {s2['dstar_n']})")
    # the winner's own evaluation must be clean — a sabotaged (penalized)
    # design may exist in the archive but cannot win
    win_arch = [a for a in job2.archive if a["x"] == w2.get("x")]
    check("low target: winning design carries no live penalty",
          win_arch and win_arch[-1]["penalty"] < optimizer.PEN_OK,
          f"(penalty {win_arch[-1]['penalty'] if win_arch else None})")

    # ---- unreachable D* is re-measured at FULL paneling before descend:
    # the coarse search bias can exceed the 0.8% deadzone, so the spring
    # must hold a level the full model actually measured ----
    job_d = optimizer.Job(dict(CFG), {"target_downforce_n": 500,
                                      "mode": "global", "budget": 100})
    job_d._full_panels = 70
    job_d._opt_panels = 50
    ndim = len(job_d.variables)
    job_d.archive = [
        {"x": [0.5] * ndim, "J": 1.0, "penalty": 0.0, "pen_gate": 0.0,
         "downforce_n": 300.0, "drag_n": 10.0, "frac_max": 0.8,
         "phase": "attain", "panels": 50},
        {"x": [0.4] * ndim, "J": 1.2, "penalty": 0.0, "pen_gate": 0.0,
         "downforce_n": 290.0, "drag_n": 10.5, "frac_max": 0.8,
         "phase": "attain", "panels": 50},
    ]
    _real_qe = optimizer.analysis.quick_objective_eval
    _panels_seen = []

    def _full_eval(cfg, **kw):
        _panels_seen.append(cfg.n_panels_per_side)
        return {"feasible": True, "downforce_n": 315.0, "drag_n": 11.0}

    optimizer.analysis.quick_objective_eval = _full_eval
    try:
        job_d._resolve_dstar()
    finally:
        optimizer.analysis.quick_objective_eval = _real_qe
    check("unreachable-high D* is re-measured at full paneling",
          job_d.target_note == "unreachable_high" and job_d.dstar == 315.0
          and len(_panels_seen) == 2 and all(p == 70 for p in _panels_seen),
          f"(dstar {job_d.dstar}, panels {_panels_seen})")
    check("descend seeds still compare against the search-paneling level",
          job_d._dstar_search == 300.0, f"({job_d._dstar_search})")
    # entries already at full paneling stand as measured — no re-evaluation
    for a in job_d.archive:
        a["panels"] = 70
    _panels_seen.clear()
    optimizer.analysis.quick_objective_eval = _full_eval
    try:
        job_d._resolve_dstar()
    finally:
        optimizer.analysis.quick_objective_eval = _real_qe
    check("full-paneling extremes are not re-measured",
          job_d.dstar == 300.0 and not _panels_seen,
          f"(dstar {job_d.dstar}, evals {_panels_seen})")

    # ---- local mode: two phases still run, snapshot contract holds ----
    job3 = run_job({"target_downforce_n": 250, "mode": "local",
                    "budget": 300})
    s3 = job3.snapshot()
    check("local mode: run completes with D* resolved",
          s3["state"] == "done" and s3["dstar_n"] is not None,
          f"(state {s3['state']}, dstar {s3['dstar_n']})")
    check("snapshot always carries dstar_n and target_note keys",
          "dstar_n" in s3 and "target_note" in s3)

    print(f"\n{sum(results)}/{len(results)} target-mode checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
