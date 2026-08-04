"""Final-stage adjoint polish — ANSYS Fluent's own gradient-based shape
optimizer, driven as a studio job.

The polish is the last stage of the pipeline, after the optimizer's
candidates and the ANSYS 2D verification: it takes a FINISHED, converged
Fluent 2D verify run, reloads that run's solved case+data into a fresh
licensed 2D session (no re-mesh, no re-solve from scratch), and lets
Fluent's native adjoint solver + gradient-based optimizer (shape-opt)
morph the profile walls toward J = downforce - k * drag, where k is the
studio's physical exchange rate (newtons of downforce one newton of
drag is worth). This is deliberately the tool's own pipeline — the
observables, the sensitivity solve, the mesh morphing and the step
control are all Fluent's — with the studio wrapped around it for what
Fluent cannot know: FSAE legality, buildability, honest bookkeeping.

Division of labor per design iteration:
  Fluent   flow solve -> adjoint solve -> morph  (one optimize() call)
  studio   extract the morphed profile nodes, re-check the rule
           envelope's edge rules (LE radius, TE thickness) and the
           manufacturing aft-thickness floor, track the best compliant
           iterate, decide stop/continue

The design region handed to Fluent is clipped to the rule envelope
BEFORE the run starts (region_bounds), so a box violation (length,
height, ground clearance) is impossible by construction; the edge rules
— which a morph can silently violate by thinning a nose or a waist —
are re-measured on the extracted contour after every iteration
(check_polished), and the first violating iterate ends the loop with
the last compliant shape as the deliverable.

What the polish claims is bounded on purpose. The per-iteration numbers
come from re-solves ON THE MORPHED MESH; morphing degrades mesh quality,
so the result card carries them as provisional and the deliverable is
the polished PROFILES, which the standard verification chain re-meshes
and re-solves cleanly (Fluent2DJob with profiles_override). The panel
model is never consulted about a polished shape — free-form contours are
outside the parametric space its calibration lives in — so polish runs
write no harvest rows and show no panel deltas.

Same lifecycle doctrine as every Fluent engine: the job registers in
cfd_run's cross-engine registry (one solver at a time), the license is
held only while the job runs and released on every path including a
raise, cancel interrupts the optimizer and shoots the session, and a
12 h wall backstop catches a wedged session at iteration boundaries.
"""
from __future__ import annotations

import copy
import json
import math
import threading
import time
import traceback
import uuid
import weakref
from pathlib import Path

import numpy as np

from . import airfoils, cfd, cfd_run, foam_post, geometry
from .fluent2d_run import cl_chord, cd_chord
from .geometry import StackConfig
from .manufacturing import aft_min_thickness

WALL_LIMIT_S = 12 * 3600     # same backstop as the other Fluent engines

# option ranges, resolved eagerly at construction (fluent2d doctrine: an
# impossible setting is refused before a licensed process starts)
K_RANGE = (0.0, 10.0)
DESIGN_ITERS_RANGE = (1, 60)
FLOW_ITERS_RANGE = (100, 5000)
ADJOINT_ITERS_RANGE = (50, 5000)
MARGIN_PCT_RANGE = (1.0, 15.0)
SETTLE_ITERS_RANGE = (50, 2000)

DEFAULTS = {
    "drag_exchange_k": 0.25,   # the optimizer tab's default exchange rate
    "design_iters": 12,
    "flow_iters": 300,
    "adjoint_iters": 250,
    "margin_pct": 6.0,
    "settle_iters": 200,
}

# early stop: J improvement below this relative bar on two consecutive
# design iterations means the optimizer has flattened out (same
# two-consecutive-hits shape as the engines' force-stop doctrine)
EARLY_STOP_REL = 5e-4
EARLY_STOP_HITS = 2
# walked-past-the-peak guard: J this far below the best, twice in a row,
# ends the loop — the best compliant iterate is the deliverable anyway
OVERSHOOT_REL = 0.01

# the settle solve re-establishes the seed's converged state in the new
# session; a baseline Cl that lands farther than this from the seed's is
# reported on the card (the mesh state carried something)
SETTLE_TOL_REL = 0.01

# node-to-baseline attribution: a morphed wall node farther than this
# (in chords) from every baseline polyline cannot be attributed to an
# element — with the morph bounded by the design region this means the
# extraction itself went wrong, and the run must say so, not guess
NODE_MATCH_TOL_C = 0.10

# with no ground-clearance rule configured, the design region floor
# still keeps this fraction of the current clearance — the morph must
# never be offered the ground plane
GROUND_KEEP_FRAC = 0.5


# every constructed polish job, weakly: the protected-dirs provider
# below keeps a job's SEED run directory and its own case directory out
# of the run-dir pruning for as long as anything (the registry, a
# snapshot consumer) still holds the job — a pruned seed strands the
# polish provenance and a pruned polish dir strands the re-verify's
# polished_profiles.json
_active: "weakref.WeakSet" = weakref.WeakSet()


def _polish_dirs() -> list[str]:
    out: list[str] = []
    for j in list(_active):
        out.append(str(j.seed_dir))
        out.append(str(j.case_dir))
    return out


cfd_run.register_protected_dirs(_polish_dirs)


def _mcp():
    """The session-facing Fluent helpers (lazy, same seam as
    fluent2d_run so the offline suite fakes the whole engine)."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent_mcp
    return fluent_mcp


def _num(value, name, lo, hi, cast=float):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    if not math.isfinite(f):
        raise ValueError(f"{name} must be a finite number")
    if cast is int and f != int(f):
        raise ValueError(f"{name} must be a whole number")
    if f < lo or f > hi:
        raise ValueError(f"{name} must be between {lo:g} and {hi:g}")
    return cast(f)


def resolve_options(options: dict | None) -> dict:
    """The polish panel's request -> the concrete values one run uses,
    every range checked here (a bad option is refused before the seed
    case, the license and the session)."""
    o = dict(options or {})

    def pick(key):
        v = o.get(key)
        return DEFAULTS[key] if v is None else v

    return {
        "drag_exchange_k": _num(pick("drag_exchange_k"),
                                "drag_exchange_k", *K_RANGE),
        "design_iters": _num(pick("design_iters"), "design_iters",
                             *DESIGN_ITERS_RANGE, int),
        "flow_iters": _num(pick("flow_iters"), "flow_iters",
                           *FLOW_ITERS_RANGE, int),
        "adjoint_iters": _num(pick("adjoint_iters"), "adjoint_iters",
                              *ADJOINT_ITERS_RANGE, int),
        "margin_pct": _num(pick("margin_pct"), "margin_pct",
                           *MARGIN_PCT_RANGE),
        "settle_iters": _num(pick("settle_iters"), "settle_iters",
                             *SETTLE_ITERS_RANGE, int),
    }


def region_bounds(polys_m: list[np.ndarray], cfg: StackConfig,
                  margin_pct: float) -> dict:
    """The cartesian design region for the morph, in meters: the stack's
    bounding box plus a margin, CLIPPED to the rule envelope so the box
    rules cannot be violated by construction.

    Clips applied (each recorded in "clips" so the run states them):
      - max_height_mm caps the region top (translated when the envelope
        measures at a different ride height — conservatively against the
        CURRENT bottom, since the post-check stays the authority),
      - min_ground_clearance_mm floors the region bottom; with no such
        rule the floor keeps GROUND_KEEP_FRAC of the current clearance —
        the ground plane is never offered to the morph,
      - max_length_mm caps the region span while always containing the
        current stack (a compliant seed fits by definition).
    """
    allp = np.vstack(polys_m)
    x_min, x_max = float(allp[:, 0].min()), float(allp[:, 0].max())
    y_min, y_max = float(allp[:, 1].min()), float(allp[:, 1].max())
    m = float(margin_pct) / 100.0 * cfg.chord_m
    x_lo, x_hi = x_min - m, x_max + m
    y_lo, y_hi = y_min - m, y_max + m
    clips: list[str] = []
    env = cfg.rule_envelope
    if env is not None and env.max_height_mm is not None:
        cap_m = env.max_height_mm / 1000.0
        if env.measure_ride_height_mm is not None:
            # the cap applies to (top - bottom) + measure_rh; against the
            # current bottom that bounds the running-frame top at:
            cap_m = (env.max_height_mm - env.measure_ride_height_mm
                     ) / 1000.0 + y_min
        if y_hi > cap_m:
            y_hi = cap_m
            clips.append(f"top clipped to the {env.max_height_mm:g} mm "
                         f"height cap")
    floor_m = GROUND_KEEP_FRAC * y_min
    if env is not None and env.min_ground_clearance_mm is not None:
        floor_m = max(floor_m, env.min_ground_clearance_mm / 1000.0)
        if y_lo < floor_m:
            clips.append(f"bottom clipped to the "
                         f"{env.min_ground_clearance_mm:g} mm ground-"
                         f"clearance floor")
    elif y_lo < floor_m:
        clips.append("bottom clipped to half the current ground "
                     "clearance (no clearance rule configured)")
    y_lo = max(y_lo, floor_m)
    if env is not None and env.max_length_mm is not None:
        allowed = env.max_length_mm / 1000.0
        if (x_hi - x_lo) > allowed:
            x_lo = max(x_lo, x_max - allowed)
            x_hi = min(x_hi, x_lo + allowed)
            clips.append(f"span clipped to the {env.max_length_mm:g} mm "
                         f"length cap")
    if y_hi <= y_lo or x_hi <= x_lo:
        raise ValueError("the rule envelope leaves no room for a design "
                         "region — the seed geometry already sits on the "
                         "envelope limits")
    return {"x": [round(x_lo, 6), round(x_hi, 6)],
            "y": [round(y_lo, 6), round(y_hi, 6)],
            "margin_m": round(m, 6), "clips": clips}


def split_wall_nodes(nodes_xy: np.ndarray,
                     base_polys: list[np.ndarray],
                     tol_m: float) -> tuple[list[np.ndarray], dict]:
    """Unordered morphed wall nodes -> one ordered polyline per element.

    The mesh export gives the profile zone's nodes with no connectivity
    and no element attribution. The baseline polylines (the exact
    installed contours the seed case was meshed from) provide both: each
    node is attributed to the baseline polyline of its nearest vertex,
    and ordered by that vertex index (ties broken by the projection onto
    the local segment) — valid because the morph is small against the
    baseline vertex spacing's scale, which the tol_m guard enforces: a
    node farther than tol_m from every baseline contour cannot be
    attributed and raises instead of guessing."""
    nodes = np.asarray(nodes_xy, float)
    if nodes.ndim != 2 or nodes.shape[1] != 2 or len(nodes) < 10:
        raise ValueError(f"unusable wall-node export "
                         f"(shape {getattr(nodes, 'shape', None)})")
    best_d = np.full(len(nodes), np.inf)
    best_e = np.zeros(len(nodes), int)
    best_v = np.zeros(len(nodes), int)
    for ei, poly in enumerate(base_polys):
        p = np.asarray(poly, float)
        # chunked pairwise distance: node counts are a few thousand and
        # baseline polylines a few hundred points — one outer product row
        # per element stays small
        d2 = ((nodes[:, None, :] - p[None, :, :]) ** 2).sum(axis=2)
        vi = np.argmin(d2, axis=1)
        dv = np.sqrt(d2[np.arange(len(nodes)), vi])
        take = dv < best_d
        best_d[take] = dv[take]
        best_e[take] = ei
        best_v[take] = vi[take]
    worst = float(best_d.max())
    if worst > tol_m:
        raise ValueError(
            f"a morphed wall node sits {worst * 1000:.1f} mm from every "
            f"baseline contour (limit {tol_m * 1000:.1f} mm) — the "
            f"extraction cannot attribute it to an element")
    out: list[np.ndarray] = []
    disp: list[float] = []
    for ei, poly in enumerate(base_polys):
        p = np.asarray(poly, float)
        idx = np.where(best_e == ei)[0]
        if len(idx) < 10:
            raise ValueError(
                f"element {ei + 1} attracted only {len(idx)} wall nodes "
                f"— the profile zone does not cover every element")
        vi = best_v[idx]
        # order along the baseline: by nearest vertex, ties by the
        # projection onto the vertex's forward segment
        seg = p[np.minimum(vi + 1, len(p) - 1)] - p[vi]
        t = np.einsum("ij,ij->i", nodes[idx] - p[vi], seg)
        order = np.lexsort((t, vi))
        out.append(nodes[idx][order])
        disp.extend(best_d[idx].tolist())
    stats = {"max_disp_mm": round(float(np.max(disp)) * 1000.0, 3),
             "mean_disp_mm": round(float(np.mean(disp)) * 1000.0, 3),
             "n_nodes": int(len(nodes))}
    return out, stats


def _element_roles(n: int) -> list[str]:
    return ["main"] + [f"flap{i}" for i in range(1, n)]


def check_polished(polys_m: list[np.ndarray], cfg: StackConfig) -> dict:
    """Rule and buildability verdicts for a polished (free-form) stack.

    The box rules reuse the parametric checker's exact arithmetic on the
    morphed extents. The edge rules (LE radius, TE thickness) and the
    manufacturing aft-thickness floor are re-MEASURED on the extracted
    contours themselves — envelope_check's edge rules measure the
    parametric spec, which a morph has left behind. Measurements run on
    the mesh-resolution contour (hundreds of nodes per element), no
    repaneling: the extracted polyline IS the as-polished shape.

    Verdict semantics mirror the parametric checker: an unmeasurable
    nose lands in "unverified" (an unresolved constraint, never a pass),
    and a radius within LE_RADIUS_BAND of its floor carries
    knife_edge=True beside whichever verdict it got."""
    env = cfg.rule_envelope
    chord_m = cfg.chord_m
    installed = [{"coords": np.asarray(p, float) / chord_m,
                  "role": role}
                 for p, role in zip(polys_m,
                                    _element_roles(len(polys_m)))]
    violations: list[dict] = []
    unverified: list[dict] = []
    rows: list[dict] = []
    ext = geometry.stack_extents(installed)
    mm = cfg.chord_mm
    extents = {"length_mm": round((ext["x_max"] - ext["x_min"]) * mm, 2),
               "top_mm": round(ext["y_max"] * mm, 2),
               "bottom_mm": round(ext["y_min"] * mm, 2)}
    if env is not None:
        length = (ext["x_max"] - ext["x_min"]) * mm
        top = ext["y_max"] * mm
        bottom = ext["y_min"] * mm
        if env.measure_ride_height_mm is not None:
            top = ((ext["y_max"] - ext["y_min"]) * mm
                   + env.measure_ride_height_mm)
        tol = geometry.ENVELOPE_TOL_MM
        if (env.max_length_mm is not None
                and length > env.max_length_mm + tol):
            violations.append({
                "rule": "max_length_mm", "edge": "length",
                "value_mm": round(length, 2),
                "limit_mm": env.max_length_mm,
                "by_mm": round(length - env.max_length_mm, 2)})
        if (env.max_height_mm is not None
                and top > env.max_height_mm + tol):
            violations.append({
                "rule": "max_height_mm", "edge": "top",
                "value_mm": round(top, 2), "limit_mm": env.max_height_mm,
                "by_mm": round(top - env.max_height_mm, 2)})
        if (env.min_ground_clearance_mm is not None
                and bottom < env.min_ground_clearance_mm - tol):
            violations.append({
                "rule": "min_ground_clearance_mm", "edge": "bottom",
                "value_mm": round(bottom, 2),
                "limit_mm": env.min_ground_clearance_mm,
                "by_mm": round(env.min_ground_clearance_mm - bottom, 2)})

    # the exposed element is computed, never assumed (a flap can sit in
    # front under a nose-down stack) — same doctrine as the parametric
    # checker
    front = int(np.argmin([e["coords"][:, 0].min() for e in installed]))
    want_le = bool(env is not None and env.min_le_radius_mm)
    want_te = bool(env is not None and env.min_te_thickness_mm)
    mfg = cfg.manufacturing
    floor_mm = (mfg.min_thickness_mm
                if mfg is not None and mfg.min_thickness_mm else None)
    tol = geometry.ENVELOPE_TOL_MM
    for i, (e, poly) in enumerate(zip(installed, polys_m)):
        p = np.asarray(poly, float)
        try:
            norm = airfoils.normalize(p)
        except ValueError as err:
            unverified.append({"rule": "contour", "element": e["role"],
                               "reason": str(err)})
            continue
        # the element's own as-polished chord: the normalize scale, not
        # the parametric chord_ratio (a morph moves the LE and TE too)
        te_mid = 0.5 * (p[0] + p[-1])
        d = p - te_mid
        c_mm = float(np.sqrt(np.einsum("ij,ij->i", d, d).max())) * 1000.0
        need_le = want_le and (env.le_radius_scope == "all" or i == front)
        info = airfoils.geometry_info(norm, with_le_radius=need_le)
        r: dict = {"element": e["role"], "chord_mm": round(c_mm, 1)}
        if need_le:
            r_c = info["le_radius"]
            r_mm = None if r_c is None else r_c * c_mm
            r["le_radius_mm"] = None if r_mm is None else round(r_mm, 2)
            r["le_radius_ok"] = (
                None if r_mm is None
                else bool(r_mm >= env.min_le_radius_mm - tol))
            r["le_radius_knife_edge"] = bool(
                r_mm is not None
                and abs(r_mm - env.min_le_radius_mm)
                <= geometry.LE_RADIUS_BAND * env.min_le_radius_mm)
            if r["le_radius_ok"] is None:
                unverified.append({"rule": "min_le_radius",
                                   "element": e["role"],
                                   "reason": info["le_radius_reason"]})
            elif not r["le_radius_ok"]:
                violations.append({
                    "rule": "min_le_radius", "edge": "element",
                    "element": e["role"],
                    "value_mm": r["le_radius_mm"],
                    "limit_mm": env.min_le_radius_mm,
                    "by_mm": round(env.min_le_radius_mm - r_mm, 2)})
        if want_te:
            te_mm = info["te_gap"] * c_mm
            r["te_thickness_mm"] = round(te_mm, 2)
            r["te_ok"] = bool(te_mm >= env.min_te_thickness_mm - tol)
            if not r["te_ok"]:
                violations.append({
                    "rule": "min_te_thickness", "edge": "element",
                    "element": e["role"],
                    "value_mm": round(te_mm, 2),
                    "limit_mm": env.min_te_thickness_mm,
                    "by_mm": round(env.min_te_thickness_mm - te_mm, 2)})
        if floor_mm is not None:
            # the buildability floor the mfg prep promised: thinnest
            # aft-of-nose station of the as-polished contour
            ref_t = info["te_gap"] if info["te_gap"] > 1e-6 else None
            t_mm = aft_min_thickness(norm, ref_t) * c_mm
            r["aft_min_thickness_mm"] = round(t_mm, 2)
            r["aft_floor_ok"] = bool(t_mm >= floor_mm - tol)
            if not r["aft_floor_ok"]:
                violations.append({
                    "rule": "min_thickness_mm", "edge": "element",
                    "element": e["role"], "value_mm": round(t_mm, 2),
                    "limit_mm": floor_mm,
                    "by_mm": round(floor_mm - t_mm, 2)})
        rows.append(r)
    out = {"ok": not violations, "violations": violations,
           "rows": rows, "extents_mm": extents}
    if unverified:
        out["unverified"] = unverified
    return out


class AdjointPolishJob:
    """One final-stage polish run; duck-type-compatible with the other
    engine jobs everywhere the registry, API and UI touch one."""

    def __init__(self, seed_job, options: dict | None = None):
        # the seed contract is validated eagerly and completely: a polish
        # must start from a FINISHED true-2D run whose solved case+data
        # still exist on disk — everything else is refused here, before
        # the registry slot and the license
        snap = seed_job.snapshot()
        if snap.get("engine") != "fluent2d":
            raise ValueError("the adjoint polish starts from an ANSYS 2D "
                             "verification run — this run was solved by "
                             f"the {snap.get('engine')} engine")
        if seed_job.state != "done" or not seed_job.result:
            raise ValueError("the seed run has not finished — polish "
                             "starts from a completed, converged "
                             "verification")
        if seed_job.result.get("profiles_override"):
            # a re-verify run meshed FREE-FORM profiles; this job's node
            # attribution and design region are built from the config's
            # parametric section, which that mesh no longer matches
            raise ValueError("this run re-verified an already-polished "
                             "shape — seed the polish from a parametric "
                             "verification run instead")
        seed_dir = Path(seed_job.case_dir)
        if not ((seed_dir / "case.cas.h5").is_file()
                and (seed_dir / "case.dat.h5").is_file()):
            raise ValueError("the seed run carries no solved case+data "
                             "(case.cas.h5/.dat.h5) — re-run the "
                             "verification, then polish")
        self.options = resolve_options(options)
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(seed_job.config)
        self.cfg = StackConfig.from_dict(self.config)
        self.seed_id = seed_job.id
        self.seed_dir = seed_dir
        self.seed_result = copy.deepcopy(seed_job.result)
        self.seed_conventions = getattr(seed_job, "conventions", "studio")
        self.seed_settings = dict(getattr(seed_job, "settings", {}) or {})
        self.n_iters = self.options["design_iters"]
        self.n_ranks = int(getattr(seed_job, "n_ranks", 1) or 1)
        self.state = "pending"
        self.phase: str | None = None
        self.error: str | None = None
        self.iteration = 0
        self.latest: dict | None = None
        self.history: list[dict] = []
        self.mesh = copy.deepcopy(getattr(seed_job, "mesh", None))
        self.result: dict | None = None
        self.case_dir = cfd_run._runs_dir() / self.id
        self.t_created = time.time()
        self.t_start: float | None = None
        self.t_end: float | None = None
        self._container = ""            # registry compatibility
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        _active.add(self)

    # ---- runner ----

    def run(self) -> None:
        with self._lock:
            self.state = "running"
            self.t_start = time.time()
        fm = None
        try:
            fm = self._run_inner()
        except Exception as e:
            with self._lock:
                if self._cancel.is_set():
                    self.state = "cancelled"
                else:
                    self.state = "failed"
                    self.error = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            with self._lock:
                self.t_end = time.time()
            if fm is not None:
                try:
                    fm.shutdown()
                except Exception:
                    pass

    def _set_phase(self, p: str) -> None:
        with self._lock:
            self.phase = p

    def _cancelled(self) -> bool:
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return True
        return False

    def _norm_coeffs(self, reports: dict) -> tuple[float, float]:
        """Fluent report values -> chord-referenced downforce-positive
        (the studio basis). What the reports already are depends on the
        seed's conventions, exactly as in fluent2d_run._finalize."""
        cl = next((v for k, v in reports.items()
                   if "lift" in k.lower()), None)
        cd = next((v for k, v in reports.items()
                   if "drag" in k.lower()), None)
        if cl is None:
            raise RuntimeError("Fluent returned no lift report — the "
                               "reloaded case carries no report "
                               "definitions")
        cd = 0.0 if cd is None else float(cd)
        if self.seed_conventions == "default":
            return cl_chord(float(cl), self.cfg.chord_m), \
                cd_chord(cd, self.cfg.chord_m)
        return float(cl), cd

    def _newtons(self, cl: float, cd: float) -> tuple[float, float, float]:
        """(downforce_n, drag_n, j_n) in the studio's credible-newton
        space: downforce via the same q*area*cl*efficiency_3d the verify
        card uses, drag via the queue's measured-drag convention
        (|downforce| * |cd| / |cl|), J = downforce - k * drag."""
        q = self.cfg.q_pa
        area = self.cfg.chord_m * (self.cfg.span_mm / 1000.0)
        df = q * area * cl * self.cfg.efficiency_3d
        drag = (abs(df) * abs(cd) / abs(cl)) if abs(cl) > 1e-9 else 0.0
        j = df - self.options["drag_exchange_k"] * drag
        return df, drag, j

    def _run_inner(self):
        if self._cancelled():
            return None
        self.case_dir.mkdir(parents=True, exist_ok=True)
        (self.case_dir / "config.json").write_text(
            json.dumps(self.config, indent=1), encoding="utf-8")

        # the baseline polylines: the exact installed contours the seed
        # case was meshed from — the node-attribution reference and the
        # design-region source
        self._set_phase("rebuilding the section geometry")
        g = cfd.section_geometry(self.cfg, "coarse")
        base_polys = [np.asarray(p, float) for p, _blunt in g["polys"]]
        bounds = region_bounds(base_polys, self.cfg,
                               self.options["margin_pct"])
        if self._cancelled():
            return None

        self._set_phase("starting ANSYS Fluent (license checkout)")
        fm = _mcp()
        fm.launch(dimension=2, precision="double",
                  processors=self.n_ranks)
        try:
            if self._cancelled():
                return fm
            self._set_phase("loading the verified case")
            loaded = fm.read_case_data(str(self.seed_dir / "case.cas.h5"))
            walls = (loaded.get("zones") or {}).get("wall", [])
            if "profile" not in walls:
                with self._lock:
                    self.state = "failed"
                    self.error = ("the reloaded case carries no "
                                  "'profile' wall zone — it is not a "
                                  "studio 2D case")
                return fm

            o = self.options
            self._set_phase(f"re-settling the flow "
                            f"({o['settle_iters']} iterations)")
            fm.solve(iterations=o["settle_iters"], initialize=False)
            base_cl, base_cd = self._norm_coeffs(
                fm._compute_reports(["lift_coef", "drag_coef"]))
            settle_note = None
            seed_cl = self.seed_result.get("cl_rans")
            if seed_cl and abs(base_cl - seed_cl) > SETTLE_TOL_REL \
                    * max(abs(seed_cl), 0.1):
                settle_note = (
                    f"the re-settled baseline (Cl {base_cl:.3f}) sits "
                    f"{(base_cl / seed_cl - 1) * 100:+.1f}% from the "
                    f"seed run's {seed_cl:.3f} — deltas are measured "
                    f"against the re-settled state")
            base_df, base_drag, base_j = self._newtons(base_cl, base_cd)
            if self._cancelled():
                return fm

            self._set_phase("configuring the adjoint optimizer")
            setup = fm.adjoint_setup(
                profile_zones=["profile"],
                drag_exchange_k=o["drag_exchange_k"],
                region_x=bounds["x"], region_y=bounds["y"],
                flow_iterations=o["flow_iters"],
                adjoint_iterations=o["adjoint_iters"])
            if setup["failed"]:
                with self._lock:
                    self.state = "failed"
                    self.error = ("adjoint setup steps failed: "
                                  + "; ".join(f"{f['step']}: {f['error']}"
                                              for f in setup["failed"]))
                return fm

            # the loop: Fluent owns the iteration, the studio owns the
            # bookkeeping and the legality gate between iterations
            best = {"j": base_j, "iter": 0, "polys": None, "cl": base_cl,
                    "cd": base_cd, "df": base_df, "drag": base_drag,
                    "stats": None, "rules": None}
            stop_reason = "design-iteration budget reached"
            flat_hits = 0
            over_hits = 0
            n_done = 0
            for it in range(1, o["design_iters"] + 1):
                if self._cancelled():
                    return fm
                if time.time() - self.t_start > WALL_LIMIT_S:
                    with self._lock:
                        self.state = "failed"
                        self.error = (
                            f"wall-clock backstop: still polishing after "
                            f"{WALL_LIMIT_S / 3600:.0f} h — the session "
                            f"appears hung, so the run was stopped")
                    return fm
                self._set_phase(f"design iteration {it}/"
                                f"{o['design_iters']} (flow + adjoint "
                                f"+ morph)")
                step = fm.adjoint_step()
                cl, cd = self._norm_coeffs(step.get("reports") or {})
                df, drag, j = self._newtons(cl, cd)
                n_done = it

                self._set_phase(f"design iteration {it}: checking the "
                                f"polished shape")
                nodes = self._export_wall_nodes(fm)
                polys, stats = split_wall_nodes(
                    nodes, base_polys, NODE_MATCH_TOL_C * self.cfg.chord_m)
                rules = check_polished(polys, self.cfg)
                row = {"iter": it, "cl": round(cl, 4), "cd": round(cd, 5),
                       "downforce_n": round(df, 1),
                       "drag_n": round(drag, 1), "j_n": round(j, 1),
                       "compliant": bool(rules["ok"]),
                       "max_disp_mm": stats["max_disp_mm"]}
                with self._lock:
                    self.iteration = it
                    self.latest = row
                    self.history = self.history + [row]
                if not rules["ok"]:
                    stop_reason = (
                        f"design iteration {it} violated "
                        f"{rules['violations'][0]['rule']} — "
                        f"delivering the last compliant shape")
                    break
                # gain measured against the best BEFORE this iterate, so
                # a slow sub-bar crawl still counts as flat
                rel_gain = (j - best["j"]) / max(abs(best["j"]), 1.0)
                if j > best["j"]:
                    best = {"j": j, "iter": it, "polys": polys,
                            "cl": cl, "cd": cd, "df": df, "drag": drag,
                            "stats": stats, "rules": rules}
                if rel_gain <= -OVERSHOOT_REL:
                    over_hits += 1
                    flat_hits = 0
                elif rel_gain < EARLY_STOP_REL:
                    flat_hits += 1
                    over_hits = 0
                else:
                    flat_hits = over_hits = 0
                if flat_hits >= EARLY_STOP_HITS:
                    stop_reason = ("polish converged — J improved less "
                                   f"than {EARLY_STOP_REL * 100:.2f}% on "
                                   f"{EARLY_STOP_HITS} consecutive "
                                   f"iterations")
                    break
                if over_hits >= EARLY_STOP_HITS:
                    stop_reason = ("the optimizer walked past the peak — "
                                   "delivering the best compliant shape")
                    break

            self._finalize(fm, bounds, setup,
                           base_cl, base_cd, base_df, base_drag, base_j,
                           settle_note, best, n_done, stop_reason)
            return fm
        except BaseException:
            # any raise after launch() must release the license here
            # (run()'s finally only sees a RETURNED handle)
            try:
                fm.shutdown()
            except Exception:
                pass
            raise

    def _export_wall_nodes(self, fm) -> np.ndarray:
        """Morphed profile-zone node coordinates from the live session
        (the same per-node export the wall-attachment channel uses)."""
        csv = self.case_dir / "wall_nodes.csv"
        fm.export_ascii(filename=str(csv),
                        quantities=["x-wall-shear", "y-wall-shear"],
                        location="node", surfaces=["profile"])
        xs, ys = [], []
        header: list[str] = []
        xi = yi = None
        with open(csv, errors="replace") as fh:
            for ln in fh:
                parts = [p.strip() for p in ln.split(",")]
                if not header:
                    header = [p.lower() for p in parts]
                    xi = next((i for i, h in enumerate(header)
                               if "x-coordinate" in h), None)
                    yi = next((i for i, h in enumerate(header)
                               if "y-coordinate" in h), None)
                    if xi is None or yi is None:
                        raise RuntimeError(
                            f"wall export carries no coordinate columns "
                            f"(header: {header})")
                    continue
                if len(parts) != len(header):
                    continue
                try:
                    xs.append(float(parts[xi]))
                    ys.append(float(parts[yi]))
                except ValueError:
                    continue
        return np.column_stack([xs, ys])

    def _finalize(self, fm, bounds, setup,
                  base_cl, base_cd, base_df, base_drag, base_j,
                  settle_note, best, n_done, stop_reason) -> None:
        o = self.options
        improved = best["iter"] > 0 and best["polys"] is not None
        delivered_polys = best["polys"] if improved else None
        # do the session artifacts represent the delivered shape? Only
        # when the loop ended exactly on the best iterate — a rule-stop
        # or overshoot leaves the session one or more morphs past it
        session_is_delivered = improved and best["iter"] == n_done
        cautions: list[str] = []
        if settle_note:
            cautions.append(settle_note)
        seed_sep = ((self.seed_result.get("field_verdict") or {})
                    .get("separated")
                    or (cfd_run.worst_reversed(self.seed_result) or 0)
                    > cfd_run.SEP_PARTIAL_MAX)
        if seed_sep:
            cautions.append(
                "the seed flow carries separation — adjoint gradients "
                "over separated flow are unreliable; treat the polish "
                "direction as provisional until the re-verification")
        cautions.append(
            "per-iteration numbers are re-solves on the MORPHED mesh — "
            "morphing degrades cell quality, so the delivered gain is "
            "provisional until the polished profiles are re-verified on "
            "a fresh mesh (Re-verify runs the standard chain)")

        wall_report = wall_verdict = None
        knife = None
        rec = None
        fverdict_obj = None
        flow_exported = False
        case_written = False
        if session_is_delivered:
            self._set_phase("exporting the polished flow field")
            try:
                self._export_flow_fields(fm)
                flow_exported = True
            except Exception as e:
                cautions.append(f"flow-field export failed ({e}); "
                                f"flow view unavailable for this run")
            if flow_exported:
                try:
                    fwall = foam_post.fluent_wall_report(self.case_dir,
                                                         self.cfg)
                except Exception:
                    fwall = None
                if fwall is not None:
                    wall_report = fwall
                    wall_verdict, knife = cfd_run.wall_verdict_text(fwall)
                try:
                    # converged=False, honestly: the optimizer's final
                    # flow solve ran a fixed budget with no drift
                    # verdict — the field measurement is a bound, and
                    # the report's own qualifier should say so
                    rec = foam_post.recirculation_report(
                        self.case_dir, self.cfg, converged=False,
                        user_stopped=False, wall=None)
                except Exception:
                    rec = None
                fverdict_obj = cfd_run.field_verdict(rec)
            self._set_phase("writing case files")
            try:
                fm.write_case_data(str(self.case_dir / "case"))
                case_written = (self.case_dir / "case.cas.h5").is_file()
            except Exception:
                pass
        elif improved:
            cautions.append(
                f"the session's final mesh is {n_done - best['iter']} "
                f"iteration(s) past the delivered shape, so no flow "
                f"view or case artifact is written — the delivered "
                f"profiles are the deliverable")

        profiles_name = None
        if improved:
            profiles_name = "polished_profiles.json"
            (self.case_dir / profiles_name).write_text(json.dumps({
                "polish_id": self.id, "seed_id": self.seed_id,
                "iteration": best["iter"],
                "k_exchange": o["drag_exchange_k"],
                "elements": [np.asarray(p).round(7).tolist()
                             for p in delivered_polys],
            }, separators=(",", ":")), encoding="utf-8")
        (self.case_dir / "polish_history.json").write_text(
            json.dumps({"history": self.history, "bounds": bounds,
                        "seed_id": self.seed_id},
                       separators=(",", ":")), encoding="utf-8")

        d_cl = (best["cl"] / base_cl - 1) * 100 if abs(base_cl) > 1e-9 \
            else None
        d_cd = (best["cd"] / base_cd - 1) * 100 if abs(base_cd) > 1e-9 \
            else None
        with self._lock:
            self.result = {
                "engine": "polish",
                "seed": {
                    "id": self.seed_id,
                    "cl_rans": self.seed_result.get("cl_rans"),
                    "cd_rans": self.seed_result.get("cd_rans"),
                    "conventions": self.seed_conventions,
                    "sizing": self.seed_result.get("sizing"),
                    "converged": self.seed_result.get("converged"),
                },
                "k_exchange": o["drag_exchange_k"],
                "baseline": {"cl": round(base_cl, 4),
                             "cd": round(base_cd, 5),
                             "downforce_n": round(base_df, 1),
                             "drag_n": round(base_drag, 1),
                             "j_n": round(base_j, 1)},
                "polished": None if not improved else {
                    "cl": round(best["cl"], 4),
                    "cd": round(best["cd"], 5),
                    "downforce_n": round(best["df"], 1),
                    "drag_n": round(best["drag"], 1),
                    "j_n": round(best["j"], 1),
                    "iteration": best["iter"]},
                "delta": None if not improved else {
                    "cl_pct": round(d_cl, 2) if d_cl is not None else None,
                    "cd_pct": round(d_cd, 2) if d_cd is not None else None,
                    "downforce_n": round(best["df"] - base_df, 1),
                    "drag_n": round(best["drag"] - base_drag, 1),
                    "j_n": round(best["j"] - base_j, 1)},
                "improved": bool(improved),
                "iterations_run": n_done,
                "delivered_iteration": best["iter"] if improved else None,
                "stop_reason": stop_reason if improved else (
                    "no compliant improvement found — " + stop_reason),
                "rules": best["rules"] if improved else None,
                "displacement": best["stats"] if improved else None,
                "region": bounds,
                "adjoint_setup": {"applied": setup.get("applied", []),
                                  "objective": setup.get("objective")},
                "cautions": cautions,
                "wall_report": wall_report,
                "wall_verdict": wall_verdict,
                "sep_knife_edge": knife,
                "recirc_report": rec,
                "field_verdict": fverdict_obj,
                "artifacts": {"case_written": case_written,
                              "flow_exported": flow_exported,
                              "profiles_json": profiles_name,
                              "session_is_delivered": session_is_delivered},
                "engine_note": (
                    "polished by ANSYS Fluent's gradient-based optimizer "
                    "(adjoint shape-opt) on the verified 2D case, "
                    f"objective J = downforce - "
                    f"{o['drag_exchange_k']:g} x drag on the profile "
                    "walls, morph bounded by the rule envelope. The "
                    "studio re-measured legality (LE radius, TE "
                    "thickness, aft-thickness floor, envelope box) on "
                    "the extracted contour after every design "
                    "iteration. Deltas are measured on the morphed "
                    "mesh against the re-settled baseline; the "
                    "polished profiles are the deliverable and the "
                    "standard re-verification is the honest measure "
                    "of the gain."),
                "settings": dict(o),
                "n_ranks": self.n_ranks,
                "user_stopped": False,
                "case_dir": str(self.case_dir),
            }
            self.phase = None
            self.state = "done"

    def _export_flow_fields(self, fm) -> None:
        """Same artifact contract as the 2D engine: cell-centre C/U/p in
        OpenFOAM format plus per-node wall shear, so the flow view and
        the attachment verdicts read a polish run like any other."""
        import importlib
        f2d = importlib.import_module("app.core.fluent2d_run")
        # reuse the exact exporter by borrowing its implementation on a
        # stand-in carrying the fields it reads (case_dir + ref density)
        rho = (f2d.FLUENT_RHO if self.seed_conventions == "default"
               else self.cfg.rho)

        class _Shim:
            pass
        shim = _Shim()
        shim.case_dir = self.case_dir
        shim.ref_rho = rho
        f2d.Fluent2DJob._export_flow_fields(shim, fm, self.iteration or 1)

    # ---- API surface (mirrors the other engine jobs) ----

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = ((self.t_end or time.time())
                       - (self.t_start or time.time()))
            if self.state in ("done", "failed", "cancelled"):
                progress = 1.0
            elif self.iteration:
                progress = min(0.97, 0.08 + 0.89 * self.iteration
                               / max(self.n_iters, 1))
            else:
                progress = 0.03
            return {
                "id": self.id, "state": self.state, "phase": self.phase,
                "error": self.error,
                "progress": round(progress, 3),
                "iteration": self.iteration, "n_iters": self.n_iters,
                "engine": "polish", "mesher": "ansys-2d",
                "seed_id": self.seed_id,
                "conventions": self.seed_conventions,
                "settings": dict(self.options),
                "elapsed_s": round(elapsed, 1),
                "latest": self.latest,
                "history": list(self.history),
                "mesh": self.mesh,
                "result": self.result if self.state == "done" else None,
                "case_dir": str(self.case_dir),
            }

    def cancel(self) -> None:
        """Cancel interrupts the optimizer (soft) and shoots the session
        (hard) so a blocking optimize() raises within seconds; the
        runner's handler reads the flag and records 'cancelled'."""
        self._cancel.set()
        if self.state == "running":
            try:
                _mcp().adjoint_interrupt()
            except Exception:
                pass
            try:
                _mcp().shutdown()
            except Exception:
                pass

    def stop_graceful(self) -> bool:
        """No keep-fields stop: like the other Fluent engines, fields
        are only written at the end."""
        return False
