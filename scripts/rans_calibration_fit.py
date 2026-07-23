"""Fit the k_g(h/c) realization curve to the RANS campaign log.

Reads docs/calibration/runs.csv, keeps converged runs that produced a
RANS-implied k_g, and fits the model's curve form

    k_g(h/c) = A * tanh(S / A * h/c)        (current: A=0.85, S=1.4)

by least squares on the implied-k_g points. Reports, for every point:
the implied k_g, the current curve's value, the refit curve's value, and
the C_est error each produces against the RANS Cl (recomputed through
the same cap/choke model the estimate uses). Read-only: this script
proposes numbers, it changes nothing.

    .venv\\Scripts\\python.exe scripts\\rans_calibration_fit.py
"""
import csv
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

LOG_CSV = ROOT / "docs" / "calibration" / "runs.csv"

ETA = 0.85          # viscous_efficiency of the campaign config
CAP_RATIO = 3.0     # gain_cap_ratio default
CAP_FLOOR = 0.1
CHOKE_HC = 0.045    # choke_h_c default


def c_est(k_g: float, c_free: float, c_ground: float, h_c: float) -> float:
    g_inv = c_ground - c_free
    cap = CAP_RATIO * max(abs(c_free), CAP_FLOOR)
    choke = math.tanh(h_c / CHOKE_HC)
    return ETA * (c_free + cap * math.tanh(k_g * g_inv / cap) * choke)


def curve(h_c, a, s):
    return a * np.tanh(s / a * np.asarray(h_c, float))


def main() -> int:
    rows = list(csv.DictReader(LOG_CSV.open(encoding="utf-8")))
    pts = []
    for r in rows:
        if r["state"] != "done" or r["converged"] != "True":
            continue
        if not r["suggested_k_g"]:
            print(f"note: {r['label']} converged but yielded no implied k_g "
                  f"(outside the model's reachable range) — excluded")
            continue
        pts.append({
            "label": r["label"], "h_c": float(r["h_over_c"]),
            "k_imp": float(r["suggested_k_g"]),
            "c_free": float(r["c_free"]), "c_ground": float(r["c_ground"]),
            "cl_rans": float(r["cl_rans"]), "cl_std": float(r["cl_rans_std"]),
            "mesh": r["mesh"],
        })
    if len(pts) < 3:
        print(f"only {len(pts)} usable points — not enough to fit")
        return 1
    pts.sort(key=lambda p: p["h_c"])
    coarse = [p for p in pts if p["mesh"] == "coarse"]

    from scipy.optimize import least_squares
    h = np.array([p["h_c"] for p in coarse])
    k = np.array([p["k_imp"] for p in coarse])
    fit = least_squares(lambda x: curve(h, *x) - k, x0=[0.85, 1.4],
                        bounds=([1e-3, 1e-3], [1.0, 10.0]))
    a, s = fit.x

    print(f"fit over {len(coarse)} coarse points: "
          f"k_g(h/c) = {a:.3f} * tanh({s:.3f}/{a:.3f} * h/c)   "
          f"(current: 0.850 * tanh(1.400/0.850 * h/c))")
    print(f"{'label':24} {'h/c':>6} {'k_imp':>6} {'k_cur':>6} {'k_fit':>6} "
          f"{'Cl_rans':>8} {'dCest_cur%':>10} {'dCest_fit%':>10}")
    for p in pts:
        k_cur = float(curve(p["h_c"], 0.85, 1.4))
        k_fit = float(curve(p["h_c"], a, s))
        e_cur = c_est(k_cur, p["c_free"], p["c_ground"], p["h_c"])
        e_fit = c_est(k_fit, p["c_free"], p["c_ground"], p["h_c"])
        d_cur = (e_cur / p["cl_rans"] - 1) * 100
        d_fit = (e_fit / p["cl_rans"] - 1) * 100
        print(f"{p['label']:24} {p['h_c']:6.3f} {p['k_imp']:6.3f} "
              f"{k_cur:6.3f} {k_fit:6.3f} {p['cl_rans']:8.3f} "
              f"{d_cur:+10.1f} {d_fit:+10.1f}")
    resid = curve(h, a, s) - k
    print(f"fit residual (k_g units): max |r| {np.abs(resid).max():.3f}, "
          f"rms {np.sqrt((resid ** 2).mean()):.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
