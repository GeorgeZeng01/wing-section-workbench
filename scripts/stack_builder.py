"""Multi-element wing-section (front wing stack) geometry builder.

Frames:
  design frame   — upright (lift = +y), main-element LE at (0,0), main chord = 1
  install frame  — y-flipped (downforce), ground plane at y = 0, stack shifted
                   so its lowest point sits at the ride height

Flap spec (repeatable):  --flap NAME:CHORD:DEFLECTION[:DX:DY]
  NAME        airfoil (UIUC name, nacaXXXX, or .dat path)
  CHORD       flap chord as a fraction of the MAIN chord
  DEFLECTION  degrees, TE-down in the design frame (positive = more load)
  DX, DY      flap LE position relative to the previous element's TE, in
              fractions of the main chord. DX < 0 overlaps the flap LE under
              the previous TE; DY < 0 drops it to form the slot.
              Defaults: -0.03, -0.03.

The literature-standard slot metrics (gap = min distance from previous TE to
the flap surface, overlap = x-projection shared with the previous TE) are
computed from the final geometry and written to the manifest, so you can
target published gap/overlap values by adjusting DX/DY.

Example:
    python scripts/stack_builder.py --name demo --main s1223 \
        --flap s1223:0.35:28 --stack-aoa 2 --ride-height 0.15 \
        --chord 0.35 --speed 15 --analyze
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from xfoil_runner import resolve_airfoil, write_dat

RESULTS = SCRIPTS.parent / "results"
NU_AIR = 1.5e-5  # kinematic viscosity, m^2/s, ~15 C sea level


@contextlib.contextmanager
def quiet_c_stdout():
    """Silence stdout at the fd level (IPOPT prints from C, bypassing sys.stdout)."""
    sys.stdout.flush()
    old_fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    try:
        yield
    finally:
        sys.stdout.flush()
        os.dup2(old_fd, 1)
        os.close(old_fd)
        os.close(devnull)


def unit_coords(spec):
    """Airfoil spec -> (name, (N,2) unit-chord coordinates, Selig order)."""
    kind, payload, name = resolve_airfoil(spec)
    if kind == "naca":
        import aerosandbox as asb
        return name, np.asarray(asb.Airfoil(f"naca{payload}").coordinates, float)
    return name, np.asarray(payload, dtype=float)


def rotate(coords, deg, center=(0.0, 0.0)):
    th = np.radians(deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return (coords - center) @ R.T + center


def te_point(coords):
    return 0.5 * (coords[0] + coords[-1])  # Selig files may have an open TE


def min_dist_to_polyline(pt, coords):
    """Min distance from point to the piecewise-linear airfoil contour."""
    a, b = coords[:-1], coords[1:]
    ab = b - a
    t = np.clip(np.einsum("ij,ij->i", pt - a, ab) /
                np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-16), 0.0, 1.0)
    proj = a + t[:, None] * ab
    return float(np.min(np.hypot(*(pt - proj).T)))


def parse_flap(spec):
    parts = spec.split(":")
    if len(parts) not in (3, 5):
        raise ValueError(f"flap spec {spec!r}: expected NAME:CHORD:DEFL[:DX:DY]")
    out = {"airfoil": parts[0], "chord": float(parts[1]), "deflection": float(parts[2]),
           "dx": -0.03, "dy": -0.03}
    if len(parts) == 5:
        out["dx"], out["dy"] = float(parts[3]), float(parts[4])
    return out


def build_stack(main_spec, flap_specs, stack_aoa=0.0):
    """Return list of element dicts in the design frame."""
    elements = []
    name, uc = unit_coords(main_spec)
    elements.append({"role": "main", "airfoil": name, "chord": 1.0,
                     "deflection": 0.0, "coords": uc.copy()})

    for i, f in enumerate(flap_specs, start=1):
        fname, fc = unit_coords(f["airfoil"])
        c = fc * f["chord"]                       # scale about LE (0,0)
        c = rotate(c, -f["deflection"])           # TE-down = clockwise
        prev_te = te_point(elements[-1]["coords"])
        c = c + prev_te + np.array([f["dx"], f["dy"]])
        elements.append({"role": f"flap{i}", "airfoil": fname, "chord": f["chord"],
                         "deflection": f["deflection"], "coords": c,
                         "dx": f["dx"], "dy": f["dy"]})

    if stack_aoa:
        for e in elements:  # positive = more incidence (about main LE)
            e["coords"] = rotate(e["coords"], stack_aoa)

    # slot metrics + sanity checks between consecutive elements
    from matplotlib.path import Path as MplPath
    for prev, cur in zip(elements, elements[1:]):
        pte = te_point(prev["coords"])
        cur["slot_gap"] = min_dist_to_polyline(pte, cur["coords"])
        cur["slot_overlap"] = float(pte[0] - cur["coords"][:, 0].min())
        if MplPath(prev["coords"]).contains_points(cur["coords"]).any():
            print(f"WARNING: {cur['role']} intersects {prev['role']} — "
                  f"increase gap (more negative DY) or reduce overlap.",
                  file=sys.stderr)
        elif cur["slot_gap"] < 0.005:
            print(f"WARNING: {cur['role']} slot gap {cur['slot_gap']*100:.2f}%c "
                  f"is very tight.", file=sys.stderr)
    return elements


def invert_stack(elements, ride_height):
    """Design frame -> install frame (downforce, ground at y=0)."""
    inv = []
    for e in elements:
        c = e["coords"] * np.array([1.0, -1.0])
        inv.append({**e, "coords": c[::-1].copy()})  # restore Selig winding
    y_min = min(e["coords"][:, 1].min() for e in inv)
    for e in inv:
        e["coords"] = e["coords"] + np.array([0.0, ride_height - y_min])
    return inv


def plot_stack(elements, title, path, ground=False):
    fig, ax = plt.subplots(figsize=(9, 5))
    for e, color in zip(elements, plt.cm.tab10.colors):
        ax.fill(e["coords"][:, 0], e["coords"][:, 1], color=color, alpha=0.55,
                lw=1.2, edgecolor="k",
                label=f"{e['role']}: {e['airfoil']}  c={e['chord']:g}  "
                      f"δ={e['deflection']:g}°")
        if "slot_gap" in e:
            le = e["coords"][np.argmin(e["coords"][:, 0])]
            ax.annotate(f"gap {e['slot_gap']*100:.1f}%c\n"
                        f"ovl {e['slot_overlap']*100:.1f}%c",
                        xy=le, xytext=(le[0], le[1] - 0.12),
                        fontsize=8, ha="center",
                        arrowprops=dict(arrowstyle="-", lw=0.6))
    if ground:
        x = ax.get_xlim()
        ax.axhline(0, color="k", lw=2)
        ax.fill_between([x[0] - 0.2, x[1] + 0.2], -0.06, 0, color="0.8",
                        hatch="///", lw=0)
    ax.set_aspect("equal")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper right")
    ax.set_title(title)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def analyze_inviscid(installed, ride_height):
    """Inviscid multi-element panel solution (AeroSandbox), with/without ground.

    Returns downforce coefficients (positive = down) referenced to the main
    chord. Inviscid: no stall, no viscous drag — use for TRENDS only, with
    single-element XFOIL/NeuralFoil CL_max limits as sanity constraints.
    """
    import aerosandbox as asb
    afs = []
    for e in installed:
        af = asb.Airfoil(name=f"{e['role']}_{e['airfoil']}", coordinates=e["coords"])
        try:
            af = af.repanel(n_points_per_side=80)
        except Exception:
            pass
        afs.append(af)
    op = asb.OperatingPoint(velocity=1.0, alpha=0.0)  # Cl is only correct at V=1
    out = {}
    with quiet_c_stdout():
        for label, ge in (("ground", True), ("free_air", False)):
            sol = asb.AirfoilInviscid(airfoil=afs, op_point=op, ground_effect=ge)
            out[f"C_downforce_{label}"] = float(-sol.Cl)
    out["ground_effect_gain"] = (out["C_downforce_ground"] /
                                 out["C_downforce_free_air"] - 1.0)
    out["ride_height_c"] = ride_height
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="stack", help="output folder name")
    ap.add_argument("--main", default="s1223")
    ap.add_argument("--flap", action="append", default=[],
                    metavar="NAME:CHORD:DEFL[:DX:DY]")
    ap.add_argument("--stack-aoa", type=float, default=0.0,
                    help="whole-stack incidence, deg (design frame, about main LE)")
    ap.add_argument("--ride-height", type=float, default=0.15,
                    help="lowest point above ground, fraction of main chord")
    ap.add_argument("--chord", type=float, default=0.35, help="main chord [m]")
    ap.add_argument("--speed", type=float, default=15.0, help="car speed [m/s]")
    ap.add_argument("--analyze", action="store_true",
                    help="run inviscid multi-element panel screening")
    args = ap.parse_args()

    flaps = [parse_flap(f) for f in args.flap]
    elements = build_stack(args.main, flaps, args.stack_aoa)
    installed = invert_stack(elements, args.ride_height)

    out_dir = RESULTS / "stacks" / args.name
    for sub in ("design", "installed"):
        (out_dir / sub).mkdir(parents=True, exist_ok=True)

    manifest = {
        "cli": " ".join(sys.argv),
        "main": args.main, "flaps": flaps, "stack_aoa_deg": args.stack_aoa,
        "ride_height_c": args.ride_height,
        "main_chord_m": args.chord, "speed_ms": args.speed,
        "elements": [],
    }

    x_all = np.vstack([e["coords"] for e in elements])
    manifest["system_chord_c"] = float(x_all[:, 0].max() - x_all[:, 0].min())
    manifest["system_chord_m"] = manifest["system_chord_c"] * args.chord

    for e, ei in zip(elements, installed):
        write_dat(e["coords"], out_dir / "design" / f"{e['role']}_{e['airfoil']}.dat",
                  f"{e['role']} {e['airfoil']}")
        write_dat(ei["coords"], out_dir / "installed" / f"{e['role']}_{e['airfoil']}.dat",
                  f"{e['role']} {e['airfoil']} (installed)")
        chord_m = e["chord"] * args.chord
        manifest["elements"].append({
            "role": e["role"], "airfoil": e["airfoil"],
            "chord_ratio": e["chord"], "chord_m": chord_m,
            "deflection_deg": e["deflection"],
            "Re_at_speed": round(args.speed * chord_m / NU_AIR),
            **({"slot_gap_pct_c": round(e["slot_gap"] * 100, 2),
                "slot_overlap_pct_c": round(e["slot_overlap"] * 100, 2)}
               if "slot_gap" in e else {}),
        })

    title = (f"{args.name}: {args.main} + {len(flaps)} flap(s), "
             f"aoa {args.stack_aoa:g}°")
    plot_stack(elements, f"{title} — design frame (upright)",
               out_dir / "stack_design.png")
    plot_stack(installed, f"{title} — installed (h = {args.ride_height:g}c)",
               out_dir / "stack_installed.png", ground=True)

    if args.analyze:
        try:
            manifest["inviscid_screening"] = analyze_inviscid(installed,
                                                              args.ride_height)
        except Exception as e:
            print(f"inviscid screening failed: {e}", file=sys.stderr)

    with open(out_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nStack '{args.name}' written to {out_dir}")
    for m in manifest["elements"]:
        slot = (f"  gap {m['slot_gap_pct_c']}%c, ovl {m['slot_overlap_pct_c']}%c"
                if "slot_gap_pct_c" in m else "")
        print(f"  {m['role']:>6}: {m['airfoil']:<12} c={m['chord_m']:.3f} m  "
              f"δ={m['deflection_deg']:g}°  Re={m['Re_at_speed']:.2e}{slot}")
    print(f"  system chord: {manifest['system_chord_m']:.3f} m")
    if "inviscid_screening" in manifest:
        s = manifest["inviscid_screening"]
        print(f"  inviscid C_downforce: {s['C_downforce_ground']:.3f} in ground "
              f"effect (h={args.ride_height:g}c), {s['C_downforce_free_air']:.3f} "
              f"free air  ({s['ground_effect_gain']:+.1%} from ground)")
        print("  (inviscid = trends only: no stall, no drag)")


if __name__ == "__main__":
    main()
