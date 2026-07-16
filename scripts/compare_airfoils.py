"""Overlay viscous polars for candidate airfoils using XFOIL and/or NeuralFoil.

Example (FSAE front-wing element screening):
    python scripts/compare_airfoils.py --airfoils s1223 e423 ch10sm fx74modsm naca4412 --re 3e5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from xfoil_runner import polar_metrics, resolve_airfoil, run_polar, save_polar_csv

RESULTS = SCRIPTS.parent / "results"

METRIC_COLS = ["CL_max", "alpha_CL_max", "LD_max", "CL_at_LD_max",
               "CD_at_CL1", "CD_at_CL1.5", "CD_at_CL2"]


def asb_airfoil(spec):
    import aerosandbox as asb
    kind, payload, name = resolve_airfoil(spec)
    if kind == "naca":
        return asb.Airfoil(f"naca{payload}"), name
    return asb.Airfoil(name=name, coordinates=payload), name


def neuralfoil_polar(spec, Re, alphas, ncrit=9.0):
    import neuralfoil as nf
    af, _ = asb_airfoil(spec)
    alphas = np.asarray(alphas, dtype=float)
    out = nf.get_aero_from_airfoil(airfoil=af, alpha=alphas, Re=Re,
                                   n_crit=ncrit, model_size="xlarge")
    return {
        "alpha": alphas,
        "CL": np.asarray(out["CL"], dtype=float),
        "CD": np.asarray(out["CD"], dtype=float),
        "CDp": np.full(alphas.shape, np.nan),
        "CM": np.asarray(out["CM"], dtype=float),
        "Top_Xtr": np.asarray(out["Top_Xtr"], dtype=float),
        "Bot_Xtr": np.asarray(out["Bot_Xtr"], dtype=float),
        "confidence": np.asarray(out["analysis_confidence"], dtype=float),
    }


def plot_comparison(polars, Re, ncrit, out_path):
    """polars: {(name, engine): polar_dict}"""
    names = sorted({k[0] for k in polars})
    colors = {n: plt.cm.tab10(i % 10) for i, n in enumerate(names)}
    style = {"xfoil": "-", "neuralfoil": "--"}

    fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
    ax_cla, ax_polar, ax_ld = axes

    for (name, engine), p in sorted(polars.items()):
        if len(p["alpha"]) == 0:
            continue
        c, ls = colors[name], style[engine]
        mk = "." if engine == "xfoil" else None  # sparse polars stay visible
        label = name if engine != "neuralfoil" or (name, "xfoil") not in polars else None
        ax_cla.plot(p["alpha"], p["CL"], ls, color=c, lw=1.6, marker=mk, ms=4,
                    label=label)
        ax_polar.plot(p["CD"], p["CL"], ls, color=c, lw=1.6, marker=mk, ms=4)
        ok = p["CD"] > 1e-6
        ax_ld.plot(p["CL"][ok], p["CL"][ok] / p["CD"][ok], ls, color=c, lw=1.6,
                   marker=mk, ms=4)

    ax_cla.set_xlabel(r"$\alpha$ [deg]"); ax_cla.set_ylabel(r"$C_L$")
    ax_polar.set_xlabel(r"$C_D$"); ax_polar.set_ylabel(r"$C_L$")
    ax_polar.set_xlim(left=0)
    ax_ld.set_xlabel(r"$C_L$"); ax_ld.set_ylabel(r"$L/D$")
    for ax in axes:
        ax.grid(alpha=0.3)
    ax_cla.legend(fontsize=9, loc="lower right")
    fig.suptitle(
        f"Airfoil comparison — Re = {Re:.3g}, Ncrit = {ncrit:g}   "
        f"(solid = XFOIL, dashed = NeuralFoil)")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def print_table(rows):
    headers = ["airfoil", "engine"] + METRIC_COLS
    widths = [max(len(h), 12) for h in headers]
    line = "  ".join(h.rjust(w) for h, w in zip(headers, widths))
    print("\n" + line)
    print("-" * len(line))
    for r in rows:
        cells = []
        for h, w in zip(headers, widths):
            v = r.get(h)
            if isinstance(v, float):
                cells.append(f"{v:.4f}".rjust(w))
            else:
                cells.append(str(v if v is not None else "-").rjust(w))
        print("  ".join(cells))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--airfoils", nargs="+",
                    default=["s1223", "e423", "ch10sm", "fx74modsm", "naca4412"])
    ap.add_argument("--re", type=float, default=3e5)
    ap.add_argument("--ncrit", type=float, default=9.0)
    ap.add_argument("--alphas", type=float, nargs=3, default=[-4, 18, 0.5],
                    metavar=("START", "STOP", "STEP"), help="NeuralFoil grid")
    ap.add_argument("--xfoil-step", type=float, default=1.0,
                    help="alpha step for XFOIL sweeps (coarser = faster)")
    ap.add_argument("--engine", choices=["both", "xfoil", "neuralfoil"],
                    default="both")
    args = ap.parse_args()

    a0, a1, da = args.alphas
    polars, rows = {}, []

    for name in args.airfoils:
        if args.engine in ("both", "xfoil"):
            print(f"XFOIL: {name} @ Re={args.re:.3g} ...", flush=True)
            p = run_polar(name, args.re, (a0, a1, args.xfoil_step),
                          ncrit=args.ncrit)
            polars[(name, "xfoil")] = p
            m = polar_metrics(p)
            rows.append({"airfoil": name, "engine": "xfoil", **m})
            save_polar_csv(p, RESULTS / "polars" / f"{name}_xfoil_Re{args.re:.2e}.csv")
        if args.engine in ("both", "neuralfoil"):
            alphas = np.arange(a0, a1 + da / 2, da)
            p = neuralfoil_polar(name, args.re, alphas, args.ncrit)
            polars[(name, "neuralfoil")] = p
            m = polar_metrics(p)
            m["min_confidence"] = float(p["confidence"].min())
            rows.append({"airfoil": name, "engine": "neuralfoil", **m})
            save_polar_csv({k: p[k] for k in
                            ["alpha", "CL", "CD", "CDp", "CM", "Top_Xtr", "Bot_Xtr"]},
                           RESULTS / "polars" / f"{name}_nf_Re{args.re:.2e}.csv")

    out_png = RESULTS / f"compare_Re{args.re:.2e}_ncrit{args.ncrit:g}.png"
    plot_comparison(polars, args.re, args.ncrit, out_png)
    print_table(rows)

    nf_rows = [r for r in rows if r["engine"] == "neuralfoil"
               and "min_confidence" in r]
    if nf_rows:
        worst = min(nf_rows, key=lambda r: r["min_confidence"])
        print(f"\nNeuralFoil min analysis_confidence: "
              f"{worst['min_confidence']:.2f} ({worst['airfoil']})")
    print(f"\nplot:   {out_png}")
    print(f"polars: {RESULTS / 'polars'}")


if __name__ == "__main__":
    main()
