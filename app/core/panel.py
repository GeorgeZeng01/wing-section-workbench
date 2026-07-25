"""Multi-element inviscid panel method (Hess-Smith) with ground effect.

Formulation
-----------
Each element contour (Selig ordering, TE -> upper -> LE -> lower -> TE) is
discretized into flat panels carrying a constant source strength sigma_j
(one unknown per panel) plus a single vortex strength tau_k shared by all
panels of element k. Boundary conditions: zero normal velocity at every panel
midpoint, plus one Kutta condition per element (equal-magnitude tangential
velocities leaving the trailing edge). This is the classical Hess-Smith
scheme, solved as one dense linear system across all elements so every
slot/stack interaction is captured.

Ground effect uses the method of images: the velocity induced by the mirror
system at a point p equals M @ u(M @ p) where M = diag(1, -1) — mirroring a
source keeps its sign and mirroring a vortex flips it, and both facts fall
out of that single identity, so images add no extra unknowns.

The solver is frame-agnostic: pass installed-frame coordinates (ground at
y = 0, all geometry above it) with ground=True, or any frame with
ground=False. Forces are reported as pressure integrals per element.

Everything is vectorized NumPy; a 4-element stack solves in ~10-100 ms,
which is what makes the downforce optimizer feasible.
"""

from __future__ import annotations

import numpy as np

_EPS = 1e-12


class PanelSolution:
    __slots__ = ("Cl", "Cd_numerical", "Cm_le", "elements", "midpoints",
                 "cp", "element_index", "n_panels", "vt", "panel_lengths")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _orient_ccw(coords: np.ndarray) -> np.ndarray:
    # closed shoelace sum: the TE-base closing edge must be included, or the
    # sign stops being translation-invariant for open-TE contours and a far
    # enough translated flap gets silently re-wound the wrong way
    x, y = coords[:, 0], coords[:, 1]
    area2 = np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)
    return coords if area2 > 0 else coords[::-1].copy()


def _panelize(element_coords: list[np.ndarray]):
    """Flatten all elements into panel arrays + bookkeeping."""
    p1, p2, elem_of = [], [], []
    first_panel, last_panel = [], []
    n = 0
    for k, c in enumerate(element_coords):
        c = _orient_ccw(np.asarray(c, float))
        # drop exactly-duplicated consecutive points (some .dat files have them)
        keep = np.ones(len(c), bool)
        keep[1:] = np.hypot(*np.diff(c, axis=0).T) > 1e-10
        c = c[keep]
        if len(c) < 20:
            raise ValueError(f"element {k}: too few distinct points ({len(c)})")
        p1.append(c[:-1])
        p2.append(c[1:])
        m = len(c) - 1
        elem_of.append(np.full(m, k))
        first_panel.append(n)
        last_panel.append(n + m - 1)
        n += m
    P1 = np.vstack(p1)
    P2 = np.vstack(p2)
    d = P2 - P1
    length = np.hypot(d[:, 0], d[:, 1])
    if (length < 1e-12).any():
        raise ValueError("zero-length panel encountered")
    t = d / length[:, None]
    nrm = np.column_stack([t[:, 1], -t[:, 0]])  # outward for CCW contour
    mid = 0.5 * (P1 + P2)
    return (P1, d, length, t, nrm, mid, np.concatenate(elem_of),
            np.array(first_panel), np.array(last_panel))


def _influence(pts: np.ndarray, P1, d, length):
    """Velocity influence of unit-strength panels at pts.

    Returns (us, vs, uv, vv), each (Q, M), global frame: source and vortex
    sheet contributions per panel.
    """
    cos = (d[:, 0] / length)[None, :]
    sin = (d[:, 1] / length)[None, :]
    rx = pts[:, 0][:, None] - P1[:, 0][None, :]
    ry = pts[:, 1][:, None] - P1[:, 1][None, :]
    xl = rx * cos + ry * sin
    yl = -rx * sin + ry * cos
    L = length[None, :]
    r1sq = xl * xl + yl * yl
    r2sq = (xl - L) ** 2 + yl * yl
    ln = 0.25 / np.pi * np.log((r1sq + _EPS) / (r2sq + _EPS))
    th = 0.5 / np.pi * (np.arctan2(yl, xl - L) - np.arctan2(yl, xl))
    # local: source (ln, th); vortex (-th... u_v = -th? see below), rotate to global
    #   source: ul = ln, vl = th
    #   vortex: ul = -th_signed, vl = ln  with th_signed = (theta1-theta2)/2pi = -th
    us = ln * cos - th * sin
    vs = ln * sin + th * cos
    uv = -th * cos - ln * sin
    vv = -th * sin + ln * cos
    return us, vs, uv, vv


def _assemble_and_solve(geo, us, vs, uv, vv, alpha_deg, ref_chord):
    (P1, d, length, t, nrm, mid, elem_of, first_panel, last_panel) = geo
    M = len(length)
    K = len(first_panel)

    # per-element vortex columns: sum panel vortex influence within element
    UV = np.zeros((M, K))
    VV = np.zeros((M, K))
    for k in range(K):
        cols = elem_of == k
        UV[:, k] = uv[:, cols].sum(axis=1)
        VV[:, k] = vv[:, cols].sum(axis=1)

    A = np.empty((M + K, M + K))
    rhs = np.empty(M + K)
    a = np.radians(alpha_deg)
    vinf = np.array([np.cos(a), np.sin(a)])

    # flow tangency at every midpoint
    A[:M, :M] = nrm[:, 0:1] * us + nrm[:, 1:2] * vs
    A[:M, M:] = nrm[:, 0:1] * UV + nrm[:, 1:2] * VV
    rhs[:M] = -(nrm @ vinf)

    # Kutta: tangential velocities at the two TE panels cancel along the contour
    Tu = t[:, 0:1] * us + t[:, 1:2] * vs      # (M, M) tangential source infl.
    Tv = t[:, 0:1] * UV + t[:, 1:2] * VV      # (M, K)
    for k in range(K):
        f, l = first_panel[k], last_panel[k]
        A[M + k, :M] = Tu[f] + Tu[l]
        A[M + k, M:] = Tv[f] + Tv[l]
        rhs[M + k] = -(t[f] + t[l]) @ vinf

    x = np.linalg.solve(A, rhs)
    sigma, tau = x[:M], x[M:]

    vt = (t @ vinf) + Tu @ sigma + Tv @ tau
    cp = 1.0 - vt**2

    # pressure integration per element
    elements = []
    total_f = np.zeros(2)
    cm_le = 0.0
    for k in range(K):
        sel = elem_of == k
        f_vec = -(cp[sel, None] * nrm[sel] * length[sel, None]).sum(axis=0)
        f_vec /= ref_chord
        r = mid[sel]
        # standard aero sign: Cm positive = nose-up = MINUS the CCW z-moment
        # (a lift force behind the origin pitches the nose down => Cm < 0)
        dm = (cp[sel] * length[sel] *
              (r[:, 0] * nrm[sel, 1] - r[:, 1] * nrm[sel, 0])).sum() / ref_chord**2
        cm_le += dm
        total_f += f_vec
        elements.append({
            "Cx": float(f_vec[0]), "Cy": float(f_vec[1]),
            "circulation": float(-tau[k] * length[sel].sum()),
        })

    lift_dir = np.array([-vinf[1], vinf[0]])
    return PanelSolution(
        Cl=float(total_f @ lift_dir),
        Cd_numerical=float(total_f @ vinf),
        Cm_le=float(cm_le),
        elements=elements,
        midpoints=mid,
        cp=cp,
        element_index=elem_of,
        n_panels=M,
        # signed tangential velocity along the CCW contour direction — the
        # wake-shadow screen splits each contour at the stagnation point,
        # which needs the sign that |vt| = sqrt(1-cp) cannot recover
        vt=vt,
        panel_lengths=length,
    )


def _base_matrices(element_coords):
    geo = _panelize(element_coords)
    (P1, d, length, t, nrm, mid, *_rest) = geo
    # Evaluate a hair off the surface on the exterior side: the self-panel
    # angle term is discontinuous exactly on the sheet, and float noise in the
    # midpoint would otherwise pick its sign at random.
    eval_pts = mid + nrm * (1e-8 * length)[:, None]
    infl = _influence(eval_pts, P1, d, length)
    return geo, eval_pts, infl


def _image_matrices(geo, eval_pts, infl):
    (P1, d, length, *_rest) = geo
    us, vs, uv, vv = infl
    mmid = eval_pts * np.array([1.0, -1.0])
    us2, vs2, uv2, vv2 = _influence(mmid, P1, d, length)
    # image velocity = M @ u(M @ p): same rule covers sources and vortices
    return us + us2, vs - vs2, uv + uv2, vv - vv2


def solve(element_coords: list[np.ndarray], alpha_deg: float = 0.0,
          ground: bool = False, ref_chord: float = 1.0) -> PanelSolution:
    """Solve the multi-element inviscid problem.

    element_coords: list of (N,2) contours (any winding; re-oriented CCW).
    alpha_deg: freestream angle. Must be 0 with ground=True — the image
    system mirrors geometry about y = 0, which is only a ground plane when
    the freestream is parallel to it; rotate the geometry instead.
    ref_chord: reference chord for all coefficients (stack units: main = 1).
    """
    if ground and abs(alpha_deg) > 1e-9:
        raise ValueError("ground=True requires alpha_deg=0 (the image plane "
                         "is only a ground plane for a parallel freestream) "
                         "— rotate the geometry instead")
    geo, eval_pts, infl = _base_matrices(element_coords)
    if ground:
        infl = _image_matrices(geo, eval_pts, infl)
    return _assemble_and_solve(geo, *infl, alpha_deg, ref_chord)


def solve_pair(element_coords: list[np.ndarray], alpha_deg: float = 0.0,
               ref_chord: float = 1.0) -> tuple[PanelSolution, PanelSolution]:
    """(free_air, ground) solutions sharing panelization and base influence
    matrices — cheaper than two independent solve() calls. Same contract as
    solve(ground=True): the ground half is only physical at alpha_deg=0."""
    if abs(alpha_deg) > 1e-9:
        raise ValueError("solve_pair requires alpha_deg=0 (the image plane "
                         "is only a ground plane for a parallel freestream) "
                         "— rotate the geometry instead")
    geo, eval_pts, infl = _base_matrices(element_coords)
    free = _assemble_and_solve(geo, *infl, alpha_deg, ref_chord)
    ground_infl = _image_matrices(geo, eval_pts, infl)
    ground = _assemble_and_solve(geo, *ground_infl, alpha_deg, ref_chord)
    return free, ground
