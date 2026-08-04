"""ANSYS Fluent MCP server — drives PyFluent for MCP-capable clients.

Wraps a single live Fluent solver session (ansys-fluent-core gRPC) behind
MCP tools so an agent can mesh-import, set up, solve and interrogate 2D
wing-section cases without the Workbench GUI. The setup tool applies the
documented manual ANSYS workflow's 2D external-aero recipe (velocity
inlet, 0 Pa pressure outlet, no-slip profile, shear-free upper bound,
optional moving ground at the inlet speed, hybrid initialization) with
two deliberate upgrades over the GUI walkthrough, both reported in the
tool output:

- coefficients are CHORD-REFERENCED: reference area = chord x depth and
  reference length = chord are set explicitly (the GUI default leaves
  area at 1 m^2, so coefficients from the manual recipe are not
  chord-normalized), and
- lift is reported DOWNFORCE-POSITIVE (force vector (0,-1,0)) to match
  the studio's Cl convention; pass downforce_positive=False for the
  textbook direction.

Meshes, two routes. Default ("fluent"): ANSYS Fluent Meshing cuts the
cells itself — mesh_native() runs the watertight geometry workflow on a
studio-built STL slab (write_slab_stl), all-ANSYS with no Docker in the
chain. Option ("gmsh"): the studio's gmsh mesh arrives through
convert_openfoam_mesh() — foamMeshToFluent (run in the same ESI Docker
image the RANS tab uses) converts a case's constant/polyMesh into a
Fluent .msh, with the app's `frontAndBack` empty patches retyped to
symmetry — the route for cross-checking the two engines on an
IDENTICAL mesh. Either way the one-cell/thin slab solves in
Fluent 3D as the walkthrough's 2D case (reference depth = the slab
extrusion). True-2D route: mesh_2d() runs the manual walkthrough's
Workbench chain (SpaceClaim fill -> Mechanical mesh, via
fluent2d_workflow.run_chain) for a real 2D FFF.msh that loads through
read_mesh() into a launch(dimension=2) session;
setup_external_aero(conventions="default-2d") then reproduces that 2D
recipe with the reference values set explicitly (area 1 m^2, length
1 m) so the outcome is deterministic,
and run_case({"dimension": 2, ...}) drives the whole 2D chain.

Licensing: launch() checks out an ANSYS license on this machine and
holds it until shutdown()/server exit — atexit releases it. Per the
2026-07-29 reference flip (DECISIONS.md), Fluent owns absolute
coefficient levels; the in-app OpenFOAM pipeline keeps the
screening/ranking role (orderings, wake-shadow, queue demotion).

Registration lives in a machine-local, gitignored .mcp.json — tooling
configuration stays on the workstation, never in the published repo. Run
standalone for a protocol check:
.venv\\Scripts\\python.exe scripts\\fluent_mcp.py
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # SDK 1.x layout
    from mcp.server.fastmcp import FastMCP as _Server

REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "app_data" / "fluent"
DEFAULT_VERSION = os.environ.get("WSS_FLUENT_VERSION", "26.1.0")
OPENFOAM_IMAGE = os.environ.get("WSS_OPENFOAM_IMAGE",
                                "opencfd/openfoam-run:2406")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

mcp = _Server(
    "fluent",
    instructions=(
        "ANSYS Fluent solver control for Wing Section Studio. "
        "run_case({geometry, mesh, physics, conventions, solve, launch}) "
        "is the whole workflow in one call — a walkthrough-style DXF, a "
        "studio stack config, or a pre-made .msh in; coefficients with "
        "a drift verdict out; every stage's knobs in the spec. Meshing "
        "is ANSYS-native by default (mesh_native — Fluent Meshing's "
        "watertight workflow on a studio STL slab, no Docker); "
        "mesher='gmsh' keeps the identical-mesh cross-check route; "
        "spec dimension=2 routes through mesh_2d — the documented "
        "manual ANSYS workflow's true-2D Workbench chain — with "
        "conventions='default-2d'. "
        "Granular tools (status/launch/mesh_native/mesh_from_dxf/"
        "mesh_from_app_config/mesh_2d/convert_openfoam_mesh/read_mesh/"
        "setup_external_aero/solve/report_forces) compose the same "
        "chain stepwise. Results are "
        "chord-referenced and downforce-positive with Fluent's residual "
        "auto-stop disabled under conventions='studio' (default); "
        "'walkthrough' reproduces the manual GUI workflow for parity "
        "measurement only; 'default-2d' is that workflow's 2D recipe "
        "with deterministic explicit references. Treat a drifting tail "
        "as a "
        "bound, not a "
        "result. launch() holds an ANSYS license until shutdown(). "
        "tui()/scheme() are raw escape hatches when no typed tool "
        "fits."),
)

_solver = None
_dim = 3
_session_dir: Path | None = None
_launch_params: dict = {}
_mesher = None      # short-lived Fluent Meshing session (mesh_native)
_MESH_CLAIM = object()   # _mesher value while a mesher is still launching
# guards the _mesher/_solver hand-off (mesh_native's claim, launch's
# replace, shutdown's release): a concurrent caller must never exit a
# session another workflow is driving mid-flight
_lifecycle = threading.Lock()


# ---------- helpers ----------

_gmsh_lock_local = threading.Lock()


def _real_path_local() -> str | None:
    # gmsh's C runtime replaces the real Win32 process PATH wholesale
    # during initialize/finalize without touching os.environ (measured
    # in app/core/cfd.py) — snapshot the live environment directly
    if sys.platform != "win32":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetEnvironmentVariableW("PATH", buf, 32768)
    return buf.value if n else None


def _restore_path_local(path: str | None) -> None:
    if path is None or sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.kernel32.SetEnvironmentVariableW("PATH", path)


def _gmsh_fence():
    """(lock, save_path, restore_path) for a gmsh call — the contract
    fluent_workflow._plane_triangulation assigns to its callers: gmsh
    serialization (global C state) plus the Win32 PATH save/restore.
    The app's shared lock/helpers are preferred so in-process callers
    serialize on ONE lock; the local equivalents keep the standalone
    MCP server working without the app import chain."""
    try:
        sys.path.insert(0, str(REPO))
        from app.core import cfd
        return cfd._gmsh_lock, cfd._real_env_path, cfd._restore_env_path
    except Exception:
        return _gmsh_lock_local, _real_path_local, _restore_path_local

def _require() -> object:
    if _solver is None:
        raise RuntimeError("no live Fluent session — call launch() first")
    return _solver


def _fence(applied: list, failed: list, step: str, fn) -> None:
    """Apply one setup step; a failure is recorded, never silent."""
    try:
        fn()
        applied.append(step)
    except Exception as e:
        failed.append({"step": step, "error": f"{type(e).__name__}: {e}"})


def _zones() -> dict:
    bc = _require().settings.setup.boundary_conditions
    out = {}
    for t in ("velocity_inlet", "pressure_outlet", "pressure_inlet",
              "wall", "symmetry", "pressure_far_field", "interior"):
        try:
            names = list(getattr(bc, t).keys())
        except Exception:
            names = []
        if names:
            out[t] = names
    return out


def _disable_residual_stop(st) -> None:
    """Turn OFF Fluent's per-equation residual convergence criteria (the
    1e-3 defaults) — measured on the bridge case, they declared "solution
    is converged" at iteration 277 while the lift history was still
    trending at ~15x the studio's drift bar. Same doctrine as the app's
    OpenFOAM runner: residual thresholds are a backstop, never a verdict;
    the iteration budget and the force-history drift own convergence."""
    try:
        eqs = st.solution.monitor.residual.equations
        for name in list(eqs.keys()):
            eqs[name].check_convergence = False
        return
    except Exception:
        pass
    n_eq = 6 if _dim == 3 else 5   # continuity + velocities + k + omega
    _require().scheme.eval(
        '(ti-menu-load-string "solve/monitors/residual/check-convergence? '
        + "no " * n_eq + '")')


# Convergence-verdict numbers, copied LITERALLY from the studio's
# engines (scripts/ must import without app/, so no cross-import):
# window and minimum from app/core/cfd_run.drift (FORCE_STOP_WINDOW=800,
# 100 rows/window floor), the transient exclusion from app/core/
# fluent_run.DRIFT_SKIP (hybrid-init startup), the bar from
# cfd_run.CONVERGED_CL_TOL. A shorter window read slow trends as flat.
_DRIFT_WINDOW = 800
_DRIFT_MIN_ROWS = 100
_DRIFT_SKIP = 200
_CONVERGED_CL_TOL = 0.006


def _drift(vals: list[float], window: int = _DRIFT_WINDOW) -> float | None:
    """Largest relative disagreement between the last three window means,
    with app/core/cfd_run.drift's numbers mirrored literally (800-row
    windows, refuses to judge below 100 rows/window): ~0 on a flat or
    bounded-limit-cycle tail, order of the climb rate while trending.
    None until judgeable. Callers pass a transient-excluded history
    (_DRIFT_SKIP rows dropped, the in-app Fluent engine's exclusion)."""
    w = min(window, len(vals) // 3)
    if w < _DRIFT_MIN_ROWS:
        return None
    m1 = sum(vals[-3 * w:-2 * w]) / w
    m2 = sum(vals[-2 * w:-w]) / w
    m3 = sum(vals[-w:]) / w
    ref = max(abs(m3), 0.05)
    return max(abs(m3 - m2), abs(m2 - m1)) / ref


def _report_histories() -> dict:
    """{report_name: [values...]} parsed from the *-rfile.out files the
    report definitions write into the session cwd each iteration."""
    out = {}
    if _session_dir is None:
        return out
    for f in sorted(_session_dir.glob("*.out")):
        try:
            names, rows = [], []
            for ln in f.read_text(errors="replace").splitlines():
                s = ln.strip()
                if s.startswith("("):
                    m = re.findall(r'"([^"]+)"', s)
                    if m:
                        names = m
                    continue
                parts = s.split()
                if len(parts) >= 2:
                    try:
                        rows.append([float(x) for x in parts])
                    except ValueError:
                        continue
            # column 0 is the iteration count; map the rest to names
            for ci in range(1, len(rows[-1]) if rows else 0):
                name = (names[ci] if ci < len(names)
                        else f"{f.stem}:col{ci}")
                out[name] = [r[ci] for r in rows if len(r) > ci]
        except OSError:
            continue
    return out


def _compute_reports(names: list[str]) -> dict:
    """Point values via report_definitions.compute, normalized."""
    s = _require()
    raw = s.settings.solution.report_definitions.compute(report_defs=names)
    vals = {}
    seq = raw if isinstance(raw, list) else [raw]
    for item in seq:
        if isinstance(item, dict):
            for k, v in item.items():
                vals[k] = v[0] if isinstance(v, list) and v else v
    return vals


# ---------- tools ----------

@mcp.tool()
def status() -> dict:
    """Installed ANSYS versions, pyfluent version, live-session state."""
    import ansys.fluent.core as pyfluent
    roots = {k: os.environ[k] for k in os.environ
             if k.startswith("AWP_ROOT")}
    alive = False
    if _solver is not None:
        try:
            alive = str(_solver.health_check.status()) == "Status.SERVING"
        except Exception:
            alive = False
    out = {"pyfluent": pyfluent.__version__, "awp_roots": roots,
           "default_version": DEFAULT_VERSION, "session_alive": alive,
           "work_dir": str(WORK)}
    if alive:
        out["dimension"] = _dim
        out["zones"] = _zones()
        out["session_dir"] = str(_session_dir)
    return out


@mcp.tool()
def launch(dimension: int = 3, precision: str = "double",
           processors: int = 4, version: str = "") -> dict:
    """Start a headless Fluent solver session (checks out a license; held
    until shutdown()). dimension=3 for the app's one-cell slab meshes,
    2 for true-2D Workbench meshes. Replaces any existing session."""
    global _solver, _dim, _session_dir, _launch_params
    import ansys.fluent.core as pyfluent
    with _lifecycle:
        old, _solver = _solver, None
    if old is not None:
        try:
            old.exit()
        except Exception:
            pass
    _session_dir = WORK / f"session-{time.strftime('%Y%m%d-%H%M%S')}"
    _session_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    s = pyfluent.launch_fluent(
        product_version=version or DEFAULT_VERSION, mode="solver",
        dimension=int(dimension), precision=precision,
        processor_count=int(processors), ui_mode="no_gui",
        start_timeout=240, cwd=str(_session_dir))
    _dim = int(dimension)
    _launch_params = {"dimension": _dim, "precision": precision,
                      "processors": int(processors)}
    with _lifecycle:
        _solver = s
    return {"launched_in_s": round(time.time() - t0, 1),
            "dimension": _dim, "precision": precision,
            "processors": processors,
            "session_dir": str(_session_dir)}


@mcp.tool()
def convert_openfoam_mesh(case_dir: str) -> dict:
    """Convert an OpenFOAM case's constant/polyMesh into a Fluent .msh
    via foamMeshToFluent in the ESI Docker image (Docker Desktop must be
    running). The app's `frontAndBack` empty patches are retyped to
    symmetry so the slab solves in Fluent 3D exactly like the OpenFOAM
    case. Returns the .msh path for read_mesh()."""
    src = Path(case_dir)
    if not (src / "constant" / "polyMesh").is_dir():
        raise ValueError(f"{src} has no constant/polyMesh")
    dst = WORK / f"bridge-{time.strftime('%Y%m%d-%H%M%S')}"
    (dst / "constant").mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "constant" / "polyMesh",
                    dst / "constant" / "polyMesh")
    if (src / "system").is_dir():
        shutil.copytree(src / "system", dst / "system")
    else:  # foamMeshToFluent only needs a minimal controlDict
        (dst / "system").mkdir(parents=True)
        (dst / "system" / "controlDict").write_text(
            'FoamFile\n{\n    version 2.0;\n    format ascii;\n'
            '    class dictionary;\n    object controlDict;\n}\n'
            'deltaT 1;\nwriteInterval 1;\n', encoding="utf-8")
    try:
        sys.path.insert(0, str(REPO))
        from app.core.cfd_run import _docker_exe
        docker = _docker_exe()
    except Exception:
        docker = "docker"
    inner = ("source /usr/lib/openfoam/openfoam*/etc/bashrc; "
             "foamDictionary constant/polyMesh/boundary "
             "-entry entry0/frontAndBack/type -set symmetry "
             ">/dev/null 2>&1; "
             "foamMeshToFluent > log.foamMeshToFluent 2>&1 "
             "&& ls fluentInterface/")
    r = subprocess.run(
        [docker, "run", "--rm", "--name",
         f"wss-fluent-bridge-{os.getpid()}",
         "-v", f"{dst}:/case", "-w", "/case",
         "--entrypoint", "/bin/bash", OPENFOAM_IMAGE, "-c", inner],
        capture_output=True, text=True, timeout=600,
        creationflags=_CREATE_NO_WINDOW)
    if r.returncode != 0:
        tail = ""
        log = dst / "log.foamMeshToFluent"
        if log.is_file():
            tail = log.read_text(errors="replace")[-500:]
        raise RuntimeError(f"foamMeshToFluent failed: "
                           f"{(r.stderr or r.stdout).strip()[-300:]}\n{tail}")
    msh = sorted((dst / "fluentInterface").glob("*.msh"))
    if not msh:
        raise RuntimeError("conversion produced no .msh")
    return {"msh_path": str(msh[0]), "bridge_dir": str(dst)}


@mcp.tool()
def read_mesh(msh_path: str) -> dict:
    """Read a Fluent .msh into the live session; returns the boundary
    zones by type (zone names drive setup_external_aero)."""
    s = _require()
    s.settings.file.read_mesh(file_name=str(Path(msh_path)))
    return {"zones": _zones()}


@mcp.tool()
def list_zones() -> dict:
    """Boundary zones of the loaded mesh, grouped by current type."""
    return _zones()


@mcp.tool()
def setup_external_aero(inlet_velocity_ms: float,
                        profile_zones: list[str], chord_m: float,
                        depth_m: float = 1.0, inlet: str = "inlet",
                        outlet: str = "outlet",
                        slip_zones: list[str] = ["top"],
                        ground: str = "ground",
                        moving_ground: bool = True,
                        rho: float = 1.225, mu: float = 1.8375e-05,
                        downforce_positive: bool = True,
                        conventions: str = "studio") -> dict:
    """Apply the documented 2D external-aero recipe to the loaded mesh:
    velocity inlet, 0 Pa outlet, no-slip profile walls, shear-free upper
    bound, moving ground at the inlet speed (or shear-free when
    moving_ground=False), SST k-omega, and lift/drag coefficient report
    definitions on the profile zones (per-iteration histories written
    to the session dir). For app slab meshes pass depth_m = 0.1 x chord;
    for true-2D meshes keep depth_m = 1. Every step reports applied or
    failed — read `failed` before trusting the setup. mu defaults to
    rho x the studio's nu.

    conventions="studio" (default) applies the reference-grade
    corrections: constant-property air at the given rho/mu, explicit
    chord-referenced reference values, downforce-positive lift, inlet
    (and outlet-backflow) turbulence pinned to the OpenFOAM cases'
    spec — 1% intensity, viscosity ratio 10, cfd.py's TURB_INTENSITY/
    TURB_VISC_RATIO — so cross-engine deltas never fold in an
    undeclared BC difference (Fluent's velocity-inlet default is 5%:
    25x the inlet k), and Fluent's residual auto-stop DISABLED so the
    iteration budget and the force-history drift own convergence.
    conventions="walkthrough" reproduces the manual workflow as
    written — Fluent default material, reference values (area stays
    1 m^2, so coefficients are NOT chord-normalized) and turbulence
    BCs, +y lift, residual criteria left ON (measured: they stop a
    still-trending case at ~iteration 277). It exists to measure the
    manual workflow, not to be used.

    conventions="default-2d" is the manual workflow's true-2D recipe
    (the default for Workbench-chain meshes): the same Fluent-default
    material, turbulence BCs, viscous model and residual criteria as
    the manual walkthrough, +y lift, but with the reference values SET
    EXPLICITLY — area 1 m^2, length 1 m, velocity and density from the
    inlet spec — instead of computed, so the setup is deterministic run
    to run. Coefficients come out per meter depth, not
    chord-normalized."""
    if conventions not in ("studio", "walkthrough", "default-2d"):
        raise ValueError("conventions must be 'studio', 'walkthrough' "
                         "or 'default-2d'")
    walkthrough = conventions != "studio"
    default2d = conventions == "default-2d"
    s = _require()
    st = s.settings
    bc = st.setup.boundary_conditions
    applied: list = []
    failed: list = []
    u = float(inlet_velocity_ms)

    zones = _zones()
    current = {n: t for t, ns in zones.items() for n in ns}
    # the slip default names the slab route's "top"; the 2D Workbench
    # chain names the same boundary "upper_bound" — an untouched
    # default resolves against the loaded mesh so the documented
    # recipe succeeds on both (an explicit slip_zones is never touched)
    if slip_zones == ["top"] and "top" not in current \
            and "upper_bound" in current:
        slip_zones = ["upper_bound"]
        applied.append("slip default resolved to upper_bound "
                       "(2D chain mesh has no 'top' zone)")
    if current.get(inlet) != "velocity_inlet":
        _fence(applied, failed, f"zone-type {inlet} -> velocity-inlet",
               lambda: bc.set_zone_type(zone_list=[inlet],
                                        new_type="velocity-inlet"))
    if current.get(outlet) != "pressure_outlet":
        _fence(applied, failed, f"zone-type {outlet} -> pressure-outlet",
               lambda: bc.set_zone_type(zone_list=[outlet],
                                        new_type="pressure-outlet"))
    # foamMeshToFluent types generic patches as pressure zones — every
    # wall-role zone must actually BE a wall before its momentum settings
    # exist to edit
    wall_role = [*profile_zones, *slip_zones] + ([ground] if ground else [])
    for z in wall_role:
        if current.get(z) != "wall":
            _fence(applied, failed, f"zone-type {z} -> wall",
                   lambda z=z: bc.set_zone_type(zone_list=[z],
                                                new_type="wall"))

    if not default2d:
        def visc():
            st.setup.models.viscous.model = "k-omega"
            st.setup.models.viscous.k_omega_model = "sst"
        _fence(applied, failed, "viscous k-omega SST", visc)
    else:
        # the manual 2D recipe never opens the viscous panel — parity
        # means leaving the model exactly where Fluent starts it
        applied.append("viscous model left at Fluent defaults "
                       "(default-2d parity)")

    if not walkthrough:
        def air():
            m = st.setup.materials.fluid["air"]
            m.density.option = "constant"
            m.density.value = rho
            m.viscosity.option = "constant"
            m.viscosity.value = mu
        _fence(applied, failed, f"air rho={rho} mu={mu}", air)
    else:
        applied.append(f"air left at Fluent defaults ({conventions} "
                       "parity)")

    def vin():
        bc.velocity_inlet[inlet].momentum.velocity_magnitude.value = u
    _fence(applied, failed, f"inlet velocity {u} m/s", vin)

    def pout():
        bc.pressure_outlet[outlet].momentum.gauge_pressure.value = 0.0
    _fence(applied, failed, "outlet 0 Pa", pout)

    if not walkthrough:
        # inlet turbulence pinned to the OpenFOAM cases' inlet spec
        # (cfd.py: TURB_INTENSITY=0.01, TURB_VISC_RATIO=10 feed 0/k and
        # 0/omega). Fluent's velocity-inlet default is 5% intensity —
        # 25x the inlet k — an undeclared BC difference that would ride
        # silently in every cross-engine delta. The walkthrough
        # conventions keep the Fluent defaults: parity with the manual
        # workflow is their whole point.
        def turb_in():
            t = bc.velocity_inlet[inlet].turbulence
            t.turbulence_specification = "Intensity and Viscosity Ratio"
            t.turbulent_intensity = 0.01
            t.turbulent_viscosity_ratio = 10.0
        _fence(applied, failed,
               "inlet turbulence 1% intensity, viscosity ratio 10",
               turb_in)

        def turb_out():
            t = bc.pressure_outlet[outlet].turbulence
            t.turbulence_specification = "Intensity and Viscosity Ratio"
            t.backflow_turbulent_intensity = 0.01
            t.backflow_turbulent_viscosity_ratio = 10.0
        _fence(applied, failed,
               "outlet backflow turbulence 1% intensity, viscosity "
               "ratio 10", turb_out)
    else:
        applied.append("turbulence BCs left at Fluent defaults "
                       f"({conventions} parity)")

    for z in profile_zones:
        _fence(applied, failed, f"{z} no-slip wall",
               lambda z=z: setattr(bc.wall[z].momentum, "shear_condition",
                                   "No Slip"))
    for z in slip_zones:
        def slip(z=z):
            wm = bc.wall[z].momentum
            wm.wall_motion = "Stationary Wall"
            wm.shear_condition = "Specified Shear"
        _fence(applied, failed, f"{z} shear-free", slip)

    if ground:
        if moving_ground:
            def gmove():
                wm = bc.wall[ground].momentum
                wm.wall_motion = "Moving Wall"
                try:
                    wm.relative = "Absolute"
                except Exception:
                    # some builds expose this as a bool (relative to the
                    # adjacent zone); the cell zone is stationary, so
                    # either spelling yields the same absolute motion
                    wm.relative = False
                wm.velocity_spec = "Translational"
                wm.speed = u
                wm.direction = [1, 0, 0] if _dim == 3 else [1, 0]
            _fence(applied, failed,
                   f"{ground} moving wall {u} m/s +x", gmove)
        else:
            def gslip():
                wm = bc.wall[ground].momentum
                wm.wall_motion = "Stationary Wall"
                wm.shear_condition = "Specified Shear"
            _fence(applied, failed, f"{ground} shear-free", gslip)

    ref_area = chord_m * depth_m if _dim == 3 else chord_m
    ref_len = chord_m
    if not walkthrough:
        def refs():
            rv = st.setup.reference_values
            rv.area = ref_area
            rv.length = chord_m
            rv.velocity = u
            rv.density = rho
            rv.viscosity = mu
            if _dim == 2:   # depth is a 2D-only reference; inactive in 3D
                rv.depth = depth_m
        _fence(applied, failed,
               f"reference values (area={ref_area:g}, length={chord_m:g})",
               refs)
    elif default2d:
        ref_area = 1.0
        ref_len = 1.0

        def refs_2d():
            # the documented step is "compute from inlet" in the GUI,
            # which leaves area at 1 m^2 and length at 1 m; setting the
            # same values explicitly (velocity/density from the inlet
            # spec) makes the setup deterministic instead of dependent
            # on what the compute happens to read
            rv = st.setup.reference_values
            rv.area = 1.0
            rv.length = 1.0
            rv.velocity = u
            rv.density = rho
        _fence(applied, failed,
               "reference values explicit (default-2d: area 1 m^2, "
               f"length 1 m, velocity {u:g} m/s, density {rho:g})",
               refs_2d)
    else:
        # the payload reports what Fluent holds: the inlet compute
        # touches velocity/density, never area or length
        ref_area = 1.0
        ref_len = 1.0

        def refs_walkthrough():
            # the walkthrough's step: velocity/density from the inlet;
            # area and length REMAIN 1 m^2 / 1 m, so the coefficients
            # come out un-normalized by chord
            rv = st.setup.reference_values
            try:
                rv.compute(from_zone_name=inlet,
                           from_zone_type="velocity-inlet")
            except Exception:
                _require().scheme.eval(
                    '(ti-menu-load-string "report/reference-values/'
                    f'compute/velocity-inlet {inlet}")')
        _fence(applied, failed,
               "reference values computed from inlet (walkthrough: area "
               "stays 1 m^2 — coefficients NOT chord-normalized)",
               refs_walkthrough)

    lift_vec = ([0, 1, 0] if walkthrough
                else [0, -1, 0] if downforce_positive else [0, 1, 0])
    drag_vec = [1, 0, 0]
    if _dim == 2:
        lift_vec, drag_vec = lift_vec[:2], drag_vec[:2]

    if not walkthrough:
        _fence(applied, failed, "residual auto-stop disabled "
               "(force-history drift owns convergence)",
               lambda: _disable_residual_stop(st))
    elif default2d:
        # untouched, not just left ON: the manual 2D recipe never opens
        # the residual panel, so parity means Fluent's own defaults
        # decide (an early stop is reported by the caller, never hidden)
        applied.append("residual criteria left at Fluent defaults "
                       "(default-2d parity)")
    else:
        applied.append("residual criteria left ON (walkthrough parity — "
                       "measured stopping a trending case at ~277)")

    def reports():
        rd = st.solution.report_definitions
        rd.drag["drag_coef"] = {}
        d = rd.drag["drag_coef"]
        d.zones = list(profile_zones)
        d.force_vector = drag_vec
        d.report_output_type = "Drag Coefficient"
        d.create_report_file = True
        rd.lift["lift_coef"] = {}
        lf = rd.lift["lift_coef"]
        lf.zones = list(profile_zones)
        lf.force_vector = lift_vec
        lf.report_output_type = "Lift Coefficient"
        lf.create_report_file = True
    _fence(applied, failed, "lift/drag coefficient reports", reports)

    return {"applied": applied, "failed": failed,
            "conventions": conventions,
            "reference_area_m2": ref_area, "reference_length_m": ref_len,
            "lift_convention": (f"+y lift ({conventions})"
                                if walkthrough else
                                "downforce-positive (0,-1)"
                                if downforce_positive else "+y lift"),
            "note": ("default-2d: explicit references (area 1 m^2, "
                     "length 1 m) — coefficients are per meter depth, "
                     "not chord-normalized"
                     if default2d else
                     "walkthrough parity: Fluent-default references and "
                     "criteria, for measuring the manual workflow"
                     if walkthrough else
                     "chord-referenced coefficients — the GUI recipe's "
                     "defaults leave reference area at 1 m^2, so numbers "
                     "from the manual workflow differ by area/chord "
                     "scaling")}


@mcp.tool()
def solve(iterations: int = 500, initialize: bool = True) -> dict:
    """Hybrid-initialize (unless continuing) and iterate. Returns final
    lift/drag coefficients, tail statistics and a 3-window drift verdict
    in the studio's style — a drifting tail is a bound, not a result;
    continue with initialize=False to extend a run."""
    s = _require()
    out: dict = {"iterations_requested": int(iterations)}
    if initialize:
        s.settings.solution.initialization.hybrid_initialize()
        out["initialized"] = "hybrid"
    t0 = time.time()
    s.settings.solution.run_calculation.iterate(
        iter_count=int(iterations))
    out["wall_s"] = round(time.time() - t0, 1)
    try:
        out["final"] = _compute_reports(["lift_coef", "drag_coef"])
    except Exception as e:
        out["final_error"] = str(e)
    hist = _report_histories()
    for name, vals in hist.items():
        if not vals:
            continue
        tail = vals[-min(len(vals), 200):]
        key = ("cl" if "lift" in name.lower()
               else "cd" if "drag" in name.lower() else name)
        # decision drifts exclude the hybrid-init transient, same as the
        # in-app engines — judged over the startup decay, the window
        # means cancel while the run still trends
        d = _drift(vals[_DRIFT_SKIP:])
        out[key] = {"last": round(vals[-1], 5),
                    "tail_mean": round(sum(tail) / len(tail), 5),
                    "tail_rows": len(tail),
                    "history_rows": len(vals),
                    "drift": round(d, 5) if d is not None else None}
    cl = out.get("cl", {})
    out["converged_hint"] = (
        None if cl.get("drift") is None
        else bool(cl["drift"] < _CONVERGED_CL_TOL))
    out["note"] = ("converged_hint: the studio's 3-window drift "
                   "(800-row windows, first 200 rows excluded) at the "
                   "0.006 bar; None means the history is too short to "
                   "judge")
    return out


@mcp.tool()
def report_forces() -> dict:
    """Current lift/drag coefficient values + history tails without
    iterating further."""
    out = {"final": _compute_reports(["lift_coef", "drag_coef"])}
    for name, vals in _report_histories().items():
        if vals:
            out[name] = {"last": round(vals[-1], 5), "rows": len(vals)}
    return out


@mcp.tool()
def tui(command: str) -> str:
    """Raw Fluent TUI journal line(s) — the escape hatch for anything
    without a typed tool (e.g. 'report/summary'). Output is captured
    from the session transcript, which console-side TUI writes bypass
    string ports for."""
    s = _require()
    trns = (sorted(_session_dir.glob("*.trn"),
                   key=lambda p: p.stat().st_mtime)
            if _session_dir else [])
    trn = trns[-1] if trns else None
    before = trn.stat().st_size if trn else 0
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    result = s.scheme.eval(f'(ti-menu-load-string "{esc}")')
    time.sleep(0.4)   # transcript flush
    if trn is not None and trn.stat().st_size > before:
        with open(trn, "r", errors="replace") as fh:
            fh.seek(before)
            return fh.read()[-6000:]
    return f"(executed, rc={result}; no transcript delta captured)"


@mcp.tool()
def scheme(expr: str) -> str:
    """Evaluate a raw Scheme expression in the Fluent session."""
    return str(_require().scheme.eval(expr))


@mcp.tool()
def export_ascii(filename: str = "solution.csv",
                 quantities: list[str] = ["x-velocity", "y-velocity",
                                          "pressure"],
                 location: str = "cell-center",
                 surfaces: list[str] | None = None) -> dict:
    """Solution export from the live session as comma-separated text
    with a header row (cellnumber/x/y/z + the requested quantities).
    location: "cell-center" or "node". The in-app Fluent engine uses
    this to feed the studio's flow view and animation; with
    surfaces=["profile"] and wall-shear quantities it also feeds the
    wall attachment channel (spiked 2026-08-03: the export accepts
    x-wall-shear/y-wall-shear on wall zones and writes per-node rows)."""
    s = _require()
    path = Path(filename)
    if not path.is_absolute():
        path = (_session_dir or WORK) / filename
    s.settings.file.export.ascii(
        file_name=str(path).replace("\\", "/"),
        surface_name_list=list(surfaces or []), delimiter="comma",
        quantities=list(quantities), location=location)
    if not path.is_file():
        raise RuntimeError("Fluent wrote no export file")
    with open(path, errors="replace") as fh:
        rows = sum(1 for _ in fh) - 1
    return {"written": str(path), "rows": rows}


@mcp.tool()
def write_case_data(stem: str = "case") -> dict:
    """Write case+data files — <stem>.cas.h5 AND <stem>.dat.h5 (Fluent
    reopens them via File > Read > Case & Data; ParaView/EnSight read
    them too). A bare stem lands in the session dir; an absolute path
    stem writes exactly there (the in-app engine points it at the run
    directory so the artifact survives the session)."""
    s = _require()
    p = Path(f"{stem}.cas.h5")
    if not p.is_absolute():
        p = (_session_dir or WORK) / p.name
    s.settings.file.write(file_type="case-data",
                          file_name=str(p).replace("\\", "/"))
    dat = p.with_name(p.name[:-len(".cas.h5")] + ".dat.h5")
    return {"written": str(p),
            "data_file": str(dat) if dat.is_file() else None}


@mcp.tool()
def read_case_data(path: str = "case.cas.h5") -> dict:
    """Read a solved case+data pair (<stem>.cas.h5 with its .dat.h5
    beside it) into the live session — the reload path for a finished
    run's artifact (the in-app engines write one beside every done
    run). Report definitions and monitors ride in the case, so the
    force histories resume in the new session's cwd. Returns the
    boundary zones so the caller can re-check the vocabulary it is
    about to drive."""
    s = _require()
    p = Path(path)
    if not p.is_absolute():
        p = (_session_dir or WORK) / p
    if not p.is_file():
        raise ValueError(f"no case file at {p}")
    s.settings.file.read_case_data(file_name=str(p).replace("\\", "/"))
    return {"zones": _zones()}


# ---------- adjoint / gradient-based polish ----------

# One objective, one name: the polish drives Fluent's own gradient-based
# optimizer (adjoint + shape morphing) toward J = downforce - k * drag,
# built as a linear-combination observable over two force observables on
# the profile walls. k is the studio's physical exchange rate (newtons of
# downforce a newton of drag is worth), dimensionless, so it applies
# identically whether the session's monitors are raw or chord-referenced.
ADJ_OBS_DOWN = "polish-down"
ADJ_OBS_DRAG = "polish-drag"
ADJ_OBS_J = "polish-objective"
# "maximize" in the live 26.1 goal vocabulary (target | step-size |
# none | equal | bounded — there is no "increase"): request a relative
# step per design iteration and let the optimizer take what it can get
ADJ_STEP_PCT = 2.0


def _design():
    """The gradient-based design root (adjoint solver + optimizer),
    presence-checked: a Fluent build without the typed design tree reads
    as one clear refusal, never an AttributeError from deep inside a
    setup step."""
    s = _require()
    d = getattr(s.settings, "design", None)
    if d is None:
        raise RuntimeError(
            "this Fluent exposes no design/gradient-based tree — the "
            "adjoint polish needs the Fluent 2024 R1+ settings API")
    return d.gradient_based


def _named(coll, name: str):
    """Fetch-or-create one entry of a settings NamedObject collection."""
    try:
        names = list(coll.get_object_names())
    except Exception:
        try:
            names = list(coll.keys())
        except Exception:
            names = []
    if name not in names:
        coll.create(name)
    return coll[name]


def _set_list_object(lst, rows: list[dict]) -> None:
    """Fill a settings ListObject from row dicts (whole-list assignment
    where the API version supports it, resize+index where it doesn't)."""
    try:
        lst.set_state(rows)
        return
    except Exception:
        pass
    try:
        lst.resize(new_size=len(rows))
    except Exception:
        lst.resize(len(rows))
    for i, row in enumerate(rows):
        item = lst[i]
        for k, v in row.items():
            setattr(item, k, v)


@mcp.tool()
def adjoint_setup(profile_zones: list[str] = ["profile"],
                  drag_exchange_k: float = 0.25,
                  region_x: list[float] = [],
                  region_y: list[float] = [],
                  flow_iterations: int = 300,
                  adjoint_iterations: int = 250,
                  morphing_method: str = "polynomials") -> dict:
    """Configure Fluent's gradient-based shape optimizer (adjoint) for a
    final-stage polish of the loaded, solved case: observables
    J = downforce - k*drag on the profile walls, a cartesian design
    region bounding the morph (pass region_x/region_y as [lo, hi] in
    meters — the caller clips them to the rule envelope so a box
    violation is impossible by construction), the balanced adjoint
    method preset, and shape-opt with a single increase-J objective,
    one design iteration per optimize() call. Fluent's residual
    auto-stop is disabled so the optimizer's interleaved flow solves
    always run their full budget (drift doctrine, same as the studio
    engines). Every step is fenced: a failure lands in `failed` with
    its own error, never silently."""
    s = _require()
    d = _design()
    k = float(drag_exchange_k)
    applied: list = []
    failed: list = []
    fvec_down = [0.0, -1.0] if _dim == 2 else [0.0, -1.0, 0.0]
    fvec_drag = [1.0, 0.0] if _dim == 2 else [1.0, 0.0, 0.0]

    def enable():
        try:
            d.enable()
        except Exception:
            d.enabled = True
    _fence(applied, failed, "enable gradient-based design", enable)
    _fence(applied, failed, "disable residual auto-stop",
           lambda: _disable_residual_stop(s.settings))

    def obs_down():
        o = _named(d.observables.definitions.force, ADJ_OBS_DOWN)
        o.walls = list(profile_zones)
        o.vector = fvec_down
    _fence(applied, failed, "observable: downforce on profile", obs_down)

    def obs_drag():
        o = _named(d.observables.definitions.force, ADJ_OBS_DRAG)
        o.walls = list(profile_zones)
        o.vector = fvec_drag
    _fence(applied, failed, "observable: drag on profile", obs_drag)

    def obs_j():
        o = _named(d.observables.definitions.linear_combination, ADJ_OBS_J)
        _set_list_object(o.entries, [
            {"coefficient": 1.0, "observable": ADJ_OBS_DOWN, "power": 1.0},
            {"coefficient": -k, "observable": ADJ_OBS_DRAG, "power": 1.0},
        ])
    _fence(applied, failed,
           f"observable: J = downforce - {k:g}*drag", obs_j)
    _fence(applied, failed, "select J for the adjoint",
           lambda: setattr(d.observables.selection, "adjoint_observable",
                           ADJ_OBS_J))
    _fence(applied, failed, "adjoint methods: balanced preset",
           lambda: d.methods.balanced())

    def region():
        r = d.design_tool.region
        r.region_type = "cartesian"
        if region_x and region_y:
            r.cartesian.extent.x = [float(region_x[0]), float(region_x[1])]
            r.cartesian.extent.y = [float(region_y[0]), float(region_y[1])]
        else:
            r.get_bounds()
        try:
            r.modifiable_zones = list(profile_zones)
        except Exception:
            r.modifiable_location = list(profile_zones)
    _fence(applied, failed, "design region (cartesian)", region)
    _fence(applied, failed, f"morpher: {morphing_method}",
           lambda: setattr(d.design_tool.morpher, "method",
                           morphing_method))

    def optimizer():
        opt = d.optimizer
        opt.optimizer_type = "shape-opt"
        opt.objectives.observables.selection = [ADJ_OBS_J]
        # the objectives list is MANAGED on the live build — one row per
        # selected observable, resize/set_state inactive (measured
        # 2026-08-03) — so the goal is written INTO the row; the row's
        # observable/condition fields are the manager's and may refuse a
        # write, which is fine as long as the goal itself lands
        goal_row = {"observable": ADJ_OBS_J, "goal": "step-size",
                    "value": float(ADJ_STEP_PCT),
                    "value_as_percentage": True}
        objs = opt.objectives.objectives
        try:
            row = objs[0]
        except Exception:
            _set_list_object(objs, [goal_row])
        else:
            landed = []
            for k, v in goal_row.items():
                try:
                    setattr(row, k, v)
                    landed.append(k)
                except Exception:
                    pass
            missing = {"goal", "value"} - set(landed)
            if missing:
                raise RuntimeError(
                    f"objective row refused {sorted(missing)}")
        st = opt.optimizer_settings
        st.design_iterations = 1
        st.flow_iterations = int(flow_iterations)
        st.adjoint_iterations = int(adjoint_iterations)
    _fence(applied, failed,
           f"optimizer: shape-opt, step J +{ADJ_STEP_PCT:g}%/iteration",
           optimizer)
    _fence(applied, failed, "optimizer initialize",
           lambda: d.optimizer.initialize())
    return {"applied": applied, "failed": failed,
            "objective": {"name": ADJ_OBS_J, "k": k},
            "dimension": _dim}


@mcp.tool()
def adjoint_step() -> dict:
    """Run ONE gradient-based design iteration (flow solve, adjoint
    solve, morph) with whatever adjoint_setup installed, and return the
    post-step force reports plus the tail of the force history. The
    caller owns the loop — chunked driving keeps cancel, progress and
    rule checks between iterations, while the iteration itself is
    entirely Fluent's own optimizer."""
    d = _design()
    t0 = time.time()
    d.optimizer.optimize()
    out: dict = {"wall_s": round(time.time() - t0, 1)}
    try:
        out["reports"] = _compute_reports(["lift_coef", "drag_coef"])
    except Exception as e:
        out["reports_error"] = str(e)
    hist = _report_histories()
    cl = next((v for n, v in hist.items() if "lift" in n.lower()), [])
    cd = next((v for n, v in hist.items() if "drag" in n.lower()), [])
    if cl:
        tail = cl[-min(len(cl), 60):]
        out["cl_tail_mean"] = round(sum(tail) / len(tail), 6)
        out["history_rows"] = len(cl)
        d_tail = _drift(cl[_DRIFT_SKIP:])
        out["cl_drift"] = (round(d_tail, 5) if d_tail is not None
                           else None)
    if cd:
        tail = cd[-min(len(cd), 60):]
        out["cd_tail_mean"] = round(sum(tail) / len(tail), 6)
    return out


@mcp.tool()
def adjoint_interrupt() -> dict:
    """Ask the running gradient-based optimizer to stop at the next
    opportunity (the soft half of cancellation — shutdown() remains the
    hard one)."""
    try:
        _design().optimizer.interrupt()
        return {"interrupted": True}
    except Exception as e:
        return {"interrupted": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def mesh_native(stl_path: str, edge_size_m: float, far_size_m: float,
                first_layer_m: float, n_layers: int = 12,
                growth: float = 1.2, cells_per_gap: int = 8,
                fill: str = "poly-hexcore",
                wall_zones: list[str] = [],
                size_functions: str = "proximity",
                processors: int = 4, version: str = "") -> dict:
    """ANSYS Fluent Meshing (watertight geometry workflow) on a studio
    slab STL — the all-ANSYS meshing route: no gmsh in the cell cutting,
    no Docker bridge. The STL comes from fluent_workflow.write_slab_stl
    (named solids drive Fluent's boundary-type inference: inlet/outlet
    auto-type, symmetry-* planes become symmetry, the rest walls).
    Surface sizing runs between edge_size_m (profile walls, via a
    scoped face size) and far_size_m with cells_per_gap across slot
    gaps; prisms grow from the walls at first_layer_m x growth^k for
    n_layers; the volume fills with poly-hexcore.

    size_functions="proximity" (default) matches the studio presets'
    intent — uniform wall sizing plus gap protection; "curvature-
    proximity" adds leading-edge curvature adaptation the gmsh presets
    never had, for reference-grade runs that intend to pay for it.
    MEASURED (2026 R1, coarse two-element slab): with proximity scoped
    to FACES the two symmetry planes see each other across the slab
    and the whole domain blankets at dz/cells_per_gap — 17.8M cells,
    identical with curvature on or off — hence the edge scoping
    hardwired below.

    Runs a short-lived Fluent Meshing session (its own license seat
    while it lives; released before returning) and writes native.msh.h5
    beside the STL for read_mesh(). Optional refinements (scoped
    sizing, boundary layers) degrade with the failure recorded in
    `failed` — read it before trusting the mesh grade."""
    global _mesher
    stl = Path(stl_path)
    if not stl.is_file():
        raise ValueError(f"no STL at {stl}")
    if fill not in ("poly-hexcore", "polyhedra", "tetrahedral",
                    "hexcore"):
        raise ValueError("fill must be poly-hexcore, polyhedra, "
                         "tetrahedral or hexcore")
    if size_functions not in ("proximity", "curvature-proximity"):
        raise ValueError("size_functions must be 'proximity' or "
                         "'curvature-proximity'")
    work = stl.parent
    msh_path = work / "native.msh.h5"
    msh_path.unlink(missing_ok=True)   # write-mesh must never prompt
    # lifecycle claim: a non-None _mesher here means another caller's
    # workflow is live (the finally below always releases) — exiting it
    # would kill their session and license mid-mesh. shutdown() is the
    # deliberate kill; concurrent meshing gets a refusal, not a victim.
    with _lifecycle:
        if _mesher is not None:
            raise RuntimeError(
                "a Fluent Meshing session is already live — another "
                "mesh_native call is in progress; wait for it or call "
                "shutdown()")
        _mesher = _MESH_CLAIM
    try:
        import ansys.fluent.core as pyfluent
        t0 = time.time()
        m = pyfluent.launch_fluent(
            product_version=version or DEFAULT_VERSION, mode="meshing",
            precision="double", processor_count=int(processors),
            ui_mode="no_gui", start_timeout=240, cwd=str(work))
    except BaseException:
        with _lifecycle:
            if _mesher is _MESH_CLAIM:
                _mesher = None
        raise
    with _lifecycle:
        _mesher = m
    transcript: list[str] = []
    cb = None
    try:
        cb = m.transcript.register_callback(
            lambda text, *a: transcript.append(str(text)))
    except Exception:
        cb = None
    applied: list = []
    failed: list = []
    try:
        # measured on 2026 R1 (see DECISIONS): the meshing kernel works
        # in MILLIMETERS — MeshUnit="m" scales the imported model m->mm
        # exactly, and every size handed to the workflow must be mm.
        # Passing SI meters produced a 2.45-micron surface-size request
        # over a 6 m domain (a size field that never finishes).
        mm = 1000.0
        wt = m.workflow
        wt.InitializeWorkflow(WorkflowType="Watertight Geometry")
        imp = wt.TaskObject["Import Geometry"]
        # STL is a MESH-format import to Fluent (FileFormat "CAD" is
        # refused with "Set File Format to 'Mesh'"), and mesh-format
        # wants MeshFileName, not FileName ("File Names ... not
        # provided" otherwise)
        fname = str(stl).replace("\\", "/")
        import_tried: list[str] = []
        for args in ({"FileFormat": "Mesh", "ImportType": "Single File",
                      "MeshFileName": fname, "MeshUnit": "m"},
                     {"FileFormat": "Mesh", "MeshFileName": fname,
                      "MeshUnit": "m"},
                     {"FileFormat": "Mesh", "FileNames": fname,
                      "MeshUnit": "m"}):
            try:
                imp.Arguments = args
                imp.Execute()
                applied.append("import geometry (mesh-format STL, "
                               "meters scaled to kernel mm)")
                break
            except Exception as e:
                import_tried.append(f"{type(e).__name__}: {e}"[:300])
        else:
            raise RuntimeError("geometry import failed — "
                               + " | ".join(import_tried))

        def local_size():
            t = wt.TaskObject["Add Local Sizing"]
            t.Arguments = {"AddChild": "yes",
                           "BOIControlName": "profile-walls",
                           "BOIExecution": "Face Size",
                           "BOISize": float(edge_size_m) * mm,
                           "BOIGrowthRate": float(growth),
                           "BOIFaceLabelList": list(wall_zones)}
            t.AddChildAndUpdate()
        if wall_zones:
            _fence(applied, failed, "scoped face size on profile walls",
                   local_size)

        surf = wt.TaskObject["Generate the Surface Mesh"]
        # proximity scoped to EDGES, never faces: on a thin slab the
        # two symmetry planes FACE each other across dz, and
        # face-proximity at cells_per_gap blankets the entire domain at
        # dz/cells_per_gap (measured: 17.8M cells either way with
        # curvature on or off — the planes were the driver, not the
        # LE). Hole-boundary edges still catch the slot gap and the
        # ride-height gap, which is the physics the knob exists for.
        sf_controls: dict = {
            "MinSize": float(edge_size_m) * mm,
            "MaxSize": float(far_size_m) * mm,
            "GrowthRate": float(growth),
            "CellsPerGap": float(cells_per_gap),
            "ScopeProximityTo": "edges"}
        if size_functions == "curvature-proximity":
            sf_controls["SizeFunctions"] = "Curvature & Proximity"
            sf_controls["CurvatureNormalAngle"] = 12
        else:
            sf_controls["SizeFunctions"] = "Proximity"
        surf.Arguments = {"CFDSurfaceMeshControls": sf_controls}
        surf.Execute()
        applied.append(f"surface mesh ({size_functions}, "
                       "edge-scoped proximity)")

        desc = wt.TaskObject["Describe Geometry"]
        try:
            desc.UpdateChildTasks(SetupTypeChanged=False)
        except Exception:
            pass
        desc.Arguments = {"SetupType": "The geometry consists of only "
                                       "fluid regions with no voids"}
        try:
            desc.UpdateChildTasks(SetupTypeChanged=True)
        except Exception:
            pass
        desc.Execute()
        applied.append("describe geometry: fluid-only")
        # the multi-solid STL imports as one part per zone; the workflow
        # asks for Share Topology to join them (the nodes are already
        # merged-conformal, so this is a cheap formal join)
        _fence(applied, failed, "apply share topology",
               lambda: wt.TaskObject["Apply Share Topology"].Execute())
        _fence(applied, failed, "update boundaries (name-inferred types)",
               lambda: wt.TaskObject["Update Boundaries"].Execute())
        _fence(applied, failed, "update regions",
               lambda: wt.TaskObject["Update Regions"].Execute())

        def bl():
            t = wt.TaskObject["Add Boundary Layers"]
            t.Arguments = {"BLControlName": "profile-bl",
                           "NumberOfLayers": int(n_layers),
                           "OffsetMethodType": "uniform",
                           "FirstHeight": float(first_layer_m) * mm,
                           "Rate": float(growth)}
            t.AddChildAndUpdate()
        _fence(applied, failed,
               f"boundary layers ({n_layers} from {first_layer_m:g} m "
               f"x {growth:g})", bl)

        vol = wt.TaskObject["Generate the Volume Mesh"]
        vargs: dict = {"VolumeFill": fill}
        if fill in ("poly-hexcore", "hexcore"):
            vargs["VolumeFillControls"] = {
                "HexMaxCellLength": float(far_size_m) * mm}
        vol.Arguments = vargs
        vol.Execute()
        applied.append(f"volume mesh ({fill})")

        m.tui.file.write_mesh(f'"{str(msh_path).replace(chr(92), "/")}"')
        if not msh_path.is_file():
            raise RuntimeError("Fluent Meshing wrote no mesh file")
        applied.append("write native.msh.h5")
    finally:
        if cb is not None:
            try:
                m.transcript.unregister_callback(cb)
            except Exception:
                pass
        try:
            m.exit()
        except Exception:
            pass
        # release only our own registration — a shutdown() may have
        # already cleared it (and a later caller may hold the slot)
        with _lifecycle:
            if _mesher is m:
                _mesher = None
    text = "".join(transcript)
    n_cells = None
    # first pattern is 2026 R1's actual completion line
    # ("NNNNNN cells were created in : X minutes")
    for pat in (r"([\d,]+)\s+cells\s+were\s+created",
                r"([\d,]+)\s+cells(?:,|\s+of)", r"cells:\s*([\d,]+)",
                r"total\s+cell\s+count\s*[:=]\s*([\d,]+)"):
        hits = re.findall(pat, text, flags=re.I)
        if hits:
            n_cells = int(hits[-1].replace(",", ""))
            break
    quality = None
    qm = re.findall(r"maximum\s+(?:cell\s+)?skewness\s*[:=]?\s*"
                    r"([0-9.eE+-]+)", text, flags=re.I)
    if qm:
        try:
            quality = {"max_skewness": float(qm[-1])}
        except ValueError:
            quality = None
    return {"msh_path": str(msh_path), "n_cells": n_cells,
            "quality": quality, "applied": applied, "failed": failed,
            "meshed_in_s": round(time.time() - t0, 1),
            "mesher": "fluent-meshing"}


@mcp.tool()
def mesh_from_dxf(dxf_path: str, mesh: dict = {}) -> dict:
    """Mesh a walkthrough-style DXF (profile splines/polylines, with or
    without the domain rectangle drawn around them) straight to a Fluent
    mesh — replaces the manual Workbench/Discovery/Mechanical chain.
    When the DXF carries the domain rectangle it is used verbatim, so a
    file drawn for the manual workflow runs unmodified; otherwise the
    box is built from the walkthrough's multipliers.

    mesh accepts every MeshSpec knob: mode ("resolved" = studio y+~1
    doctrine, default; "walkthrough" = the manual workflow's
    1 mm/10-layer inflation), edge_size_m, first_layer_m, n_layers,
    growth, front_l, back_l, top_h, ground_y, far_size_m, speed_ms, nu,
    dz_frac — plus "mesher": "fluent" (default — ANSYS Fluent Meshing
    cuts the cells from a studio-built STL slab; all-ANSYS, no Docker)
    or "gmsh" (the studio mesher + containerized conversion — the
    identical-mesh cross-check route). Both meshers resolve their sizes
    through the same code path, so 'walkthrough'/'resolved' mean the
    same numbers either way. Returns the mesh path, full sizing
    provenance and the profile zone names for setup_external_aero."""
    sys.path.insert(0, str(REPO))
    from scripts import fluent_workflow as wf
    mesh = dict(mesh or {})
    mesher = mesh.pop("mesher", "fluent")
    if mesher not in ("fluent", "gmsh"):
        raise ValueError("mesher must be 'fluent' or 'gmsh'")
    # native-route surface size functions: "proximity" (default; the
    # presets' intent) or "curvature-proximity" (reference-grade LE
    # adaptation — measured 17.8M cells on the coarse slab; costly)
    sfn = mesh.pop("size_functions", "proximity")
    geo = wf.read_dxf(dxf_path, scale=mesh.pop("scale", None))
    spec = wf.MeshSpec(**mesh)
    work = WORK / f"mesh-{time.strftime('%Y%m%d-%H%M%S')}"
    # both routes run gmsh (domain mesher / plane tessellator) — the
    # caller-owned serialization + Win32 PATH restore contract applies
    lock, save_path, restore_path = _gmsh_fence()
    if mesher == "gmsh":
        with lock:
            saved = save_path()
            try:
                stats = wf.build_domain_mesh(geo["profiles"], work, spec,
                                             domain_m=geo["domain"])
            finally:
                restore_path(saved)
        stats["mesher"] = "gmsh-bridge"
        fluent_msh = wf.gmsh_to_fluent(stats["msh_path"], work)
        return {"fluent_msh": fluent_msh, "mesh": stats,
                "dxf_units_scale": geo["scale"],
                "domain_from_dxf": geo["domain"] is not None}
    profiles = wf.clean_profiles(geo["profiles"])
    s = wf.resolve_sizes(profiles, spec, geo["domain"])
    names = [f"profile_e{i+1}" for i in range(len(profiles))]
    with lock:
        saved = save_path()
        try:
            stl = wf.write_slab_stl(profiles, work / "slab.stl",
                                    (s["x0"], s["yb"], s["x1"], s["yt"]),
                                    s["dz"], zone_names=names,
                                    plane_size_m=s["far"])
        finally:
            restore_path(saved)
    mn = mesh_native(stl["stl_path"], edge_size_m=s["edge"],
                     far_size_m=s["far"], first_layer_m=s["h1"],
                     n_layers=s["n_layers_explicit"],
                     growth=spec.growth, wall_zones=names,
                     size_functions=sfn)
    stats = {"msh_path": mn["msh_path"], "n_cells": mn["n_cells"],
             "mesher": "fluent-meshing", "mode": spec.mode,
             "edge_size_m": s["edge"], "far_size_m": s["far"],
             "first_layer_m": s["h1"],
             "n_layers": s["n_layers_explicit"], "growth": spec.growth,
             "applied": mn["applied"], "failed": mn["failed"],
             "quality": mn["quality"],
             "domain_m": stl["domain_m"],
             "domain_source": "dxf" if geo["domain"] is not None
             else "auto",
             "ground": spec.ground_y is not None
             or geo["domain"] is not None,
             "length_m": s["lx"], "dz_m": s["dz"],
             "profiles": len(profiles), "profile_zones": names}
    return {"fluent_msh": mn["msh_path"], "mesh": stats,
            "dxf_units_scale": geo["scale"],
            "domain_from_dxf": geo["domain"] is not None}


@mcp.tool()
def mesh_from_app_config(config: dict, mesh_size: str = "medium",
                         mesher: str = "fluent",
                         size_functions: str = "proximity") -> dict:
    """Mesh a Wing Section Studio stack config for Fluent. mesher=
    "fluent" (default): ANSYS Fluent Meshing cuts the cells from the
    studio's geometry (STL slab; all-ANSYS, no Docker). mesher="gmsh":
    the studio's own validated mesher (slot refinement, y+~1 quad
    boundary layers) + containerized conversion — the route for
    cross-checking studio designs against the OpenFOAM screening
    engine on an IDENTICAL mesh. Both read the same section_geometry,
    so they argue about cells, never about geometry."""
    sys.path.insert(0, str(REPO))
    from app.core import cfd
    from app.core.geometry import StackConfig
    from scripts import fluent_workflow as wf
    if mesher not in ("fluent", "gmsh"):
        raise ValueError("mesher must be 'fluent' or 'gmsh'")
    cfg = StackConfig.from_dict(config)
    work = WORK / f"mesh-{time.strftime('%Y%m%d-%H%M%S')}"
    if mesher == "gmsh":
        summary = cfd.build_case(cfg, work, mesh_size)
        fluent_msh = wf.gmsh_to_fluent(work / "mesh.msh",
                                       work / "fluent_bridge")
        return {"fluent_msh": fluent_msh,
                "mesh": {"n_cells": summary["n_cells"],
                         "mesh_size": mesh_size,
                         "mesher": "gmsh-bridge",
                         "bl_mode": summary["bl_mode"],
                         "profile_zones": [f"wing_e{i+1}" for i in
                                           range(len(cfg.elements))]},
                "chord_m": cfg.chord_m, "depth_m": cfd.DZ_C * cfg.chord_m,
                "speed_ms": cfg.speed_ms, "rho": cfg.rho,
                "mu": cfg.rho * cfg.nu}
    g = cfd.section_geometry(cfg, mesh_size)
    # the STL writer's plane tessellation runs gmsh — same
    # serialization + PATH-restore contract as the mesher route
    lock, save_path, restore_path = _gmsh_fence()
    with lock:
        saved = save_path()
        try:
            stl = wf.write_slab_stl([p for p, _blunt in g["polys"]],
                                    work / "slab.stl", g["domain_m"],
                                    g["dz_m"], zone_names=g["wings"],
                                    plane_size_m=g["far_size_m"])
        finally:
            restore_path(saved)
    mn = mesh_native(stl["stl_path"], edge_size_m=g["wall_size_m"],
                     far_size_m=g["far_size_m"],
                     first_layer_m=g["first_layer_m"],
                     n_layers=g["n_bl_layers"], growth=g["bl_growth"],
                     cells_per_gap=g["slot_gap_cells"],
                     wall_zones=g["wings"],
                     size_functions=size_functions)
    return {"fluent_msh": mn["msh_path"],
            "mesh": {"n_cells": mn["n_cells"],
                     "mesh_size": mesh_size,
                     "mesher": "fluent-meshing",
                     "applied": mn["applied"], "failed": mn["failed"],
                     "quality": mn["quality"],
                     "profile_zones": g["wings"]},
            "chord_m": cfg.chord_m, "depth_m": g["dz_m"],
            "speed_ms": cfg.speed_ms, "rho": cfg.rho,
            "mu": cfg.rho * cfg.nu}


def _dxf_ground_2d(dxf_path: str | Path) -> bool:
    """Ground provenance of a walkthrough-style 2D DXF: True when the
    DOMAIN rectangle's floor is the ground plane — drawn on the
    installed frame's y = 0, or hugging the lowest profile point by a
    ride-height gap — False when it mirrors well below the section
    (write_dxf_2d's ground=False free-air layout puts it 3 stack
    heights down). An unreadable or rectangle-less file reads as
    ground: the chain's SpaceClaim stage requires the rectangle and
    fails loudly there, never here."""
    from scripts import fluent_workflow as wf
    try:
        g = wf.read_dxf(dxf_path)
    except Exception:
        return True
    dom, profs = g["domain"], g["profiles"]
    if dom is None or not profs:
        return True
    y_lo = min(float(p[:, 1].min()) for p in profs)
    y_hi = max(float(p[:, 1].max()) for p in profs)
    h = max(y_hi - y_lo, 1e-9)
    return abs(dom[1]) <= 1e-3 * h or (y_lo - dom[1]) <= 1.5 * h


@mcp.tool()
def mesh_2d(dxf_path: str, sizing: str = "default", config: dict = {},
            version: str = "", sc_budget_s: float = 0.0,
            wb_budget_s: float = 0.0) -> dict:
    """True-2D mesh from a walkthrough-style DXF through the documented
    manual ANSYS Workbench chain (SpaceClaim fill -> Mechanical mesh ->
    FFF.msh) — the manual 2D route, automated end to end with no gmsh
    and no Docker. sizing="default" is the manual walkthrough's numbers
    exactly: 0.1 mm profile edge sizing, 1 mm first inflation
    layer, 10 layers; "studio-yplus1" resolves the first layer to
    y+~1 from the app's correlation (config must carry the stack's
    chord/speed — a studio stack config dict). Zones arrive named
    inlet/outlet/ground/upper_bound/profile: load the mesh with
    launch(dimension=2) + read_mesh(), then
    setup_external_aero(conventions="default-2d",
    profile_zones=["profile"], slip_zones=["upper_bound"]) — the slab
    routes' default slip zone "top" does not exist on these meshes
    (the untouched default resolves to upper_bound, but say what you
    mean).

    sc_budget_s / wb_budget_s are the SpaceClaim and Workbench stage
    wall-clock budgets in seconds; 0 (default) keeps the chain's own
    300 s / 900 s."""
    if sizing not in ("default", "studio-yplus1"):
        raise ValueError("sizing must be 'default' or 'studio-yplus1'")
    budgets = {}
    if sc_budget_s:
        if not 30.0 <= float(sc_budget_s) <= 7200.0:
            raise ValueError("sc_budget_s must be between 30 and 7200 s")
        budgets["sc_budget_s"] = float(sc_budget_s)
    if wb_budget_s:
        if not 60.0 <= float(wb_budget_s) <= 43200.0:
            raise ValueError("wb_budget_s must be between 60 and "
                             "43200 s")
        budgets["wb_budget_s"] = float(wb_budget_s)
    sys.path.insert(0, str(REPO))
    from scripts import fluent2d_workflow as wf2
    sz = wf2.mesh_sizing(sizing, dict(config or {}))
    work = WORK / f"mesh2d-{time.strftime('%Y%m%d-%H%M%S')}"
    r = wf2.run_chain(
        str(dxf_path), work, edge_size_mm=sz["edge_size_mm"],
        first_layer_mm=sz["first_layer_mm"], n_layers=sz["n_layers"],
        growth=sz["growth"], version=version, **budgets)
    return {"msh_path": r["msh_path"], "n_cells": r["n_cells"],
            "zones": r["zones"], "stage_s": r["stage_s"],
            "project_dir": r["project_dir"], "sizing": sizing,
            "sizes": sz, "mesher": "ansys-2d",
            "ground": _dxf_ground_2d(dxf_path)}


@mcp.tool()
def run_case(spec: dict) -> dict:
    """One call, whole workflow: geometry -> mesh -> launch -> setup ->
    solve -> coefficients with a drift verdict. The automated equivalent
    of the documented manual ANSYS chain, with every stage configurable:

    spec = {
      "dimension": 3,                   # 3 (default) = slab meshes;
                                        # 2 = the manual walkthrough's
                                        # true-2D Workbench chain
                                        # (mesh_2d, mesher "ansys-2d",
                                        # conventions default
                                        # "default-2d")
      "geometry": {"dxf_path": "..."}            # walkthrough DXF, or
               | {"app_config": {...}, "mesh_size": "coarse|medium|fine",
                  "mesher": "fluent|gmsh"}       # native ANSYS default
               | {"msh_path": "..."},            # pre-made Fluent mesh
      "mesh": { MeshSpec knobs incl. "mesher", DXF route only — see
                mesh_from_dxf; dimension=2 instead takes "sizing":
                "default|studio-yplus1" (+ "config" for studio sizing,
                "sc_budget_s"/"wb_budget_s" for the stage budgets) },
      "physics": {"velocity_ms": 15.0, "rho": 1.225, "mu": 1.8375e-5,
                  "chord_m": null,      # null = from geometry
                  "depth_m": null,      # null = from geometry (slab dz)
                  "moving_ground": null, # null = mesh provenance: a
                                        # free-air DXF gets a slip
                                        # floor (3D: no rectangle, no
                                        # ground_y; 2D: the rectangle's
                                        # floor mirrored below the
                                        # section, not on its ground
                                        # line); everything else the
                                        # moving wall
                  "downforce_positive": true,
                  "profile_zones": null # null = from geometry
                 },
      "conventions": "studio",          # or "walkthrough" (manual
                                        # parity — for measurement,
                                        # not for use) or "default-2d"
                                        # (the dimension=2 default)
      "solve": {"iterations": 3000,
                "continue_on_setup_failure": false},
      "launch": {"processors": 4, "precision": "double"}
    }
    Missing keys take the defaults above. A live session is reused when
    its precision/processor count match (its previous report histories
    are purged so the new run's verdicts start clean); otherwise one is
    launched. Failed setup steps skip the solve unless
    solve.continue_on_setup_failure opts in."""
    sys.path.insert(0, str(REPO))
    spec = dict(spec or {})
    dim = int(spec.get("dimension", 3))
    if dim not in (2, 3):
        raise ValueError("dimension must be 2 or 3")
    geo = dict(spec.get("geometry") or {})
    phys = dict(spec.get("physics") or {})
    conv = spec.get("conventions",
                    "default-2d" if dim == 2 else "studio")
    # validated HERE, not where the setup consumes it: setup_external_aero
    # runs after the whole meshing chain and the solver license checkout,
    # so a bad (or retired) name would burn minutes of ANSYS batch seats
    # and a license before answering. Same wording as the setup's own
    # check, which still re-checks
    if conv not in ("studio", "walkthrough", "default-2d"):
        raise ValueError("conventions must be 'studio', 'walkthrough' "
                         "or 'default-2d'")
    sol = dict(spec.get("solve") or {})
    lau = dict(spec.get("launch") or {})
    out: dict = {"conventions": conv, "dimension": dim}

    u = float(phys.get("velocity_ms", 15.0))
    rho = float(phys.get("rho", 1.225))
    mu = float(phys.get("mu", 1.8375e-05))
    chord = phys.get("chord_m")
    depth = phys.get("depth_m")
    zones = phys.get("profile_zones")

    ground_default = True
    slip = ["top"]
    if dim == 2:
        # the true-2D Workbench chain of the documented manual ANSYS
        # workflow: SpaceClaim fill -> Mechanical mesh -> FFF.msh, with
        # that workflow's zone names
        mesh_opts = dict(spec.get("mesh") or {})
        mesher = mesh_opts.pop("mesher", geo.get("mesher", "ansys-2d"))
        if mesher != "ansys-2d":
            raise ValueError("dimension=2 meshes through the Workbench "
                             "chain — mesher must be 'ansys-2d'")
        slip = ["upper_bound"]
        sizing = mesh_opts.pop("sizing", "default")
        mesh_cfg = mesh_opts.pop("config", None) or {}
        # a null budget means "the documented default", not "0 s"
        budget_kw = {}
        for _k in ("sc_budget_s", "wb_budget_s"):
            _v = mesh_opts.pop(_k, None)
            if _v is not None:
                budget_kw[_k] = float(_v)
        if mesh_opts:
            # a silently dropped knob is a default-sized mesh the caller
            # believes they resized — mirror the 3D route, where
            # MeshSpec(**mesh) rejects unknown keys
            raise ValueError("dimension=2 mesh accepts only mesher/"
                             "sizing/config/sc_budget_s/wb_budget_s — "
                             f"unknown: {sorted(mesh_opts)}")
        if "msh_path" in geo:
            msh = geo["msh_path"]
        elif "dxf_path" in geo:
            m = mesh_2d(geo["dxf_path"], sizing=sizing, config=mesh_cfg,
                        **budget_kw)
            out["mesh"] = m
            msh = m["msh_path"]
            # the chain's provenance, exactly like the 3D DXF route: a
            # free-air rectangle (floor mirrored below the section, not
            # on the ground line) gets the slip floor, never the belt
            ground_default = bool(m.get("ground", True))
        elif "app_config" in geo:
            from app.core import cfd
            from app.core.geometry import StackConfig
            from scripts import fluent2d_workflow as wf2
            cfg = StackConfig.from_dict(geo["app_config"])
            # polys only — meters, ground at y=0; the 2D domain
            # rectangle comes from write_dxf_2d, never from the slab
            g = cfd.section_geometry(cfg, "coarse")
            work = WORK / f"dxf2d-{time.strftime('%Y%m%d-%H%M%S')}"
            work.mkdir(parents=True, exist_ok=True)
            d = wf2.write_dxf_2d([p for p, _blunt in g["polys"]],
                                 str(work / "section.dxf"))
            m = mesh_2d(d["dxf_path"], sizing=sizing,
                        config=geo["app_config"], **budget_kw)
            out["mesh"] = m
            msh = m["msh_path"]
            chord = chord or cfg.chord_m
            if "velocity_ms" not in phys:
                u = cfg.speed_ms
            if "rho" not in phys:
                rho = cfg.rho
            if "mu" not in phys:
                mu = cfg.rho * cfg.nu
        else:
            raise ValueError("geometry needs dxf_path, app_config or "
                             "msh_path")
        # the chain groups every profile island's edges into ONE zone
        zones = zones or ["profile"]
        depth = depth or 1.0
        if not chord:
            if conv == "studio":
                raise ValueError("dimension=2 with studio conventions "
                                 "needs physics.chord_m for the chord "
                                 "references")
            chord = 1.0   # default-2d refs are 1 m^2 / 1 m — unused
    elif "dxf_path" in geo:
        mesh_opts = dict(spec.get("mesh") or {})
        mesh_opts.setdefault("speed_ms", u)
        mesh_opts.setdefault("nu", mu / rho)
        m = mesh_from_dxf(geo["dxf_path"], mesh_opts)
        out["mesh"] = m["mesh"]
        msh = m["fluent_msh"]
        chord = chord or m["mesh"]["length_m"]
        depth = depth or m["mesh"]["dz_m"]
        zones = zones or m["mesh"]["profile_zones"]
        # the mesher's own provenance: no domain rectangle and no
        # ground_y means free air — the floor is a mirrored slip
        # plane, not a road (studio app_config sections are always
        # ground-referenced, so only the DXF route can be free air)
        ground_default = bool(m["mesh"].get("ground", True))
    elif "app_config" in geo:
        m = mesh_from_app_config(geo["app_config"],
                                 geo.get("mesh_size", "medium"),
                                 geo.get("mesher", "fluent"))
        out["mesh"] = m["mesh"]
        msh = m["fluent_msh"]
        chord = chord or m["chord_m"]
        depth = depth or m["depth_m"]
        zones = zones or m["mesh"]["profile_zones"]
        if "velocity_ms" not in phys:
            u = m["speed_ms"]
        if "rho" not in phys:
            rho = m["rho"]
        if "mu" not in phys:
            mu = m["mu"]
    elif "msh_path" in geo:
        msh = geo["msh_path"]
        if not (chord and depth and zones):
            raise ValueError("msh_path geometry needs explicit physics."
                             "chord_m, depth_m and profile_zones")
    else:
        raise ValueError("geometry needs dxf_path, app_config or "
                         "msh_path")

    procs = int(lau.get("processors", 4))
    prec = lau.get("precision", "double")
    alive = False
    if _solver is not None:
        try:
            alive = str(_solver.health_check.status()) == "Status.SERVING"
        except Exception:
            alive = False
    wanted = {"dimension": dim, "precision": prec, "processors": procs}
    if not (alive and _launch_params == wanted):
        out["launch"] = launch(dimension=dim, precision=prec,
                               processors=procs)
    elif _session_dir is not None:
        # reused session, same cwd: Fluent resumes appending to the
        # existing *-rfile.out files and _report_histories() would
        # concatenate the previous case's converged tail into this
        # run's drift/tail verdicts — purge before the new reports
        for f in _session_dir.glob("*.out"):
            try:
                f.unlink()
            except OSError:
                pass
    out["zones_after_read"] = read_mesh(msh)["zones"]
    mg = phys.get("moving_ground")
    mg = ground_default if mg is None else bool(mg)
    out["setup"] = setup_external_aero(
        inlet_velocity_ms=u, profile_zones=list(zones),
        chord_m=float(chord), depth_m=float(depth),
        slip_zones=slip,
        moving_ground=mg,
        rho=rho, mu=mu,
        downforce_positive=bool(phys.get("downforce_positive", True)),
        conventions=conv)
    out["ground_treatment"] = ("moving no-slip wall (inlet speed)"
                               if mg else "shear-free (slip floor)")
    if out["setup"]["failed"] and not sol.get("continue_on_setup_failure"):
        # a failed step means the case is not the recipe — the in-app
        # engine fails the job outright; burning the full iteration
        # budget here would return plausible coefficients off a
        # partially configured case with the evidence buried mid-payload
        out["error"] = (
            "setup steps failed - solve skipped: "
            + "; ".join(f["step"] for f in out["setup"]["failed"])
            + " (set solve.continue_on_setup_failure to solve anyway)")
        out["session_dir"] = str(_session_dir)
        return out
    out["solve"] = solve(iterations=int(sol.get("iterations", 3000)))
    out["session_dir"] = str(_session_dir)
    return out


@mcp.tool()
def contour_png(field: str = "velocity-magnitude",
                filename: str = "contour.png") -> dict:
    """Best-effort contour render from the headless session (the
    walkthrough's 'graphics' step). Some builds refuse to rasterize
    without a GUI — on failure, write_case_data() + ParaView, or the
    studio's own flow view for app-sourced cases, are the reliable
    routes."""
    s = _require()
    path = str((_session_dir or WORK) / filename).replace("\\", "/")
    try:
        s.scheme.eval('(ti-menu-load-string "display/set/picture/'
                      'driver png")')
        s.scheme.eval(f'(ti-menu-load-string "display/contour {field} '
                      f', ,")')
        s.scheme.eval(f'(ti-menu-load-string "display/save-picture '
                      f'\\"{path}\\"")')
        p = Path(path)
        if p.is_file() and p.stat().st_size > 1000:
            return {"written": str(p), "bytes": p.stat().st_size}
        return {"failed": "no picture produced (headless rasterizer "
                          "unavailable) — use write_case_data + ParaView"}
    except Exception as e:
        return {"failed": f"{type(e).__name__}: {e}"}


@mcp.tool()
def shutdown() -> dict:
    """Exit the Fluent session(s) — solver and any meshing session —
    and release the ANSYS license(s)."""
    global _solver, _mesher
    released = []
    with _lifecycle:
        mesher, solver = _mesher, _solver
        # a claim means a mesher is still LAUNCHING — no handle to exit
        # yet; the claim stays with its owner (whose own cleanup runs on
        # the raise/finally path), so a later caller cannot double-launch
        if mesher is _MESH_CLAIM:
            mesher = None
        else:
            _mesher = None
        _solver = None
    if mesher is not None:
        try:
            mesher.exit()
        except Exception:
            pass
        released.append("meshing")
    if solver is not None:
        solver.exit()
        released.append("solver")
    if not released:
        return {"status": "no session"}
    return {"status": f"exited ({' + '.join(released)}) — license "
                      f"released"}


def _cleanup() -> None:
    global _solver, _mesher
    for handle in ("_solver", "_mesher"):
        s = globals()[handle]
        if s is not None and s is not _MESH_CLAIM:
            try:
                s.exit()
            except Exception:
                pass
            globals()[handle] = None


atexit.register(_cleanup)

if __name__ == "__main__":
    mcp.run()
