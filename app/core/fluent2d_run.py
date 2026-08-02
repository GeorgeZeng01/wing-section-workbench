"""In-app ANSYS Fluent 2D verification runs — the documented manual
ANSYS workflow, automated.

Replicates that documented manual 2D workflow end to end as the
default: profile DXF -> SpaceClaim fill -> Mechanical mesh (edge
sizing + inflation, named selections) -> Fluent 2D double precision
with the walkthrough's boundary conditions, reference values and
report definitions — every meshing step done by ANSYS tools, a true-2D
solve, no slab extrusion anywhere. The studio's own conventions
(chord-referenced downforce-positive display, SST pinned with the
studio inlet turbulence, residual auto-stop disabled, three-window
drift verdict) are OPT-IN, never defaults: conventions="default"
reproduces what the manual walkthrough shows in Fluent;
conventions="studio" reproduces how the studio judges a run.

Mirrors fluent_run.FluentJob's surface (state machine, snapshot,
cancel, result keys) so the registry, API and UI treat a 2D run like
any other. The geometry is the same installed section every engine
meshes (cfd.section_geometry's polylines, meters, ground at y = 0);
the domain rectangle is the walkthrough's (front 3L, back 7L, top 3H,
ground at y = 0) unless the settings ask for other proportions, built
by the workflow's DXF writer. The meshing chain (SpaceClaim ->
Workbench/Mechanical) runs licensed ANSYS batch processes through
scripts/fluent2d_workflow; the solve runs a headless Fluent session
through the same scripts/fluent_mcp helpers as the 3D engine. The
license is held only while a job runs, released on every path
including a raise, and cancel shoots the live session.

Coefficients arrive in Fluent's own convention (default force vectors,
reference area 1 m2 per meter of depth) and are converted here —
cl_chord = -cl_raw / chord, cd_chord = cd_raw / chord — so the
headline number stays comparable across engines while the raw values
remain visible for parity with the manual workflow. Under the default
conventions the run goes exactly n_iters unless Fluent's own residual
criteria end it early (reported as residual_stop, never dressed up as
a drift stop); in studio mode the OpenFOAM force-stop doctrine applies
with n_iters as the cap.

Every knob the settings panel exposes — the sizing recipe, the four
mesh controls, the domain proportions, iterations, ranks and the two
meshing-stage budgets — is resolved ONCE at construction: an override
that is null or absent falls back to the sizing recipe or the
documented default, and every value is range-checked there, so an
impossible setting is refused before a licensed ANSYS process starts.
The resolved set travels with the snapshot and the result, so a
finished run states exactly what it ran on: where the meshing chain has
to cap the inflation stack to the clearances it faces (or drops it
altogether on a degrade retry), the resolved set follows the mesh and
keeps the request beside it under *_requested. An override never
rewrites the sizing label, which names the recipe the overrides sit on.

The flow view and the animated view work on 2D runs: the solved
cell-centre field is exported from the live session — 2D exports carry
no z column — and written as OpenFOAM-format C/U/p files with z = 0
and w = 0, so foam_post reads a 2D Fluent run exactly like every other
engine. Wall-shear attachment verdicts remain an OpenFOAM-engine
feature; the result says so. One job at a time across ALL engines:
2D jobs register in cfd_run's registry, so the existing guard covers
them.
"""
from __future__ import annotations

import copy
import math
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

from . import analysis, cfd, cfd_run, foam_post
from .geometry import StackConfig

# solve in chunks so the chart updates and (in studio mode) the drift
# criterion is evaluated on genuinely new history between chunks; 2D
# meshes are small enough that the fixed chunk is minutes, not an hour
CHUNK_ITERS = 250
FIRST_CHUNK = 60
DRIFT_SKIP = 200          # hybrid-init transient excluded from decisions
MAX_PROCESSORS = 32
WALL_LIMIT_S = 12 * 3600  # absolute backstop, checked at chunk
                          # boundaries — a session still going past this
                          # is wedged, not solving

# a torn or not-yet-flushed rfile row reads as a tiny shortfall the
# instant solve() returns — a residual stop must survive one settled
# re-read before it counts (a real stop undershoots by far more)
RESIDUAL_SLACK_ROWS = 2
RESIDUAL_SETTLE_S = 0.5

# Fluent's default air: the documented manual workflow never opens the
# materials panel, so its reference values and the Cp conversion must
# use the density the solve actually ran, never the configuration's
FLUENT_RHO = 1.225
FLUENT_MU = 1.7894e-05

SIZING_MODES = ("default", "studio-yplus1")
CONVENTIONS = ("default", "studio")

# domain proportions of the documented manual workflow, in multiples of
# the section's streamwise extent (L) and stack height (H) — what the
# DXF writer builds when the settings leave them empty
DOMAIN_DEFAULTS = {"front_l": 3.0, "back_l": 7.0, "top_h": 3.0}
# per-stage wall budgets for the meshing chain, seconds
BUDGET_DEFAULTS = {"sc_budget_s": 300.0, "wb_budget_s": 900.0}

# a first layer more than a few times the y+ ~ 1 height leaves the
# viscous sublayer to wall functions — the caveat follows the RESOLVED
# first layer, since an override can put either sizing on either side
WALL_FUNCTION_RATIO = 5.0


def _mcp():
    """The session-facing Fluent helpers (lazy: pyfluent and the repo's
    scripts package load only when a job actually starts). Module-level
    seam so the offline suite can fake the whole engine."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent_mcp
    return fluent_mcp


def _workflow():
    """The 2D meshing-chain helpers (scripts.fluent2d_workflow), loaded
    the same lazy way."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent2d_workflow
    return fluent2d_workflow


def _chain(dxf_path, work_dir, **kw):
    """DXF -> SpaceClaim -> Workbench/Mechanical -> FFF.msh (the manual
    walkthrough's meshing chain, ANSYS batch processes). Seam for
    tests."""
    return _workflow().run_chain(dxf_path, work_dir, **kw)


def _dxf(profiles_m, out_path, **kw):
    """Profiles + the domain rectangle -> R2000 DXF. Seam for tests."""
    return _workflow().write_dxf_2d(profiles_m, out_path, **kw)


def _sizing(mode, cfg):
    """Sizing mode -> Mechanical mesh controls (the walkthrough's
    constants or the studio y+ ~ 1 wall resolution). Seam for tests."""
    return _workflow().mesh_sizing(mode, cfg)


def _num(value, name, cast=float):
    """One settings value as its own type. Every message names the
    field, so a rejected panel value points at itself.

    Finiteness and wholeness are decided BEFORE the cast: int(inf)
    raises OverflowError (outside this contract) and int(10.9) would
    silently truncate — the same two refusals the request bodies make."""
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
    return cast(f)


def _positive(value, name):
    v = _num(value, name)
    if v <= 0:
        raise ValueError(f"{name} must be a positive number")
    return v


def _bounded(value, name, lo, hi, cast=float, lo_open=False):
    v = _num(value, name, cast)
    if (v <= lo if lo_open else v < lo) or v > hi:
        raise ValueError(f"{name} must be {'>' if lo_open else '>='} "
                         f"{lo:g} and <= {hi:g}")
    return v


def resolve_settings(settings: dict | None, recipe: dict, sizing: str,
                     n_iters: int, n_ranks: int,
                     conventions: str) -> dict:
    """The settings panel's request -> the concrete values one run uses.

    An override that is null or absent falls back to the sizing recipe
    (the four mesh controls) or to the documented default (domain
    proportions, stage budgets). Every value is range-checked here so
    an impossible setting is refused before any licensed ANSYS process
    starts. sizing stays the label of the recipe the overrides sit on —
    an override never rewrites it."""
    s = dict(settings or {})

    def pick(key, fallback):
        v = s.get(key)
        return fallback if v is None else v

    return {
        "sizing": sizing,
        "edge_size_mm": _positive(
            pick("edge_size_mm", recipe["edge_size_mm"]), "edge_size_mm"),
        "first_layer_mm": _positive(
            pick("first_layer_mm", recipe["first_layer_mm"]),
            "first_layer_mm"),
        "n_layers": _bounded(pick("n_layers", recipe["n_layers"]),
                             "n_layers", 0, 100, int),
        "growth": _bounded(pick("growth", recipe["growth"]),
                           "growth", 1.0, 3.0, lo_open=True),
        "front_l": _bounded(pick("front_l", DOMAIN_DEFAULTS["front_l"]),
                            "front_l", 0.5, 20.0),
        "back_l": _bounded(pick("back_l", DOMAIN_DEFAULTS["back_l"]),
                           "back_l", 0.5, 40.0),
        "top_h": _bounded(pick("top_h", DOMAIN_DEFAULTS["top_h"]),
                          "top_h", 0.5, 20.0),
        # the walkthrough's plain 500-iteration run sits far under the
        # other engines' floor — short parity runs stay legal here
        "n_iters": _bounded(n_iters, "iterations", 50, 20_000, int),
        "n_ranks": _bounded(n_ranks, "n_ranks", 1, MAX_PROCESSORS, int),
        "conventions": conventions,
        "sc_budget_s": _bounded(
            pick("sc_budget_s", BUDGET_DEFAULTS["sc_budget_s"]),
            "sc_budget_s", 30.0, 7200.0),
        "wb_budget_s": _bounded(
            pick("wb_budget_s", BUDGET_DEFAULTS["wb_budget_s"]),
            "wb_budget_s", 60.0, 43200.0),
    }


def cl_chord(cl_raw: float, chord_m: float) -> float:
    """Fluent-convention lift coefficient (default +y force vector,
    reference area 1 m2 per meter of depth) -> the studio's
    chord-referenced DOWNFORCE-POSITIVE coefficient. The sign flips
    because Fluent's default lift vector points up and a front wing
    pushes down."""
    return -cl_raw / chord_m


def cd_chord(cd_raw: float, chord_m: float) -> float:
    """Fluent-convention drag coefficient -> chord-referenced. Drag keeps
    its sign; only the reference length changes."""
    return cd_raw / chord_m


class Fluent2DJob:
    """One true-2D ANSYS verification run; duck-type-compatible with
    FluentJob/RansJob everywhere the registry, API and UI touch a job."""

    def __init__(self, config: dict, mesh_size: str = "default",
                 n_iters: int = 500, n_ranks: int = 1,
                 conventions: str = "default",
                 settings: dict | None = None):
        # mesh_size carries the SIZING mode here (the recipe the mesh
        # overrides sit on): "default" = the walkthrough's 0.1 mm /
        # 1 mm x 10 inflation, "studio-yplus1" = resolved-wall first
        # layer from cfd.first_layer. settings is the panel's object; it
        # repeats the four run parameters, and a value it carries wins
        # over the positional one, so both call styles resolve to one
        # answer instead of two disagreeing ones
        s = dict(settings or {})
        if s.get("sizing") is not None:
            mesh_size = s["sizing"]
        if s.get("conventions") is not None:
            conventions = s["conventions"]
        if s.get("n_iters") is not None:
            n_iters = s["n_iters"]
        if s.get("n_ranks") is not None:
            n_ranks = s["n_ranks"]
        if mesh_size not in SIZING_MODES:
            raise ValueError(
                f"mesh sizing must be one of {list(SIZING_MODES)}")
        if conventions not in CONVENTIONS:
            raise ValueError(
                f"conventions must be one of {list(CONVENTIONS)}")
        self.mesher = "ansys-2d"
        self.conventions = conventions
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(config)
        self.cfg = StackConfig.from_dict(config)   # validates eagerly
        # the density the coefficients and Cp are referenced to: the
        # default conventions solve Fluent's own air, studio the config's
        self.ref_rho = (FLUENT_RHO if conventions == "default"
                        else self.cfg.rho)
        self.mesh_size = mesh_size
        # the recipe first, then the overrides on top of it: resolving
        # here means a bad setting is refused before the run dir, the
        # ANSYS batch processes and the license, not minutes into them
        try:
            recipe = _sizing(mesh_size, self.config)
        except ValueError as e:
            raise ValueError(f"2D mesh sizing rejected: {e}")
        except Exception:
            # a missing/broken meshing package reads as unavailable, and
            # says so in the user's terms — never in module paths
            raise ValueError("the ANSYS 2D meshing tools are not "
                             "installed with this app")
        self.settings = resolve_settings(s, recipe, mesh_size, n_iters,
                                         n_ranks, conventions)
        self.n_iters = self.settings["n_iters"]
        self.n_ranks = self.settings["n_ranks"]
        self.state = "pending"
        self.phase: str | None = None
        self.error: str | None = None
        self.iteration = 0
        self.latest: dict | None = None
        self.history: list[dict] = []
        self.mesh: dict | None = None
        self.result: dict | None = None
        self.case_dir = cfd_run._runs_dir() / self.id
        self.t_created = time.time()
        self.t_start: float | None = None
        self.t_end: float | None = None
        self._container = ""          # registry compatibility (no docker
                                      # solver container to sweep)
        self._cancel = threading.Event()
        self._lock = threading.Lock()

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
                    # cancel() kills the live session (and the meshing
                    # chain's process tree) so a blocking call lands
                    # within seconds — the resulting exception IS the
                    # cancellation, not a failure
                    self.state = "cancelled"
                else:
                    self.state = "failed"
                    self.error = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            with self._lock:
                self.t_end = time.time()
            if fm is not None:
                try:
                    # release the license on every path (idempotent —
                    # a hard cancel may have already shut the session)
                    fm.shutdown()
                except Exception:
                    pass

    def _set_phase(self, p: str) -> None:
        with self._lock:
            self.phase = p

    def _harvest_history(self, cl: list, cd: list) -> None:
        # the two histories come from two independently parsed rfiles
        # and a torn last line is dropped — clamp to the common length
        # instead of indexing cd with lift-derived positions
        if cd:
            m = min(len(cl), len(cd))
            cl, cd = cl[:m], cd[:m]
        n = len(cl)
        if not n:
            return
        # the chart shows the monitors as Fluent emits them, and the
        # CONVENTION decides what those already are: default-2d setup
        # leaves Fluent's defaults (raw, ref area 1 m2, lift +up), the
        # studio setup references the monitors to the chord with a
        # downforce-positive lift vector at the source — converting
        # here again would flip the sign and divide by chord twice
        # ceiling division: floor overshoots the ~300-point target — the
        # chart budget is a cap
        step = max(1, -(-n // 300))
        idx = list(range(0, n, step))
        if idx[-1] != n - 1:
            idx.append(n - 1)
        hist = [{"iter": i + 1, "cl": round(cl[i], 4),
                 "cd": round(cd[i], 5) if cd else None} for i in idx]
        with self._lock:
            self.iteration = n
            self.latest = hist[-1]
            self.history = hist

    def _run_inner(self):
        import json
        import shutil
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None
        self.case_dir.mkdir(parents=True, exist_ok=True)
        (self.case_dir / "config.json").write_text(
            json.dumps(self.config, indent=1), encoding="utf-8")

        # the identical installed section every engine meshes; only the
        # polylines matter here — the 2D domain rectangle follows the
        # resolved proportions, built by the DXF writer, not the
        # studio's fixed box
        self._set_phase("building the section geometry")
        try:
            g = cfd.section_geometry(self.cfg, "coarse")
        except ValueError as e:
            shutil.rmtree(self.case_dir, ignore_errors=True)
            with self._lock:
                self.state = "failed"
                self.error = f"case geometry failed: {e}"
            return None
        polys = [p for p, _blunt in g["polys"]]
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None

        self._set_phase("writing the profile DXF")
        st = self.settings
        try:
            dxf = _dxf(polys, self.case_dir / "profile.dxf",
                       front_l=st["front_l"], back_l=st["back_l"],
                       top_h=st["top_h"])
        except (ValueError, RuntimeError) as e:
            with self._lock:
                self.state = "failed"
                self.error = f"2D case preparation failed: {e}"
            return None
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None

        # the clearances the inflation stack actually faces: the chain
        # caps the resolved layers where they physically cannot fit
        # (opposing fronts in a slot gap collapsed Mechanical's whole
        # generation to 0 elements when left uncapped)
        slot_gaps = [e.get("slot_gap_pct") for e in
                     self.config.get("elements", [])
                     if e.get("slot_gap_pct")]
        slot_gap_mm = (min(slot_gaps) / 100.0 * self.cfg.chord_m * 1000.0
                       if slot_gaps else None)
        ground_clear_mm = min(float(p[:, 1].min()) for p in polys) * 1000.0

        self._set_phase("meshing in ANSYS (SpaceClaim, then Workbench "
                        "Mechanical; typically several minutes)")
        try:
            # the chain polls cancel_evt and kills the running ANSYS
            # batch process tree — cancel lands mid-stage, not after it
            chain = _chain(dxf["dxf_path"], self.case_dir / "ansys2d",
                           edge_size_mm=st["edge_size_mm"],
                           first_layer_mm=st["first_layer_mm"],
                           n_layers=st["n_layers"], growth=st["growth"],
                           slot_gap_mm=slot_gap_mm,
                           ground_clear_mm=ground_clear_mm,
                           sc_budget_s=st["sc_budget_s"],
                           wb_budget_s=st["wb_budget_s"],
                           cancel_evt=self._cancel)
        except Exception as e:
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return None
            with self._lock:
                self.state = "failed"
                self.error = f"ANSYS 2D meshing failed: {e}"
            return None
        inflation = chain.get("inflation") or {}
        with self._lock:
            # the chain caps the inflation stack to the clearances it
            # actually faces, and drops it entirely on a degrade retry:
            # the resolved set must state what Mechanical BUILT, with the
            # request kept beside it — everything downstream (the
            # near-wall caveat, the result, the snapshot) reads it here
            if inflation:
                self.settings = dict(
                    self.settings,
                    first_layer_mm_requested=self.settings["first_layer_mm"],
                    n_layers_requested=self.settings["n_layers"],
                    first_layer_mm=inflation.get(
                        "first_layer_mm", self.settings["first_layer_mm"]),
                    n_layers=inflation.get("n_layers",
                                           self.settings["n_layers"]))
            self.mesh = {"n_cells": chain.get("n_cells"),
                         "mesh_size": self.mesh_size,
                         "mesher": "ansys-2d",
                         "zones": chain.get("zones"),
                         "stage_s": chain.get("stage_s"),
                         "inflation": chain.get("inflation")}
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None

        self._set_phase("starting ANSYS Fluent (license checkout)")
        fm = _mcp()
        fm.launch(dimension=2, precision="double",
                  processors=self.n_ranks)
        try:
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return fm
            fm.read_mesh(chain["msh_path"])
            self._set_phase("applying the manual walkthrough's recipe"
                            if self.conventions == "default"
                            else "applying the studio recipe")
            # the 2D chain's zone vocabulary, not the 3D slab's: the
            # shear-free ceiling is "upper_bound" (there is no "top")
            # and the moving belt is "ground" — same roles run_case's
            # dimension=2 arm passes
            setup = fm.setup_external_aero(
                inlet_velocity_ms=self.cfg.speed_ms,
                profile_zones=["profile"], chord_m=self.cfg.chord_m,
                depth_m=1.0,
                slip_zones=["upper_bound"], ground="ground",
                # the default conventions leave the material at Fluent's
                # own air, so the references must use that density too —
                # normalizing default-air forces by the config's rho
                # would fold a silent rho/1.225 factor into every
                # coefficient; studio sets the material to the config's
                # air, so there the config values ARE the solved fluid
                rho=self.ref_rho,
                mu=(FLUENT_MU if self.conventions == "default"
                    else self.cfg.rho * self.cfg.nu),
                # the only translation point between this module's
                # vocabulary and the session layer's: the 2D recipe is
                # "default-2d" there
                conventions=("default-2d" if self.conventions == "default"
                             else "studio"))
            if setup["failed"]:
                with self._lock:
                    self.state = "failed"
                    self.error = ("Fluent setup steps failed: "
                                  + "; ".join(f"{f['step']}: {f['error']}"
                                              for f in setup["failed"]))
                return fm

            self._set_phase("solving")
            done_iters = 0
            first = True
            cl_hist: list[float] = []
            cd_hist: list[float] = []
            stopped_by_drift = False
            residual_stop = False
            stop_hits = 0
            while done_iters < self.n_iters:
                if self._cancel.is_set():
                    with self._lock:
                        self.state = "cancelled"
                    return fm
                if time.time() - self.t_start > WALL_LIMIT_S:
                    # a blocking solve cannot be timed out from this
                    # thread — this boundary check catches the slow
                    # wedge; the hard one (a call that never returns)
                    # is cancel()'s job, which shoots the session
                    with self._lock:
                        self.state = "failed"
                        self.error = (
                            f"wall-clock backstop: still solving after "
                            f"{WALL_LIMIT_S / 3600:.0f} h — the session "
                            f"appears hung, so the run was stopped")
                    return fm
                chunk = min(FIRST_CHUNK if first else CHUNK_ITERS,
                            self.n_iters - done_iters)
                fm.solve(iterations=chunk, initialize=first)
                first = False
                hists = fm._report_histories()
                cl_hist = next((v for k, v in hists.items()
                                if "lift" in k.lower()), cl_hist)
                cd_hist = next((v for k, v in hists.items()
                                if "drag" in k.lower()), cd_hist)
                self._harvest_history(cl_hist, cd_hist)
                # the default conventions leave Fluent's residual
                # criteria active, so Fluent itself may end a chunk
                # early — but
                # the rfiles are read the instant solve() returns, and a
                # torn or not-yet-flushed last row reads short too. A
                # shortfall within the slack must survive one settled
                # re-read before it counts as Fluent's stop (a real stop
                # undershoots by far more than the slack); it is
                # reported as a residual stop, never dressed up as a
                # drift verdict
                if self.conventions == "default" and cl_hist:
                    short = done_iters + chunk - len(cl_hist)
                    if 0 < short <= RESIDUAL_SLACK_ROWS:
                        time.sleep(RESIDUAL_SETTLE_S)
                        hists = fm._report_histories()
                        cl_hist = next((v for k, v in hists.items()
                                        if "lift" in k.lower()), cl_hist)
                        cd_hist = next((v for k, v in hists.items()
                                        if "drag" in k.lower()), cd_hist)
                        self._harvest_history(cl_hist, cd_hist)
                        short = done_iters + chunk - len(cl_hist)
                    if short > 0:
                        residual_stop = True
                        done_iters = len(cl_hist)
                        break
                done_iters += chunk
                if self.conventions != "studio":
                    continue
                # studio conventions: force stop, same doctrine as the
                # other engines — Cl AND Cd drift under their bars (Cd
                # converges last — looser bar), three full
                # post-transient windows on the clock, and the combined
                # criterion holding on two consecutive chunk boundaries
                d = cfd_run.drift(cl_hist[DRIFT_SKIP:])
                d_cd = cfd_run.drift(cd_hist[DRIFT_SKIP:])
                if (d is not None and d < cfd_run.FORCE_STOP_CL_TOL
                        and d_cd is not None
                        and d_cd < cfd_run.FORCE_STOP_CD_TOL
                        and done_iters >= DRIFT_SKIP
                        + 3 * cfd_run.FORCE_STOP_WINDOW):
                    stop_hits += 1
                    if stop_hits >= 2:
                        stopped_by_drift = True
                        break
                else:
                    stop_hits = 0
            self._finalize(cl_hist, cd_hist, done_iters,
                           stopped_by_drift, residual_stop)
            # "done" is deferred until the exported field files exist:
            # the UI fetches the flow view the moment it sees done, and
            # a done-before-export order makes the first fetch race the
            # export (measured on the 3D engine, not hypothetical)
            if self.result is not None and self.state == "running":
                self._set_phase("exporting the flow field")
                try:
                    self._export_flow_fields(fm, done_iters)
                except Exception as e:
                    # the numbers stand; the views degrade with a stated
                    # reason instead of a silent blank
                    with self._lock:
                        self.result["engine_note"] += (
                            f" — flow-field export failed ({e}); "
                            f"flow view unavailable for this run")
                # ONLY NOW does the field exist. This engine exports no wall
                # shear, so wall_report has nothing to say and stays None;
                # the field report reading the just-written C/U/p is how the
                # ANSYS path gets a separation measurement at all. UNGRADED:
                # it separates the collapse from the healthy cases by a wide
                # margin, but the line that turns that into a verdict has to
                # be regressed against paired wall-shear runs first.
                try:
                    rec = foam_post.recirculation_report(
                        self.case_dir, self.cfg,
                        converged=bool((self.result or {}).get("converged")),
                        user_stopped=False, wall=None)
                except Exception:
                    rec = None
                with self._lock:
                    if self.result is not None:
                        self.result["recirc_report"] = rec
                # the calibration row is written here rather than at result
                # assembly so it carries the field measurement, not a None
                try:
                    cfd_run.append_harvest(cfd_run.harvest_row(
                        self.result or {}, self.config, "fluent2d",
                        self.mesh_size))
                except Exception:
                    pass
                try:
                    self._set_phase("writing case files")
                    # absolute stem: the case+data land in the RUN dir,
                    # where the ANSYS-export endpoint expects them
                    fm.write_case_data(str(self.case_dir / "case"))
                except Exception:
                    pass   # numbers stand; the artifact is best-effort
                with self._lock:
                    self.phase = None
                    self.state = "done"
            return fm
        except BaseException:
            # the license is held only while a job runs: run()'s finally
            # only sees a handle that was RETURNED, so any raise after
            # launch() must release the session here (shutdown is
            # idempotent — a hard cancel may already have shot it)
            try:
                fm.shutdown()
            except Exception:
                pass
            raise

    def _export_flow_fields(self, fm, n_run: int) -> None:
        """Solved cell-centre field -> OpenFOAM-format C/U/p in a time
        directory, so the studio's flow view and animation read this run
        through the exact same foam_post pipeline as every other engine.
        A 2D export carries no z column — z and w are written as zeros.
        Fluent's static pressure (Pa) is converted to the kinematic
        p/rho the Cp view expects, using the density the solve actually
        ran (Fluent's own air under the default conventions)."""
        csv = self.case_dir / "fluent_flow.csv"
        fm.export_ascii(filename=str(csv),
                        quantities=["x-velocity", "y-velocity",
                                    "pressure"],
                        location="cell-center")
        header: list[str] = []
        cols: list[list[float]] = []
        with open(csv, errors="replace") as fh:
            for ln in fh:
                parts = [p.strip() for p in ln.split(",")]
                if not header:
                    header = [p.lower() for p in parts]
                    cols = [[] for _ in parts]
                    continue
                if len(parts) != len(header):
                    continue
                try:
                    vals = [float(p) for p in parts]
                except ValueError:
                    continue
                for c, v in zip(cols, vals):
                    c.append(v)

        def col(name: str, required: bool = True) -> np.ndarray | None:
            for i, h in enumerate(header):
                if name in h:
                    return np.asarray(cols[i], float)
            if required:
                raise RuntimeError(f"export carries no '{name}' column "
                                   f"(header: {header})")
            return None

        x, y = col("x-coordinate"), col("y-coordinate")
        z = col("z-coordinate", required=False)
        if z is None:
            z = np.zeros_like(x)
        u, v = col("x-velocity"), col("y-velocity")
        p_kin = col("pressure") / self.ref_rho
        if len(x) < 100:
            raise RuntimeError(f"only {len(x)} exported points")

        tdir = self.case_dir / str(int(n_run))
        tdir.mkdir(parents=True, exist_ok=True)

        def foam_head(cls_, obj):
            return ("FoamFile\n{\n    version     2.0;\n"
                    "    format      ascii;\n"
                    f"    class       {cls_};\n"
                    f"    object      {obj};\n}}\n\n")

        def write_vec(path, ax, ay, az):
            body = "\n".join(f"({a:.8g} {b:.8g} {c:.8g})"
                             for a, b, c in zip(ax, ay, az))
            path.write_text(
                foam_head("volVectorField", path.name)
                + f"internalField   nonuniform List<vector> \n{len(ax)}\n"
                f"(\n{body}\n)\n;\n\nboundaryField\n{{\n}}\n",
                encoding="utf-8", newline="\n")

        def write_scalar(path, a):
            body = "\n".join(f"{val:.8g}" for val in a)
            path.write_text(
                foam_head("volScalarField", path.name)
                + f"internalField   nonuniform List<scalar> \n{len(a)}\n"
                f"(\n{body}\n)\n;\n\nboundaryField\n{{\n}}\n",
                encoding="utf-8", newline="\n")

        write_vec(tdir / "C", x, y, z)
        write_vec(tdir / "U", u, v, np.zeros_like(u))
        write_scalar(tdir / "p", p_kin)

    def _finalize(self, cl_hist, cd_hist, n_run, stopped_by_drift,
                  residual_stop):
        if not cl_hist:
            with self._lock:
                self.state = "failed"
                self.error = "Fluent produced no force history"
            return
        # stats and drift are measured on the histories AS FLUENT
        # EMITTED them; drift is scale/sign-invariant so the verdict is
        # identical in either reference. What those histories already
        # are depends on the conventions: default-2d monitors are raw
        # (Fluent defaults, ref area 1 m2, lift +up) and need the
        # chord/sign map; studio monitors are chord-referenced and
        # downforce-positive at the source and must NOT be converted
        # again
        cl_mean, cl_std, n_tail = cfd_run._tail_stats(cl_hist)
        cd_mean, cd_std, _ = cfd_run._tail_stats(cd_hist or [0.0])
        cl_drift = cfd_run.drift(cl_hist[DRIFT_SKIP:])
        cd_drift = cfd_run.drift(cd_hist[DRIFT_SKIP:]) if cd_hist else None
        c = self.cfg.chord_m
        if self.conventions == "default":
            clc_mean, clc_std = cl_chord(cl_mean, c), cl_std / c
            cdc_mean, cdc_std = cd_chord(cd_mean, c), cd_std / c
        else:
            clc_mean, clc_std = cl_mean, cl_std
            cdc_mean, cdc_std = cd_mean, cd_std
        history_flat = (cl_drift is not None
                        and cl_drift < cfd_run.CONVERGED_CL_TOL)
        converged = history_flat
        if self.conventions == "studio":
            stop_reason = (
                "force history converged" if stopped_by_drift and converged
                else "iteration cap reached" if not stopped_by_drift and
                cl_drift is not None
                else f"stopped after {n_run} iterations — too few to "
                f"verify the force history")
        else:
            stop_reason = (
                f"Fluent's residual criteria ended the run at {n_run} "
                f"iterations (left active by the manual walkthrough)"
                if residual_stop
                else "requested iterations completed"
                if cl_drift is not None
                else f"stopped after {n_run} iterations — too few to "
                f"verify the force history")

        panel = None
        panel_error = None
        suggestion = None
        try:
            r = analysis.analyze(self.cfg, include_geometry=False)
            co, fo = r["coefficients"], r["forces"]
            panel = {
                "c_est": co["C_downforce_estimated"],
                "c_free": co["C_downforce_inviscid_free"],
                "c_ground": co["C_downforce_inviscid_ground"],
                "cd_profile": co["CD_profile_stack"],
                # the drag comparison is only as honest as the polar
                # lookup behind it — carry the cap flag with the number
                "cd_profile_is_lower_bound": fo[
                    "drag_profile_is_lower_bound"],
                "cd_capped_roles": fo["drag_capped_roles"],
                # what the CHEAP screen predicted for this design, kept
                # beside the expensive measurement so a calibration row can
                # pair them without re-running the panel model
                "shadow_mins": [e.get("shadow_min") for e in r["elements"]],
                "k_g_used": co["k_ground_realization"],
                "downforce_n": fo["downforce_n"],
            }
            if converged:
                # the suggestion is offered under BOTH conventions — it
                # is always computed from the chord-referenced
                # downforce-positive value, never from the raw one
                suggestion = cfd_run.suggested_k_g(
                    clc_mean, panel["c_free"], panel["c_ground"],
                    self.cfg)
        except Exception as e:
            panel_error = f"{type(e).__name__}: {e}"

        # the near-wall caveat follows the inflation the mesh ACTUALLY
        # got (reconciled with the chain's cap/degrade), not the sizing
        # label: an override moves either recipe across the y+ ~ 1 line,
        # and a stack of ZERO layers leaves the first-layer height
        # meaningless — the wall is then unresolved whatever the recipe
        # asked for
        fl_mm = self.settings["first_layer_mm"]
        no_bl = self.settings["n_layers"] == 0
        try:
            wall_functions = (no_bl or fl_mm > WALL_FUNCTION_RATIO
                              * cfd.first_layer(self.cfg)[0] * 1000.0)
        except Exception:
            wall_functions = no_bl or self.mesh_size == "default"
        # the cap/degrade note the chain wrote is the only place a user
        # learns the stack was cut — state it on the card
        bl_note = ((self.mesh or {}).get("inflation") or {}).get("note")

        # chord-referenced on both sides: cdc_mean is the studio-convention
        # value, the same basis as the estimate's CD_profile_stack
        d_cd, d_cd_bound = cfd_run.delta_cd(cdc_mean, panel)

        q = self.cfg.q_pa
        area = self.cfg.chord_m * (self.cfg.span_mm / 1000.0)
        with self._lock:
            self.result = {
                "engine": "fluent2d",
                "cl_rans": round(clc_mean, 4),
                "cl_rans_std": round(clc_std, 4),
                "cd_rans": round(cdc_mean, 5),
                "cd_rans_std": round(cdc_std, 5),
                "cl_raw": (round(cl_mean, 4)
                           if self.conventions == "default" else None),
                "cd_raw": (round(cd_mean, 5)
                           if self.conventions == "default" else None),
                "cl_chord": round(clc_mean, 4),
                "cd_chord": round(cdc_mean, 5),
                "ref_note": (
                    "Raw coefficients follow Fluent's own convention: "
                    "its default force directions with reference "
                    "area 1 m2 per meter of depth. The solve and the "
                    "reference values both use Fluent's default air "
                    "(rho 1.225 kg/m3), not the configuration's."
                    if self.conventions == "default" else
                    "Coefficients are chord-referenced and downforce-"
                    "positive at the source (studio conventions) — "
                    "there are no separate raw values."),
                "conventions": self.conventions,
                "tail_rows": n_tail,
                "n_iters_run": n_run,
                "residual_stop": bool(residual_stop),
                "converged": converged,
                "stop_reason": stop_reason,
                "cl_drift": (round(cl_drift, 5)
                             if cl_drift is not None else None),
                "cd_drift": (round(cd_drift, 5)
                             if cd_drift is not None else None),
                "downforce_n_at_rans_cl": round(
                    q * area * clc_mean * self.cfg.efficiency_3d, 1),
                "panel": panel,
                "panel_error": panel_error,
                "delta_cl_pct": (round((clc_mean / panel["c_est"] - 1)
                                       * 100, 1)
                                 if panel and abs(panel["c_est"]) > 1e-9
                                 else None),
                "delta_cl_provisional": not converged,
                # NOT a separation signal: measured, an ATTACHED
                # validated baseline reads +626% and a separated case
                # +396%, so the classes overlap and the healthy design
                # reads highest (see cfd_run.delta_cd). It measures the
                # capped polar estimate's shortfall against a loaded
                # stack's real drag -- information about the estimate,
                # not about the flow.
                "delta_cd_pct": d_cd,
                "delta_cd_is_upper_bound": d_cd_bound,
                "cl_trend_note": (
                    None if converged else
                    f"history not flat at stop (drift "
                    f"{cl_drift:.4f}) — treat {clc_mean:.2f} as "
                    f"provisional" if cl_drift is not None else
                    f"only {len(cl_hist)} iterations of force history — "
                    f"too few to judge"),
                "wall_report": None,
                "wall_verdict": None,
                "sep_knife_edge": None,
                # filled in after the flow export lands, NOT here: this
                # runs BEFORE _export_flow_fields writes C/U/p, so reading
                # the field at result-assembly time finds nothing and
                # silently reports None — which is exactly what shipped
                # until a verification solve caught it
                "recirc_report": None,
                "engine_note": (("solved by ANSYS Fluent (2D, double "
                                 "precision) on the mesh from the "
                                 "documented manual ANSYS workflow — "
                                 "SpaceClaim fill, Mechanical edge "
                                 "sizing and inflation — with that "
                                 "workflow's solver defaults (Fluent's "
                                 "residual criteria active). The "
                                 "headline coefficient is chord-"
                                 "referenced and downforce-positive; "
                                 "the raw Fluent-convention values are "
                                 "shown alongside. "
                                 if self.conventions == "default" else
                                 "solved by ANSYS Fluent (2D, double "
                                 "precision) on the mesh from the "
                                 "documented manual ANSYS workflow "
                                 "under studio conventions (k-omega "
                                 "SST, studio inlet turbulence, "
                                 "residual auto-stop disabled, "
                                 "drift-based force stop). The "
                                 "coefficient is chord-referenced and "
                                 "downforce-positive at the source. ")
                                + "The flow "
                                "view and animation read the exported "
                                "Fluent field; wall-shear attachment "
                                "verdicts remain an OpenFOAM-engine "
                                "feature. This 2D mesh is not the "
                                "studio's calibration-grade preset — "
                                "treat a pinned k_g as a screening "
                                "value. "
                                # the two near-wall regimes are not
                                # comparable — the caution must state
                                # the resolution this run actually had
                                + ("This mesh carries no inflation "
                                   "layers, so the near-wall flow is "
                                   "unresolved and the wall is modeled "
                                   "with wall functions — coefficients "
                                   "are not directly comparable to "
                                   "resolved-wall runs (the y+ ~ 1 "
                                   "sizing or the OpenFOAM engine). "
                                   if no_bl else
                                   f"The {fl_mm:g} mm first layer puts "
                                   f"the first cell well above y+ 1, so "
                                   f"the wall is modeled with wall "
                                   f"functions — coefficients are not "
                                   f"directly comparable to resolved-"
                                   f"wall runs (the y+ ~ 1 sizing or "
                                   f"the OpenFOAM engine). "
                                   if wall_functions else "")
                                + (f"Mesh note: {bl_note}. "
                                   if bl_note else "")
                                + "The solved case+data "
                                "(case.cas.h5 / .dat.h5) are written "
                                "beside the run — Export case for "
                                "ANSYS copies them into the exports "
                                "folder for opening in Fluent"),
                "suggested_k_g": suggestion,
                # neither 2D sizing is the calibration-grade 3D preset:
                # a k_g pinned here is a screening value, always
                "mesh_caution": True,
                "mesher": "ansys-2d",
                "sizing": self.mesh_size,
                # the RESOLVED set, never the request: a finished run
                # states the numbers it actually ran on
                "settings": dict(self.settings),
                "n_ranks": self.n_ranks,
                "user_stopped": False,
                "case_dir": str(self.case_dir),
            }
            # state stays "running" — the caller flips to done once the
            # flow-field export has landed (see _run_inner), and the
            # recirculation report and the harvest row are written there
            # too, because both need the exported field

    # ---- API surface (mirrors FluentJob/RansJob) ----

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = ((self.t_end or time.time())
                       - (self.t_start or time.time()))
            if self.state in ("done", "failed", "cancelled"):
                progress = 1.0
            elif self.iteration:
                progress = min(0.97, 0.05 + 0.92 * self.iteration
                               / max(self.n_iters, 1))
            elif self.mesh is not None:
                progress = 0.05
            else:
                progress = 0.01
            return {
                "id": self.id, "state": self.state, "phase": self.phase,
                "error": self.error,
                "progress": round(progress, 3),
                "iteration": self.iteration, "n_iters": self.n_iters,
                "mesh_size": self.mesh_size, "n_ranks": self.n_ranks,
                "engine": "fluent2d", "mesher": "ansys-2d",
                "conventions": self.conventions,
                "settings": dict(self.settings),
                "elapsed_s": round(elapsed, 1),
                "latest": self.latest,
                "history": list(self.history),
                "mesh": self.mesh,
                "result": self.result if self.state == "done" else None,
                "case_dir": str(self.case_dir),
            }

    def cancel(self) -> None:
        """Cancel kills the run and keeps nothing — and it must LAND:
        the runner only polls the flag between stages/chunks, and a
        blocking Fluent call can hold it for many minutes. Setting the
        flag makes the meshing chain kill its ANSYS batch process tree
        (run_chain polls the same event); shutting the live session down
        makes a blocked gRPC call raise within seconds; the runner's
        exception handler reads the flag and records 'cancelled', never
        'failed'. Idempotent on every path (a job that has not launched
        Fluent yet just sees 'no session')."""
        self._cancel.set()
        if self.state == "running":
            try:
                _mcp().shutdown()
            except Exception:
                pass

    def stop_graceful(self) -> bool:
        """The 2D engine solves in short chunks, so cancel already lands
        within seconds; a separate keep-fields stop is not meaningful
        here (fields are only written at the end)."""
        return False
