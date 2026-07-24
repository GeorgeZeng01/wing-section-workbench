"""Pareto front: the archive's clean downforce-drag trade-off, mined,
re-analyzed at full fidelity, trust-annotated, and capped — the clickable
front door the scalar objectives are shortcuts into.

Run directly:  python app/tests/test_pareto_front.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import optimizer  # noqa: E402
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


def main():
    job = optimizer.Job(dict(CFG), {"target_downforce_n": 250,
                                    "mode": "global", "budget": 500})
    job.run()
    s = job.snapshot()
    front = s["pareto"] or []
    check("front returned on done and non-empty",
          s["state"] == "done" and len(front) >= 3, f"({len(front)} points)")
    check("front capped", len(front) <= optimizer.PARETO_MAX)

    dominated = any(
        (b["downforce_n"] >= a["downforce_n"]
         and b["drag_n"] <= a["drag_n"]
         and (b["downforce_n"] > a["downforce_n"]
              or b["drag_n"] < a["drag_n"]))
        for a in front for b in front if a is not b)
    check("front is non-dominated at full fidelity", not dominated)

    w = (s["candidates"] or [{}])[0]
    check("front reaches at least the winner's downforce",
          front and max(p["downforce_n"] for p in front)
          >= (w.get("summary") or {}).get("downforce_n", 1e9) - 1.0,
          f"(front max {max(p['downforce_n'] for p in front):.1f} vs winner "
          f"{(w.get('summary') or {}).get('downforce_n')})")
    check("front extends below the winner's drag (the cheap end exists)",
          front and min(p["drag_n"] for p in front)
          <= w.get("drag_n", -1e9) + 0.05)

    check("every front point carries trust summary keys",
          all(p.get("summary")
              and all(k in p["summary"] for k in
                      ("confidence_min", "low_confidence", "near_stall",
                       "frac_max", "slot_signature", "efficiency_ld"))
              for p in front))
    ok_rt = True
    for p in front:
        try:
            StackConfig.from_dict(p["config"])
        except Exception:
            ok_rt = False
    check("front configs round-trip through StackConfig", ok_rt)

    # front points must come from the clean pool (penalized evaluations
    # exist in the archive but cannot appear on the front)
    by_x = {tuple(a["x"]): a for a in job.archive}
    src = [by_x.get(tuple(p["x"])) for p in front]
    check("front sources are clean archive entries",
          all(a is not None and a["penalty"] < optimizer.PEN_OK
              for a in src))

    cloud = s["cloud"] or []
    check("snapshot carries the trade-off cloud",
          len(cloud) > 0 and len(cloud) <= 250
          and all(len(c) == 3 and c[2] in (0, 1, 2) for c in cloud),
          f"({len(cloud)} points)")

    print(f"\n{sum(results)}/{len(results)} pareto-front checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
