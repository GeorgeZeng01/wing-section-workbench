"""Flow-field extraction and rendering from a solved OpenFOAM case.

The RANS cases are 2D (one cell thick), so the internal field IS the
cross-section: no ParaView needed. run.sh finishes with
`postProcess -func writeCellCentres`, which writes the cell-centre
coordinates as a volVectorField `C` beside `U` and `p` in the final time
directory; this module parses those ASCII fields, interpolates them onto a
regular grid, and renders the section view the studio already uses —
as driven, ground at y = 0, flow left to right — with velocity-magnitude
or Cp contours plus streamlines, the element silhouettes, and the ground.

Note on pressure: incompressible OpenFOAM solves kinematic pressure
(p/rho, m^2/s^2), so Cp = p / (0.5 U_inf^2) with no density factor.
"""

from __future__ import annotations

import io
import math
import re
import threading
from pathlib import Path

import numpy as np

from . import geometry
from .geometry import StackConfig

GRID_NX = 640          # interpolation grid; plenty for a screen-size figure
PNG_DPI = 115

# rendering uses the object-oriented matplotlib API (no pyplot, no global
# figure registry), but serialize anyway: two concurrent renders would
# double the peak memory of the griddata grids for no wall-clock gain
_render_lock = threading.Lock()


class PostError(RuntimeError):
    """The case does not carry a readable flow field."""


# ---------- OpenFOAM ASCII field parsing ----------

def _parse_foam_list(text: str, kind: str, n: int, start: int) -> np.ndarray:
    """Numbers of a List<scalar|vector> body starting right after '('.

    The closing paren is found at nesting depth 0 — OpenFOAM writes both
    one-entry-per-line and single-line lists, and vector entries carry
    their own parens."""
    depth, i = 1, start
    while depth and i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    if depth:
        raise PostError("unterminated internalField list")
    block = text[start:i - 1]
    if kind == "vector":
        toks = block.replace("(", " ").replace(")", " ").split()
        vals = np.array(toks, dtype=float)
        if vals.size != 3 * n:
            raise PostError(f"vector field: expected {3 * n} numbers, "
                            f"got {vals.size}")
        return vals.reshape(n, 3)
    vals = np.array(block.split(), dtype=float)
    if vals.size != n:
        raise PostError(f"scalar field: expected {n} numbers, got {vals.size}")
    return vals


def parse_internal_field(text: str) -> np.ndarray:
    """The internalField of an ASCII vol field as an array.

    nonuniform List<scalar>  ->  (N,)
    nonuniform List<vector>  ->  (N, 3)
    Handles the one-per-line, single-line and compact N{value} encodings
    (OpenFOAM emits the last whenever every value is identical).
    Uniform fields are rejected — for the solved fields this module reads,
    a uniform internalField means the case never actually ran."""
    m = re.search(r"internalField\s+nonuniform\s+List<(scalar|vector)>\s*"
                  r"(\d+)\s*([({])", text)
    if m is None:
        if re.search(r"internalField\s+uniform", text):
            raise PostError("field is uniform — the case has no solved flow")
        raise PostError("no parsable internalField in the file")
    kind, n = m.group(1), int(m.group(2))
    if m.group(3) == "{":
        end = text.find("}", m.end())
        if end < 0:
            raise PostError("unterminated internalField list")
        block = text[m.end():end]
        if kind == "vector":
            one = np.array(block.replace("(", " ").replace(")", " ").split(),
                           dtype=float)
            return np.tile(one, (n, 1))
        return np.full(n, float(block))
    return _parse_foam_list(text, kind, n, m.end())


def _patch_boundary_values(text: str, patch: str) -> np.ndarray | None:
    """boundaryField values of one patch as an (N, 3) array, or None.

    Handles the nonuniform list and the uniform single-vector forms."""
    i = text.find("boundaryField")
    if i < 0:
        return None
    bf = text[i:]
    m = re.search(rf"\b{re.escape(patch)}\s*\{{", bf)
    if m is None:
        return None
    seg = bf[m.end():]
    vm = re.search(r"value\s+nonuniform\s+List<vector>\s*(\d+)\s*\(", seg)
    if vm is not None:
        return _parse_foam_list(seg, "vector", int(vm.group(1)), vm.end())
    um = re.search(r"value\s+uniform\s*\(([^)]+)\)", seg)
    if um is not None:
        return np.array([float(x) for x in um.group(1).split()],
                        dtype=float).reshape(1, 3)
    return None


def wall_report(case_dir: Path) -> dict | None:
    """Measured wall state from the latest written diagnostics, or None
    when the case predates them (runs generated before the yPlus1 /
    wallShearStress1 function objects existed).

    Per wing patch: reversed-flow fraction from the wallShearStress field
    (OpenFOAM's wall shear vector points along the near-wall flow with the
    sign of the patch-normal contraction — attached left-to-right flow
    reads tau_x < 0, so tau_x > 0 marks reversed flow), and y+ min/max/avg
    from the yPlus1 file output."""
    case_dir = Path(case_dir)
    # newest time dir carrying the shear field (write times + stop writes)
    tdirs = []
    for d in case_dir.iterdir() if case_dir.is_dir() else []:
        try:
            t = float(d.name)
        except ValueError:
            continue
        if (d / "wallShearStress").is_file():
            tdirs.append((t, d))
    yplus: dict[str, dict] = {}
    for dat in sorted(case_dir.glob("postProcessing/yPlus1/*/yPlus.dat")):
        try:
            for ln in dat.read_text(errors="replace").splitlines():
                if ln.startswith("#") or not ln.strip():
                    continue
                parts = ln.split()
                if len(parts) >= 5:
                    yplus[parts[1]] = {"min": round(float(parts[2]), 2),
                                       "max": round(float(parts[3]), 1),
                                       "avg": round(float(parts[4]), 2)}
        except (OSError, ValueError):
            continue
    separation: dict[str, dict] = {}
    sep_time = None
    if tdirs:
        _, tdir = max(tdirs)
        sep_time = tdir.name
        try:
            text = (tdir / "wallShearStress").read_text(errors="replace")
            for m in re.finditer(r"\b(wing_e\d+)\s*\{", text):
                patch = m.group(1)
                tau = _patch_boundary_values(text, patch)
                if tau is None or not len(tau):
                    continue
                separation[patch] = {
                    "reversed_frac": round(float((tau[:, 0] > 0).mean()), 3),
                    "n_faces": int(len(tau)),
                }
        except OSError:
            pass
    if not yplus and not separation:
        return None
    # the write time travels with the reading: a field measurement taken
    # from a different iteration than this shear field cannot be compared
    # against it, and the caller can only notice if both say when they are
    return {"yplus": {k: v for k, v in yplus.items()} or None,
            "separation": separation or None,
            "time": sep_time if separation else None}


def fluent_wall_report(case_dir: Path, cfg: StackConfig) -> dict | None:
    """The wall attachment channel from a Fluent run's own solution.

    Reads wall_shear.csv (per-node x/y + x-wall-shear/y-wall-shear on the
    profile zone, written by the 2D engine beside C/U/p) and attributes
    each node to its element by nearest installed-polygon vertex — the same
    cKDTree pattern the recirculation report uses, so the two channels
    cannot disagree about which element a point belongs to.

    SIGN CONVENTION, opposite of the OpenFOAM parser and both are correct:
    Fluent exports the shear exerted ON THE WALL, which points WITH the
    near-wall flow, so attached left-to-right flow reads x-shear > 0 and
    reversed flow reads x-shear < 0. OpenFOAM's wallShearStress is the
    traction on the FLUID, so attached reads tau_x < 0 there. Validated on
    the retained collapse case: e2 reads 0.378 here against its own field
    probe's 0.385, and on the 2026-08 OF pairing record the engines agree
    on every main element while genuinely differing on the baseline flap
    (0.102 here vs 0.437 there) — a physics difference, not a parser one.

    None when the case carries no wall_shear.csv (runs from before the
    channel existed) — absence, never a pass."""
    import numpy as np

    path = Path(case_dir) / "wall_shear.csv"
    if not path.is_file():
        return None
    try:
        rows = np.genfromtxt(path, delimiter=",", skip_header=1)
    except (OSError, ValueError):
        return None
    if rows.ndim != 2 or rows.shape[1] < 4 or not len(rows):
        return None
    finite = np.isfinite(rows[:, 1:4]).all(axis=1)
    rows = rows[finite]
    if not len(rows):
        return None
    xy, tx = rows[:, 1:3], rows[:, 3]

    from scipy.spatial import cKDTree
    polys = _installed_polys_m(cfg)
    pts = np.vstack(polys)
    owner = np.concatenate([np.full(len(p), k)
                            for k, p in enumerate(polys)])
    _, idx = cKDTree(pts).query(xy)
    own = owner[idx]
    separation = {}
    for k in range(len(polys)):
        m = own == k
        if not m.any():
            continue
        separation[f"wing_e{k + 1}"] = {
            "reversed_frac": round(float((tx[m] < 0).mean()), 3),
            "n_faces": int(m.sum()),
        }
    if not separation:
        return None
    return {"yplus": None, "separation": separation, "time": None,
            "source": "fluent-x-wall-shear"}


# ---------- recirculation topology (both engines) ----------
#
# wall_report answers HOW MUCH, on the OpenFOAM engine only, as one scalar
# per element. It cannot say where the reversal sits, how big it is, or
# whether it closes -- and on the Fluent 2D engine, which exports no wall
# shear, it cannot say anything at all. This section reads the same solved
# field the flow view is drawn from, so it works on both engines, and adds
# the structure the scalar throws away.
#
# Two channels with a strict division of labour:
#   A. an equal-arc near-wall probe    -> MAGNITUDE (the wall_report analogue)
#   B. a connected reversed-region census -> STRUCTURE (where, how big, cove)
#
# NEITHER IS PRIMARY. This module originally called Channel A primary,
# because on the first five retained cases the census was near-silent (30-32
# reversed cells in the whole window) where the probe read correctly. A later
# probe refuted that: a 45-degree-flap design read near_wall_reversed_frac
# 0.0000 on element 2 and 0.0609 on element 1 -- i.e. Channel A saw
# essentially nothing -- while the census found a SINGLE 7521-cell region,
# 0.19 c^2, 1.30 chords long, reattaches=False, anchored on the MAIN
# element's suction side. The flow was massively separated and the near-wall
# probe missed it, because that separation is off-body in extent and sits on
# a different element than the one being read.
#
# So the two channels fail in opposite directions and both must be consulted:
# the probe catches confluence collapses hugging a flap; the census catches
# large off-body separations the probe walks straight past.
#
# AND: A ZERO FROM CHANNEL A IS NOT "ATTACHED". On an engine with no wall
# shear -- which is every Fluent run, see fluent2d_run -- a 0.0000 is an
# unmeasured case, not a measured attachment. The probe already reads 0.0000
# on a design whose preserved wall-shear fraction was 0.217. Treating that
# zero as attachment is the mistake this comment exists to prevent; it was
# made, in analysis, and it invalidated a round of probes.
#
# CLOSURE IS A SHAPE STATEMENT, NEVER A SEVERITY STATEMENT. This is measured,
# not assumed: on the retained collapse case the dead element reattaches 13%
# of arc ahead of its trailing edge -- an enormous CLOSED bubble covering 81%
# of its upper side, at 39% reversed stations -- while a healthy element at
# 2% reversed reads "does not close" because its thin reversal runs onto the
# trailing edge. The two are orthogonal and neither may be derived from the
# other.
#
# Nothing here is graded. Field-side grading lines are a calibration job
# against paired wall-shear runs, and the shipped wall-face lines
# (SEP_ATTACHED_MAX etc.) may NOT be transplanted onto a probe that has a
# documented one-sided smearing bias.

RECIRC_CELL_C = 0.005            # analysis cell edge, in chords
RECIRC_NX_MIN = 320
RECIRC_NX_MAX = 1280
RECIRC_STATIONS_PER_CELL = 2.0   # arc stations per analysis cell
RECIRC_STATIONS_MIN = 200
RECIRC_STATIONS_MAX = 2000
RECIRC_PROBE_CELLS = (1, 2, 3, 4)     # wall-offset ladder, in cells
RECIRC_HEADLINE_CELLS = 2             # calibration item C1
RECIRC_RUN_MIN_CELLS = 2         # distinct sampled cells for a run to list
RECIRC_CLOSE_CELLS = 3           # distinct forward cells to call closure
RECIRC_TAIL_UNREAD_CELLS = 2     # unreadable arc before the TE voids closure
RECIRC_MIN_REGION_CELLS = 6      # region LIST floor; never gates a scalar
RECIRC_STATION_UNREAD_MAX = 0.20      # per-element data-sufficiency floor
RECIRC_LE_SPLIT_BAND = (0.20, 0.80)   # valid arc position of the min-x point

_RECIRC_NOTE = (
    "Near-wall reversal thinner than about {res:.1f} mm off the surface is "
    "below this sensor: the field is linearly interpolated from the solver's "
    "cell centres onto a {dx:.2f} mm grid, so a reversed film inside the "
    "inflation layer is smeared away. Readings are systematically LOW "
    "against a wall-shear face fraction, and the bias is largest where the "
    "reversal is thinnest. The interpolation hull ends {fy:.1f} mm above the "
    "moving ground ({fyh:.0%} of the ride height), so reversal below that "
    "height is not seen. A closure call is a shape statement, not a severity "
    "statement: on the retained record an element with 2% reversed stations "
    "reads 'does not close' (its reversal runs onto the trailing edge) while "
    "an element with 39% reversed stations reads 'closes' (an 81%-of-arc "
    "bubble that reattaches 13% of arc ahead of the trailing edge). Nothing "
    "here is graded.")


def _r(v, n):
    """Round for byte-identical re-serialisation; pass None through."""
    return None if v is None else round(float(v), n)


def _geom_invariants_match(a: StackConfig, b: StackConfig) -> bool:
    """Do two configs describe the same installed geometry?

    Compared on invariants, NOT on vertex arrays: a different
    n_panels_per_side is the same wing and must not read as a mismatch.

    The length tolerance is measured, not guessed. Across n_panels_per_side
    70 vs 120 on the retained three-element stack the polygonal
    discretization moves the trailing edge by at most 0.003 %c, the
    bounding box by 1.5e-5 m and the contour length by 0.011 %c; the
    smallest genuine design change checked (one element's deflection 27 ->
    12 deg) moves its trailing edge 5.5 %c. 0.05 %c sits ~16x above the
    discretization noise and ~100x below the real change."""
    if len(a.elements) != len(b.elements):
        return False
    for f in ("chord_mm", "ride_height_mm", "stack_aoa_deg", "speed_ms"):
        if abs(float(getattr(a, f)) - float(getattr(b, f))) > 1e-6:
            return False
    tol = 5e-4 * min(a.chord_m, b.chord_m)
    pa, pb = _installed_polys_m(a), _installed_polys_m(b)
    for qa, qb in zip(pa, pb):
        if np.max(np.abs(np.asarray(geometry.te_point(qa))
                         - np.asarray(geometry.te_point(qb)))) > tol:
            return False
        for k in range(2):
            if (abs(qa[:, k].min() - qb[:, k].min()) > tol
                    or abs(qa[:, k].max() - qb[:, k].max()) > tol):
                return False
        la = float(np.hypot(*np.diff(np.vstack([qa, qa[:1]]), axis=0).T).sum())
        lb = float(np.hypot(*np.diff(np.vstack([qb, qb[:1]]), axis=0).T).sum())
        if abs(la - lb) > tol:
            return False
    return True


def _recirc_unknown(reason: str, **extra) -> dict:
    """The shape returned whenever the case is readable but the measurement
    is not. Never None -- the caller has to be able to SHOW the reason."""
    out = {"status": "unknown", "unknown_reason": reason,
           "iteration": None, "engine_has_wall_shear": False,
           "geometry_verified": None, "bound": True,
           "bound_reason": "no measurement", "n_elements": None,
           "grid": None, "reversed_area_frac": None, "reversed_area_c2": None,
           "reversed_area_is_bound": False, "n_reversed_cells": None,
           "n_regions": None, "n_regions_below_floor": None,
           "elements": None, "regions": None, "confluence_pairs": [],
           "wall_agreement": None, "resolution_note": None, "caveats": [],
           "verdict": None,
           "verdict_basis": "ungraded in v1: field-side lines require a "
                            "regression against paired wall-shear runs"}
    out.update(extra)
    return out


def _arc_stations(poly: np.ndarray, dx: float) -> dict:
    """Equal-arc stations around one closed element contour, with outward
    normals and a geometric leading-edge side split.

    Equal spacing is the point: an unweighted mean over equally spaced
    stations IS an arc-weighted fraction, which is the same kind of object
    as wall_report's mean over a quasi-uniform surface mesh. The closing
    segment is the blunt manufacturing base and its stations are KEPT (the
    wall-shear field counts those faces too) and flagged."""
    loop = np.vstack([poly, poly[:1]])
    seg = np.hypot(*np.diff(loop, axis=0).T)
    s_cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(s_cum[-1])
    n_st = int(np.clip(round(RECIRC_STATIONS_PER_CELL * total / max(dx, 1e-12)),
                       RECIRC_STATIONS_MIN, RECIRC_STATIONS_MAX))
    s_t = np.linspace(0.0, total, n_st, endpoint=False)
    base = np.column_stack([np.interp(s_t, s_cum, loop[:, 0]),
                            np.interp(s_t, s_cum, loop[:, 1])])
    is_te_base = s_t >= s_cum[-2]

    t = np.roll(base, -1, 0) - np.roll(base, 1, 0)
    tn = np.hypot(*t.T)
    tn[tn < 1e-15] = 1.0
    t = t / tn[:, None]
    nrm = np.column_stack([t[:, 1], -t[:, 0]])
    from matplotlib.path import Path as MplPath
    step = 0.25 * max(dx, 1e-9)
    flip = MplPath(poly).contains_points(base + step * nrm)
    nrm[flip] *= -1.0

    i_le = int(np.argmin(base[:, 0]))
    split_frac = s_t[i_le] / total if total > 0 else 0.0
    lo, hi = RECIRC_LE_SPLIT_BAND
    degenerate = not (lo <= split_frac <= hi)

    a_idx = np.arange(i_le, -1, -1)              # LE -> TE, one way round
    b_idx = np.arange(i_le, n_st)                # LE -> TE, the other way
    sides = {}
    if not degenerate:
        for key, idx in (("A", a_idx), ("B", b_idx)):
            pts = base[idx]
            d = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
            arc = float(d[-1]) if len(d) > 1 else 0.0
            sides[key] = {"idx": idx, "s": d / arc if arc > 0 else d * 0.0,
                          "arc_m": arc, "ymean": float(pts[:, 1].mean())}
        upper = "A" if sides["A"]["ymean"] >= sides["B"]["ymean"] else "B"
        named = {"upper": sides[upper],
                 "lower": sides["B" if upper == "A" else "A"]}
    else:
        named = {}
    return {"base": base, "nrm": nrm, "n_st": n_st, "arc_m": total,
            "is_te_base": is_te_base, "sides": named,
            "degenerate": degenerate, "split_frac": float(split_frac)}


_RECIRC_CAVEATS = [
    "Thin near-wall reversal is under-read, one-sidedly, worst in the "
    "attached/partial band. The reverse disagreement (field reading MORE "
    "reversed than the wall) has no smearing explanation and should be "
    "treated as a sensor fault, not a discovery.",
    "The ground band is blind for the first grid row above the moving "
    "ground; the measured height is published per case as "
    "first_finite_y_mm rather than assumed to be 'a row or two'.",
    "Blunt manufacturing bases produce genuine standing recirculation on "
    "every element (te_mode 'thicken' is the default). That is real "
    "physics, not a defect. It is flagged, counted for wall-shear parity, "
    "and never silently excluded.",
    "8-connectivity can merge two phenomena into one region. The contact "
    "counts expose it, and a merge cannot flip a closure call because "
    "closure is measured per element-side run, not per region.",
    "gu < 0 under-reports past 90 degrees of local tangent; guarded by "
    "reporting side_split 'degenerate' rather than corrected, because the "
    "correction needs a tangent that is undefined in the confluence "
    "corridor and in the free wake.",
    "Steady RANS of a massively separated flow is a time-mean fiction; a "
    "limit-cycling run's extents carry the cycle amplitude, which is why "
    "bound defaults to True.",
    "Tight-slot attribution can flip with grid phase when the throat is a "
    "few cells wide. No synthesized confidence number is emitted; the raw "
    "contact counts are published instead.",
]


def _recirc_closes(sarc, rv, ok, iy, ix, i0, i1):
    """Does one reversed run close before the trailing edge?

    A SHAPE question, never a severity one. Ladder, first match wins:
      1. the run reaches the final station        -> False
      2. too much unreadable arc before the TE    -> None
      3. too little forward flow to be sure       -> None (knife edge)
      4. otherwise                                -> True
    Every threshold is in DISTINCT SAMPLED CELLS, not stations: three
    stations can be three independent cells on a long element and barely
    one on a short one, which would make the same rule three times more
    permissive exactly on the small shadowed elements it exists to judge."""
    n = len(sarc)
    if i1 >= n - 1:
        return False, "reversed_at_trailing_edge", False, None
    tail = slice(i1 + 1, n)
    bad = ~ok[tail]
    if bad.any():                       # longest unreadable stretch, in cells
        runlen = worst = 0
        seen = set()
        for j in range(i1 + 1, n):
            if not ok[j]:
                seen.add((int(iy[j]), int(ix[j])))
                runlen = len(seen)
                worst = max(worst, runlen)
            else:
                seen, runlen = set(), 0
        if worst > RECIRC_TAIL_UNREAD_CELLS:
            return None, "unmeasured_arc_before_te", False, None
    fwd = set()
    for j in range(i1 + 1, n):
        if rv[j]:
            break
        if ok[j]:
            fwd.add((int(iy[j]), int(ix[j])))
    if len(fwd) < RECIRC_CLOSE_CELLS:
        return None, "knife_edge_at_te", True, None
    return True, "reattaches_before_te", False, float(sarc[i1 + 1])


def _recirc_runs(sarc, rv, ok, iy, ix, lab, is_te_base, pts, h, dx, dy):
    """Maximal contiguous reversed runs along one side, LE -> TE."""
    out, i, n = [], 0, len(sarc)
    while i < n:
        if not rv[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and rv[j + 1]:
            j += 1
        cells = {(int(iy[t]), int(ix[t])) for t in range(i, j + 1)}
        if len(cells) >= RECIRC_RUN_MIN_CELLS:
            closes, basis, knife, r_s = _recirc_closes(
                sarc, rv, ok, iy, ix, i, j)
            rids = sorted({int(lab[iy[t], ix[t]]) for t in range(i, j + 1)}
                          - {0})
            out.append({
                "id": len(out),
                "s_start": _r(sarc[i], 4), "s_end": _r(sarc[j], 4),
                "arc_frac": _r(sarc[j] - sarc[i], 4),
                "x_start_m": _r(pts[i, 0], 5), "x_end_m": _r(pts[j, 0], 5),
                "n_stations": int(j - i + 1),
                "n_distinct_cells": len(cells),
                "te_base_only": bool(is_te_base[i:j + 1].all()),
                "region_ids": rids,
                "closes": closes, "basis": basis, "knife_edge": knife,
                "reattach_s": _r(r_s, 4),
                "reversed_downstream_of_run": bool(rv[j + 1:].any()),
                "closure_by_offset": {},
                "_i0": i, "_i1": j,
            })
        i = j + 1
    return out


def _recirc_regions(keep, lab, sizes, gx, gy, ndimage, contacts, elements,
                    zones, pairs_out, cell_area, c_m, ny, nx_a, row0):
    """Region census. Closure is COMPOSED from the element-side runs that
    touch a region, never decided from one 'owner' element -- a region
    bridging a slot would otherwise be judged against a coin-flip owner's
    trailing edge."""
    slices = ndimage.find_objects(lab)
    by_region = {}
    for e in elements:
        for side_name, side in (e.get("sides") or {}).items():
            for r_ in side.get("runs", []):
                for rid in r_["region_ids"]:
                    by_region.setdefault(rid, []).append(
                        {"element": e["index"], "side": side_name,
                         "closes": r_["closes"], "basis": r_["basis"],
                         "s_start": r_["s_start"], "s_end": r_["s_end"]})
    out = []
    for rid in keep:
        sl = slices[rid - 1]
        sub = lab[sl] == rid
        xs, ys = gx[sl][sub], gy[sl][sub]
        n_cells = int(sizes[rid])
        x0r, x1r = float(xs.min()), float(xs.max())
        y0r, y1r = float(ys.min()), float(ys.max())
        length_c = (x1r - x0r) / c_m
        area_c2 = n_cells * cell_area / c_m ** 2
        cont = contacts.get(rid, {})
        closures = by_region.get(rid, [])
        if not closures:
            # a free-wake region has no reattachment question at all; a
            # region that DOES touch a surface but carries no listed run
            # (its stations never spanned the minimum distinct cells) is a
            # different, and honestly different, kind of unknown
            reatt = None
            basis = "no_listed_run" if cont else "no_wall_contact"
        elif any(c["closes"] is False for c in closures):
            reatt, basis = False, "reversed_at_trailing_edge"
        elif any(c["closes"] is None for c in closures):
            reatt, basis = None, "unmeasured_or_knife_edge"
        else:
            reatt, basis = True, "reattaches_before_te"
        share, pair = 0.0, None
        for z, p in zip(zones, pairs_out):
            s_ = float((z[sl] & sub).sum()) / max(n_cells, 1)
            if s_ > share:
                share, pair = s_, p["pair"]
        rows = np.flatnonzero(sub.any(axis=1))
        cols = np.flatnonzero(sub.any(axis=0))
        r_off = sl[0].start
        c_off = sl[1].start
        out.append({
            "id": rid, "n_cells": n_cells,
            "area_c2": _r(area_c2, 6),
            "x_start_m": _r(x0r, 5), "x_end_m": _r(x1r, 5),
            "y_min_m": _r(y0r, 5), "y_max_m": _r(y1r, 5),
            "length_c": _r(length_c, 5),
            "height_c": _r((y1r - y0r) / c_m, 5),
            "thickness_mean_c": _r(area_c2 / length_c if length_c > 1e-9
                                   else None, 5),
            "centroid_c": [_r(float(xs.mean()) / c_m, 5),
                           _r(float(ys.mean()) / c_m, 5)],
            "elements_touched": sorted(cont),
            "spans_elements": len(cont) > 1,
            "contact_stations_by_element": {str(k): int(v)
                                            for k, v in sorted(cont.items())},
            "owner": (max(cont, key=lambda k: cont[k]) if cont else None),
            "kind": "surface" if cont else "wake",
            "reattaches": reatt, "reattach_basis": basis,
            "reattaches_anywhere": (any(c["closes"] is True
                                        for c in closures)
                                    if closures else None),
            "run_closures": closures,
            "in_confluence": bool(share > 0.0),
            "confluence_pair": pair,
            "confluence_share": _r(share, 4),
            "truncated": {
                "downstream": bool((c_off + cols.max()) >= nx_a - 1),
                "upstream": bool((c_off + cols.min()) <= 0),
                "top": bool((r_off + rows.max()) >= ny - 1),
            },
            "touches_first_finite_row": bool((r_off + rows.min()) <= row0),
        })
    out.sort(key=lambda r: (-r["n_cells"], r["x_start_m"], r["y_min_m"],
                            r["id"]))
    return out


def recirculation_report(case_dir: Path, cfg: StackConfig, *,
                         converged: bool | None = None,
                         user_stopped: bool = False,
                         wall: dict | None = None,
                         cell_c: float | None = None) -> dict | None:
    """Reversed-flow magnitude and structure from the solved field.

    Returns None ONLY when the case carries no readable field at all (the
    same contract as wall_report). Every other failure returns a dict with
    status="unknown" and a reason the caller can display.

    converged / user_stopped come from the runner, which owns force-history
    state; this function never re-derives them, because a hand stop is
    invisible in the written fields. Omitted means bound=True.

    Costs one LinearNDInterpolator build over the cell centres, about the
    same as one flow_png. Call it once at result assembly, never per poll."""
    with _render_lock:
        return _recirculation_report_locked(
            case_dir, cfg, converged=converged, user_stopped=user_stopped,
            wall=wall, cell_c=cell_c)


def _recirculation_report_locked(case_dir: Path, cfg: StackConfig, *,
                                 converged: bool | None = None,
                                 user_stopped: bool = False,
                                 wall: dict | None = None,
                                 cell_c: float | None = None) -> dict | None:
    import json
    from scipy import ndimage
    from scipy.spatial import cKDTree
    # the arc window the shipped screen already evaluates on; reused so the
    # two channels talk about the same stretch of surface
    from .wake_shadow import ARC_HI as _ARC_HI, ARC_LO as _ARC_LO

    case_dir = Path(case_dir)
    try:
        latest_time_dir(case_dir)          # the only None path
    except PostError:
        return None

    bound = not (converged is True and not user_stopped)
    bound_reason = None
    if bound:
        bound_reason = ("run stopped by hand — extents are a bound"
                        if user_stopped else
                        "force history not converged — a steady RANS mean of "
                        "a separated flow carries the cycle amplitude, so "
                        "every extent here is a bound, not a result"
                        if converged is False else
                        "convergence state not supplied by the caller")

    # geometry provenance: invariants, never vertex arrays (a different
    # n_panels_per_side is the SAME wing and must not read as a mismatch)
    geometry_verified = None
    cfgj = case_dir / "config.json"
    if cfgj.is_file():
        try:
            cfg_case = StackConfig.from_dict(
                json.loads(cfgj.read_text(errors="replace")))
            geometry_verified = _geom_invariants_match(cfg, cfg_case)
        except (OSError, ValueError, KeyError, TypeError):
            geometry_verified = None       # unreadable record is not mismatch
    if geometry_verified is False:
        return _recirc_unknown(
            "the configuration supplied does not describe this case",
            geometry_verified=False)

    try:
        polys_pre = _installed_polys_m(cfg)
        x0p, x1p, _, _ = _section_window(np.vstack(polys_pre), cfg.chord_m)
        cell = (cell_c or RECIRC_CELL_C) * cfg.chord_m
        nx_want = int(round((x1p - x0p) / max(cell, 1e-12)))
        nx = int(np.clip(nx_want, RECIRC_NX_MIN, RECIRC_NX_MAX))
        g = _field_grid(case_dir, cfg, "umag", nx, "section")
    except PostError as e:
        return _recirc_unknown(str(e), geometry_verified=geometry_verified)
    except (OSError, ValueError, KeyError) as e:
        return _recirc_unknown(f"{type(e).__name__}: {e}",
                               geometry_verified=geometry_verified)

    gx, gy, gu, gv = g["gx"], g["gy"], g["gu"], g["gv"]
    polys = g["polys"]
    ny, nx_a = gu.shape
    dx = float(gx[0, 1] - gx[0, 0])
    dy = float(gy[1, 0] - gy[0, 0])
    h = max(dx, dy)
    cell_area = dx * dy
    c_m = cfg.chord_m

    # reversed mask. gu < 0 strictly, no deadband, no tangent projection:
    # the sign agrees with a surface tangent for every |theta| < 90 deg, and
    # the projection is undefined exactly in the confluence corridor and in
    # the free wake. NOTE the sign conventions differ from wall_report and
    # say the same thing: OpenFOAM's wall shear points along the near-wall
    # flow, so attached reads tau_x < 0 there, while the field reads
    # reversed as gu < 0 here.
    fin = np.isfinite(gu) & np.isfinite(gv)
    rev = np.zeros(fin.shape, bool)
    np.less(gu, 0.0, out=rev, where=fin)
    n_fin = int(fin.sum())
    n_rev = int(rev.sum())

    row0 = int(np.argmax(fin.any(axis=1))) if fin.any() else 0
    first_y = float(gy[row0, 0])
    ride_m = max(cfg.ride_height_c * c_m, 1e-9)

    edge_touch = bool(rev[0, :].any() or rev[-1, :].any()
                      or rev[:, 0].any() or rev[:, -1].any())

    grid_block = {
        "nx": int(nx_a), "ny": int(ny),
        "dx_mm": _r(dx * 1e3, 3), "dy_mm": _r(dy * 1e3, 3),
        "cell_c": _r(dx / c_m, 6),
        "x0_m": _r(g["x0"], 5), "x1_m": _r(g["x1"], 5),
        "y0_m": _r(g["y0"], 5), "y1_m": _r(g["y1"], 5),
        "valid_frac": _r(n_fin / fin.size, 4),
        "anisotropic": not (0.8 <= dy / dx <= 1.25),
        "nx_clamped": nx != nx_want,
        "first_finite_y_mm": _r(first_y * 1e3, 2),
        "first_finite_y_over_h": _r(first_y / ride_m, 4),
        "min_resolvable_mm": _r(h * RECIRC_RUN_MIN_CELLS * 1e3, 2),
    }

    # ---- Channel B: connected reversed regions -------------------------
    # No morphology. Opening erases the 1-2 cell near-wall sheets that ARE
    # the signal; closing is extensive and desynchronises region area from
    # the unfiltered scalar. 8-connectivity prefers merging over splitting
    # (a diagonal pinch is grid phase); merges are disclosed, and since
    # closure is measured per element-side run a merge can never flip it.
    lab, n_lab = ndimage.label(rev, structure=np.ones((3, 3), int))
    sizes = np.bincount(lab.ravel(), minlength=n_lab + 1)
    sizes[0] = 0
    keep = [int(i) for i in np.flatnonzero(sizes >= RECIRC_MIN_REGION_CELLS)]
    below = int((sizes[1:] > 0).sum() - len(keep))

    # ---- stations, probe ladder, attribution ---------------------------
    st = [_arc_stations(p, dx) for p in polys]
    all_pts, all_owner = [], []
    for k, s in enumerate(st):
        all_pts.append(s["base"])
        all_owner.append(np.full(s["n_st"], k))
    tree = cKDTree(np.vstack(all_pts))
    owner_flat = np.concatenate(all_owner)
    _, near_i = tree.query(np.column_stack([gx.ravel(), gy.ravel()]))
    owner_elem = owner_flat[near_i].reshape(gx.shape)

    def probe(pts, nrm, m):
        p = pts + m * h * nrm
        ix = np.clip(np.rint((p[:, 0] - g["x0"]) / dx).astype(int), 0, nx_a - 1)
        iy = np.clip(np.rint((p[:, 1] - g["y0"]) / dy).astype(int), 0, ny - 1)
        u = gu[iy, ix]
        ok = np.isfinite(u)
        return iy, ix, u, ok, (u < 0) & ok

    region_contacts = {i: {} for i in keep}
    elements = []
    for k, s in enumerate(st):
        base, nrm, n_st = s["base"], s["nrm"], s["n_st"]
        ladder, unread, ncells = {}, {}, {}
        for m in RECIRC_PROBE_CELLS:
            iy, ix, _u, ok, rv = probe(base, nrm, m)
            ladder[str(m)] = _r(rv[ok].mean() if ok.any() else None, 4)
            unread[m] = int((~ok).sum())
            ncells[m] = len({(int(a), int(b)) for a, b in zip(iy, ix)})
            if m == 1:
                for a, b, is_rev in zip(iy, ix, rv):
                    if is_rev:
                        lv = int(lab[a, b])
                        if lv in region_contacts:
                            region_contacts[lv][k] = \
                                region_contacts[lv].get(k, 0) + 1
        hm = RECIRC_HEADLINE_CELLS
        measurable = unread[hm] / max(n_st, 1) <= RECIRC_STATION_UNREAD_MAX
        floor = 1.0 / max(ncells[hm], 1)
        head = ladder[str(hm)] if measurable else None

        sides_out = None
        if s["sides"] and measurable:
            sides_out = {}
            for name, side in s["sides"].items():
                idx, sarc = side["idx"], side["s"]
                sl = {}
                for m in RECIRC_PROBE_CELLS:
                    _iy, _ix, _u, ok, rv = probe(base[idx], nrm[idx], m)
                    sl[str(m)] = _r(rv[ok].mean() if ok.any() else None, 4)
                iy, ix, _u, ok, rv = probe(base[idx], nrm[idx], hm)
                runs = _recirc_runs(sarc, rv, ok, iy, ix, lab,
                                    s["is_te_base"][idx], base[idx],
                                    h, dx, dy)
                for m in RECIRC_PROBE_CELLS:
                    _iy2, _ix2, _u2, ok2, rv2 = probe(base[idx], nrm[idx], m)
                    for r_ in runs:
                        r_["closure_by_offset"][str(m)] = _recirc_closes(
                            sarc, rv2, ok2, _iy2, _ix2,
                            r_["_i0"], r_["_i1"])[0]
                for r_ in runs:
                    r_.pop("_i0", None)
                    r_.pop("_i1", None)
                ratios = {}
                for m in (8, 16):
                    _iy3, _ix3, u3, ok3, _rv3 = probe(base[idx], nrm[idx], m)
                    w = ok3 & (sarc >= _ARC_LO) & (sarc <= _ARC_HI)
                    ratios[str(m)] = _r(
                        float(np.median(u3[w])) / cfg.speed_ms
                        if w.any() and cfg.speed_ms > 1e-9 else None, 4)
                sides_out[name] = {
                    "arc_len_mm": _r(side["arc_m"] * 1e3, 2),
                    "n_stations": int(len(idx)),
                    "reversed_frac_ladder": sl,
                    "reversed_frac": sl[str(hm)],
                    "attached_at_te": bool(ok[-1] and not rv[-1]),
                    "n_runs": len(runs),
                    "runs": runs,
                    "stream_u_ratio": ratios,
                }

        wsep = ((wall or {}).get("separation") or {})
        patch = f"wing_e{k + 1}"
        wfrac = (wsep.get(patch) or {}).get("reversed_frac")
        elements.append({
            "index": k, "patch": patch,
            "measurable": bool(measurable),
            "unmeasured_reason": (None if measurable else
                                  f"{unread[hm]} of {n_st} probe stations "
                                  f"fall outside the readable field"),
            "arc_len_mm": _r(s["arc_m"] * 1e3, 2),
            "n_stations": int(n_st),
            "n_stations_unreadable": int(unread[hm]),
            "n_distinct_cells": int(ncells[hm]),
            "side_split": "degenerate" if s["degenerate"] else "geometric_le",
            "near_wall_reversed_frac": head,
            "near_wall_reversed_frac_ladder": ladder if measurable else None,
            "detection_floor_frac": _r(floor, 5),
            "below_detection_floor": bool(head == 0.0),
            "reversed_frac_note": (
                f"no reversal at any station; reversal thinner than about "
                f"{h * 1e3:.1f} mm off the wall is below this sensor"
                if head == 0.0 else None),
            "te_base_station_frac": _r(s["is_te_base"].mean(), 4),
            "sides": sides_out,
            "wall_reversed_frac": wfrac,
            "delta_vs_wall": (_r(head - wfrac, 4)
                              if head is not None and wfrac is not None
                              else None),
            "agrees": None,      # needs grading lines the caller owns
            "field_grade": None,  # v1: never graded
        })

    # ---- confluence corridors ------------------------------------------
    te_x = [float(geometry.te_point(p)[0]) for p in polys]
    zones, pairs_out = [], []
    for k in range(len(polys) - 1):
        z = ((gx >= te_x[k] - dx) & (gx <= te_x[k + 1])
             & (owner_elem == k + 1))
        zones.append(z)
        pairs_out.append({"pair": [k, k + 1],
                          "te_x_m": [_r(te_x[k], 5), _r(te_x[k + 1], 5)],
                          "zone_cells": int(z.sum())})

    regions = _recirc_regions(keep, lab, sizes, gx, gy, ndimage,
                              region_contacts, elements, zones, pairs_out,
                              cell_area, c_m, ny, nx_a, row0)

    wall_agreement = None
    if wall and (wall.get("separation") or {}):
        per = [{"patch": e["patch"], "field_frac": e["near_wall_reversed_frac"],
                "wall_frac": e["wall_reversed_frac"], "delta": e["delta_vs_wall"],
                "agrees": None} for e in elements]
        deltas = [abs(p["delta"]) for p in per if p["delta"] is not None]
        wall_agreement = {
            "available": True, "wall_time": wall.get("time"),
            "field_time": g["tdir"].name,
            "same_time": bool(wall.get("time") == g["tdir"].name),
            "per_patch": per,
            "worst_delta": _r(max(deltas), 4) if deltas else None,
            "any_disagreement": None,
            "reason": "no grading lines supplied — foam_post reports the "
                      "delta; the class comparison belongs to the caller",
        }

    return {
        "status": "measured", "unknown_reason": None,
        "iteration": g["tdir"].name,
        "engine_has_wall_shear": bool(wall and wall.get("separation")),
        "geometry_verified": geometry_verified,
        "bound": bool(bound), "bound_reason": bound_reason,
        "chord_mm": _r(cfg.chord_mm, 4), "speed_ms": _r(cfg.speed_ms, 4),
        "n_elements": len(polys),
        "grid": grid_block,
        "reversed_area_frac": _r(n_rev / n_fin if n_fin else None, 5),
        "reversed_area_c2": _r(n_rev * cell_area / c_m ** 2, 6),
        "reversed_area_is_bound": edge_touch,
        "n_reversed_cells": n_rev,
        "n_regions": len(keep), "n_regions_below_floor": below,
        "elements": elements, "regions": regions,
        "confluence_pairs": pairs_out,
        "wall_agreement": wall_agreement,
        "resolution_note": _RECIRC_NOTE.format(
            res=h * RECIRC_RUN_MIN_CELLS * 1e3, dx=dx * 1e3,
            fy=first_y * 1e3, fyh=first_y / ride_m),
        "caveats": _RECIRC_CAVEATS,
        "verdict": None,
        "verdict_basis": "ungraded in v1: field-side lines require a "
                         "regression against paired wall-shear runs; the "
                         "wall-face lines may not be transplanted onto a "
                         "probe with a one-sided smearing bias",
    }


def latest_time_dir(case_dir: Path) -> Path:
    """The newest solved time directory that carries U and cell centres."""
    best, best_t = None, -1.0
    for d in Path(case_dir).iterdir():
        if not d.is_dir():
            continue
        try:
            t = float(d.name)
        except ValueError:
            continue
        if t > best_t and (d / "U").is_file() and (d / "C").is_file():
            best, best_t = d, t
    if best is None:
        raise PostError("no time directory with both U and cell centres "
                        "(C) — the case carries no readable flow field "
                        "(for an OpenFOAM case, check that run.sh's "
                        "postProcess step ran)")
    return best


# ---------- rendering ----------

def _installed_polys_m(cfg: StackConfig) -> list[np.ndarray]:
    design = geometry.build_stack(cfg)
    installed = geometry.install_stack(design, cfg.ride_height_c)
    return [np.asarray(e["coords"], float) * cfg.chord_m for e in installed]


def _section_window(all_pts: np.ndarray, c: float) -> tuple:
    """The working-view box around the installed section, in metres.

    ONE definition, shared by the grid and by anything that has to reason
    about how much room the window leaves. The downstream margin is what
    makes a reattachment question answerable: x1 is 1.10 chords past the
    aft-most point of any element, and every element's trailing edge is at
    or ahead of that point, so no trailing edge is ever within 1.10 chords
    of the downstream edge. A run that reaches the trailing edge is
    therefore measured on interior data; only a region's downstream EXTENT
    can be truncated, and that is reported as a bound."""
    return (float(all_pts[:, 0].min() - 0.45 * c),
            float(all_pts[:, 0].max() + 1.10 * c),
            0.0,
            float(max(all_pts[:, 1].max() + 0.55 * c, 0.85 * c)))


# figure chrome + defaults per app theme: the dark set is the studio's
# original look; the light set sits on the drafting theme without a
# heavy dark slab, and defaults to the rainbow map Fluent users read
# natively. The colormap, range and streamlines are per-request
# settings on top (the "viewing settings you'd have manually").
_THEMES = {
    "dark": {"bg": "#0c111c", "panel": "#232c40", "edge": "#93a3c2",
             "ink": "#c6d0e2", "tick": "#7d8aa5", "spine": "#2a3550",
             "stream_umag": "#ffffff", "stream_cp": "#41506e",
             "cmap_umag": "magma", "cmap_cp": "RdBu_r"},
    "light": {"bg": "#ffffff", "panel": "#dfe4ee", "edge": "#46536f",
              "ink": "#242b3a", "tick": "#5a6478", "spine": "#c3cad8",
              "stream_umag": "#1c2434", "stream_cp": "#46536f",
              "cmap_umag": "turbo", "cmap_cp": "coolwarm"},
}
FLOW_CMAPS = ("magma", "viridis", "turbo", "coolwarm", "RdBu_r")


def flow_png(case_dir: Path, cfg: StackConfig, field: str = "umag",
             theme: str = "dark", cmap: str | None = None,
             vmin: float | None = None, vmax: float | None = None,
             streamlines: bool = True, extent: str = "section") -> bytes:
    """Render the solved section flow as a PNG (bytes).

    field: "umag" (velocity magnitude) or "cp". theme selects the figure
    chrome + default colormap ("dark" = the studio's original look);
    cmap/vmin/vmax/streamlines override the defaults the way the manual
    GUI's contour dialog would."""
    if field not in ("umag", "cp"):
        raise PostError("field must be 'umag' or 'cp'")
    if theme not in _THEMES:
        raise PostError("theme must be 'dark' or 'light'")
    if cmap is not None and cmap not in FLOW_CMAPS:
        raise PostError(f"cmap must be one of {FLOW_CMAPS}")
    if (vmin is not None and vmax is not None and not vmax > vmin):
        raise PostError("vmax must exceed vmin")
    if extent not in ("section", "domain"):
        raise PostError("extent must be 'section' or 'domain'")
    with _render_lock:
        return _flow_png_locked(case_dir, cfg, field, theme, cmap,
                                vmin, vmax, streamlines, extent)


def _field_grid(case_dir: Path, cfg: StackConfig, field: str,
                nx: int, extent: str = "section",
                window: tuple | None = None,
                with_cp: bool = False) -> dict:
    """The solved field interpolated onto a regular grid with element
    interiors masked — ONE code path shared by the PNG renderer and the
    animated-flow JSON endpoint, so the two views can never disagree
    about the flow they show. extent="section" windows in around the
    installed section (the working view); "domain" spans the full solve
    domain. window=(x0, y0, x1, y1) overrides both with an arbitrary
    box (clamped to the solved cloud) — the animated view's
    level-of-detail refetch grids just the zoomed-in region at full
    resolution, so zooming never runs out of pixels. with_cp adds the
    Cp field on the same grid as a fourth interpolation column, so the
    animated view's pressure backdrop never pays a second file parse
    or Delaunay rebuild."""
    from matplotlib.path import Path as MplPath
    from scipy.interpolate import LinearNDInterpolator

    tdir = latest_time_dir(case_dir)
    cxy = parse_internal_field((tdir / "C").read_text(errors="replace"))[:, :2]
    u = parse_internal_field((tdir / "U").read_text(errors="replace"))[:, :2]
    cp = None
    if field == "cp" or with_cp:
        p = parse_internal_field((tdir / "p").read_text(errors="replace"))
        cp = p / (0.5 * cfg.speed_ms ** 2)
    scalar = cp if field == "cp" else np.hypot(u[:, 0], u[:, 1])

    # a spiky or hand-stopped solve can carry non-finite cells; a few are
    # droppable, a field full of them is a diverged case, not a picture
    finite = (np.isfinite(scalar) & np.isfinite(u).all(axis=1)
              & np.isfinite(cxy).all(axis=1))
    if cp is not None:
        finite &= np.isfinite(cp)
    n_bad = int((~finite).sum())
    if n_bad:
        if n_bad > 0.005 * finite.size:
            raise PostError(
                f"{n_bad} of {finite.size} cells are non-finite — the solve "
                f"diverged; there is no flow field to draw")
        cxy, u, scalar = cxy[finite], u[finite], scalar[finite]
        if cp is not None:
            cp = cp[finite]

    polys = _installed_polys_m(cfg)
    all_pts = np.vstack(polys)
    c = cfg.chord_m
    if window is not None:
        wx0, wy0, wx1, wy1 = (float(v) for v in window)
        cx0, cx1 = float(cxy[:, 0].min()), float(cxy[:, 0].max())
        cy0, cy1 = float(cxy[:, 1].min()), float(cxy[:, 1].max())
        x0, x1 = max(wx0, cx0), min(wx1, cx1)
        y0, y1 = max(wy0, cy0), min(wy1, cy1)
        if not (x1 - x0 > 1e-6 and y1 - y0 > 1e-6):
            raise PostError("window lies outside the solved domain")
    elif extent == "domain":
        x0 = float(cxy[:, 0].min())
        x1 = float(cxy[:, 0].max())
        y0 = max(float(cxy[:, 1].min()), 0.0) \
            if cxy[:, 1].min() > -1e-6 else float(cxy[:, 1].min())
        y1 = float(cxy[:, 1].max())
    else:
        x0, x1, y0, y1 = _section_window(all_pts, c)

    # ny tracks the window aspect but the total pixel budget stays fixed:
    # a sliver window after the cloud clamp (1 mm wide by the full domain
    # height) must never allocate a multi-GB grid under the render lock
    ny = min(max(int(nx * (y1 - y0) / (x1 - x0)), 160 * nx // GRID_NX),
             4 * GRID_NX)
    gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y0, y1, ny))
    # one triangulation + one simplex search for all three fields — three
    # separate griddata calls each rebuilt the Delaunay of ~100k centres
    # and tripled the render time under the lock
    cols = [scalar, u[:, 0], u[:, 1]]
    if with_cp:
        cols.append(cp)
    interp = LinearNDInterpolator(cxy, np.column_stack(cols))
    stacked = interp(np.column_stack([gx.ravel(), gy.ravel()]))
    gs = stacked[:, 0].reshape(gx.shape)
    gu = stacked[:, 1].reshape(gx.shape)
    gv = stacked[:, 2].reshape(gx.shape)
    gcp = stacked[:, 3].reshape(gx.shape) if with_cp else None

    # blank the element interiors — griddata happily interpolates across them
    flat = np.column_stack([gx.ravel(), gy.ravel()])
    # keep the PER-ELEMENT masks, not just their union: the recirculation
    # report has to attribute cells to elements, and it must do it with the
    # very masks the render blanked with rather than recomputing a ~100k
    # point-in-polygon test per element in another module
    inside_each = [MplPath(poly).contains_points(flat).reshape(gx.shape)
                   for poly in polys]
    inside = np.zeros(gx.shape, dtype=bool)
    for m in inside_each:
        inside |= m
    for g in ((gs, gu, gv) if gcp is None else (gs, gu, gv, gcp)):
        g[inside] = np.nan

    return {"tdir": tdir, "gx": gx, "gy": gy, "gs": gs, "gu": gu,
            "gv": gv, "gcp": gcp, "polys": polys, "inside": inside_each,
            "x0": x0, "x1": x1, "y0": y0, "y1": y1}


def flow_field_json(case_dir: Path, cfg: StackConfig,
                    nx: int = 320, extent: str = "section",
                    window: tuple | None = None,
                    include_cp: bool = False) -> dict:
    """The solved velocity field on a regular grid, JSON-ready — the
    studio's animated flow view advects particles through it client-side
    at the case's actual velocities. Same parsing, gridding and interior
    masking as the PNG renderer; masked or out-of-hull cells are null.
    Arrays are row-major flat lists, row 0 at the BOTTOM (y ascending).
    extent: "section" (working window) or "domain" (the whole box).
    include_cp adds the Cp field on the identical grid (the animated
    view's pressure backdrop) plus cp_p98, its symmetric auto-scale."""
    if extent not in ("section", "domain"):
        raise PostError("extent must be 'section' or 'domain'")
    nx = max(120, min(int(nx), GRID_NX))
    with _render_lock:
        g = _field_grid(case_dir, cfg, "umag", nx, extent, window,
                        with_cp=include_cp)
    # a window sitting entirely inside an element silhouette grids to all
    # NaN — nanpercentile would hand json.dumps(allow_nan=False) a NaN
    if not np.isfinite(g["gs"]).any():
        raise PostError("window contains no flow cells — it lies entirely "
                        "inside an element silhouette")

    def flat(a):
        return [None if not math.isfinite(x) else round(float(x), 3)
                for x in np.asarray(a, float).ravel()]

    ny, nx_actual = g["gs"].shape
    out = {
        "nx": int(nx_actual), "ny": int(ny),
        "x0": round(float(g["x0"]), 5), "x1": round(float(g["x1"]), 5),
        "y0": round(float(g["y0"]), 5), "y1": round(float(g["y1"]), 5),
        "iter": g["tdir"].name,
        "speed_ms": cfg.speed_ms, "chord_m": cfg.chord_m,
        "umag_p99": round(float(np.nanpercentile(g["gs"], 99)), 3),
        "u": flat(g["gu"]), "v": flat(g["gv"]), "umag": flat(g["gs"]),
        "polys": [np.round(p, 5).tolist() for p in g["polys"]],
    }
    if g["gcp"] is not None:
        out["cp"] = flat(g["gcp"])
        # same floor as the static Cp render's auto-range
        out["cp_p98"] = round(max(float(np.nanpercentile(
            np.abs(g["gcp"]), 98)), 0.5), 3)
    return out


def _flow_png_locked(case_dir: Path, cfg: StackConfig, field: str,
                     theme: str = "dark", cmap: str | None = None,
                     vmin: float | None = None, vmax: float | None = None,
                     streamlines: bool = True,
                     extent: str = "section") -> bytes:
    from matplotlib.figure import Figure

    g = _field_grid(case_dir, cfg, field, GRID_NX, extent)
    tdir, polys = g["tdir"], g["polys"]
    gx, gy, gs, gu, gv = g["gx"], g["gy"], g["gs"], g["gu"], g["gv"]
    x0, x1, y0, y1 = g["x0"], g["x1"], g["y0"], g["y1"]

    pal = _THEMES[theme]
    bg, panel_fill, panel_edge = pal["bg"], pal["panel"], pal["edge"]
    fig = Figure(figsize=(11.6, 11.6 * (y1 - y0) / (x1 - x0) + 0.55),
                 dpi=PNG_DPI)
    ax = fig.add_subplot()
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    use_cmap = cmap or pal["cmap_cp" if field == "cp" else "cmap_umag"]
    # each bound overrides its default independently, both fields alike
    if field == "cp":
        lim = max(float(np.nanpercentile(np.abs(gs), 98)), 0.5)
        lo = vmin if vmin is not None else -lim
        hi = vmax if vmax is not None else lim
        cb_label = "Cp"
    else:
        hi = vmax if vmax is not None else \
            float(np.nanpercentile(gs, 99))
        lo = vmin if vmin is not None else 0.0
        cb_label = "|U|  [m/s]"
    # a lone bound can invert the resolved range (vmin above the auto
    # ceiling) — refuse with a parameter message, not a matplotlib error
    if not hi > lo:
        raise PostError("vmax must exceed vmin after defaults "
                        f"(resolved range {lo:g}..{hi:g})")
    cf = ax.contourf(gx, gy, gs, levels=np.linspace(lo, hi, 41),
                     cmap=use_cmap, extend="both")
    if streamlines:
        # per-theme stream ink: white carries on the dark magma field,
        # dark slate on the light chrome and the mostly-light Cp maps
        stream_color = pal["stream_umag" if field == "umag"
                           else "stream_cp"]
        ax.streamplot(gx, gy, gu, gv, density=(2.4, 1.5),
                      color=stream_color, linewidth=0.55, arrowsize=0.7)
    # streamplot paints arrows over the blanked interiors too — the
    # silhouettes go on last so the elements always read as solid
    for poly in polys:
        ax.fill(poly[:, 0], poly[:, 1], facecolor=panel_fill,
                edgecolor=panel_edge, linewidth=1.0, zorder=5)
    ax.axhline(0.0, color=panel_edge, linewidth=1.6, zorder=6)

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.tick_params(colors=pal["tick"], labelsize=8)
    for s in ax.spines.values():
        s.set_color(pal["spine"])
    ax.set_xlabel("x  [m]", color=pal["tick"], fontsize=9)
    ax.set_ylabel("y  [m]", color=pal["tick"], fontsize=9)
    ax.set_title(f"RANS section flow — iteration {tdir.name}, "
                 f"as driven (flow left to right)",
                 color=pal["ink"], fontsize=10)
    cb = fig.colorbar(cf, ax=ax, fraction=0.035, pad=0.015)
    cb.set_label(cb_label, color=pal["ink"], fontsize=9)
    cb.ax.tick_params(colors=pal["tick"], labelsize=8)
    cb.outline.set_edgecolor(pal["spine"])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg, bbox_inches="tight")
    return buf.getvalue()
