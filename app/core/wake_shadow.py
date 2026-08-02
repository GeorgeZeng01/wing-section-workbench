"""Wake-shadow separation screen: the validated per-element check for the
top-side flow collapse the optimizer could not see.

The failure it detects
----------------------
Every converged three-element RANS case on record measures a downstream
element separated over a fifth to two-fifths of its surface (wall-shear
reversed-face fractions 0.22-0.40, docs/calibration/wall_truth.json)
while the optimizer's element-integral loading checks read clean. The
mechanism, visible in the retained flow fields: the element sits in its
neighbors' circulation shadow, the stream over its UPPER side — the
stream carrying the upstream element's wake — decelerates below about
half the freestream, and the wake+boundary-layer system over that side
collapses into a standing dead-air bubble. The flow never reaches the
trailing edge, downforce is delivered by a flow state the screening model
does not describe, and RANS then reads far below the estimate.

The measured discriminator
--------------------------
For each element after the first, take the minimum realized tangential
velocity over the middle of its upper-side arc (15-92%, stagnation split;
the aft cut excludes the potential-flow TE recompression no real flow
has). Measured against every wall-shear-graded element on record
(scripts/separation_metric_check.py, 2026-07-24):

    separated-measured elements:  ueUmin 0.389 .. 0.459
    attached/partial-measured:    ueUmin 0.533 .. 0.619
    (validated baselines and gap-leg flaps: 0.561 .. 0.618;
     stage-2 "flagged"-class flaps, no wall truth: 0.519 .. 0.524)

The gap is stable under paneling 45-70/side (< 0.01 drift) and under the
realization ratio r swept 0 -> 0.35 (~0.03 uniform shift). SHADOW_SEP
sits mid-gap at half the freestream; SHADOW_WARN is the penalty onset so
the optimizer feels the wall before the line, house style.

The first element is exempt: no upstream wake rides over it, its slow
upper side is an ordinary pressure side, and the record confirms mains
attached at upper-side minima down to 0.44 (they are guarded by the
existing loading machinery instead). Two integral-BL alternatives were
built and measured NON-discriminating on the same record before this
shipped — a Thwaites/Michel/Head chordwise march (all classes overlap at
the booked realization) and the earlier canonical-recovery endpoint
metrics — see scripts/separation_metric_check.py and
scripts/recovery_metric_check.py for the executable evidence.

Scope: validated on climbing front-wing stacks (flaps behind and above
their predecessor) in ground effect, s1223-class sections, h/c 0.086 to
0.114, at the booked realized field vt_free + r (vt_ground - vt_free).
Slot-jet/boundary-layer merging in tight gaps remains invisible to any
inviscid quantity (recorded conclusion) — the slot-signature advisory
covers that axis; RANS remains the referee.
"""

from __future__ import annotations

import numpy as np

SHADOW_SEP = 0.50    # predicted top-side collapse below this (mid-gap of
                     # the measured classes, and half the freestream)
SHADOW_WARN = 0.53   # penalty onset / caution band top

# ---------------------------------------------------------------------------
# Validated envelope. MEASURED over every config in docs/calibration, not
# asserted: scripts/separation_metric_check.py re-derives these from the
# record and fails if they stop bounding it.
#
# This exists because the scope paragraph above was prose that nothing
# enforced, and a design outside it got a confident "ok". Measured on two
# converged wall-resolved Fluent solves: a stack running sections absent from
# the record, with a second element at chord ratio 0.5 against a 0.381
# maximum, read shadow_min 0.5471 ("ok", zero penalty) while its second
# element carried 0.3848 reversed near-wall stations. Moving the screen to
# 0.5607 left the flow unchanged at 0.3895 -- outside the envelope the metric
# is not merely mis-scaled, it is not even directional.
#
# A reading outside this envelope is not evidence. It is not a pass either.
SCOPE_HC = (0.0857, 0.1143)          # ride height / chord across the record
SCOPE_SECTIONS = ("s1223", "as6099", "be6699")   # base specs, wrappers off
SCOPE_CHORD_RATIO_FLAP = (0.20, 0.381)           # non-main elements
SCOPE_N_ELEMENTS = (2, 3)
# Knife-edge skirt around the SEP/WARN cutoffs. The cutoffs were calibrated
# against RANS wall-shear labels, and near separation onset those labels
# themselves move with the solver's iteration path: the 2026-07-25 cross-
# rank study measured the separating element's reversed fraction spreading
# 0.042 across 1..16 ranks (cfd_run.SEP_KNIFE_BAND is the wall-side band).
# Mapped through the validation record's class geometry (attached-class
# floor 0.533 at frac ~0.12 vs separated-class ceiling 0.459 at frac
# ~0.22: ~0.74 shadow-units per frac-unit), that spread is ~0.03 in
# shadow_min units. A shadow_min this close to the lines marks a
# knife-edge candidate whichever side it computed on. The flag is
# information for the designer; it does not change the status or any
# penalty.
SHADOW_KNIFE_BAND = 0.03
ARC_LO = 0.15        # evaluated arc window on the upper side: past the
ARC_HI = 0.92        # nose spill, ahead of the TE recompression


def element_sides(mid: np.ndarray, vt: np.ndarray, lengths: np.ndarray,
                  ) -> dict | None:
    """Split one element's contour at the stagnation point.

    mid/vt/lengths: panel midpoints, SIGNED tangential velocity (along the
    CCW contour direction) and panel lengths, in contour order (the
    panelizer's TE -> around -> TE ordering). Returns {"lower": ...,
    "upper": ..., "s_total": ...} — sides labeled GEOMETRICALLY (installed
    frame: lower faces the ground) — each side {"s", "ue", "idx"} with s
    ascending from the stagnation point, ue = |vt| (a leading (0, 0)
    stagnation station is prepended), and idx the original panel indices
    in march order. None when no stagnation point exists (a degenerate,
    non-lifting solution).
    """
    vt = np.asarray(vt, float)
    mid = np.asarray(mid, float)
    lengths = np.asarray(lengths, float)
    n = len(vt)
    if n < 8:
        return None
    s_mid = np.cumsum(lengths) - 0.5 * lengths
    s_total = float(lengths.sum())

    # stagnation candidates: sign changes between consecutive midpoints;
    # pick the one geometrically farthest from the TE (the contour's ends)
    te = 0.5 * (mid[0] + mid[-1])
    d_te = np.hypot(mid[:, 0] - te[0], mid[:, 1] - te[1])
    flips = np.nonzero(vt[:-1] * vt[1:] < 0)[0]
    if len(flips) == 0:
        return None
    j = int(flips[np.argmax(0.5 * (d_te[flips] + d_te[flips + 1]))])
    # linear zero crossing between s_mid[j] and s_mid[j+1]
    v0, v1 = vt[j], vt[j + 1]
    frac = float(v0 / (v0 - v1)) if v0 != v1 else 0.5
    s0 = float(s_mid[j] + frac * (s_mid[j + 1] - s_mid[j]))

    # side A: stagnation -> contour start (panels j..0); side B: j+1..end
    idx_a = np.arange(j, -1, -1)
    s_a = np.concatenate([[0.0], s0 - s_mid[idx_a]])
    u_a = np.concatenate([[0.0], np.abs(vt[idx_a])])
    idx_b = np.arange(j + 1, n)
    s_b = np.concatenate([[0.0], s_mid[idx_b] - s0])
    u_b = np.concatenate([[0.0], np.abs(vt[idx_b])])
    # strictly ascending guard (first real station can coincide with s0)
    for arr in (s_a, s_b):
        for k in range(1, len(arr)):
            if arr[k] <= arr[k - 1]:
                arr[k] = arr[k - 1] + 1e-9

    a = {"s": s_a, "ue": u_a, "idx": idx_a}
    b = {"s": s_b, "ue": u_b, "idx": idx_b}
    # geometric labels: the side whose midpoints sit lower faces the ground
    ya = float(np.mean(mid[idx_a, 1])) if len(idx_a) else 0.0
    yb = float(np.mean(mid[idx_b, 1])) if len(idx_b) else 0.0
    if ya <= yb:
        return {"lower": a, "upper": b, "s_total": s_total}
    return {"lower": b, "upper": a, "s_total": s_total}


def base_section(spec) -> str:
    """The underlying airfoil behind any wrapper spec.

    "shape:b:b:b:ts:BASE" and "mfg:mode:gap:BASE" both wrap another spec,
    and can nest. The validated envelope is about the SECTION, so the
    wrappers come off before the comparison."""
    s = str(spec or "")
    for _ in range(4):
        if s.startswith("shape:") or s.startswith("mfg:"):
            s = s.split(":")[-1]
        else:
            break
    return s.strip().lower()


def scope_check(cfg) -> dict:
    """Which axes of the validated envelope this design leaves.

    The screen's numbers are evidence only inside the envelope they were
    measured in. Outside it a reading is neither a pass nor a fail: it is
    not evidence. This reports that rather than letting a confident number
    travel unlabelled, which is exactly what happened on the design that
    read "ok" at shadow_min 0.5471 while carrying 0.38 reversed near-wall
    stations on its second element.

    Returns {"in_scope": bool, "out": [{axis, value, envelope, detail}]}.
    Deliberately NOT a verdict on the design — a stack outside the envelope
    may be perfectly healthy. It is a statement about what the screen knows.
    """
    out = []
    n = len(cfg.elements)
    if not (SCOPE_N_ELEMENTS[0] <= n <= SCOPE_N_ELEMENTS[1]):
        out.append({"axis": "n_elements", "value": n,
                    "envelope": list(SCOPE_N_ELEMENTS),
                    "detail": f"{n}-element stack; the record holds "
                              f"{SCOPE_N_ELEMENTS[0]}- to "
                              f"{SCOPE_N_ELEMENTS[1]}-element stacks"})
    hc = float(cfg.ride_height_c)
    if not (SCOPE_HC[0] - 1e-6 <= hc <= SCOPE_HC[1] + 1e-6):
        out.append({"axis": "ride_height_c", "value": round(hc, 4),
                    "envelope": list(SCOPE_HC),
                    "detail": f"h/c {hc:.4f} is outside the measured "
                              f"{SCOPE_HC[0]:.4f}-{SCOPE_HC[1]:.4f}"})
    unknown = sorted({base_section(e.airfoil) for e in cfg.elements}
                     - set(SCOPE_SECTIONS))
    if unknown:
        out.append({"axis": "sections", "value": unknown,
                    "envelope": list(SCOPE_SECTIONS),
                    "detail": f"section(s) {', '.join(unknown)} appear "
                              f"nowhere in the calibration record"})
    lo, hi = SCOPE_CHORD_RATIO_FLAP
    bad = [round(float(e.chord_ratio), 3) for e in cfg.elements[1:]
           if not (lo - 1e-6 <= float(e.chord_ratio) <= hi + 1e-6)]
    if bad:
        out.append({"axis": "flap_chord_ratio", "value": bad,
                    "envelope": [lo, hi],
                    "detail": f"flap chord ratio(s) {bad} outside the "
                              f"measured {lo:.3f}-{hi:.3f}"})
    return {"in_scope": not out, "out": out}


def scope_warning(cfg) -> str | None:
    """One line for the user when the screen is being asked something it was
    never validated to answer. None when the design is inside the envelope."""
    sc = scope_check(cfg)
    if sc["in_scope"]:
        return None
    return ("separation screen out of validated scope — "
            + "; ".join(o["detail"] for o in sc["out"])
            + ". Its reading for this design is not evidence either way "
              "(measured: a design outside this envelope read 'ok' while "
              "RANS found 38% reversed flow on its second element, and "
              "moving the screen did not move the flow). Verify with RANS "
              "rather than trusting the screen here.")


def stack_shadow(free_sol, ground_sol, r_gain: float) -> list[dict]:
    """Per-element wake-shadow metric at the realized operating field.

    free_sol / ground_sol: PanelSolutions from panel.solve_pair (shared
    panelization, index-aligned arrays). r_gain: the realization ratio
    from analysis.realized_gain — the same r the downforce estimate books.

    Returns one dict per element:
      shadow_min   min upper-side Ue/V_inf over the ARC window; None for
                   the first element (exempt — see module docstring) and
                   for degenerate solutions
      arc_at_min   arc fraction (of the upper side) where the minimum sits
      x_min, y_min midpoint coordinates of that station (plot marker)
      status       "ok" | "warn" (< SHADOW_WARN) | "collapse" (< SHADOW_SEP)
                   | None where shadow_min is None
    """
    r = float(np.clip(r_gain, 0.0, 1.0))
    vt_eff = free_sol.vt + r * (ground_sol.vt - free_sol.vt)
    out = []
    n_elem = int(free_sol.element_index.max()) + 1 \
        if len(free_sol.element_index) else 0
    for k in range(n_elem):
        if k == 0:
            out.append({"shadow_min": None, "arc_at_min": None,
                        "x_min": None, "y_min": None, "status": None,
                        "knife_edge": None})
            continue
        sel = free_sol.element_index == k
        sides = element_sides(free_sol.midpoints[sel], vt_eff[sel],
                              free_sol.panel_lengths[sel])
        if sides is None:
            out.append({"shadow_min": None, "arc_at_min": None,
                        "x_min": None, "y_min": None, "status": None,
                        "knife_edge": None})
            continue
        up = sides["upper"]
        s, ue = up["s"], up["ue"]
        st = float(s[-1])
        m = (s >= ARC_LO * st) & (s <= ARC_HI * st)
        if not m.any():
            out.append({"shadow_min": None, "arc_at_min": None,
                        "x_min": None, "y_min": None, "status": None,
                        "knife_edge": None})
            continue
        i_rel = int(np.argmin(ue[m]))
        i_abs = int(np.nonzero(m)[0][i_rel])
        v = float(ue[i_abs])
        # map back to the source panel midpoint (s/ue carry a prepended
        # stagnation station; idx aligns with s[1:])
        mid_sel = free_sol.midpoints[sel]
        i_panel = up["idx"][max(i_abs - 1, 0)]
        status = ("collapse" if v < SHADOW_SEP
                  else "warn" if v < SHADOW_WARN else "ok")
        out.append({
            "shadow_min": round(v, 4),
            "arc_at_min": round(float(s[i_abs] / st), 3),
            "x_min": round(float(mid_sel[i_panel, 0]), 5),
            "y_min": round(float(mid_sel[i_panel, 1]), 5),
            "status": status,
            "knife_edge": bool(SHADOW_SEP - SHADOW_KNIFE_BAND <= v
                               < SHADOW_WARN + SHADOW_KNIFE_BAND),
        })
    return out
