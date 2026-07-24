"""Objective modes and floors: max_downforce must return only designs
inside the measured 90% loading trust line (re-verified at full fidelity),
min_ld and min_confidence must hold on every returned candidate, and
impossible floors must fail with a message that names the cause.

Run directly:  python app/tests/test_optimizer_objectives.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import analysis, optimizer  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

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


def run_job(config, options):
    job = optimizer.Job(dict(config), dict(options))
    job.run()
    return job, job.snapshot()


def main():
    # ---- option validation ----
    for opts, why in [({"objective": "fastest"}, "unknown objective"),
                      ({"min_ld": 999}, "min_ld out of range"),
                      ({"min_ld": -1}, "negative min_ld"),
                      ({"min_ld": "big"}, "non-numeric min_ld"),
                      ({"min_confidence": 2}, "min_confidence out of range"),
                      ({"min_confidence": "hi"}, "non-numeric confidence")]:
        try:
            optimizer.Job(dict(CFG), opts)
            check(f"rejects {why}", False)
        except ValueError:
            check(f"rejects {why}", True)

    # ---- confidence knee follows the user floor (unit) ----
    ev = {"confidences": [0.6], "fracs": [0.5],
          "at_grid_edge": [False], "cd_capped": [False]}
    check("knee: conf 0.6 is free under floor 0.5",
          optimizer._confidence_penalty(ev, 0.5) == 0.0)
    check("knee: conf 0.6 is charged under floor 0.7",
          optimizer._confidence_penalty(ev, 0.7) > 0.0)

    # ---- max_downforce: trusted output ----
    ev0 = analysis.quick_objective_eval(StackConfig.from_dict(CFG))
    job, s = run_job(CFG, {"objective": "max_downforce", "mode": "global",
                           "budget": 500})
    cands = s["candidates"] or []
    w = cands[0] if cands else {}
    ws = w.get("summary") or {}
    check("max mode: run completes with a winner",
          s["state"] == "done" and bool(cands),
          f"(state {s['state']}, {len(cands)} candidates)")
    check("max mode: snapshot reports the objective",
          s["objective"] == "max_downforce"
          and s["dstar_n"] is None and s["target_note"] is None)
    check("max mode: winner and every candidate inside the trust line",
          cands and all((c.get("summary") or {}).get("frac_max", 9)
                        <= analysis.LOAD_WARN
                        + optimizer.MAXDF_FRAC_SLACK_FULL
                        for c in cands),
          f"(fracs {[ (c.get('summary') or {}).get('frac_max') for c in cands ]})")
    check("max mode: winner does not fall below the clean baseline",
          ws and ws["downforce_n"] >= ev0["downforce_n"] - 2.0,
          f"(baseline {ev0['downforce_n']:.1f} -> {ws.get('downforce_n')})")
    check("max mode: archive prefilter left clean entries to pick from",
          any(a["penalty"] < optimizer.PEN_OK
              and (a.get("frac_max") or 0) <= 0.91 for a in job.archive))

    # ---- max_downforce from the recorded hot near-stall winner ----
    flagged = json.loads(
        (ROOT / "docs" / "calibration" / "stage2_flagged.json").read_text())
    ev_hot = analysis.quick_objective_eval(StackConfig.from_dict(flagged))
    job2, s2 = run_job(flagged, {"objective": "max_downforce",
                                 "mode": "global", "budget": 500})
    c2 = s2["candidates"] or []
    check("hot start: run proceeds with the cap note (no refusal)",
          s2["state"] == "done" and s2["load_cap_note"]
          == "baseline_exceeds_cap",
          f"(state {s2['state']}, note {s2['load_cap_note']})")
    check("hot start: winner pulled inside the trust line",
          c2 and all((c.get("summary") or {}).get("frac_max", 9) <= 0.905
                     for c in c2),
          f"(baseline frac {max(ev_hot['fracs']):.2f} -> "
          f"{(c2[0].get('summary') or {}).get('frac_max') if c2 else None})")

    # ---- min_ld floor holds on the output, and it must BIND: 8.0 is
    # above the unconstrained max-mode winner's L/D (~7.4), so passing
    # requires the floor to have actually reshaped the search ----
    ld_floor = 8.0
    ld_free = (ws or {}).get("efficiency_ld")
    check("min_ld test premise: the floor binds vs the free winner",
          ld_free is not None and ld_free < ld_floor,
          f"(free-run L/D {ld_free})")
    job3, s3 = run_job(CFG, {"objective": "max_downforce", "mode": "global",
                             "budget": 1000, "min_ld": ld_floor})
    c3 = s3["candidates"] or []
    # the contract: either every returned candidate holds the floor, or
    # the run fails LOUDLY naming the floor and the best value found —
    # silently returning violators is the only wrong outcome
    held = (s3["state"] == "done" and c3
            and all((c.get("summary") or {}).get("efficiency_ld", 0)
                    >= 0.98 * ld_floor for c in c3))
    named = (s3["state"] == "failed"
             and f"L/D >= {ld_floor:.1f}" in (s3["error"] or ""))
    check("min_ld: binding floor holds on output or fails by name",
          held or named,
          f"(state {s3['state']}, lds "
          f"{[(c.get('summary') or {}).get('efficiency_ld') for c in c3]}, "
          f"err {(s3['error'] or '')[:60]})")

    # ---- impossible confidence floor fails with a named cause ----
    job4, s4 = run_job(CFG, {"target_downforce_n": 250, "mode": "global",
                             "budget": 400, "min_confidence": 0.99})
    check("impossible confidence floor: failed state, message names it",
          s4["state"] == "failed" and "confidence floor 0.99" in
          (s4["error"] or ""), f"({(s4['error'] or '')[:90]})")

    # ---- floor 0 disables the confidence drop entirely ----
    job5, s5 = run_job(CFG, {"target_downforce_n": 250, "mode": "global",
                             "budget": 400, "min_confidence": 0.0})
    check("confidence floor 0: run completes normally",
          s5["state"] == "done" and (s5["candidates"] or []))

    print(f"\n{sum(results)}/{len(results)} objective-mode checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
