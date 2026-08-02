"""In-app ANSYS Fluent verification runs — the RANS tab's second engine.

Mirrors cfd_run.RansJob's surface (state machine, snapshot, cancel,
result keys) so the existing verify tab renders a Fluent run unchanged:
live convergence chart, honest verdicts, k_g suggestion from a converged
run. Per the 2026-07 reference flip (DECISIONS.md), Fluent is the
reference of record for absolute coefficient levels; this engine makes
it reachable from the same one-click workflow as the screening engine.

The pipeline, from the mesher on: a headless Fluent session driven
through the same scripts/fluent_mcp helpers the MCP server uses -> the
"studio" conventions (chord-referenced, downforce-positive, residual
auto-stop disabled) -> chunked iteration with the studio's three-window
drift criterion deciding convergence, same doctrine as the OpenFOAM
runner. Requires a licensed local Fluent (2024 R2+); the license is held
only while a job runs.

Two meshers feed it. mesher="fluent" (default): ANSYS Fluent Meshing
cuts the cells from a studio-built STL slab of the identical section
geometry (cfd.section_geometry) — everything ANSYS, no Docker anywhere
in the chain. mesher="gmsh": the studio's own gmsh mesh (identical to
what the OpenFOAM engine solves) -> foamMeshToFluent in the ESI
container (Docker required for that conversion step) — the option to
keep when the point is comparing engines on an IDENTICAL mesh.

The flow view and the animated view work on Fluent runs: the solved
cell-centre field is exported from the live session and written as
OpenFOAM-format C/U/p files in a time directory beside the case
(pressure converted to the kinematic convention the Cp view expects),
so foam_post reads a Fluent run exactly like an OpenFOAM one — one
rendering pipeline, no divergence. Wall-shear attachment verdicts
remain OpenFOAM-engine features for now; the result says so. One job
at a time across BOTH engines: Fluent jobs register in cfd_run's
registry, so the existing guard covers them.

Cancel is polled at every step boundary of the pipeline and lands
mid-step by shooting the live session; a cancel that arrives inside a
step that has no session to shoot yet (the mesher's own launch, the
Docker bridge conversion) lands only when that step returns.
"""
from __future__ import annotations

import copy
import threading
import time
import traceback
import uuid
from pathlib import Path

import numpy as np

from . import analysis, cfd, cfd_run
from .geometry import StackConfig

# solve in chunks so the chart updates and the drift criterion is
# evaluated on genuinely new history between chunks. The first chunk is
# deliberately small — first datapoints on screen in minutes, not an
# hour — and big native meshes get shorter chunks (a 250-iteration
# chunk at 651k cells on one core is ~an hour of radio silence)
CHUNK_ITERS = 250
FIRST_CHUNK = 60
BIG_MESH_CELLS = 400_000
BIG_MESH_CHUNK = 100
DRIFT_SKIP = 200          # hybrid-init transient excluded from decisions
MAX_PROCESSORS = 32
WALL_LIMIT_S = 12 * 3600  # absolute backstop, checked at chunk
                          # boundaries — generous because one big-mesh
                          # chunk can legitimately run an hour; a session
                          # still going past this is wedged, not solving


def _mcp():
    """The session-facing Fluent helpers (lazy: pyfluent and the repo's
    scripts package load only when a Fluent job actually starts).
    Module-level seam so the offline suite can fake the whole engine."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent_mcp
    return fluent_mcp


def _bridge(msh_path: Path, work_dir: Path) -> str:
    """gmsh mesh.msh -> Fluent case.msh (containerized conversion).
    Seam for tests."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent_workflow
    return fluent_workflow.gmsh_to_fluent(msh_path, work_dir)


def _slab_stl(polys, out_path: Path, domain_m, dz: float,
              zone_names, plane_size_m: float | None = None) -> dict:
    """The native route's geometry artifact (fluent_workflow.
    write_slab_stl through the scripts package). Seam for tests.
    The plane tessellation inside runs gmsh — global C state that also
    clobbers the real Win32 PATH — so this serializes on the same lock
    and does the same PATH save/restore as every other gmsh use."""
    import sys
    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from scripts import fluent_workflow
    with cfd._gmsh_lock:
        saved = cfd._real_env_path()
        try:
            return fluent_workflow.write_slab_stl(
                polys, out_path, domain_m, dz, zone_names,
                plane_size_m=plane_size_m)
        finally:
            cfd._restore_env_path(saved)


def availability() -> dict:
    """Is a licensed Fluent reachable from this machine? Cheap checks
    only (env roots + importability); the definitive test is launching."""
    import importlib.util
    import os
    roots = {k: v for k, v in os.environ.items()
             if k.startswith("AWP_ROOT")}
    has_pyfluent = importlib.util.find_spec("ansys.fluent.core") is not None
    return {"available": bool(roots) and has_pyfluent,
            "awp_roots": roots, "pyfluent": has_pyfluent,
            "detail": ("" if roots and has_pyfluent else
                       "needs a local ANSYS installation (AWP_ROOT* "
                       "environment variable) and the ansys-fluent-core "
                       "package in the app's Python environment")}


def export_native_case(config: dict, mesh_size: str,
                       exports_dir: Path) -> dict:
    """The Export tab's ANSYS Fluent artifact: geometry (slab.stl) +
    the ANSYS-native mesh (native.msh.h5, cut by Fluent Meshing AT
    EXPORT TIME — holds a license for the few minutes it runs) + a
    conventions README + config.json, under exports/fluent_mesh_*.
    Zones arrive in Fluent pre-typed (inlet/outlet/symmetry by name),
    so File > Read > Mesh gives a setup-ready case; the README lists
    the studio recipe step by step. No solve happens here — a FINISHED
    Fluent verify run's 'Export case for ANSYS' is the artifact that
    carries solved case+data."""
    import json
    import shutil

    cfg = StackConfig.from_dict(config)
    g = cfd.section_geometry(cfg, mesh_size)   # raises ValueError early
    exports_dir = Path(exports_dir)
    exports_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = exports_dir / f"fluent_mesh_{stamp}"
    k = 2
    while True:   # mkdir is the claim (same guard as the case exports)
        try:
            dest.mkdir()
            break
        except FileExistsError:
            dest = exports_dir / f"fluent_mesh_{stamp}-{k}"
            k += 1
    try:
        stl = _slab_stl([p for p, _b in g["polys"]], dest / "slab.stl",
                        g["domain_m"], g["dz_m"], g["wings"],
                        plane_size_m=g["far_size_m"])
        fm = _mcp()
        mn = fm.mesh_native(stl["stl_path"],
                            edge_size_m=g["wall_size_m"],
                            far_size_m=g["far_size_m"],
                            first_layer_m=g["first_layer_m"],
                            n_layers=g["n_bl_layers"],
                            growth=g["bl_growth"],
                            cells_per_gap=g["slot_gap_cells"],
                            wall_zones=g["wings"], processors=4)
        # the meshing session ran with cwd=dest — sweep its scratch
        # (checkpoint dirs, transcripts, cleanup scripts) so the export
        # folder holds the deliverables, not the session's droppings.
        # The Fluent process is still flushing its transcript for a few
        # seconds after the session returns — a locked file must never
        # fail an export whose mesh already landed (measured: it did)
        for p in dest.iterdir():
            if (p.name.startswith("FM_") or p.suffix == ".trn"
                    or p.name == "native_workflow_files"
                    or (p.name.startswith("cleanup-fluent")
                        and p.suffix == ".bat")):
                for attempt in range(6):
                    try:
                        (shutil.rmtree if p.is_dir()
                         else Path.unlink)(p)
                        break
                    except OSError:
                        if attempt == 5:
                            break   # cosmetic leftover, not a failure
                        time.sleep(1.0)
        (dest / "config.json").write_text(
            json.dumps(config, indent=1), encoding="utf-8")
        n_cells = mn.get("n_cells")
        failed = mn.get("failed") or []
        cells_txt = (str(n_cells) if n_cells is not None
                     else "see Fluent mesh info")
        (dest / "README.txt").write_text(
            "Wing Section Studio - ANSYS Fluent native mesh export\n"
            "=====================================================\n\n"
            "Open in Fluent (3D, double precision): File > Read > "
            "Mesh,\npick native.msh.h5. Boundary zones arrive already "
            "typed by\nname (velocity-inlet 'inlet', pressure-outlet "
            "'outlet',\n'symmetry-front/back'; profile walls "
            f"{', '.join(g['wings'])};\n'ground' and 'top' as walls). "
            "slab.stl is the geometry the\nmesh was cut from "
            "(watertight named-solid slab).\n\n"
            "Mesh provenance (ANSYS Fluent Meshing, watertight "
            "workflow):\n"
            f"  - preset {mesh_size}: wall size "
            f"{g['wall_size_m'] * 1e3:.3g} mm, far "
            f"{g['far_size_m'] * 1e3:.3g} mm\n"
            f"  - prisms: {g['n_bl_layers']} layers from "
            f"{g['first_layer_m'] * 1e3:.4g} mm x "
            f"{g['bl_growth']:g}\n"
            f"  - slot/gap protection: {g['slot_gap_cells']} cells per "
            "gap (edge-scoped proximity)\n"
            f"  - cells: {cells_txt}\n"
            + (f"  - degraded steps: "
               f"{'; '.join(f['step'] for f in failed)}\n"
               if failed else "")
            + "\nStudio recipe to reproduce the app's solve "
            "(conventions):\n"
            f"  - air: rho {cfg.rho:g} kg/m3, mu "
            f"{cfg.rho * cfg.nu:g} kg/m-s (constant)\n"
            "  - viscous: k-omega SST\n"
            f"  - inlet velocity {cfg.speed_ms:g} m/s; outlet 0 Pa\n"
            f"  - inlet turbulence: {cfd.TURB_INTENSITY * 100:g}% "
            f"intensity, viscosity ratio {cfd.TURB_VISC_RATIO:g} "
            "(matches the\n    OpenFOAM engine's inlet)\n"
            f"  - ground: moving wall, {cfg.speed_ms:g} m/s +x; top: "
            "specified shear (0)\n"
            f"  - reference values: area = chord x depth = "
            f"{cfg.chord_m:g} x {g['dz_m']:g} m2, length "
            f"{cfg.chord_m:g} m\n"
            "  - lift monitor force vector (0,-1,0): reported Cl is "
            "DOWNFORCE-POSITIVE\n"
            "  - disable the residual convergence criteria; judge "
            "convergence on the\n    force history (the studio uses a "
            "3-window drift criterion)\n\n"
            "Or skip the manual setup entirely: the RANS verify tab's "
            "ANSYS Fluent\nengine runs this recipe end to end, and a "
            "finished run's 'Export case\nfor ANSYS' carries the "
            "solved case.cas.h5/.dat.h5.\n"
            f"\nExported {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
            encoding="utf-8")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    files = sorted(p.name for p in dest.iterdir())
    return {"filename": dest.name, "path": str(dest),
            "dir": str(exports_dir), "files": files,
            "n_cells": n_cells,
            "quality": mn.get("quality"),
            "mesh_failed_steps": failed or None}


class FluentJob:
    """One Fluent verification run; duck-type-compatible with RansJob
    everywhere the registry, API and UI touch a job."""

    def __init__(self, config: dict, mesh_size: str = "coarse",
                 n_iters: int = cfd.N_ITERS, n_ranks: int = 1,
                 mesher: str = "fluent"):
        if mesh_size not in cfd.MESH_PRESETS:
            raise ValueError(
                f"mesh_size must be one of {sorted(cfd.MESH_PRESETS)}")
        if mesher not in ("fluent", "gmsh"):
            raise ValueError("mesher must be 'fluent' (native ANSYS "
                             "meshing) or 'gmsh' (identical mesh to "
                             "the OpenFOAM engine)")
        self.mesher = mesher
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(config)
        self.cfg = StackConfig.from_dict(config)
        self.mesh_size = mesh_size
        self.n_iters = int(n_iters)
        if not (100 <= self.n_iters <= 20_000):
            raise ValueError("max_iters must be between 100 and 20000")
        # the tab's "Solver cores" select: MPI ranks for OpenFOAM,
        # processor count here — one knob, same meaning
        self.n_ranks = int(n_ranks)
        if not (1 <= self.n_ranks <= MAX_PROCESSORS):
            raise ValueError(f"n_ranks must be between 1 and "
                             f"{MAX_PROCESSORS}")
        self.state = "pending"
        self.phase: str | None = None
        self.error: str | None = None
        self.iteration = 0
        self.latest: dict | None = None
        self.history: list[dict] = []
        self.mesh: dict | None = None
        self.result: dict | None = None
        self.case_dir = cfd_run._runs_dir() / self.id
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
                    # cancel() kills the live session so a blocking
                    # meshing/solve call lands within seconds — the
                    # resulting exception IS the cancellation, not a
                    # failure
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
        # ceiling division: floor overshoots the ~300-point target
        # (n=1310 -> step 4 -> 329 points) — the chart budget is a cap
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
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None
        self.case_dir.mkdir(parents=True, exist_ok=True)
        (self.case_dir / "config.json").write_text(
            json.dumps(self.config, indent=1), encoding="utf-8")
        depth_m = cfd.DZ_C * self.cfg.chord_m
        wings = [f"wing_e{i+1}" for i in range(len(self.cfg.elements))]
        if self.mesher == "gmsh":
            self._set_phase("meshing (studio gmsh)")
            try:
                mesh = cfd.build_case(self.cfg, self.case_dir,
                                      self.mesh_size, self.n_iters)
                with self._lock:
                    self.mesh = {**mesh, "mesher": "gmsh-bridge"}
            except (ValueError, cfd.MeshError) as e:
                import shutil
                shutil.rmtree(self.case_dir, ignore_errors=True)
                with self._lock:
                    self.state = "failed"
                    self.error = f"case generation failed: {e}"
                return None
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return None
            self._set_phase("converting mesh for Fluent (Docker)")
            try:
                fluent_msh = _bridge(self.case_dir / "mesh.msh",
                                     self.case_dir / "fluent_bridge")
            except Exception as e:
                with self._lock:
                    self.state = "failed"
                    self.error = (f"mesh conversion failed (Docker "
                                  f"Desktop running?): {e}")
                return None
        else:
            # native route: identical section geometry, ANSYS cuts the
            # cells — no Docker anywhere in this chain
            self._set_phase("building geometry for ANSYS meshing")
            try:
                g = cfd.section_geometry(self.cfg, self.mesh_size)
                stl = _slab_stl(
                    [p for p, _blunt in g["polys"]],
                    self.case_dir / "fluent_native" / "slab.stl",
                    g["domain_m"], g["dz_m"], g["wings"],
                    plane_size_m=g["far_size_m"])
            except ValueError as e:
                import shutil
                shutil.rmtree(self.case_dir, ignore_errors=True)
                with self._lock:
                    self.state = "failed"
                    self.error = f"case geometry failed: {e}"
                return None
            depth_m = g["dz_m"]
            wings = g["wings"]
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return None
            self._set_phase("meshing in ANSYS Fluent Meshing (holds "
                            "the license; typically a few minutes)")
            fm_mesh = _mcp()
            mn = fm_mesh.mesh_native(
                stl["stl_path"], edge_size_m=g["wall_size_m"],
                far_size_m=g["far_size_m"],
                first_layer_m=g["first_layer_m"],
                n_layers=g["n_bl_layers"], growth=g["bl_growth"],
                cells_per_gap=g["slot_gap_cells"],
                wall_zones=g["wings"], processors=self.n_ranks)
            bl_ok = not any("boundary layers" in (f.get("step") or "")
                            for f in (mn.get("failed") or []))
            with self._lock:
                self.mesh = {"n_cells": mn.get("n_cells"),
                             "mesh_size": self.mesh_size,
                             "mesher": "fluent-meshing",
                             "bl_mode": ("native-prisms" if bl_ok
                                         else None),
                             "quality": mn.get("quality"),
                             "mesh_failed_steps":
                                 (mn.get("failed") or None)}
            fluent_msh = mn["msh_path"]
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return None

        self._set_phase("starting ANSYS Fluent (license checkout)")
        fm = _mcp()
        fm.launch(dimension=3, precision="double",
                  processors=self.n_ranks)
        try:
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return fm
            fm.read_mesh(fluent_msh)
            if not (self.mesh or {}).get("n_cells"):
                # the meshing transcript did not yield a count — ask
                # the solver (mesh/size-info prints "Level Cells ...")
                try:
                    import re as _re
                    info = fm.tui("mesh/size-info")
                    m_sz = _re.search(r"^\s*0\s+(\d+)", str(info),
                                      _re.M)
                    if m_sz:
                        with self._lock:
                            self.mesh = {**(self.mesh or {}),
                                         "n_cells": int(m_sz.group(1))}
                except Exception:
                    pass   # cosmetic — the run does not depend on it
            self._set_phase("applying the studio recipe")
            setup = fm.setup_external_aero(
                inlet_velocity_ms=self.cfg.speed_ms,
                profile_zones=wings, chord_m=self.cfg.chord_m,
                depth_m=depth_m,
                rho=self.cfg.rho, mu=self.cfg.rho * self.cfg.nu,
                conventions="studio")
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
            stop_hits = 0
            n_cells = (self.mesh or {}).get("n_cells") or 0
            steady_chunk = (BIG_MESH_CHUNK if n_cells > BIG_MESH_CELLS
                            else CHUNK_ITERS)
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
                chunk = min(FIRST_CHUNK if first else steady_chunk,
                            self.n_iters - done_iters)
                fm.solve(iterations=chunk, initialize=first)
                first = False
                done_iters += chunk
                hists = fm._report_histories()
                cl_hist = next((v for k, v in hists.items()
                                if "lift" in k.lower()), cl_hist)
                cd_hist = next((v for k, v in hists.items()
                                if "drag" in k.lower()), cd_hist)
                self._harvest_history(cl_hist, cd_hist)
                # force stop, same doctrine as the OpenFOAM runner: Cl
                # AND Cd drift under their bars (Cd converges last —
                # looser bar), three full post-transient windows on the
                # clock, and the combined criterion holding on two
                # consecutive chunk boundaries
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
                           stopped_by_drift)
            # "done" is deferred until the exported field files exist:
            # the UI fetches the flow view the moment it sees done, and
            # a done-before-export order made the first fetch race the
            # export and 422 (the view then worked only on a later
            # retry — measured, not hypothetical)
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
                try:
                    self._set_phase("writing case files")
                    # absolute stem: the case+data land in the RUN dir
                    # (not the transient Fluent session dir), where the
                    # ANSYS-export endpoint expects to find them
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
        through the exact same foam_post pipeline as an OpenFOAM one.
        Fluent's static pressure (Pa) is converted to the kinematic
        p/rho the Cp view expects."""
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

        def col(name: str) -> np.ndarray:
            for i, h in enumerate(header):
                if name in h:
                    return np.asarray(cols[i], float)
            raise RuntimeError(f"export carries no '{name}' column "
                               f"(header: {header})")

        x, y, z = col("x-coordinate"), col("y-coordinate"), \
            col("z-coordinate")
        u, v = col("x-velocity"), col("y-velocity")
        p_kin = col("pressure") / self.cfg.rho
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

    def _finalize(self, cl_hist, cd_hist, n_run, stopped_by_drift):
        if not cl_hist:
            with self._lock:
                self.state = "failed"
                self.error = "Fluent produced no force history"
            return
        cl_mean, cl_std, n_tail = cfd_run._tail_stats(cl_hist)
        cd_mean, cd_std, _ = cfd_run._tail_stats(cd_hist or [0.0])
        cl_drift = cfd_run.drift(cl_hist[DRIFT_SKIP:])
        cd_drift = cfd_run.drift(cd_hist[DRIFT_SKIP:]) if cd_hist else None
        history_flat = (cl_drift is not None
                        and cl_drift < cfd_run.CONVERGED_CL_TOL)
        converged = history_flat
        # no false-plateau branch here: unlike the OpenFOAM solver, no
        # rows are appended after the loop breaks, so a drift stop
        # (< FORCE_STOP_CL_TOL) always rereads flat (< CONVERGED_CL_TOL)
        stop_reason = (
            "force history converged" if stopped_by_drift and converged
            else "iteration cap reached" if not stopped_by_drift and
            cl_drift is not None
            else f"stopped after {n_run} iterations — too few to verify "
            f"the force history")

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
                suggestion = cfd_run.suggested_k_g(
                    cl_mean, panel["c_free"], panel["c_ground"], self.cfg)
        except Exception as e:
            panel_error = f"{type(e).__name__}: {e}"

        d_cd, d_cd_bound = cfd_run.delta_cd(cd_mean, panel)

        q = self.cfg.q_pa
        area = self.cfg.chord_m * (self.cfg.span_mm / 1000.0)
        with self._lock:
            self.result = {
                "engine": "fluent",
                "cl_rans": round(cl_mean, 4),
                "cl_rans_std": round(cl_std, 4),
                "cd_rans": round(cd_mean, 5),
                "cd_rans_std": round(cd_std, 5),
                "tail_rows": n_tail,
                "n_iters_run": n_run,
                "residual_stop": False,
                "converged": converged,
                "stop_reason": stop_reason,
                "cl_drift": (round(cl_drift, 5)
                             if cl_drift is not None else None),
                "cd_drift": (round(cd_drift, 5)
                             if cd_drift is not None else None),
                "downforce_n_at_rans_cl": round(
                    q * area * cl_mean * self.cfg.efficiency_3d, 1),
                "panel": panel,
                "panel_error": panel_error,
                "delta_cl_pct": (round((cl_mean / panel["c_est"] - 1)
                                       * 100, 1)
                                 if panel and abs(panel["c_est"]) > 1e-9
                                 else None),
                "delta_cl_provisional": not converged,
                # drag carries the separation signal far more loudly than
                # lift does; an upper-bound flag rides with it because a
                # capped polar lookup understates the estimate's drag
                "delta_cd_pct": d_cd,
                "delta_cd_is_upper_bound": d_cd_bound,
                "cl_trend_note": (
                    None if converged else
                    f"history not flat at stop (drift "
                    f"{cl_drift:.4f}) — treat {cl_mean:.2f} as "
                    f"provisional" if cl_drift is not None else
                    f"only {len(cl_hist)} iterations of force history — "
                    f"too few to judge"),
                "wall_report": None,
                "wall_verdict": None,
                "sep_knife_edge": None,
                "engine_note": (("solved by ANSYS Fluent on an "
                                 "ANSYS-native mesh (Fluent Meshing "
                                 "watertight workflow; studio "
                                 "conventions; reference of record for "
                                 "absolute levels — note the cell "
                                 "count is not the studio preset's). "
                                 if self.mesher == "fluent" else
                                 "solved by ANSYS Fluent (studio "
                                 "conventions; reference of record for "
                                 "absolute levels) on the studio's "
                                 "gmsh mesh — identical to what the "
                                 "OpenFOAM engine solves. ")
                                + "The flow view and animation read "
                                "the exported Fluent field; wall-shear "
                                "attachment verdicts remain an "
                                "OpenFOAM-engine feature. The solved "
                                "case+data (case.cas.h5 / .dat.h5) are "
                                "written beside the run — Export case "
                                "for ANSYS below copies them into "
                                "exports/ for opening in Fluent"),
                "suggested_k_g": suggestion,
                "mesh_caution": cfd_run.mesh_below_calibration_grade(
                    self.mesh_size),
                "mesher": self.mesher,
                "n_ranks": self.n_ranks,
                "user_stopped": False,
                "case_dir": str(self.case_dir),
            }
            # state stays "running" — the caller flips to done once the
            # flow-field export has landed (see _run_inner)
        # a calibration row per finished solve, outliving its case dir
        cfd_run.append_harvest(cfd_run.harvest_row(
            self.result, self.config, "fluent", self.mesh_size))

    # ---- API surface (mirrors RansJob) ----

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
                "engine": "fluent", "mesher": self.mesher,
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
        blocking Fluent call (native meshing, or one solve chunk on a
        large mesh) can hold it for many minutes. Shutting the live
        session down makes the blocked gRPC call raise within seconds;
        the runner's exception handler reads the flag and records
        'cancelled', never 'failed'. Idempotent on every path (a job
        that has not launched Fluent yet just sees 'no session')."""
        self._cancel.set()
        if self.state == "running":
            try:
                _mcp().shutdown()
            except Exception:
                pass

    def stop_graceful(self) -> bool:
        """The Fluent engine solves in short chunks, so cancel already
        lands within seconds; a separate keep-fields stop is not
        meaningful here (fields are only written at the end)."""
        return False
