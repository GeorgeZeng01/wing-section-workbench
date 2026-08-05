"""Surrogate-vs-XFOIL sweep — the measurement behind the cross-check
gate's thresholds.

For the library sections the optimizer actually hunts (the top NeuralFoil
CL_max claims at the operating Reynolds number, plus any explicitly named
sections), run the real XFOIL polar and record, per section:

    conv_frac   converged XFOIL points / requested points
    cl_max_nf   NeuralFoil's isolated CL_max (large model, ncrit as given)
    cl_max_xf   XFOIL's converged CL_max (None when nothing converged)
    ratio       cl_max_nf / cl_max_xf

The clusters in (conv_frac, ratio) space place the gate's lines with
measured margin instead of guesses. Writes a JSON table beside stdout.

    .venv\\Scripts\\python.exe scripts\\surrogate_sweep.py ^
        [--re 4.67e5] [--ncrit 7] [--top 40] [--extra name,name,...]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# the sweep measures RAW surrogate claims against its own XFOIL calls —
# with the gate live, the library ranking would trigger a gate XFOIL run
# per section (hours) and the claims column would show capped values
import os  # noqa: E402

os.environ["WSS_SURROGATE_CHECK"] = "off"

import numpy as np  # noqa: E402

from app.core import airfoils, viscous  # noqa: E402

# every section seen in the 2026-08-04 session (candidates + knee + the
# working config) — the decision surface the sweep must cover even when a
# section's claim would not rank it in the top slice
SESSION_SET = ("s9104BTE", "s9104", "s1223rtl", "s1223", "fx74modsm",
               "mid151c", "goe525", "ch10sm", "mid151b", "ah7476",
               "goe804", "mid153c", "mid153b", "be6699", "s1210",
               "s9104BTE2")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--re", type=float, default=4.67e5)
    ap.add_argument("--ncrit", type=float, default=7.0)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--extra", type=str, default="")
    args = ap.parse_args()

    lib = airfoils.library_names()
    print(f"ranking {len(lib)} library sections by NeuralFoil CL_max "
          f"at Re {args.re:.3g}, ncrit {args.ncrit:g}...", flush=True)
    claims = []
    for name in lib:
        try:
            lim = viscous.cl_limit(name, args.re, args.ncrit)
            claims.append((lim["CL_max"], name))
        except Exception:
            continue
    claims.sort(reverse=True)
    picks = [n for _, n in claims[:args.top]]
    for n in SESSION_SET:
        if n in lib and n not in picks:
            picks.append(n)
    for n in (s.strip() for s in args.extra.split(",") if s.strip()):
        if n not in picks:
            picks.append(n)
    print(f"sweeping {len(picks)} sections with XFOIL "
          f"(~5-20 s each)...", flush=True)

    a0, a1, da = -6.0, 24.0, 1.0
    n_req = int(round((a1 - a0) / da)) + 1
    rows = []
    for k, name in enumerate(picks, 1):
        lim = viscous.cl_limit(name, args.re, args.ncrit)
        t0 = time.time()
        try:
            xp = viscous.xfoil_polar(name, args.re, args.ncrit,
                                     alphas=(a0, a1, da))
            cl = np.asarray(xp.get("CL", []), float)
            conv = len(cl) / n_req
            xf_max = float(cl.max()) if len(cl) else None
        except Exception as e:
            conv, xf_max = 0.0, None
            print(f"  {name}: XFOIL failed ({type(e).__name__}: {e})",
                  flush=True)
        ratio = (lim["CL_max"] / xf_max) if xf_max else None
        rows.append({"name": name, "cl_max_nf": round(lim["CL_max"], 3),
                     "conf_nf": round(lim["confidence"], 3),
                     "conv_frac": round(conv, 3),
                     "cl_max_xf": (round(xf_max, 3)
                                   if xf_max is not None else None),
                     "ratio": (round(ratio, 3)
                               if ratio is not None else None)})
        r = rows[-1]
        print(f"  [{k:3d}/{len(picks)}] {name:16s} nf {r['cl_max_nf']:5.2f} "
              f"(conf {r['conf_nf']:.2f})  xf "
              f"{r['cl_max_xf'] if r['cl_max_xf'] is not None else '  --'} "
              f" conv {r['conv_frac']:.2f}  ratio "
              f"{r['ratio'] if r['ratio'] is not None else '--'}  "
              f"({time.time() - t0:.0f}s)", flush=True)

    out = REPO / "app_data" / "surrogate_sweep.json"
    out.write_text(json.dumps({"re": args.re, "ncrit": args.ncrit,
                               "alphas": [a0, a1, da], "rows": rows},
                              indent=1), encoding="utf-8")
    ok = [r for r in rows if r["ratio"] is not None]
    good = [r for r in ok if r["conv_frac"] >= 0.65
            and r["ratio"] <= 1.25]
    bad = [r for r in ok if r["conv_frac"] < 0.65 or r["ratio"] > 1.25]
    none = [r for r in rows if r["ratio"] is None]
    print(f"\nwrote {out}")
    print(f"agreeing (conv>=0.65, ratio<=1.25): {len(good)}")
    if good:
        print(f"  conv min {min(r['conv_frac'] for r in good):.2f}  "
              f"ratio max {max(r['ratio'] for r in good):.2f}")
    print(f"disagreeing/underconverged: {len(bad)}")
    for r in sorted(bad, key=lambda r: -(r["ratio"] or 0))[:15]:
        print(f"  {r['name']:16s} conv {r['conv_frac']:.2f} "
              f"ratio {r['ratio']}")
    print(f"nothing converged: {len(none)}: "
          f"{[r['name'] for r in none]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
