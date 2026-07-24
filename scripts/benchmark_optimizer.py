"""Optimizer behavior benchmark: fixed scenarios, comparable numbers.

Runs the optimizer synchronously (no server) on a fixed set of scenarios
and writes one JSON of measured outcomes per invocation. Run it once on a
code version, again on another, and diff the JSONs — the scenarios and the
seed (DE seed=1) are fixed, so any change in the numbers is a change in
behavior, not in the weather.

    .venv\\Scripts\\python.exe scripts\\benchmark_optimizer.py --out docs\\benchmarks\\baseline_main.json

Every winner is re-measured with a fresh quick_objective_eval at full
panel resolution, so the physics columns (downforce, drag, loading
fractions, slot gaps) are comparable across versions even when the
internal objective J changes meaning. Scenario keys are stable; the
before/after comparison doc joins on them.

Scenarios that need options the running code version does not support yet
(e.g. objective="max_downforce" on the pre-upgrade optimizer) are recorded
as {"supported": false} instead of failing the whole benchmark.
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import analysis, optimizer  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

# The UI's "Two-element baseline" preset (server.PRESETS), copied rather
# than imported so the benchmark does not drag the FastAPI app in.
BASELINE_2EL = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0},
    ],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}

STAGE2_FLAGGED = json.loads(
    (ROOT / "docs" / "calibration" / "stage2_flagged.json").read_text())


def scenarios() -> list[dict]:
    return [
        # The low-target pathology: 60 N is far below what this stack makes
        # even at minimum loading, so the old search rewards shedding
        # downforce however it can (slot sabotage is penalty-free at the
        # gap band edge).
        {"key": "low_target_60n", "config": BASELINE_2EL,
         "options": {"target_downforce_n": 60, "mode": "global",
                     "budget": 1500}},
        # The preset's own reachable target, non-thorough: exercises the
        # early stop (first on-target basin wins on the old code).
        {"key": "preset_target_250n", "config": BASELINE_2EL,
         "options": {"target_downforce_n": 250, "mode": "global",
                     "budget": 1500}},
        # Thorough reference: what "good" costs today.
        {"key": "thorough_250n", "config": BASELINE_2EL,
         "options": {"target_downforce_n": 250, "mode": "global",
                     "budget": 3000}},
        # Do-no-harm from the recorded hot near-stall winner: the upgrade
        # must not regress the "optimizing a hot design does not lose
        # downforce" guarantee (in target mode).
        {"key": "hot_start_430n", "config": STAGE2_FLAGGED,
         "options": {"target_downforce_n": 430, "mode": "global",
                     "budget": 1500}},
        # New-mode scenarios: recorded as unsupported on the old code.
        {"key": "max_downforce", "config": BASELINE_2EL,
         "options": {"objective": "max_downforce", "mode": "global",
                     "budget": 1500}},
        {"key": "max_downforce_ld14", "config": BASELINE_2EL,
         "options": {"objective": "max_downforce", "min_ld": 14.0,
                     "mode": "global", "budget": 1500}},
    ]


def measure_config(cfg_dict: dict) -> dict:
    """Version-independent physics of a design at full panel resolution."""
    cfg = StackConfig.from_dict(cfg_dict)
    ev = analysis.quick_objective_eval(cfg)
    if not ev.get("feasible"):
        return {"feasible": False, "reason": ev.get("reason")}
    out = {
        "feasible": True,
        "downforce_n": round(ev["downforce_n"], 1),
        "drag_n": round(ev["drag_n"], 2),
        "ld": round(ev["downforce_n"] / max(ev["drag_n"], 1e-9), 2),
        "frac_max": round(max(ev["fracs"]), 3),
        "fracs": [round(f, 3) for f in ev["fracs"]],
        "gaps_pct": [round(g * 100, 2) for g in ev["gaps"]],
        "overlaps_pct": [round(o * 100, 2) for o in ev["overlaps"]],
        "confidence_min": round(min(ev["confidences"]), 3)
        if ev.get("confidences") else None,
    }
    # present only on the upgraded analysis — recorded when available so
    # the comparison can show what the old winners would have scored
    if "crf" in ev:
        out["crf"] = [round(c, 3) for c in ev["crf"]]
    return out


def run_scenario(sc: dict) -> dict:
    t0 = time.time()
    try:
        job = optimizer.Job(dict(sc["config"]), dict(sc["options"]))
    except ValueError as exc:
        return {"supported": False, "reason": str(exc)}
    job.run()
    elapsed = round(time.time() - t0, 1)
    out = {"supported": True, "state": job.state, "error": job.error,
           "elapsed_s": elapsed, "n_eval": job.n_eval,
           "n_infeasible": job.n_infeasible}
    snap = job.snapshot()
    # version-added fields pass through when present — including on failed
    # runs (a loud guarantee failure still ran the requested objective)
    for k in ("dstar_n", "target_note", "load_cap_note", "conf_note",
              "objective"):
        if snap.get(k) is not None:
            out[k] = snap[k]
    if job.state != "done":
        return out
    cands = snap.get("candidates") or []
    out["n_candidates"] = len(cands)
    if snap.get("pareto"):
        out["pareto_n"] = len(snap["pareto"])
    if cands:
        w = cands[0]
        s = w.get("summary") or {}
        out["winner"] = {
            "J": w.get("J"),
            "penalty": None,  # candidates don't carry it; archive does
            "claimed_downforce_n": w.get("downforce_n"),
            "claimed_drag_n": w.get("drag_n"),
            "on_target": w.get("on_target"),
            "summary": {k: s.get(k) for k in
                        ("downforce_n", "drag_total_n", "efficiency_ld",
                         "warnings", "confidence_min", "low_confidence",
                         "near_stall")},
            "design": _design_fingerprint(w.get("config") or {}),
            "measured": measure_config(w["config"]) if w.get("config")
            else None,
        }
    # archive statistics: what the search actually explored
    arch = [a for a in job.archive if a.get("penalty", 99.0) < 0.5]
    if arch:
        dns = [a["downforce_n"] for a in arch]
        out["archive"] = {"n_total": len(job.archive),
                          "n_clean": len(arch),
                          "clean_downforce_min": round(min(dns), 1),
                          "clean_downforce_max": round(max(dns), 1)}
    return out


def _design_fingerprint(cfg: dict) -> dict:
    els = cfg.get("elements", [])
    return {
        "stack_aoa_deg": cfg.get("stack_aoa_deg"),
        "deflections_deg": [e.get("deflection_deg") for e in els],
        "slot_gaps_pct": [e.get("slot_gap_pct") for e in els[1:]],
        "slot_overlaps_pct": [e.get("slot_overlap_pct") for e in els[1:]],
        "airfoils": [e.get("airfoil") for e in els],
    }


def git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="output JSON path (default: docs/benchmarks/"
                         "benchmark_<rev>.json)")
    ap.add_argument("--only", nargs="*", default=None,
                    help="run only these scenario keys")
    args = ap.parse_args()
    rev = git_rev()
    out_path = (Path(args.out) if args.out else
                ROOT / "docs" / "benchmarks" / f"benchmark_{rev}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    results = {"git_rev": rev,
               "timestamp_utc": datetime.now(timezone.utc).isoformat(
                   timespec="seconds"),
               "scenarios": {}}
    for sc in scenarios():
        if args.only and sc["key"] not in args.only:
            continue
        print(f"=== {sc['key']} ...", flush=True)
        r = run_scenario(sc)
        results["scenarios"][sc["key"]] = r
        if not r.get("supported"):
            print(f"    unsupported on this version: {r.get('reason')}")
        else:
            w = r.get("winner", {}).get("measured") or {}
            print(f"    state={r['state']} evals={r['n_eval']} "
                  f"t={r['elapsed_s']}s  winner: "
                  f"dn={w.get('downforce_n')} N drag={w.get('drag_n')} N "
                  f"L/D={w.get('ld')} frac_max={w.get('frac_max')} "
                  f"gaps={w.get('gaps_pct')}")
    out_path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
