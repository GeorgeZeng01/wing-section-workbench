"""Combined stack analysis: inviscid panel solution + viscous corrections.

Prediction model
----------------
The panel method gives exact inviscid downforce coefficients (main-chord
referenced) in free air (C_f) and in ground effect (C_g). Inviscid values
overpredict reality — mildly in free air, severely in strong ground effect,
where the real venturi flow saturates through boundary-layer growth that the
inviscid model cannot see. The corrected estimate is

    C_est = eta_visc * [ C_f + G_real ]
    G_real = cap * tanh( k_g * (C_g - C_f) / cap ) * tanh( h/c / h_choke )
    cap    = gain_cap_ratio * max(|C_f|, 0.1)

(The 0.1 floor under |C_f| keeps the cap physical when the free-air load is
near zero — a symmetric section at zero incidence still develops real
downforce in ground effect, and a cap of "a few times nothing" would wrongly
report zero for it.)

Structure: k_g * (C_g - C_f) is the realized share of the inviscid gain
(the calibrated behavior at ordinary ride heights, where the tanh terms are
near-linear / near-1 and the model reduces to the familiar
eta * [C_f + k_g * (C_g - C_f)]). The saturation cap bounds the realized
gain — the inviscid gain diverges as h -> 0 while real flow cannot deliver
more than a few times the free-air load — and the choke term models the
force reduction measured close to the ground (Zerihan & Zhang: downforce
peaks around h/c ~ 0.06-0.1, then falls as the boundary layers merge). With
the defaults the estimate peaks near h/c = 0.065 on the two-element baseline
and decreases monotonically below it, instead of growing without bound.

User-adjustable knobs (all in the config, all reported in the output):
  eta_visc        free-air viscous realization (default 0.85)
  k_g             realization factor; None (default) uses the ride-height
                  curve ground_gain_factor(), a number pins it
  gain_cap_ratio  realized-gain ceiling as a multiple of |C_f| (default 3.0)
  choke_h_c       ride height (in chords) below which the venturi chokes
                  (default 0.045)

Per-element loading is budgeted the classical way (Smith, "High-Lift
Aerodynamics"): each element's FREE-AIR inviscid load, knocked down by
eta_visc, is compared against its isolated CL_max (NeuralFoil, at the
element's own Reynolds number). Warnings start at 90% of CL_max, criticals
at 110% (slotted elements can carry somewhat more than isolated CL_max).
In addition, each element's REALIZED ground-effect operating CL (the same
number the drag lookup uses, consistent with the global estimate by
construction) is compared against GROUND_CL_ALLOWANCE x CL_max — sections
near the ground have been measured carrying roughly up to twice their
free-air maximum before the flow gives up, so exceeding that is flagged as
a screening-validity warning rather than a hard stall verdict.

All inviscid inputs to the estimate are reported alongside it. Treat the
estimate as a screening number for optimization ranking; RANS or tunnel data
remain the truth model, and the knobs should be recalibrated against them
when available.
"""

from __future__ import annotations

import dataclasses

import numpy as np

from . import geometry, panel, viscous
from .geometry import StackConfig

LOAD_WARN = 0.90
LOAD_CRIT = 1.10
# floor under the saturation cap's |C_f| reference: with the cap tied purely
# to the free-air load, a symmetric section at zero incidence (C_f ~ 0) got
# its entire — physically real — ground-effect gain crushed to zero. Below
# this coefficient the cap reads "a few times a small but physical load"
# instead of "a few times nothing".
CAP_FLOOR_C = 0.1
# realized ground operating CL beyond this multiple of the isolated CL_max
# is outside what measured sections sustain near the ground — the screening
# model is optimistic there
GROUND_CL_ALLOWANCE = 2.0

# Venturi-choke onset measured by the 2026-07 RANS cross-referencing
# campaign (docs/calibration): fine-mesh truth runs on the two-element
# baseline VALIDATED C_est at h/c 0.086 and 0.429, but below the choke
# onset the flow loses lift outright — something the gain-only model cannot
# express (its floor is eta*C_f) — measured -19% at h/c 0.071 and worsening
# toward the ground. Estimates below this ride height are upper bounds and
# get a standing warning. (The same campaign measured the model CONSERVATIVE
# through the mid-height gain peak — up to +67% at h/c 0.171 — which is
# deliberately NOT a warning: it says the wing makes more than claimed, and
# inflating warning counts would wrongly mark such designs as suspect. It is
# documented in the model dialog and docs/calibration instead.)
HC_CHOKE_OPTIMISM = 0.08
# the same campaign's mid-height band: fine-mesh RANS measured the estimate
# CONSERVATIVE here (+67% at h/c 0.171, +35% at 0.257; validated exact at
# 0.086-0.114 and 0.429). Not a warning — the wing makes MORE than claimed —
# but the operating map shades the band so the conservatism is visible where
# ride-height decisions are made.
HC_CONSERVATIVE = (0.12, 0.35)


def ground_gain_factor(ride_height_c: float) -> float:
    """Default realization-factor curve k_g(h/c).

    k_g = 0.85·tanh(1.4/0.85 · h/c): the calibrated small-h slope (1.4 per
    h/c — a two-element section at racing ride heights lands at wing
    CL ~ 4-6, L/D ~ 4-8) with a 0.85 large-h ceiling. On its own this curve
    does NOT bound the estimate as h -> 0 (the inviscid gain it multiplies
    diverges faster than the curve vanishes); the saturation cap and choke
    term in realized_gain() do that. Calibrate against RANS or tunnel data
    when available — the config's k_g field pins the factor to a constant.
    """
    return float(0.85 * np.tanh(1.4 / 0.85 * ride_height_c))


def gain_cap(c_free: float, cfg: StackConfig) -> float:
    """Saturation cap for the realized ground gain. One definition, shared
    with the RANS runner's k_g inversion — the two must stay consistent or
    a suggested k_g would not reproduce the RANS result it came from."""
    return cfg.gain_cap_ratio * max(abs(c_free), CAP_FLOOR_C)


def realized_gain(c_free: float, c_ground: float, cfg: StackConfig,
                  ) -> tuple[float, float, float]:
    """(G_real, k_g_used, realization_ratio r = G_real / (C_g - C_f)).

    The bounded ground-gain model described in the module docstring. r is
    the fraction of the inviscid gain actually realized; per-element
    operating points reuse it so element numbers and the global estimate
    stay consistent.
    """
    k_g = (float(cfg.k_g) if cfg.k_g is not None
           else ground_gain_factor(cfg.ride_height_c))
    g_inv = c_ground - c_free
    cap = gain_cap(c_free, cfg)
    choke = np.tanh(cfg.ride_height_c / cfg.choke_h_c)
    g_real = float(cap * np.tanh(k_g * g_inv / cap) * choke)
    r = g_real / g_inv if abs(g_inv) > 1e-12 else 0.0
    return g_real, float(k_g), float(r)


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
                        ) -> tuple[float, float, float]:
    """(C_est, k_g used, realization ratio r).

    Downforce-positive coefficients in, same out."""
    g_real, k_g, r = realized_gain(c_free, c_ground, cfg)
    c_est = cfg.viscous_efficiency * (c_free + g_real)
    return float(c_est), k_g, r


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
    c_est, k_g, r_gain = corrected_downforce(c_free, c_ground, cfg)

    q = cfg.q_pa
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    downforce_n = q * area * c_est * cfg.efficiency_3d
    downforce_n_inviscid = q * area * c_ground * cfg.efficiency_3d

    # per-element loads + viscous limits (free-air load budgeting)
    elements = []
    cd_stack = 0.0
    warnings = []
    ground_hot = []   # elements past the ground-effect loading allowance
    for i, e in enumerate(design):
        c_ratio = e["chord_ratio"]
        re_e = cfg.element_re(i)
        spec_v = e.get("airfoil_eff", e["airfoil"])  # as-manufactured section
        cl_free = -free.elements[i]["Cy"] / c_ratio    # element-chord referenced
        cl_ground = -ground.elements[i]["Cy"] / c_ratio
        cl_check = cl_free * cfg.viscous_efficiency
        lim = viscous.cl_limit(spec_v, re_e, cfg.ncrit, model_size)
        frac = cl_check / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        # realized ground-effect operating CL: the element's own inviscid
        # gain, realized at the same ratio r as the global estimate, so
        # sum(cl_op * chord_ratio) == C_est by construction. This is the
        # profile-drag lookup point and the ground-loading indicator.
        cl_op = cfg.viscous_efficiency * (cl_free + r_gain * (cl_ground - cl_free))
        frac_ground = cl_op / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        op = viscous.operating_point(spec_v, re_e,
                                     min(cl_op, 0.95 * lim["CL_max"]),
                                     cfg.ncrit, model_size)
        cd_stack += op["CD"] * c_ratio
        drag_capped = bool(op["clamped_high"] or cl_op > 0.95 * lim["CL_max"])
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
        if frac_ground > GROUND_CL_ALLOWANCE:
            ground_hot.append((e["role"], frac_ground))
        elements.append({
            "role": e["role"], "airfoil": e["airfoil"],
            "airfoil_eff": spec_v,
            "airfoil_name": e["airfoil_name"],
            "chord_ratio": c_ratio,
            "chord_mm": c_ratio * cfg.chord_mm,
            "deflection_deg": e["deflection_deg"],
            "Re": round(re_e),
            "re_clamped": bool(re_e < viscous.RE_FLOOR),
            "Cl_free_inviscid": round(cl_free, 3),
            "Cl_checked": round(cl_check, 3),
            "Cl_operating": round(cl_op, 3),
            "ground_multiplier": round(cl_ground / cl_free, 2)
                if abs(cl_free) > 1e-6 else None,
            "CL_max_isolated": round(lim["CL_max"], 3),
            "loading_fraction": round(frac, 3),
            "loading_fraction_ground": round(frac_ground, 3),
            "loading_status": status,
            "CD_profile": round(op["CD"], 5),
            "alpha_equiv_deg": round(op["alpha"], 2),
            "cd_lookup_capped": drag_capped,
            "cl_max_at_grid_edge": bool(lim.get("at_grid_edge")),
            "nf_confidence": round(lim["confidence"], 3),
            "slot_gap_pct": round(design[i].get("slot_gap", np.nan) * 100, 2)
                if i else None,
            "slot_overlap_pct": round(design[i].get("slot_overlap", np.nan) * 100, 2)
                if i else None,
        })
    clamped = [e["role"] for i, e in enumerate(design)
               if cfg.element_re(i) < viscous.RE_FLOOR]
    if clamped:
        warnings.append(
            f"{', '.join(clamped)}: Reynolds number below the surrogate's "
            f"{viscous.RE_FLOOR:.0f} training floor — viscous data was "
            f"evaluated at the floor, so CL_max and drag for these elements "
            f"are extrapolations, not predictions.")
    if ground_hot:
        names = ", ".join(f"{role} ({fg:.1f}x)" for role, fg in ground_hot)
        warnings.append(
            f"realized ground-effect loading is past "
            f"{GROUND_CL_ALLOWANCE:.0f}x the isolated stall limit on: {names}. "
            f"Measured sections rarely sustain more — treat the downforce "
            f"estimate as optimistic and the profile drag (capped at the "
            f"pre-stall polar) as understated; verify with RANS or tunnel "
            f"data.")
    if cfg.ride_height_c < HC_CHOKE_OPTIMISM:
        warnings.append(
            f"ride height h/c = {cfg.ride_height_c:.3f} is below the "
            f"venturi-choke onset (~{HC_CHOKE_OPTIMISM:.2f}): RANS truth "
            f"runs measured outright lift loss here that the estimate's "
            f"model cannot express (-19% at h/c 0.071 on the baseline, "
            f"worsening toward the ground) — treat the estimate as an "
            f"upper bound and verify with RANS on a medium or fine mesh.")

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
            "k_ground_source": "fixed" if cfg.k_g is not None else "auto",
            "gain_realization_ratio": round(r_gain, 4),
            "ground_gain_realized": round(r_gain * (c_ground - c_free), 4),
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
    c_est, k_g, r_gain = corrected_downforce(c_free, c_ground, cfg)

    q = cfg.q_pa
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    downforce_n = q * area * c_est * cfg.efficiency_3d
    cd_stack = 0.0
    load_excess = 0.0
    load_excess_ground = 0.0
    fracs = []          # per-element free-air loading fractions — the
    fracs_ground = []   # optimizer's baseline-relative bands need them
    confidences = []    # NeuralFoil confidence at CL_max, per element — the
    at_grid_edge = []   # optimizer's trust penalty reads these instead of
    cd_capped = []      # discarding them
    for i, e in enumerate(design):
        cl_free_e = -free.elements[i]["Cy"] / e["chord_ratio"]
        cl_ground_e = -ground.elements[i]["Cy"] / e["chord_ratio"]
        cl_check = cl_free_e * cfg.viscous_efficiency
        re_e = cfg.element_re(i)
        spec_v = e.get("airfoil_eff", e["airfoil"])
        lim = viscous.cl_limit(spec_v, re_e, cfg.ncrit, model_size)
        frac = cl_check / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        fracs.append(frac)
        load_excess += max(0.0, frac - 1.05) ** 2
        cl_op = cfg.viscous_efficiency * (
            cl_free_e + r_gain * (cl_ground_e - cl_free_e))
        frac_g = cl_op / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        fracs_ground.append(frac_g)
        load_excess_ground += max(0.0, frac_g / GROUND_CL_ALLOWANCE - 1.0) ** 2
        op = viscous.operating_point(spec_v, re_e,
                                     min(cl_op, 0.95 * lim["CL_max"]),
                                     cfg.ncrit, model_size)
        cd_stack += op["CD"] * e["chord_ratio"]
        confidences.append(float(lim["confidence"]))
        at_grid_edge.append(bool(lim.get("at_grid_edge")))
        cd_capped.append(bool(op["clamped_high"]
                              or cl_op > 0.95 * lim["CL_max"]))

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
        "load_excess_ground": load_excess_ground,
        "fracs": fracs,
        "fracs_ground": fracs_ground,
        "confidences": confidences,
        "at_grid_edge": at_grid_edge,
        "cd_capped": cd_capped,
        "gaps": gaps,
        "overlaps": overlaps,
    }


SWEEP_VARIABLES = ("ride_height_mm", "speed_ms")
SWEEP_MAX_POINTS = 40
SWEEP_MAX_PANELS = 60   # per-point solves stay fast at map resolution


def _sweep_point(cfg: StackConfig, design: list[dict], installed: list[dict],
                 free, ground, model_size: str) -> dict:
    """One operating-map point: the force/efficiency slice of analyze().

    Same model, same numbers — just without plots, geometry payload or
    warning prose. n_warnings counts the messages analyze() would emit."""
    c_free, c_ground = -free.Cl, -ground.Cl
    c_est, k_g, r_gain = corrected_downforce(c_free, c_ground, cfg)

    q = cfg.q_pa
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    downforce_n = q * area * c_est * cfg.efficiency_3d

    cd_stack = 0.0
    frac_ground_max = 0.0
    n_warnings = 0
    ground_hot = 0
    for i, e in enumerate(design):
        c_ratio = e["chord_ratio"]
        re_e = cfg.element_re(i)
        spec_v = e.get("airfoil_eff", e["airfoil"])
        cl_free = -free.elements[i]["Cy"] / c_ratio
        cl_ground = -ground.elements[i]["Cy"] / c_ratio
        cl_check = cl_free * cfg.viscous_efficiency
        lim = viscous.cl_limit(spec_v, re_e, cfg.ncrit, model_size)
        frac = cl_check / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        cl_op = cfg.viscous_efficiency * (cl_free + r_gain * (cl_ground - cl_free))
        frac_ground = cl_op / lim["CL_max"] if lim["CL_max"] > 1e-6 else 99.0
        frac_ground_max = max(frac_ground_max, frac_ground)
        op = viscous.operating_point(spec_v, re_e,
                                     min(cl_op, 0.95 * lim["CL_max"]),
                                     cfg.ncrit, model_size)
        cd_stack += op["CD"] * c_ratio
        if frac > LOAD_WARN:
            n_warnings += 1                       # free-air loading message
            if lim.get("at_grid_edge"):
                n_warnings += 1                   # CL_max lower-bound caveat
        if frac_ground > GROUND_CL_ALLOWANCE:
            ground_hot += 1
    if ground_hot:
        n_warnings += 1                           # combined allowance message
    if any(cfg.element_re(i) < viscous.RE_FLOOR for i in range(len(design))):
        n_warnings += 1                           # combined Re-floor caveat
    if cfg.ride_height_c < HC_CHOKE_OPTIMISM:
        n_warnings += 1                           # sub-choke optimism caveat

    drag_profile_n = q * area * cd_stack
    drag_induced, _ = induced_drag_n(downforce_n, cfg, installed)
    drag_total_n = drag_profile_n + drag_induced
    return {
        "downforce_n": round(downforce_n, 1),
        "drag_total_n": round(drag_total_n, 2),
        "drag_induced_n": round(drag_induced, 2),
        "drag_profile_n": round(drag_profile_n, 2),
        "efficiency_ld": round(downforce_n / drag_total_n, 2)
            if drag_total_n > 1e-9 else None,
        "C_downforce_estimated": round(c_est, 4),
        "k_ground_realization": round(k_g, 3),
        "loading_fraction_ground_max": round(frac_ground_max, 3),
        "n_warnings": n_warnings,
    }


def sweep(cfg: StackConfig, variable: str, values: list[float],
          model_size: str = "large") -> dict:
    """Operating map: re-evaluate the design across ride height or speed.

    The section geometry never changes along either sweep, so the stack is
    built once. A ride-height sweep re-installs and re-solves the panel
    system per point (the ground image moves); a speed sweep reuses ONE
    inviscid solution (it is speed-independent) and only the Reynolds-number
    lookups and dynamic pressure vary. Points that fail the config bounds are
    skipped with a per-point "error" entry — dataclasses.replace() bypasses
    from_dict validation, so each value is re-checked here.
    """
    if variable not in SWEEP_VARIABLES:
        raise ValueError(f"sweep variable must be one of {SWEEP_VARIABLES}")
    if len(values) > SWEEP_MAX_POINTS:
        raise ValueError(f"sweep is capped at {SWEEP_MAX_POINTS} points "
                         f"({len(values)} requested)")
    n_used = min(cfg.n_panels_per_side, SWEEP_MAX_PANELS)
    base = dataclasses.replace(cfg, n_panels_per_side=n_used)
    design = geometry.build_stack(base)
    if any(e.get("intersects") for e in design):
        raise ValueError("elements intersect — fix the slot geometry before "
                         "sweeping")

    fixed = None   # speed sweep: (installed, free, ground), solved once
    if variable == "speed_ms":
        installed = geometry.install_stack(design, base.ride_height_c)
        coords = [e["coords"] for e in installed]
        fixed = (installed, *panel.solve_pair(coords, 0.0))

    points = []
    for v in values:
        pt_cfg = dataclasses.replace(base, **{variable: float(v)})
        try:
            geometry._validate(pt_cfg)
        except ValueError as e:
            points.append({"value": float(v), "error": str(e)})
            continue
        if fixed is not None:
            installed, free, ground = fixed
        else:
            installed = geometry.install_stack(design, pt_cfg.ride_height_c)
            coords = [e["coords"] for e in installed]
            try:
                free, ground = panel.solve_pair(coords, 0.0)
            except np.linalg.LinAlgError:
                points.append({"value": float(v),
                               "error": "panel system is singular"})
                continue
        points.append({"value": float(v),
                       **_sweep_point(pt_cfg, design, installed, free,
                                      ground, model_size)})
    return {
        "variable": variable,
        "n_panels_per_side_used": n_used,
        "points": points,
        # measured model-validity bands (docs/calibration), in h/c — the
        # map renders them only on ride-height sweeps, converted with the
        # sweep's own chord
        "trust_bands": {
            "optimistic_below_hc": HC_CHOKE_OPTIMISM,
            "conservative_hc": list(HC_CONSERVATIVE),
        },
    }
