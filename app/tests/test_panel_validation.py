"""Panel-method validation against AeroSandbox (pressure-integrated) and XFOIL.

Run directly:  python app/tests/test_panel_validation.py
Slow (~1 min): builds several AirfoilInviscid reference solutions.
"""
import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.core import airfoils, geometry, panel  # noqa: E402


@contextlib.contextmanager
def quiet():
    sys.stdout.flush()
    old = os.dup(1)
    dn = os.open(os.devnull, os.O_WRONLY)
    os.dup2(dn, 1)
    try:
        yield
    finally:
        sys.stdout.flush()
        os.dup2(old, 1)
        os.close(old)
        os.close(dn)


def asb_pressure_cl(coord_list, ground):
    """AirfoilInviscid solve, force taken by pressure integration (its
    headline Cl=2*Gamma is Kutta-Joukowski and invalid in ground effect)."""
    import aerosandbox as asb
    afs = [asb.Airfoil(name=f"e{i}", coordinates=c)
           for i, c in enumerate(coord_list)]
    op = asb.OperatingPoint(velocity=1.0, alpha=0.0)
    with quiet():
        sol = asb.AirfoilInviscid(airfoil=afs, op_point=op, ground_effect=ground)
    total_fy = 0.0
    for af in afs:
        c = np.column_stack([af.x(), af.y()])
        d = np.diff(c, axis=0)
        ln = np.hypot(d[:, 0], d[:, 1])
        t = d / ln[:, None]
        nrm = np.column_stack([t[:, 1], -t[:, 0]])
        mid = 0.5 * (c[:-1] + c[1:]) + nrm * (1e-4 * ln)[:, None]
        u, v = sol.calculate_velocity(mid[:, 0], mid[:, 1])
        cp = 1.0 - (np.asarray(u) ** 2 + np.asarray(v) ** 2)
        total_fy += float(-(cp * nrm[:, 1] * ln).sum())
    return total_fy


def xfoil_inviscid_cl(dat_coords, alpha):
    """Independent check: XFOIL's own inviscid panel solution.

    OPERi prints its a/CL summary through the plot machinery (invisible in
    batch mode), so the CL is taken from a PACC polar file instead.
    """
    import tempfile
    exe = ROOT / "xfoil" / "xfoil.exe"
    if not exe.exists():
        return None
    with tempfile.TemporaryDirectory(prefix="xf_inv_") as td:
        dat = Path(td) / "af.dat"
        with open(dat, "w", newline="\n") as f:
            f.write("af\n")
            for x, y in dat_coords:
                f.write(f" {x:.6f} {y:.6f}\n")
        cmds = (f"PLOP\nG\n\nLOAD af.dat\nPPAR\nN 200\n\n\nOPER\nPACC\n"
                f"polar.txt\n\nALFA {alpha:.3f}\n\nQUIT\n")
        subprocess.run([str(exe)], input=cmds.encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       cwd=td, timeout=60)
        polar = Path(td) / "polar.txt"
        if not polar.exists():
            return None
        in_data = False
        for line in polar.read_text().splitlines():
            if line.strip().startswith("------"):
                in_data = True
                continue
            parts = line.split()
            if in_data and len(parts) >= 2:
                try:
                    return float(parts[1])  # data row: alpha, CL, ...
                except ValueError:
                    continue
    return None


results = []


def check(label, mine, ref, tol_pct):
    err = 100 * abs(mine - ref) / max(abs(ref), 1e-9)
    ok = err < tol_pct
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {label}: mine={mine:+.4f} "
          f"ref={ref:+.4f} err={err:.2f}% (tol {tol_pct}%)")


def main():
    # 1. naca0012, alpha 5, free air (rotate(-5) == alpha +5 for the reference)
    _, c0012 = airfoils.repaneled("naca0012", 80)
    sol = panel.solve([c0012], alpha_deg=5.0, ground=False)
    ref = asb_pressure_cl([geometry.rotate(c0012, -5.0)], False)
    check("naca0012 a=5 free", sol.Cl, ref, 2.0)

    # 2. s1223, alpha 0, free air — asb + XFOIL inviscid
    _, s1223 = airfoils.repaneled("s1223", 80)
    sol2 = panel.solve([s1223], alpha_deg=0.0, ground=False)
    check("s1223 a=0 free vs asb", sol2.Cl, asb_pressure_cl([s1223], False), 3.0)
    xf = xfoil_inviscid_cl(s1223, 0.0)
    if xf is not None:
        check("s1223 a=0 free vs XFOIL", sol2.Cl, xf, 3.0)

    # 3. demo two-element stack, installed frame.
    # The original stack_builder demo used --stack-aoa 2 under the legacy
    # (inverted) rotation convention; -2 under the corrected convention
    # reproduces that exact geometry, keeping the manifest reference values.
    cfg = geometry.StackConfig.from_dict({
        "elements": [{"airfoil": "s1223"},
                     {"airfoil": "s1223", "chord_ratio": 0.35,
                      "deflection_deg": 28, "dx": -0.03, "dy": -0.03}],
        "stack_aoa_deg": -2.0, "ride_height_mm": 52.5, "chord_mm": 350.0,
    })
    des = geometry.build_stack(cfg)
    inst = geometry.install_stack(des, cfg.ride_height_c)
    coords = [e["coords"] for e in inst]

    t0 = time.perf_counter()
    sol3 = panel.solve(coords, 0.0, ground=True)
    dt = time.perf_counter() - t0
    check("2-elem ground", sol3.Cl, asb_pressure_cl(coords, True), 4.0)
    print(f"      ground solve {dt*1000:.0f} ms, Cd_num={sol3.Cd_numerical:+.4f}")

    sol4 = panel.solve(coords, 0.0, ground=False)
    check("2-elem free", sol4.Cl, asb_pressure_cl(coords, False), 4.0)

    # 4. image method must equal an explicit mirrored-geometry solve
    mirrors = [c * np.array([1.0, -1.0]) for c in coords]
    tw = panel.solve(coords + mirrors, 0.0, ground=False)
    cl_top = sum(e["Cy"] for e in tw.elements[:2])
    check("image == explicit twin", sol3.Cl, cl_top, 0.1)

    # 5. slot metrics match the original stack_builder demo manifest
    g = des[1]["slot_gap"] * 100
    o = des[1]["slot_overlap"] * 100
    ok = abs(g - 1.68) < 0.3 and abs(o - 2.99) < 0.3
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  slot metrics: gap {g:.2f}%c (ref 1.68), "
          f"ovl {o:.2f}%c (ref 2.99)")

    # 6. sign convention: positive stack angle must ADD downforce in ground
    # effect (regression for the inverted-rotation defect)
    def df_at(aoa):
        c = geometry.StackConfig.from_dict({
            "elements": [{"airfoil": "naca4412"}],
            "stack_aoa_deg": aoa, "ride_height_mm": 50.0, "chord_mm": 350.0,
        })
        inst = geometry.install_stack(geometry.build_stack(c), c.ride_height_c)
        return -panel.solve([inst[0]["coords"]], 0.0, ground=True).Cl
    d0, d4 = df_at(0.0), df_at(4.0)
    ok = d4 > d0 > 0
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  stack-angle sign: downforce "
          f"{d0:.2f} @ 0° -> {d4:.2f} @ +4° (must increase)")

    # 7. slot placement solver: requested gap/overlap must be achieved exactly
    worst = 0.0
    for gap, ovl, defl in ((1.5, 3.0, 24), (0.8, 0.0, 40), (3.5, 5.0, 10),
                           (2.0, 3.5, 55)):
        c = geometry.StackConfig.from_dict({
            "elements": [{"airfoil": "s1223"},
                         {"airfoil": "s1223", "chord_ratio": 0.35,
                          "deflection_deg": defl,
                          "slot_gap_pct": gap, "slot_overlap_pct": ovl}],
            "chord_mm": 350.0, "ride_height_mm": 30.0,
        })
        d = geometry.build_stack(c)
        worst = max(worst, abs(d[1]["slot_gap"] * 100 - gap),
                    abs(d[1]["slot_overlap"] * 100 - ovl))
    ok = worst < 0.05
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  slot gap/overlap solver: worst error "
          f"{worst:.4f} %c (tol 0.05)")

    # 8. winding regression: an open-TE (manufacturing-prepped) contour must
    # solve identically at the origin and translated to a flap position —
    # the unclosed shoelace orientation test used to flip and silently
    # zero the flap load
    _, uc_mfg = airfoils.repaneled("mfg:thicken:0.022857:s1223", 70)
    c_mfg = uc_mfg * 0.15
    cl_origin = panel.solve([c_mfg], 0.0, False, 0.15).Cl
    cl_moved = panel.solve([c_mfg + np.array([1.0, 0.15])],
                           0.0, False, 0.15).Cl
    ok = abs(cl_origin - cl_moved) < 1e-9 * max(abs(cl_origin), 1.0)
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  open-TE winding translation-invariant: "
          f"Cl {cl_origin:+.4f} vs {cl_moved:+.4f} translated")

    # 9. drag realism: induced drag dominates a loaded front wing; total L/D
    # must land in the physically plausible band, not the profile-only 300+
    from app.core import analysis
    cfg_d = geometry.StackConfig.from_dict({
        "elements": [{"airfoil": "s1223"},
                     {"airfoil": "s1223", "chord_ratio": 0.35,
                      "deflection_deg": 24,
                      "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
        "stack_aoa_deg": 1.0, "ride_height_mm": 30.0, "chord_mm": 350.0,
        "span_mm": 1400.0, "speed_ms": 15.0, "ncrit": 7.0,
        "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
    })
    res = analysis.analyze(cfg_d, include_geometry=False)
    f = res["forces"]
    ok = (f["drag_induced_n"] > 3 * f["drag_profile_n"]
          and 2.0 < f["efficiency_ld"] < 40.0
          and abs(f["drag_total_n"]
                  - f["drag_profile_n"] - f["drag_induced_n"]) < 0.2)
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  drag realism: total "
          f"{f['drag_total_n']} N ({f['drag_induced_n']} induced + "
          f"{f['drag_profile_n']} profile), L/D {f['efficiency_ld']}")

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
