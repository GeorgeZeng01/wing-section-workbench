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
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import analysis  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

LOG_CSV = ROOT / "docs" / "calibration" / "runs.csv"

# model constants come from the app itself: the campaign runs the StackConfig
# defaults, and a local copy would silently diverge from what C_est actually
# used the day the defaults change
_DEFAULTS = StackConfig()
ETA = _DEFAULTS.viscous_efficiency
CAP_RATIO = _DEFAULTS.gain_cap_ratio
CAP_FLOOR = analysis.CAP_FLOOR_C
CHOKE_HC = _DEFAULTS.choke_h_c


def c_est(k_g: float, c_free: float, c_ground: float, h_c: float) -> float:
    g_inv = c_ground - c_free
    cap = CAP_RATIO * max(abs(c_free), CAP_FLOOR)
    choke = math.tanh(h_c / CHOKE_HC)
    return ETA * (c_free + cap * math.tanh(k_g * g_inv / cap) * choke)


def curve(h_c, a, s):
    return a * np.tanh(s / a * np.asarray(h_c, float))


def curve_logistic(h_c, k, m, w):
    """Convex-onset candidate: k / (1 + exp(-((h/c) - m) / w)).

    Near-zero realization below the onset midpoint m, saturating at k —
    the shape the Stage 1 sweep measured and the concave tanh cannot
    take."""
    return k / (1.0 + np.exp(-(np.asarray(h_c, float) - m) / w))


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
    fit_l = least_squares(lambda x: curve_logistic(h, *x) - k,
                          x0=[0.85, 0.20, 0.04],
                          bounds=([0.05, 0.01, 0.005], [1.0, 0.60, 0.30]))
    kl, ml, wl = fit_l.x

    print(f"tanh fit over {len(coarse)} coarse points: "
          f"k_g(h/c) = {a:.3f} * tanh({s:.3f}/{a:.3f} * h/c)   "
          f"(current curve: analysis.ground_gain_factor)")
    print(f"logistic fit: k_g(h/c) = {kl:.3f} / "
          f"(1 + exp(-((h/c) - {ml:.3f}) / {wl:.3f}))")
    print(f"{'label':30} {'h/c':>6} {'k_imp':>6} {'k_cur':>6} {'k_fit':>6} "
          f"{'k_log':>6} {'Cl_rans':>8} {'dcur%':>7} {'dfit%':>7} "
          f"{'dlog%':>7}")
    for p in pts:
        k_cur = float(analysis.ground_gain_factor(p["h_c"]))
        k_fit = float(curve(p["h_c"], a, s))
        k_log = float(curve_logistic(p["h_c"], kl, ml, wl))
        e_cur = c_est(k_cur, p["c_free"], p["c_ground"], p["h_c"])
        e_fit = c_est(k_fit, p["c_free"], p["c_ground"], p["h_c"])
        e_log = c_est(k_log, p["c_free"], p["c_ground"], p["h_c"])
        d_cur = (e_cur / p["cl_rans"] - 1) * 100
        d_fit = (e_fit / p["cl_rans"] - 1) * 100
        d_log = (e_log / p["cl_rans"] - 1) * 100
        print(f"{p['label']:30} {p['h_c']:6.3f} {p['k_imp']:6.3f} "
              f"{k_cur:6.3f} {k_fit:6.3f} {k_log:6.3f} {p['cl_rans']:8.3f} "
              f"{d_cur:+7.1f} {d_fit:+7.1f} {d_log:+7.1f}")
    for name, res in (("tanh", curve(h, a, s) - k),
                      ("logistic", curve_logistic(h, kl, ml, wl) - k)):
        print(f"{name} residual (k_g units): max |r| "
              f"{np.abs(res).max():.3f}, rms {np.sqrt((res ** 2).mean()):.3f}")
    fine = [p for p in pts if p["mesh"] != "coarse"]
    if fine:
        rf = np.array([p["k_imp"] - float(curve(p["h_c"], a, s))
                       for p in fine])
        print(f"non-coarse rows vs the coarse-only tanh fit (k_g units): "
              f"mean {rf.mean():+.3f}, max |r| {np.abs(rf).max():.3f} — "
              f"the fit uses coarse rows only, so any refit it proposes "
              f"carries this mesh offset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
