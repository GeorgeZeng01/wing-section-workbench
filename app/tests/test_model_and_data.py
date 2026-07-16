"""Model-shape, data-pipeline and API-contract validation.

Covers what the panel validation suite cannot: the corrected-downforce
model's required shape (bounded, force-reduction peak), exact recomputation
of the induced-drag formula, calibration-knob plumbing, the .dat parser
against known-hostile catalog formats, normalize() measurement fidelity,
the viscous surrogate contract, and the analysis payload contract the UI
depends on.

Run directly:  python app/tests/test_model_and_data.py
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import airfoils, analysis, geometry, screener, viscous  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


DEFAULT = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9, "span_efficiency": 0.9,
    "n_panels_per_side": 45,
}


def main():
    # ---- 1. estimate model shape: bounded, peaked, force reduction ----
    hs = [1.75, 3.5, 7, 10.5, 14, 17.5, 22.75, 30, 42, 70, 105]
    vals = []
    for h_mm in hs:
        cfg = geometry.StackConfig.from_dict({**DEFAULT,
                                              "ride_height_mm": h_mm})
        r = analysis.analyze(cfg, include_geometry=False)
        vals.append(r["coefficients"]["C_downforce_estimated"])
    pk = int(np.argmax(vals))
    h_pk = hs[pk] / 350.0
    check("C_est(h) has an interior force-reduction peak",
          0 < pk < len(hs) - 1 and 0.03 <= h_pk <= 0.12,
          f"(peak {vals[pk]:.2f} at h/c={h_pk:.3f})")
    check("C_est bounded toward the ground (no divergence)",
          vals[0] < 0.85 * vals[pk],
          f"(floor {vals[0]:.2f} vs peak {vals[pk]:.2f})")
    check("C_est decreases monotonically below the peak",
          all(vals[i] < vals[i + 1] + 1e-9 for i in range(pk)))
    check("C_est decreases monotonically above the peak",
          all(vals[i] > vals[i + 1] - 1e-9 for i in range(pk, len(vals) - 1)))

    # ---- 2. induced drag: exact formula recomputation ----
    cfg = geometry.StackConfig.from_dict(DEFAULT)
    r = analysis.analyze(cfg, include_geometry=False)
    f = r["forces"]
    im = f["induced_model"]
    q = f["q_pa"]
    area = f["reference_area_m2"]
    span_m = cfg.span_mm / 1000.0
    cl_wing = abs(f["downforce_n"]) / (q * area)
    ar = span_m / cfg.chord_m
    inst = geometry.install_stack(geometry.build_stack(cfg), cfg.ride_height_c)
    ys = np.concatenate([e["coords"][:, 1] for e in inst])
    h_mid = float(ys.min() + ys.max()) / 2.0 * cfg.chord_m
    hb = 16.0 * h_mid / span_m
    phi = hb * hb / (1.0 + hb * hb)
    cdi = cl_wing ** 2 / (np.pi * ar * cfg.span_efficiency) * phi
    di_expect = q * area * cdi
    check("CDi formula recomputed exactly",
          abs(im["CL_wing"] - cl_wing) < 5e-3
          and abs(im["AR"] - ar) < 5e-3
          and abs(im["ground_factor_phi"] - phi) < 5e-3
          and abs(f["drag_induced_n"] - di_expect) < 0.02 * max(di_expect, 1),
          f"(D_i {f['drag_induced_n']} N vs {di_expect:.1f} N recomputed)")
    # a deliberately broken formula (x2) must NOT pass this gate
    check("gate rejects a 2x-broken CDi",
          not abs(f["drag_induced_n"] - 2 * di_expect)
          < 0.02 * max(di_expect, 1))

    # ---- 3. calibration knobs ----
    cfg_k = geometry.StackConfig.from_dict({**DEFAULT, "k_g": 0.4})
    r_k = analysis.analyze(cfg_k, include_geometry=False)
    check("k_g pins the realization factor",
          r_k["coefficients"]["k_ground_realization"] == 0.4
          and r_k["coefficients"]["k_ground_source"] == "fixed")
    check("auto k_g reported as auto",
          r["coefficients"]["k_ground_source"] == "auto")
    try:
        geometry.StackConfig.from_dict({**DEFAULT, "k_g": 1.5})
        check("k_g out of range rejected", False)
    except ValueError:
        check("k_g out of range rejected", True)
    # per-element operating points must compose to the global estimate
    tot = sum(e["Cl_operating"] * e["chord_ratio"] for e in r["elements"])
    check("element operating CLs compose to C_est",
          abs(tot - r["coefficients"]["C_downforce_estimated"]) < 0.01,
          f"({tot:.3f} vs {r['coefficients']['C_downforce_estimated']})")

    # ---- 4. loading budget: ride-height awareness + hot-design warning ----
    frac_g = [e["loading_fraction_ground"] for e in r["elements"]]
    check("ground loading fractions present and bounded",
          all(0 < g < 10 for g in frac_g), f"({frac_g})")
    hot = geometry.StackConfig.from_dict({
        **DEFAULT, "stack_aoa_deg": 2.0,
        "elements": [DEFAULT["elements"][0],
                     {**DEFAULT["elements"][1], "deflection_deg": 30}]})
    r_hot = analysis.analyze(hot, include_geometry=False)
    check("overloaded design triggers the ground-allowance warning",
          any("isolated stall limit" in w for w in r_hot["warnings"]),
          f"({len(r_hot['warnings'])} warnings)")
    check("shipped default passes its own loading budget",
          not r["warnings"], f"({r['warnings'][:1]})")

    # ---- 5. .dat parser vs hostile catalog formats ----
    name, c = airfoils.read_dat(
        "NC090\n -2.0  3.0  -2.5  3.5\n 1.0 0.0\n 0.5 0.05\n 0.0 0.0\n"
        " 0.5 -0.05\n 1.0 0.0\n" + " 0.7 0.02\n" * 6, "t")
    check("MSES plot-window header is not a coordinate",
          not (c[:, 0] == -2.0).any())
    for spec, t_lo, t_hi in (("tasopt-c090", 0.085, 0.095),
                             ("naca23021", 0.20, 0.22),
                             ("nm26-3smoothed", 0.04, 0.06)):
        _, cc = airfoils.resolve(spec)
        n = airfoils.normalize(cc)
        info = airfoils.geometry_info(cc)
        check(f"{spec} parses to sane geometry",
              -0.02 <= n[:, 0].min() <= 0.005 and 0.98 <= n[:, 0].max() <= 1.08
              and t_lo <= info["max_thickness"] <= t_hi,
              f"(t={info['max_thickness']:.4f})")
    # trailing "a -> b" edit notes must not become contour points
    _, junk = airfoils.read_dat(
        "foo\n1.0 0.0\n0.5 0.1\n0.0 0.0\n0.5 -0.1\n1.0 0.0\n"
        + "\n".join(f"{x:.2f} {0.05*(1-x):.3f}" for x in np.linspace(1, 0, 8))
        + "\nmodif  0.99993 -> 1.00000\n0.00000 0.00102 -> 0.00001 0.00102\n",
        "t")
    check("trailing edit notes ignored", len(junk) == 13, f"({len(junk)} pts)")

    # ---- 6. normalize(): measurement fidelity ----
    info = airfoils.geometry_info(airfoils.naca_coords("2412"))
    check("naca2412 camber measured at defining value",
          abs(info["max_camber"] - 0.0200) < 0.0006
          and abs(info["x_max_camber"] - 0.40) < 0.015
          and abs(info["max_thickness"] - 0.1200) < 0.0015,
          f"(camber {info['max_camber']} @ {info['x_max_camber']})")
    c = airfoils.naca_coords("4412")
    n1 = airfoils.normalize(c)
    check("normalize is a no-op for chord-aligned sections",
          np.abs(n1 - airfoils.normalize(n1)).max() < 1e-14)
    th = np.radians(6.0)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    back = airfoils.normalize((c * 0.7 + np.array([0.2, 0.9])) @ R.T)
    check("baked-in incidence is de-rotated",
          np.abs(back - n1).max() < 0.004,
          f"(residual {np.abs(back - n1).max():.5f})")
    try:
        airfoils.naca_coords("4012")
        check("naca4012 (m>0, p=0) rejected", False)
    except ValueError:
        check("naca4012 (m>0, p=0) rejected", True)

    # ---- 7. viscous surrogate contract ----
    p = viscous.polar("naca0012", 5e3, 9.0, "large")
    check("low-Re polar reports the clamp",
          p["re_clamped"] and p["re_used"] == 1e4)
    op = viscous.operating_point("s1223", 3e5, 5.0, 7.0, "large")
    check("operating point clamps high with flag",
          op["clamped"] and op["clamped_high"] and not op["clamped_low"])
    op2 = viscous.operating_point("s1223", 3e5, -3.0, 7.0, "large")
    check("operating point flags the low grid edge",
          op2["clamped_low"] or op2["alpha"] > -8.1,
          f"(alpha {op2['alpha']:.1f})")
    row = screener.metrics_for("s1223", 3e5, 7.0)
    check("screener rows carry the CL_max lower-bound flag",
          row is not None and "CL_max_lower_bound" in row)

    # ---- 8. filesystem .dat cache staleness ----
    import tempfile
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        fp = Path(td) / "stale_check.dat"
        fp.write_text("s\n" + "\n".join(
            f" {x:.6f} {y:.6f}" for x, y in airfoils.naca_coords("0010")))
        _, c1 = airfoils.repaneled(str(fp), 40)
        t1 = airfoils.geometry_info(c1)["max_thickness"]
        import os
        fp.write_text("s\n" + "\n".join(
            f" {x:.6f} {y:.6f}" for x, y in airfoils.naca_coords("0016")))
        os.utime(fp, (fp.stat().st_atime, fp.stat().st_mtime + 5))
        _, c2 = airfoils.repaneled(str(fp), 40)
        t2 = airfoils.geometry_info(c2)["max_thickness"]
        check("editing a filesystem .dat invalidates its caches",
              abs(t1 - 0.10) < 0.005 and abs(t2 - 0.16) < 0.005,
              f"({t1:.3f} -> {t2:.3f})")

    # ---- 9. non-adjacent element intersection is caught ----
    # flap2 clears flap1 but doubles back into the MAIN (legal dx/dy inputs)
    bad = geometry.StackConfig.from_dict({
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0},
            {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 20,
             "dx": -0.03, "dy": -0.25},
            {"airfoil": "s1223", "chord_ratio": 0.30, "deflection_deg": 30,
             "dx": -0.70, "dy": 0.45}],
        "chord_mm": 350, "ride_height_mm": 30})
    des = geometry.build_stack(bad)
    check("non-adjacent overlap sets the intersection flag",
          any(e.get("intersects") for e in des))
    try:
        analysis.analyze(bad, include_geometry=False)
        check("analyze refuses intersecting geometry", False)
    except ValueError:
        check("analyze refuses intersecting geometry", True)
    ev = analysis.quick_objective_eval(bad)
    check("optimizer eval marks it infeasible", not ev["feasible"])

    # ---- 10. analysis payload contract (the fields the UI reads) ----
    need_coeff = {"C_downforce_inviscid_ground", "C_downforce_inviscid_free",
                  "C_downforce_estimated", "k_ground_realization",
                  "k_ground_source", "gain_realization_ratio",
                  "CD_profile_stack", "Cm_le_inviscid", "x_cp_c"}
    need_forces = {"downforce_n", "drag_total_n", "drag_profile_n",
                   "drag_induced_n", "induced_model", "efficiency_ld",
                   "q_pa", "reference_area_m2"}
    need_elem = {"role", "airfoil", "airfoil_name", "chord_ratio", "chord_mm",
                 "Re", "Cl_free_inviscid", "Cl_checked", "Cl_operating",
                 "ground_multiplier", "CL_max_isolated", "loading_fraction",
                 "loading_fraction_ground", "loading_status", "CD_profile",
                 "alpha_equiv_deg", "cd_lookup_capped", "nf_confidence",
                 "slot_gap_pct", "slot_overlap_pct"}
    full = analysis.analyze(geometry.StackConfig.from_dict(DEFAULT))
    check("coefficients contract", need_coeff <= set(full["coefficients"]),
          f"(missing {need_coeff - set(full['coefficients'])})")
    check("forces contract", need_forces <= set(full["forces"]),
          f"(missing {need_forces - set(full['forces'])})")
    check("element contract",
          all(need_elem <= set(e) for e in full["elements"]),
          f"(missing {need_elem - set(full['elements'][0])})")
    check("cp distributions per element",
          len(full["cp_distributions"]) == 2
          and all(len(cp["x"]) == len(cp["cp"]) > 20
                  for cp in full["cp_distributions"]))
    check("geometry frames present",
          {"design", "installed", "warnings"} <= set(full["geometry"])
          and all("coords" in e for e in full["geometry"]["design"]))

    print(f"\n{sum(results)}/{len(results)} model/data checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
