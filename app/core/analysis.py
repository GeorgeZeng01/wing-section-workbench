"""Combined stack analysis: inviscid panel solution + viscous corrections.

Prediction model
----------------
The panel method gives exact inviscid downforce coefficients (main-chord
referenced) in free air (C_f) and in ground effect (C_g). Inviscid values
overpredict reality — mildly in free air, severely in strong ground effect,
where the real venturi flow saturates through boundary-layer growth that the
inviscid model cannot see. The corrected estimate is

    C_est = eta_visc * [ C_f + k_g * (C_g - C_f) ]

with two transparent, user-adjustable knobs:
  eta_visc  free-air viscous realization (default 0.85)
  k_g       ground-gain realization factor; by default ride-height dependent,
            k_g = 0.85 * tanh(1.4/0.85 * h/c) — vanishing toward the ground
            where the inviscid image-model gain diverges but the real flow
            chokes, approaching full realization at large ride heights.

Per-element loading is budgeted the classical way (Smith, "High-Lift
Aerodynamics"): each element's FREE-AIR inviscid load, knocked down by
eta_visc, is compared against its isolated CL_max (NeuralFoil, at the
element's own Reynolds number). Ground effect is treated as a global gain
with its own realization factor rather than being folded into element
loading — inviscid ground loads grow without bound as h -> 0 and would make
the per-element check meaningless. Each element's inviscid ground multiplier
is reported alongside for transparency. Warnings start at 90% of CL_max,
criticals at 110% (slotted elements can carry somewhat more than isolated
CL_max).

All inviscid inputs to the estimate are reported alongside it. Treat the
estimate as a screening number for optimization ranking; RANS or tunnel data
remain the truth model, and both knobs should be recalibrated against them
when available.
"""

from __future__ import annotations

import numpy as np

from . import geometry, panel, viscous
from .geometry import StackConfig

LOAD_WARN = 0.90
LOAD_CRIT = 1.10


def ground_gain_factor(ride_height_c: float) -> float:
    """Fraction of the inviscid ground-effect gain that is realized.

    Must fall toward zero as h -> 0: the inviscid venturi gain grows without
    bound there while the real flow chokes on boundary-layer growth.
    k_g = 0.85·tanh(1.4/0.85 · h/c) keeps the calibrated small-h slope (1.4
    per h/c — a two-element section at racing ride heights lands at wing
    CL ~ 4-6, L/D ~ 4-8) and the 0.85 large-h ceiling, but actually vanishes
    at the ground. The old hard floor of 0.10 kept 10 % of a diverging
    inviscid gain, so C_est itself diverged as h -> 0 — the opposite of the
    documented behavior. Calibrate against RANS or tunnel data when
    available.
    """
    return float(0.85 * np.tanh(1.4 / 0.85 * ride_height_c))


def induced_drag_n(downforce_n: float, cfg: StackConfig,
                   installed: list[dict]) -> tuple[float, dict]:
    """Induced drag from the achieved downforce.

    CDi = CL_wing^2 / (pi * AR * e) * phi, with CL_wing taken from the same
    downforce the tool reports (so lift and induced drag are consistent),
    AR = b/c for the rectangular planform, e the span-efficiency input
    (endplates push it toward ~1), and phi McCormick's ground-effect factor
    phi = (16 h/b)^2 / (1 + (16 h/b)^2) evaluated at the section's
    mid-height — wings near the ground shed much weaker trailing vorticity.
    """
    q = cfg.q_pa
    span_m = cfg.span_mm / 1000.0
    area = cfg.chord_m * span_m
    if q * area <= 0 or span_m <= 0:
        return 0.0, {}
    cl_wing = abs(downforce_n) / (q * area)
    ar = span_m / cfg.chord_m
    ys = np.concatenate([np.asarray(e["coords"], float)[:, 1] for e in installed])
    h_mid_m = float(ys.min() + ys.max()) / 2.0 * cfg.chord_m
    hb = max(16.0 * h_mid_m / span_m, 1e-6)
    phi = hb * hb / (1.0 + hb * hb)
    cdi = cl_wing ** 2 / (np.pi * ar * cfg.span_efficiency) * phi
    return float(q * area * cdi), {
        "CL_wing": round(cl_wing, 3), "AR": round(ar, 2),
        "ground_factor_phi": round(float(phi), 3),
        "CDi": round(float(cdi), 4),
    }


def corrected_downforce(c_free: float, c_ground: float, cfg: StackConfig,
                        k_g: float | None = None) -> tuple[float, float]:
    """(C_est, k_g used). Downforce-positive coefficients in, same out."""
    if k_g is None:
        k_g = ground_gain_factor(cfg.ride_height_c)
    c_est = cfg.viscous_efficiency * (c_free + k_g * (c_ground - c_free))
    return float(c_est), float(k_g)


def analyze(cfg: StackConfig, include_geometry: bool = True,
            model_size: str = "large") -> dict:
    """Full analysis for the UI. Raises ValueError on broken geometry."""
    design = geometry.build_stack(cfg)
    installed = geometry.install_stack(design, cfg.ride_height_c)
    coords = [e["coords"] for e in installed]

    if any(e.get("intersects") for e in design):
        raise ValueError("elements intersect — fix the slot geometry before analysis")

    free, ground = panel.solve_pair(coords, alpha_deg=0.0, ref_chord=1.0)

    # installed frame: downforce = -y
    c_free = -free.Cl
    c_ground = -ground.Cl
    c_est, k_g = corrected_downforce(c_free, c_ground, cfg)

    q = cfg.q_pa
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    downforce_n = q * area * c_est * cfg.efficiency_3d
    downforce_n_inviscid = q * area * c_ground * cfg.efficiency_3d

    # per-element loads + viscous limits (free-air load budgeting)
    elements = []
    cd_stack = 0.0
    warnings = []
    for i, e in enumerate(design):
        c_ratio = e["chord_ratio"]
        re_e = cfg.element_re(i)
        spec_v = e.get("airfoil_eff", e["airfoil"])  # as-manufactured section
        cl_free = -free.elements[i]["Cy"] / c_ratio    # element-chord referenced
        cl_ground = -ground.elements[i]["Cy"] / c_ratio
        cl_check = cl_free * cfg.viscous_efficiency
        lim = viscous.cl_limit(spec_v, re_e, cfg.ncrit, model_size)
        frac = cl_check / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        # profile drag at the ground-effect operating point: the element runs
        # harder than in free air by the realized share of its inviscid
        # ground multiplier (capped to the pre-stall polar inside)
        mult = cl_ground / cl_free if abs(cl_free) > 1e-6 else 1.0
        cl_drag = cl_check * (1.0 + k_g * (mult - 1.0))
        op = viscous.operating_point(spec_v, re_e,
                                     min(cl_drag, 0.95 * lim["CL_max"]),
                                     cfg.ncrit, model_size)
        cd_stack += op["CD"] * c_ratio
        status = ("critical" if frac > LOAD_CRIT
                  else "warning" if frac > LOAD_WARN else "ok")
        if status == "critical":
            warnings.append(f"{e['role']}: free-air loading {frac:.0%} of "
                            f"isolated CL_max — expect separation; reduce "
                            f"deflection or loading.")
        elif status == "warning":
            warnings.append(f"{e['role']}: loading at {frac:.0%} of isolated "
                            f"CL_max — near the viscous limit.")
        if lim.get("at_grid_edge") and frac > LOAD_WARN:
            warnings.append(f"{e['role']}: its polar had not stalled by the "
                            f"last analyzed angle, so the CL_max behind this "
                            f"loading figure is a lower bound, not a stall.")
        elements.append({
            "role": e["role"], "airfoil": e["airfoil"],
            "airfoil_eff": spec_v,
            "airfoil_name": e["airfoil_name"],
            "chord_ratio": c_ratio,
            "chord_mm": c_ratio * cfg.chord_mm,
            "deflection_deg": e["deflection_deg"],
            "Re": round(re_e),
            "Cl_free_inviscid": round(cl_free, 3),
            "Cl_checked": round(cl_check, 3),
            "ground_multiplier": round(cl_ground / cl_free, 2)
                if abs(cl_free) > 1e-6 else None,
            "CL_max_isolated": round(lim["CL_max"], 3),
            "loading_fraction": round(frac, 3),
            "loading_status": status,
            "CD_profile": round(op["CD"], 5),
            "alpha_equiv_deg": round(op["alpha"], 2),
            "nf_confidence": round(lim["confidence"], 3),
            "slot_gap_pct": round(design[i].get("slot_gap", np.nan) * 100, 2)
                if i else None,
            "slot_overlap_pct": round(design[i].get("slot_overlap", np.nan) * 100, 2)
                if i else None,
        })

    drag_profile_n = q * area * cd_stack
    drag_induced, induced_detail = induced_drag_n(downforce_n, cfg, installed)
    drag_total_n = drag_profile_n + drag_induced
    # Cm_le is nose-up positive; the CCW z-moment is -Cm_le, and the center
    # of pressure satisfies x_cp * Cl = tau_z, so x_cp = -Cm_le / Cl
    x_cp_c = (-ground.Cm_le / ground.Cl) if abs(ground.Cl) > 1e-9 else 0.0

    # Cp distributions (ground solution) for plotting
    cp_plots = []
    for k in range(len(coords)):
        sel = ground.element_index == k
        cp_plots.append({
            "role": design[k]["role"],
            "x": np.round(ground.midpoints[sel, 0], 5).tolist(),
            "y": np.round(ground.midpoints[sel, 1], 5).tolist(),
            "cp": np.round(ground.cp[sel], 4).tolist(),
        })

    out = {
        "coefficients": {
            "C_downforce_inviscid_ground": round(c_ground, 4),
            "C_downforce_inviscid_free": round(c_free, 4),
            "ground_gain_inviscid": round(c_ground / c_free - 1, 4)
                if abs(c_free) > 1e-9 else None,
            "C_downforce_estimated": round(c_est, 4),
            "k_ground_realization": round(k_g, 3),
            "viscous_efficiency": cfg.viscous_efficiency,
            "CD_profile_stack": round(cd_stack, 5),
            "Cm_le_inviscid": round(ground.Cm_le, 4),
            "x_cp_c": round(x_cp_c, 4),
            "Cd_numerical_residual": round(ground.Cd_numerical, 5),
        },
        "forces": {
            "downforce_n": round(downforce_n, 1),
            "downforce_inviscid_n": round(downforce_n_inviscid, 1),
            "drag_total_n": round(drag_total_n, 1),
            "drag_profile_n": round(drag_profile_n, 2),
            "drag_induced_n": round(drag_induced, 1),
            "induced_model": induced_detail,
            "efficiency_ld": round(downforce_n / drag_total_n, 1)
                if drag_total_n > 1e-9 else None,
            "q_pa": round(q, 2),
            "reference_area_m2": round(area, 4),
        },
        "elements": elements,
        "warnings": warnings,
        "conditions": {
            "speed_ms": cfg.speed_ms, "speed_kmh": round(cfg.speed_ms * 3.6, 1),
            "rho": cfg.rho, "nu": cfg.nu,
            "ride_height_mm": cfg.ride_height_mm,
            "ride_height_c": round(cfg.ride_height_c, 4),
            "chord_mm": cfg.chord_mm, "span_mm": cfg.span_mm,
            "stack_aoa_deg": cfg.stack_aoa_deg, "ncrit": cfg.ncrit,
            "efficiency_3d": cfg.efficiency_3d,
        },
        "cp_distributions": cp_plots,
    }
    if include_geometry:
        geo = geometry.geometry_report(cfg)
        out["geometry"] = geo
        out["warnings"] = geo["warnings"] + out["warnings"]
    return out


def quick_objective_eval(cfg: StackConfig, model_size: str = "large") -> dict:
    """Slim evaluation for the optimizer: no plots, no geometry payload."""
    design = geometry.build_stack(cfg)
    if any(e.get("intersects") for e in design):
        return {"feasible": False, "reason": "intersection"}
    installed = geometry.install_stack(design, cfg.ride_height_c)
    coords = [e["coords"] for e in installed]
    try:
        free, ground = panel.solve_pair(coords, 0.0)
    except (np.linalg.LinAlgError, ValueError) as e:
        return {"feasible": False, "reason": f"solver: {e}"}

    c_free, c_ground = -free.Cl, -ground.Cl
    c_est, k_g = corrected_downforce(c_free, c_ground, cfg)

    q = cfg.q_pa
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    downforce_n = q * area * c_est * cfg.efficiency_3d
    cd_stack = 0.0
    load_excess = 0.0
    for i, e in enumerate(design):
        cl_free_e = -free.elements[i]["Cy"] / e["chord_ratio"]
        cl_ground_e = -ground.elements[i]["Cy"] / e["chord_ratio"]
        cl_check = cl_free_e * cfg.viscous_efficiency
        re_e = cfg.element_re(i)
        spec_v = e.get("airfoil_eff", e["airfoil"])
        lim = viscous.cl_limit(spec_v, re_e, cfg.ncrit, model_size)
        frac = cl_check / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        load_excess += max(0.0, frac - 1.05) ** 2
        mult = cl_ground_e / cl_free_e if abs(cl_free_e) > 1e-6 else 1.0
        cl_drag = cl_check * (1.0 + k_g * (mult - 1.0))
        op = viscous.operating_point(spec_v, re_e,
                                     min(cl_drag, 0.95 * lim["CL_max"]),
                                     cfg.ncrit, model_size)
        cd_stack += op["CD"] * e["chord_ratio"]

    drag_induced, _ = induced_drag_n(downforce_n, cfg, installed)
    gaps = [e.get("slot_gap") for e in design[1:]]
    overlaps = [e.get("slot_overlap") for e in design[1:]]
    return {
        "feasible": True,
        "downforce_n": downforce_n,
        "drag_n": q * area * cd_stack + drag_induced,
        "c_est": c_est,
        "c_ground": c_ground,
        "load_excess": load_excess,
        "gaps": gaps,
        "overlaps": overlaps,
    }
