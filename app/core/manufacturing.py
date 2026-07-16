"""Manufacturing preparation of airfoil contours.

Theoretical airfoil coordinates close to a knife edge at the trailing edge —
zero thickness at a shallow included angle. No layup, print or hot-wire cut
can produce that, so for manufacturing the TE is opened to a finite
thickness. Two treatments are offered:

thicken   XFOIL-TGAP-style: extra thickness is added symmetrically about the
          camber line, blended in over the aft part of the chord (quadratic
          ramp from BLEND_START to the TE), then a hard thickness floor is
          applied so that no station aft of the section's thickest point is
          thinner than the TE gap itself — the reason the TE is opened is
          that anything thinner cannot be built, and that argument applies
          to a mid-chord waist just as much as to the TE. Chord, planform
          and camber are preserved, so the section keeps its design loading.
          This is the standard aerodynamic prep and the default.
truncate  the contour is cut where the section last reaches the target
          thickness and closed by a straight base, then rescaled back to
          unit chord. Loses the (highly cambered) last few percent of the
          profile — use it when a part or mold must literally be cut back.
          Because this mode means "literally cut the part", no thickness
          floor is applied ahead of the cut; geometry_report measures the
          as-built minimum aft thickness and warns if a waist remains.

A V-notch cut was considered and rejected: it replaces one knife edge with
two and adds a stress riser, with no aerodynamic benefit at these speeds.

Treated sections are addressed by derived specs

    "mfg:<mode>:<gap_c>:<base spec>"

where gap_c is the TE thickness as a fraction of the *element* chord (the
base spec may itself contain colons, e.g. "custom:my-foil", so it comes
last). gap_c is quantized UP to a coarse grid so the spec strings — and with
them the repanel / polar caches — stay stable while the optimizer sweeps
chord ratios. `airfoils.resolve` dispatches "mfg:" specs back to this module,
which makes the treated geometry a first-class airfoil everywhere: the panel
solver, NeuralFoil polars, XFOIL reference runs, the screener and every
exporter all see the manufactured shape.

All functions operate on unit-chord Selig contours (TE -> upper -> LE ->
lower -> TE).
"""

from __future__ import annotations

import numpy as np

MODES = ("thicken", "truncate")
GAP_STEP = 2.5e-4      # spec quantization: 0.025 %c (≈0.09 mm on a 350 mm chord)
GAP_C_MAX = 0.12       # sanity ceiling for a derived spec
BLEND_START = 0.65     # thicken: ramp starts at 65 %c
MAX_ADD_C = 0.06       # thicken: never add more than 6 %c of thickness
X_CUT_MIN = 0.85       # truncate: never cut ahead of 85 %c
_TINY = 1e-9


def te_gap(coords: np.ndarray) -> float:
    c = np.asarray(coords, float)
    return float(np.hypot(*(c[0] - c[-1])))


def derived_spec(base_spec: str, gap_c: float, mode: str = "thicken") -> str:
    """Spec string for `base_spec` with its TE opened to >= gap_c."""
    if mode not in MODES:
        raise ValueError(f"TE treatment must be one of {MODES}, got {mode!r}")
    g = float(np.clip(gap_c, GAP_STEP, GAP_C_MAX))
    g = float(np.ceil(g / GAP_STEP - 1e-9) * GAP_STEP)   # round UP: ">= gap"
    return f"mfg:{mode}:{g:.6f}:{base_spec}"


def parse(spec: str) -> tuple[str, float, str]:
    """"mfg:<mode>:<gap_c>:<base>" -> (mode, gap_c, base)."""
    parts = str(spec).split(":", 3)
    if len(parts) != 4 or parts[0].lower() != "mfg":
        raise ValueError(f"not a manufacturing spec: {spec!r}")
    mode, gap_s, base = parts[1], parts[2], parts[3]
    if mode not in MODES:
        raise ValueError(f"unknown TE treatment {mode!r} in {spec!r}")
    gap_c = float(gap_s)
    if not (0.0 < gap_c <= GAP_C_MAX):
        raise ValueError(f"TE gap {gap_c} out of range in {spec!r}")
    return mode, gap_c, base


def _surfaces(c: np.ndarray):
    """(i_le, upper LE->TE, lower LE->TE) of a Selig contour."""
    i_le = int(np.argmin(c[:, 0]))
    return i_le, c[: i_le + 1][::-1], c[i_le:]


def thickness_at(c: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Vertical thickness of a Selig contour at station(s) x."""
    _, up, lo = _surfaces(np.asarray(c, float))
    yu = np.interp(x, up[:, 0], up[:, 1])
    yl = np.interp(x, lo[:, 0], lo[:, 1])
    return yu - yl


def _aft_window(c: np.ndarray, ref_t: float | None = None) -> np.ndarray:
    """Sampling stations for the buildable-thickness window.

    The window runs from the point where the section stops being "nose" to
    (almost) the TE — the last 0.2%c is the TE base itself, whose thickness
    te_gap() already reports. With ref_t given, the nose ends at the FIRST
    station that reaches ref_t (everything behind it must stay buildable);
    without it, or when the section never reaches ref_t (a plate thinner
    than the reference everywhere), the thickest point is used."""
    xs = np.linspace(0.005, float(c[:, 0].max()) - 0.002, 800)
    t = thickness_at(c, xs)
    j = int(np.argmax(t))
    if ref_t is not None:
        ok = np.where(t >= ref_t)[0]
        if len(ok):
            j = int(ok[0])
    return xs[j:]


def aft_min_thickness(c: np.ndarray, ref_t: float | None = None) -> float:
    """Thinnest station between the end of the nose taper and the TE.

    This is the number a builder cares about: the TE may have been opened to
    a buildable gap, but a waist ahead of it that is thinner defeats the
    treatment. ref_t (usually the TE gap) defines where the nose taper ends;
    see _aft_window."""
    cc = np.asarray(c, float)
    return float(thickness_at(cc, _aft_window(cc, ref_t)).min())


FLOOR_PAD = 1.5e-4   # extra floor height soaking up interpolation noise and
                     # the downstream spline repanel's undershoot at the kinks


def _floor_aft_thickness(c: np.ndarray, floor_c: float) -> tuple[np.ndarray, bool]:
    """Raise local thickness to >= floor_c everywhere aft of the thickest
    station, adding material symmetrically about the camber line.

    The deficient window is densified (segment midpoints inserted) before
    lifting, so the floor holds between the original nodes too and the
    cosine repanel spline downstream has enough fit points to follow the
    plateau instead of undercutting it."""
    x_max = float(c[:, 0].max())
    # the buildable window starts where the ORIGINAL section first reaches
    # the floor (nose taper ends there) — not at its thickest point, or a
    # thin stretch between the two would silently stay thinner than the TE.
    # Sections that never reach the floor (plates thinner than the requested
    # TE everywhere) fall back to the thickest point; geometry_report flags
    # those separately.
    x_start = float(_aft_window(c, floor_c)[0])

    def deficits(cc: np.ndarray) -> np.ndarray:
        # the pad soaks up spline undershoot along the plateau; the TE base
        # endpoints are exact fit points, so it tapers off there and the
        # requested TE gap stays exact
        x = cc[:, 0]
        target = floor_c + FLOOR_PAD * np.clip((x_max - x) / 0.01, 0.0, 1.0)
        d = np.maximum(target - thickness_at(cc, x), 0.0)
        d[x < x_start] = 0.0
        return d

    if deficits(c).max() <= _TINY and aft_min_thickness(c, floor_c) >= floor_c:
        return c, False
    cc = c.copy()
    for _ in range(4):
        d = deficits(cc)
        if d.max() > _TINY:
            # densify the deficient window so the floor holds between nodes
            lo_x = float(cc[d > 0, 0].min())
            seg = (cc[:-1, 0] >= lo_x - 1e-6) & (cc[1:, 0] >= lo_x - 1e-6)
            mids = 0.5 * (cc[:-1][seg] + cc[1:][seg])
            cc = np.insert(cc, np.nonzero(seg)[0] + 1, mids, axis=0)
            i_le = int(np.argmin(cc[:, 0]))
            d = deficits(cc)
            sign = np.ones(len(cc))
            sign[i_le + 1:] = -1.0    # lower surface moves down
            cc[:, 1] += 0.5 * d * sign
        # verify with the same fine-grid measurement the report uses,
        # not just at the nodes
        if aft_min_thickness(cc, floor_c) >= floor_c - _TINY:
            break
    return cc, True


def _thicken(c: np.ndarray, gap_c: float) -> tuple[np.ndarray, dict]:
    need = gap_c - te_gap(c)
    out = c.copy()
    clamped = False
    if need > _TINY:
        x = c[:, 0]
        i_le = int(np.argmin(x))
        w = np.clip((x - BLEND_START) / (1.0 - BLEND_START), 0.0, None) ** 2
        # endpoints are the TE pair; scale delta so the added vertical opening
        # between them equals `need` exactly
        denom = 0.5 * (w[0] + w[-1])
        delta = need / max(denom, 1e-6)
        clamped = bool(delta > MAX_ADD_C)
        delta = min(delta, MAX_ADD_C)
        sign = np.ones(len(c))
        sign[i_le + 1:] = -1.0        # lower surface moves down
        out[:, 1] += 0.5 * delta * w * sign
    # the ramp only opens the TE; a section that is thinner than the gap
    # somewhere ahead of it is just as unbuildable there, so floor the whole
    # aft thickness distribution at the gap
    out, floored = _floor_aft_thickness(out, gap_c)
    return out, {"clamped": clamped, "floored": floored}


def _truncate(c: np.ndarray, gap_c: float) -> tuple[np.ndarray, dict]:
    if te_gap(c) >= gap_c - _TINY:
        return c.copy(), {"clamped": False, "x_cut": None}
    _, up, lo = _surfaces(c)
    xs = np.linspace(0.40, 1.0, 600)
    yu = np.interp(xs, up[:, 0], up[:, 1])
    yl = np.interp(xs, lo[:, 0], lo[:, 1])
    # the cut section is rescaled back to unit chord (/x_cut), which scales
    # its thickness too — solve t(x) = gap_c * x so the FINAL TE gap equals
    # the request instead of overshooting it by the rescale factor
    t = yu - yl
    ok = np.where(t >= gap_c * xs)[0]
    clamped = False
    if len(ok) == 0:
        x_cut, clamped = X_CUT_MIN, True
    else:
        j = int(ok[-1])               # rear-most station still thick enough
        if j >= len(xs) - 1:
            x_cut = xs[-1] - 1e-4
        else:
            # linear refinement of the crossing between xs[j] and xs[j+1]
            g = t - gap_c * xs
            f = g[j] / max(g[j] - g[j + 1], 1e-12)
            x_cut = float(xs[j] + f * (xs[j + 1] - xs[j]))
        if x_cut < X_CUT_MIN:
            x_cut, clamped = X_CUT_MIN, True
    yu_c = float(np.interp(x_cut, up[:, 0], up[:, 1]))
    yl_c = float(np.interp(x_cut, lo[:, 0], lo[:, 1]))
    up_new = np.vstack([up[up[:, 0] < x_cut - _TINY], [x_cut, yu_c]])
    lo_new = np.vstack([lo[lo[:, 0] < x_cut - _TINY], [x_cut, yl_c]])
    out = np.vstack([up_new[::-1], lo_new[1:]]) / x_cut   # back to unit chord
    return out, {"clamped": clamped, "x_cut": round(x_cut, 4)}


def apply_te_treatment(coords: np.ndarray, gap_c: float,
                       mode: str = "thicken") -> tuple[np.ndarray, dict]:
    """Open the TE of a unit-chord Selig contour to >= gap_c.

    Returns (coords, meta); meta["clamped"] is True when the target could not
    be reached without destroying the section (the treatment then stops at
    its safety limit and geometry_report raises a warning instead).
    """
    c = np.asarray(coords, float)
    if mode == "thicken":
        return _thicken(c, float(gap_c))
    if mode == "truncate":
        return _truncate(c, float(gap_c))
    raise ValueError(f"TE treatment must be one of {MODES}, got {mode!r}")


def resolve_mfg(spec: str) -> tuple[str, np.ndarray]:
    """Resolve a "mfg:" spec -> (display name, treated unit-chord coords)."""
    from . import airfoils
    mode, gap_c, base = parse(spec)
    name, raw = airfoils.resolve(base)
    out, _meta = apply_te_treatment(airfoils.normalize(raw), gap_c, mode)
    return f"{name} (mfg)", out
