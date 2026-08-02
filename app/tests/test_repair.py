"""Auto-repair: the cheap-model search for a design without the collapse.

Pins app/core/repair.py.

The contract that matters most here is honesty about what a repair IS. The
search runs on the panel model, and the panel model's separation screen is
the same screen that missed the collapse being repaired. So a repair is a
CANDIDATE, never a result, and every verdict carries verified=False and says
why. A future change that lets this module imply verification breaks here.

Second contract: a design that cannot be repaired inside its budget is a
verdict, not an error. It returns "infeasible" with the price of the
cheapest repair attached — because "no" and "not at that price" are
different findings, and only one of them is actionable.

Third: no new threshold. The target is built from two shipped constants
(SHADOW_WARN + SHADOW_HEADROOM); this module invents no line of its own.

Measured on the retained record (too slow for this suite; see the handoff):
all three gate cases are infeasible inside a 5% downforce budget, and the
cheapest repair costs 24.7-29.1% downforce while cutting drag 27.3-44.9% —
so every available repair is L/D-positive, and the collapse on these designs
is load-driven rather than a tuning miss.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_repair.py
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import optimizer, repair, wake_shadow  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# a loaded two-element stack; small eval budgets keep the suite fast — the
# expensive full-budget runs against the record live in the handoff
TWO = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 26.0,
         "slot_gap_pct": 1.0, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": -2.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 45,
}
ONE = {
    "elements": [{"airfoil": "s1223", "chord_ratio": 1.0,
                  "deflection_deg": 0}],
    "stack_aoa_deg": -2.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 45,
}
FAST = {"max_evals": 70, "n_starts": 1}


def main() -> bool:
    # ---- the target is assembled, not invented -----------------------
    check("the repair target is built from two SHIPPED constants",
          abs(repair.REPAIR_TARGET
              - (wake_shadow.SHADOW_WARN + optimizer.SHADOW_HEADROOM)) < 1e-12,
          f"({repair.REPAIR_TARGET})")
    check("it aims ABOVE the caution top, not at it — aiming at a line is "
          "how designs end up parked on it",
          repair.REPAIR_TARGET > wake_shadow.SHADOW_WARN
          > wake_shadow.SHADOW_SEP)
    src = (ROOT / "app" / "core" / "repair.py").read_text(encoding="utf-8")
    check("repair defines no separation threshold of its own",
          "SHADOW_SEP =" not in src and "SHADOW_WARN =" not in src
          and "SEP_ATTACHED_MAX" not in src)

    # ---- a stack with no shadow reading cannot be 'repaired' ---------
    r1 = repair.repair(ONE, **FAST)
    check("a single-element stack has nothing to clear and says so",
          r1["status"] == "already_clean"
          and "single-element" in r1["reason"], f"({r1['status']})")

    # ---- unmeasurable input is a verdict, not a crash ----------------
    broken = copy.deepcopy(TWO)
    broken["elements"][1]["slot_gap_pct"] = -50.0
    rb = repair.repair(broken, **FAST)
    check("a seed that cannot be evaluated returns a verdict, not an "
          "exception",
          rb["status"] in ("unmeasurable", "infeasible", "already_clean",
                           "repaired") and "reason" in rb,
          f"({rb['status']})")

    # ---- the loaded stack: whatever the outcome, the contract holds --
    r = repair.repair(TWO, **FAST)
    check("a repair verdict is one of the four declared states",
          r["status"] in ("already_clean", "repaired", "infeasible",
                          "unmeasurable"), f"({r['status']})")
    check("NOTHING here claims verification",
          r["verified"] is False, f"({r['verified']})")
    check("the seed measurement always travels with the verdict",
          r["seed"] is not None and "worst_shadow" in r["seed"]
          and "downforce_n" in r["seed"] and "drag_n" in r["seed"])
    check("the target and budget are reported, so a verdict is readable "
          "without knowing the defaults",
          abs(r["target"] - repair.REPAIR_TARGET) < 1e-6
          and r["budget_pct"] == repair.REPAIR_BUDGET_PCT)
    check("the evaluation count is reported and bounded",
          0 < r["n_evals"] <= FAST["max_evals"] * 2 + 5, f"({r['n_evals']})")

    if r["status"] == "repaired":
        check("a repaired candidate clears the target",
              r["best"]["worst_shadow"] >= r["target"],
              f"({r['best']['worst_shadow']})")
        check("a repaired candidate respects the downforce budget",
              r["best"]["downforce_n"]
              >= r["seed"]["downforce_n"] * (1 - r["budget_pct"] / 100) - 1e-6)
        check("a repaired candidate carries a config and the deltas",
              r["config"] is not None
              and r["downforce_delta_pct"] is not None
              and r["drag_delta_pct"] is not None)
        check("the repaired config is a real config, not the seed object",
              r["config"] is not TWO
              and "elements" in r["config"])
        check("a repair candidate says plainly it has not been solved",
              "verify_note" in r and "solver" in r["verify_note"])
    elif r["status"] == "infeasible":
        check("an unrepairable design prices the refusal instead of just "
              "refusing",
              "cheapest_repair" in r
              and "cheapest_repair_downforce_delta_pct" in r)
        if r.get("cheapest_repair"):
            check("the priced repair actually clears the target",
                  r["cheapest_repair"]["worst_shadow"] >= r["target"])
            check("the priced repair costs more than the budget allowed — "
                  "which is exactly why it was refused",
                  r["cheapest_repair_downforce_delta_pct"]
                  < -r["budget_pct"] + 1e-6,
                  f"({r['cheapest_repair_downforce_delta_pct']}% vs "
                  f"{r['budget_pct']}% budget)")
        check("no repaired config is offered when nothing was accepted",
              r["config"] is None and r["best"] is None)

    # ---- determinism -------------------------------------------------
    a = repair.repair(TWO, **FAST)
    b = repair.repair(TWO, **FAST)
    check("two repairs of the same seed agree exactly — no RNG anywhere",
          json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))

    # ---- budget is honoured as an input ------------------------------
    try:
        repair.repair(TWO, budget_pct=-1.0, **FAST)
        bad = False
    except ValueError:
        bad = True
    check("a nonsensical budget is refused eagerly", bad)
    tight = repair.repair(TWO, budget_pct=0.0, **FAST)
    check("a zero budget can never return a design that loses downforce",
          tight["status"] != "repaired"
          or tight["best"]["downforce_n"] >= tight["seed"]["downforce_n"] - 1e-6)

    # ---- the searcher is REUSED, not rewritten -----------------------
    check("the search reuses the optimizer's variables/decode/seed rather "
          "than defining a parallel search space",
          "optimizer.build_variables" in src
          and "optimizer.apply_vector" in src
          and "optimizer.initial_vector" in src)
    check("feasibility comes from the same gate the optimizer uses, so a "
          "repair cannot propose what the optimizer would refuse",
          "quick_objective_eval" in src)

    print(f"\n{sum(results)}/{len(results)} repair checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
