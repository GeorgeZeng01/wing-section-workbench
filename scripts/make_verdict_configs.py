"""Produce the stage-9 verdict configs: the trusted max-downforce winner
and the Pareto knee from a seeded Standard run on the two-element
baseline, written as config JSONs for scripts/rans_calibration.py
--config runs (fine mesh, judged against the recorded -14% healthy
baseline in docs/calibration/LOG.md).

    .venv\\Scripts\\python.exe scripts\\make_verdict_configs.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import optimizer  # noqa: E402

BASELINE = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0},
    ],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}

OUT = ROOT / "docs" / "calibration"


def knee_of(front):
    if len(front) < 3:
        return None
    dn = [p["downforce_n"] for p in front]
    dr = [p["drag_n"] for p in front]
    dn_s = (max(dn) - min(dn)) or 1.0
    dr_s = (max(dr) - min(dr)) or 1.0
    a, b = front[0], front[-1]
    ax, ay = a["drag_n"] / dr_s, a["downforce_n"] / dn_s
    bx, by = b["drag_n"] / dr_s, b["downforce_n"] / dn_s
    ln = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 or 1.0
    best, bd = None, 0.0
    for p in front:
        px, py = p["drag_n"] / dr_s, p["downforce_n"] / dn_s
        d = abs((bx - ax) * (ay - py) - (ax - px) * (by - ay)) / ln
        if d > bd:
            best, bd = p, d
    return best


def main() -> int:
    job = optimizer.Job(dict(BASELINE),
                        {"objective": "max_downforce", "mode": "global",
                         "budget": 1500})
    job.run()
    s = job.snapshot()
    if s["state"] != "done" or not s["candidates"]:
        print(f"run did not finish usable: {s['state']} {s['error']}")
        return 1
    w = s["candidates"][0]
    (OUT / "stage3_maxdf_winner.json").write_text(
        json.dumps(w["config"], indent=1), encoding="utf-8")
    ws = w["summary"]
    print(f"max-downforce winner: {ws['downforce_n']:.1f} N, "
          f"drag {ws['drag_total_n']:.2f} N, frac_max {ws['frac_max']:.3f} "
          f"-> docs/calibration/stage3_maxdf_winner.json")
    knee = knee_of(s["pareto"] or [])
    if knee is not None:
        (OUT / "stage3_pareto_knee.json").write_text(
            json.dumps(knee["config"], indent=1), encoding="utf-8")
        print(f"pareto knee: {knee['downforce_n']:.1f} N, "
              f"drag {knee['drag_n']:.2f} N "
              f"-> docs/calibration/stage3_pareto_knee.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
