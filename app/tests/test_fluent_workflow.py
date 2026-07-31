"""Fluent workflow front half — everything that runs without Fluent,
Docker or a license: DXF profile/domain extraction (walkthrough-style
files: splines, polylines, the domain rectangle drawn around the
profile), loop chaining, and the generalized domain mesher in both its
modes (manual-walkthrough inflation vs studio resolved-wall).

Run directly:  python app/tests/test_fluent_workflow.py
"""
import math
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scripts import fluent_workflow as wf  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def oval(cx, cy, rx, ry, n=80):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    return np.column_stack([cx + rx * np.cos(t), cy + ry * np.sin(t)])


TMP = Path(tempfile.mkdtemp(prefix="wss_fluent_wf_"))

# ---- DXF reading: walkthrough-style file (profile + domain rect) ----

import ezdxf  # noqa: E402

doc = ezdxf.new("R2010")
doc.header["$INSUNITS"] = 4          # millimetres, the CAD default
msp = doc.modelspace()
# profile: a closed spline oval, chord 100 mm, sitting 30 mm above y=0
# (the first fit point repeated at the end — coincident endpoints, the
# way CAD exports a closed profile spline)
prof_mm = oval(50, 45, 50, 15, n=48)
msp.add_spline(fit_points=[(x, y, 0) for x, y in
                           np.vstack([prof_mm, prof_mm[:1]])])
# domain rectangle per the walkthrough: 3L front, 7L back, ~3H above,
# ground at y=0
msp.add_lwpolyline([(-300, 0), (800, 0), (800, 250), (-300, 250)],
                   close=True)
dxf_rect = TMP / "walkthrough.dxf"
doc.saveas(dxf_rect)

geo = wf.read_dxf(dxf_rect)
check("walkthrough DXF: one profile, domain rectangle separated out",
      len(geo["profiles"]) == 1 and geo["domain"] is not None,
      f"({len(geo['profiles'])} profiles, domain {geo['domain']})")
check("mm units scale to meters",
      abs(geo["scale"] - 1e-3) < 1e-12
      and abs(geo["profiles"][0][:, 0].max() - 0.100) < 2e-3
      and abs(geo["domain"][2] - 0.800) < 1e-6,
      f"(x_max {geo['profiles'][0][:, 0].max():.4f} m)")
check("profile loop is closed and ordered",
      len(geo["profiles"][0]) >= 40)

# ---- chaining: profile drawn as two open halves must weld ----

doc2 = ezdxf.new("R2010")
doc2.header["$INSUNITS"] = 4
msp2 = doc2.modelspace()
upper = prof_mm[:24]
lower = np.vstack([prof_mm[23:], prof_mm[:1]])
msp2.add_spline(fit_points=[(x, y, 0) for x, y in upper])
msp2.add_spline(fit_points=[(x, y, 0) for x, y in lower])
dxf_halves = TMP / "halves.dxf"
doc2.saveas(dxf_halves)
geo2 = wf.read_dxf(dxf_halves)
check("two open splines weld into one closed profile",
      len(geo2["profiles"]) == 1 and geo2["domain"] is None,
      f"({len(geo2['profiles'])} loops)")

# ---- multi-element + no rectangle ----

doc3 = ezdxf.new("R2010")
doc3.header["$INSUNITS"] = 6         # meters
msp3 = doc3.modelspace()
for c in (oval(0.05, 0.04, 0.05, 0.012), oval(0.13, 0.06, 0.03, 0.008)):
    msp3.add_lwpolyline([(x, y) for x, y in c], close=True)
dxf_two = TMP / "two.dxf"
doc3.saveas(dxf_two)
geo3 = wf.read_dxf(dxf_two)
check("two closed polylines -> two profiles, largest first, no domain",
      len(geo3["profiles"]) == 2 and geo3["domain"] is None
      and wf._poly_area(geo3["profiles"][0])
      > wf._poly_area(geo3["profiles"][1]))

# ---- mesher: walkthrough mode inside the DXF's own rectangle ----

spec_walk = wf.MeshSpec(mode="walkthrough", edge_size_m=3e-3,
                        first_layer_m=1e-3, n_layers=6)
stats = wf.build_domain_mesh(geo["profiles"], TMP / "mesh_walkthrough",
                             spec_walk, domain_m=geo["domain"])
check("walkthrough-mode mesh generates with cells",
      stats["n_cells"] > 1000 and Path(stats["msh_path"]).is_file(),
      f"({stats['n_cells']} cells, bl {stats['bl_mode']})")
check("walkthrough-mode sizing provenance is explicit",
      stats["mode"] == "walkthrough" and stats["first_layer_m"] == 1e-3
      and stats["n_layers"] == 6 and stats["domain_source"] == "dxf"
      and stats["profile_zones"] == ["profile_e1"])

msh_text = Path(stats["msh_path"]).read_text()
names = set()
if "$PhysicalNames" in msh_text:
    import re
    names = set(re.findall(
        r'^\d+ \d+ "([^"]+)"$',
        msh_text[msh_text.index("$PhysicalNames"):
                 msh_text.index("$EndPhysicalNames")], re.M))
check("mesh declares the workflow's physical groups",
      {"inlet", "outlet", "top", "ground", "profile_e1",
       "frontAndBack", "internal"} <= names, f"({sorted(names)})")
check("mesh is MSH2 (gmshToFoam-compatible)",
      msh_text.splitlines()[1].startswith("2.2"))

# ---- resolved mode: y+ ~ 1 first layer from the correlation ----

spec_res = wf.MeshSpec(mode="resolved", speed_ms=20.0, nu=1.5e-5,
                       edge_size_m=3e-3)
stats_r = wf.build_domain_mesh(geo3["profiles"], TMP / "mesh_res",
                               spec_res)
L = float(np.vstack(geo3["profiles"])[:, 0].max()
          - np.vstack(geo3["profiles"])[:, 0].min())
expect_h1 = wf._first_layer_resolved(20.0, 1.5e-5, L)
check("resolved-mode first layer follows the y+~1 correlation",
      abs(stats_r["first_layer_m"] - expect_h1) < 1e-12
      and stats_r["first_layer_m"] < 1e-4,
      f"(h1 {stats_r['first_layer_m']:.2e} m)")
check("free-air domain builds from the walkthrough multipliers "
      "(no ground)",
      stats_r["domain_source"] == "auto" and stats_r["ground"] is False
      and stats_r["profiles"] == 2
      and stats_r["profile_zones"] == ["profile_e1", "profile_e2"])
check("free-air floor sits below the profiles",
      stats_r["domain_m"][1] < float(
          np.vstack(geo3["profiles"])[:, 1].min()))

# ---- validation ----

for bad_mode in ("ultra", "team"):
    try:
        wf.build_domain_mesh(geo["profiles"], TMP / "x",
                             wf.MeshSpec(mode=bad_mode))
        check(f"mesh mode {bad_mode!r} is rejected", False)
    except ValueError:
        check(f"mesh mode {bad_mode!r} is rejected", True)
try:
    wf.build_domain_mesh(geo["profiles"], TMP / "y",
                         wf.MeshSpec(ground_y=0.05))
    check("ground above the profile is rejected", False)
except ValueError:
    check("ground above the profile is rejected", True)
try:
    wf.read_dxf(TMP / "nothing_here.dxf")
    check("missing DXF raises", False)
except Exception:
    check("missing DXF raises", True)

# ---- degenerate vertices: duplicated consecutive + closing points ----
# (the DXF round-trip regression: gmsh refuses zero-length lines)
dirty = oval(0.05, 0.05, 0.04, 0.012, n=36)
dirty = np.repeat(dirty, 2, axis=0)          # every vertex duplicated
dirty = np.vstack([dirty, dirty[:1]])        # closing duplicate too
stats_d = wf.build_domain_mesh([dirty], TMP / "mesh_dirty",
                               wf.MeshSpec(mode="walkthrough",
                                           edge_size_m=3e-3,
                                           n_layers=4))
check("duplicated vertices are cleaned before meshing",
      stats_d["n_cells"] > 500, f"({stats_d['n_cells']} cells)")

# ---- native route: shared sizing + watertight multi-solid STL ----

# both meshers must resolve identical numbers from one spec
sz = wf.resolve_sizes(wf.clean_profiles(geo3["profiles"]), spec_res)
check("resolve_sizes matches the gmsh mesher's recorded provenance",
      abs(sz["edge"] - stats_r["edge_size_m"]) < 1e-15
      and abs(sz["far"] - stats_r["far_size_m"]) < 1e-15
      and abs(sz["h1"] - stats_r["first_layer_m"]) < 1e-15
      and [round(v, 5) for v in
           (sz["x0"], sz["yb"], sz["x1"], sz["yt"])]
      == stats_r["domain_m"],
      f"(edge {sz['edge']:.2e}, h1 {sz['h1']:.2e})")
check("explicit layer count derived for the native mesher",
      4 <= sz["n_layers_explicit"] <= 30
      and wf.resolve_sizes(wf.clean_profiles(geo["profiles"]),
                           spec_walk,
                           geo["domain"])["n_layers_explicit"] == 6,
      f"(resolved-mode {sz['n_layers_explicit']} layers)")

# a concave profile (reflex TE cusp) + a second element, boxed
hook = np.array([[0.00, 0.000], [0.06, -0.010], [0.12, -0.006],
                 [0.10, 0.012], [0.06, 0.008], [0.02, 0.018]])
stl_profiles = [hook + [0.0, 0.05], oval(0.16, 0.075, 0.03, 0.008)]
stl = wf.write_slab_stl(stl_profiles, TMP / "slab.stl",
                        (-0.4, 0.0, 1.0, 0.5), dz=0.02,
                        zone_names=["wing_e1", "wing_e2"])
check("slab STL: all boundary-zone solids present, named for "
      "Fluent's type inference",
      set(stl["zones"]) == {"symmetry-back", "symmetry-front", "inlet",
                            "outlet", "ground", "top", "wing_e1",
                            "wing_e2"}, f"({stl['zones']})")
vol_want = (1.4 * 0.5 - sum(wf._poly_area(np.asarray(p))
                            for p in stl_profiles)) * 0.02
check("slab volume equals box minus tunnels (closed, outward normals)",
      math.isclose(stl["volume_m3"], vol_want, rel_tol=1e-6),
      f"({stl['volume_m3']:.6g} vs {vol_want:.6g})")
stl_lines = Path(stl["stl_path"]).read_text().splitlines()
check("STL is ASCII multi-solid with matching facet count",
      sum(ln.startswith(" facet normal") for ln in stl_lines)
      == stl["n_facets"]
      and stl_lines.count("solid wing_e1") == 1
      and stl_lines.count("endsolid symmetry-front") == 1)
# conformality and volume are self-checked in the writer (it raises
# on any non-closed surface); the input guards must fire too
try:
    wf.write_slab_stl([stl_profiles[0]], TMP / "slab_bad.stl",
                      (-0.4, 0.0, 1.0, 0.5), dz=-1.0)
    check("non-positive slab depth is rejected", False)
except ValueError:
    check("non-positive slab depth is rejected", True)
try:
    wf.write_slab_stl(stl_profiles, TMP / "slab_bad2.stl",
                      (-0.4, 0.0, 1.0, 0.5), dz=0.02,
                      zone_names=["only_one"])
    check("zone-name count mismatch is rejected", False)
except ValueError:
    check("zone-name count mismatch is rejected", True)

# ---- spline flattening: physical sagitta target, units-aware ----
# the same physical circle drawn in a meters file and a mm file must
# flatten to comparable point counts (a fixed drawing-unit tolerance
# gave the meters file a millimetre sagitta — the LE collapsed)

circ_m = oval(0.0, 0.1, 0.03, 0.03, n=8)


def _spline_dxf(units, pts, name):
    d = ezdxf.new("R2010")
    d.header["$INSUNITS"] = units
    d.modelspace().add_spline(
        fit_points=[(x, y, 0) for x, y in np.vstack([pts, pts[:1]])])
    p = TMP / name
    d.saveas(p)
    return wf.read_dxf(p)["profiles"][0]


pm = _spline_dxf(6, circ_m, "circle_m.dxf")
pmm = _spline_dxf(4, circ_m * 1000.0, "circle_mm.dxf")
check("meters-file spline flattening meets the physical sagitta target",
      len(pm) > 50, f"({len(pm)} points; drawing-unit tol gave ~32)")
check("flattening density is invariant to the DXF's units",
      0.7 < len(pm) / len(pmm) < 1.4,
      f"(m {len(pm)} vs mm {len(pmm)} points)")

# ---- explicit layer count respects the clearance cap (native route) ----
# two elements ~7 mm apart: the walkthrough's 10-layer/1 mm stack
# (~26 mm) must be capped through the geometric series the way the gmsh
# route's Thickness field caps the stack — the native mesher takes a
# count
near = [oval(0.05, 0.05, 0.05, 0.010, n=48),
        oval(0.05, 0.075, 0.04, 0.008, n=48)]
szc = wf.resolve_sizes(near, wf.MeshSpec(mode="walkthrough"))
check("walkthrough-mode explicit layer count is capped by the slot "
      "clearance",
      1 <= szc["n_layers_explicit"] <= 3 and max(szc["thick"]) < 0.005,
      f"({szc['n_layers_explicit']} layers, thick {szc['thick']})")
szo = wf.resolve_sizes([oval(0.05, 0.2, 0.05, 0.015, n=48)],
                       wf.MeshSpec(mode="walkthrough"))
check("open-clearance walkthrough stack keeps its explicit 10 layers",
      szo["n_layers_explicit"] == 10)

# ---- fluent_mcp: offline checks against the MCP server module ----
# (no Fluent, no license: everything below fakes the session layer)

from scripts import fluent_mcp as fm  # noqa: E402

# _drift mirrors the studio's numbers (800-row windows, 100-row floor)
check("_drift refuses to judge below 100 rows per window",
      fm._drift([2.5] * 299) is None and fm._drift([2.5] * 300) == 0.0)
slow = [2.3 + 1.5e-5 * i for i in range(3000)]
d_slow = fm._drift(slow)
check("_drift's 800-row windows catch a slow trend (200-row read flat)",
      d_slow is not None and d_slow > 0.004,
      f"(drift {d_slow:.5f}; 200-row windows read ~0.0013)")


class _Rec:
    """Attribute-tree recorder standing in for a pyfluent session."""

    def __init__(self, log, path=""):
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_kids", {})

    def _child(self, key):
        kids = object.__getattribute__(self, "_kids")
        if key not in kids:
            sep = "." if self._path else ""
            kids[key] = _Rec(self._log, f"{self._path}{sep}{key}")
        return kids[key]

    def __getattr__(self, k):
        if k.startswith("__"):
            raise AttributeError(k)
        return self._child(k)

    def __setattr__(self, k, v):
        sep = "." if self._path else ""
        self._log.append((f"{self._path}{sep}{k}", v))

    def __getitem__(self, k):
        return self._child(f"[{k}]")

    def __setitem__(self, k, v):
        self._log.append((f"{self._path}[{k}]", v))

    def __call__(self, *a, **kw):
        self._log.append((f"{self._path}()", kw))
        return None

    def keys(self):
        return []


def _has(log, suffix, val):
    return any(p.endswith(suffix) and v == val for p, v in log)


_saved_state = (fm._solver, fm._session_dir, dict(fm._launch_params),
                fm._mesher)

# studio conventions pin the inlet/backflow turbulence to the OpenFOAM
# cases' spec (1% intensity, viscosity ratio 10); walkthrough keeps the
# Fluent defaults for manual-workflow parity
setup_log = []
fm._solver = _Rec(setup_log)
fm._dim = 3
su = fm.setup_external_aero(inlet_velocity_ms=15.0,
                            profile_zones=["wing_e1"], chord_m=0.35,
                            depth_m=0.035)
check("studio setup pins inlet turbulence to the OpenFOAM inlet spec",
      _has(setup_log, ".turbulence.turbulent_intensity", 0.01)
      and _has(setup_log, ".turbulence.turbulent_viscosity_ratio", 10.0)
      and su["failed"] == [],
      f"(failed: {su['failed']})")
check("studio setup pins the outlet backflow turbulence too",
      _has(setup_log, ".turbulence.backflow_turbulent_intensity", 0.01)
      and _has(setup_log,
               ".turbulence.backflow_turbulent_viscosity_ratio", 10.0))
walk_log = []
fm._solver = _Rec(walk_log)
su_t = fm.setup_external_aero(inlet_velocity_ms=15.0,
                              profile_zones=["wing_e1"], chord_m=0.35,
                              depth_m=0.035, conventions="walkthrough")
check("walkthrough keeps Fluent-default turbulence BCs (parity)",
      not any("turbulent_intensity" in p for p, _v in walk_log)
      and any("turbulence BCs left at Fluent defaults" in a
              for a in su_t["applied"]))
# the compute-from-inlet step never touches area or length — the
# payload must report the 1 m^2 / 1 m the live case actually holds
check("walkthrough metadata reports the references Fluent holds",
      su_t["reference_area_m2"] == 1.0
      and su_t["reference_length_m"] == 1.0)

# solve()'s verdict excludes the hybrid-init transient and needs the
# studio's row minimum before judging
sess = TMP / "mcp_sess"
sess.mkdir()
rfile = sess / "lift_coef-rfile.out"


def _write_hist(n_total):
    rows = ['("Iteration" "lift_coef")']
    for i in range(1, n_total + 1):
        v = 1.0 + 1.5 * min(i, 200) / 200.0   # 200-row climb, then flat
        rows.append(f"{i} {v:.6f}")
    rfile.write_text("\n".join(rows) + "\n")


fm._session_dir = sess
_write_hist(600)
fm._solver = _Rec([])
sv = fm.solve(iterations=3, initialize=False)
check("solve() judges drift on the transient-excluded history",
      sv["cl"]["history_rows"] == 600 and sv["cl"]["drift"] == 0.0
      and sv["converged_hint"] is True,
      f"(drift {sv['cl']['drift']})")
_write_hist(400)
fm._solver = _Rec([])
sv2 = fm.solve(iterations=3, initialize=False)
check("solve() reports None when the post-transient history is short",
      sv2["cl"]["drift"] is None and sv2["converged_hint"] is None)

# run_case: session-reuse purge, ground provenance, setup gate
_real = {n: getattr(fm, n) for n in
         ("launch", "read_mesh", "setup_external_aero", "solve",
          "mesh_from_dxf")}
setup_kw: dict = {}
fail_steps: list = []
solve_calls: list = []
launched: list = []


class _AliveSolver:
    class _HC:
        @staticmethod
        def status():
            return "Status.SERVING"
    health_check = _HC()


def _fake_launch(dimension=3, precision="double", processors=4,
                 version=""):
    launched.append(int(processors))
    fm._session_dir = TMP / "mcp_sess2"
    fm._session_dir.mkdir(exist_ok=True)
    fm._launch_params = {"dimension": 3, "precision": precision,
                         "processors": int(processors)}
    return {"launched_in_s": 0.0}


def _fake_setup(**kw):
    setup_kw.clear()
    setup_kw.update(kw)
    return {"applied": [], "failed": list(fail_steps)}


def _fake_solve(iterations=500, initialize=True):
    solve_calls.append(int(iterations))
    return {"iterations_requested": int(iterations)}


fm.launch = _fake_launch
fm.read_mesh = lambda p: {"zones": {}}
fm.setup_external_aero = _fake_setup
fm.solve = _fake_solve

MSH_SPEC = {"geometry": {"msh_path": "m.msh"},
            "physics": {"chord_m": 0.35, "depth_m": 0.035,
                        "profile_zones": ["w1"]},
            "solve": {"iterations": 100}}
try:
    stale = sess / "lift_coef-rfile.out"   # left over from the solve()
    stale.write_text("1 1.0\n")            # tests above — a stale tail
    fm._solver = _AliveSolver()
    fm._launch_params = {"dimension": 3, "precision": "double",
                         "processors": 4}
    fm._session_dir = sess
    out_r = fm.run_case(MSH_SPEC)
    check("run_case reuse purges the previous case's report histories",
          not stale.exists() and "launch" not in out_r
          and solve_calls == [100])
    check("msh-path route keeps the moving ground default",
          setup_kw["moving_ground"] is True
          and out_r["ground_treatment"].startswith("moving"))

    stale2 = sess / "drag_coef-rfile.out"
    stale2.write_text("1 0.1\n")
    fm._solver = None                      # dead session -> fresh launch
    out_f = fm.run_case(MSH_SPEC)
    check("fresh launch path rotates the session dir, purges nothing",
          "launch" in out_f and launched == [4] and stale2.exists())

    fm.mesh_from_dxf = lambda p, m: {
        "fluent_msh": "y.msh",
        "mesh": {"ground": False, "length_m": 0.3, "dz_m": 0.03,
                 "profile_zones": ["profile_e1"]},
        "dxf_units_scale": 1.0, "domain_from_dxf": False}
    out_d = fm.run_case({"geometry": {"dxf_path": "free.dxf"}})
    check("free-air DXF provenance defaults the floor to slip",
          setup_kw["moving_ground"] is False
          and out_d["ground_treatment"].startswith("shear-free"))
    fm.run_case({"geometry": {"dxf_path": "free.dxf"},
                 "physics": {"moving_ground": True}})
    check("explicit moving_ground overrides the mesh provenance",
          setup_kw["moving_ground"] is True)

    fail_steps.append({"step": "inlet velocity 15.0 m/s", "error": "X"})
    n0 = len(solve_calls)
    out_e = fm.run_case(MSH_SPEC)
    check("failed setup steps skip the solve (license not burned)",
          "error" in out_e and "solve" not in out_e
          and len(solve_calls) == n0
          and "inlet velocity" in out_e["error"])
    out_c = fm.run_case({**MSH_SPEC,
                         "solve": {"iterations": 50,
                                   "continue_on_setup_failure": True}})
    check("continue_on_setup_failure opts back into the solve",
          "solve" in out_c and solve_calls[-1] == 50)
    fail_steps.clear()
finally:
    for n, f in _real.items():
        setattr(fm, n, f)

# lifecycle lock: a live meshing session must never be exited by a
# concurrent mesh_native; a launching claim survives shutdown()
check("fluent_mcp has the session lifecycle lock",
      isinstance(fm._lifecycle, type(threading.Lock())))
fm._mesher = object()
try:
    fm.mesh_native(stl["stl_path"], 1e-3, 0.1, 1e-4)
    check("mesh_native refuses to kill a live meshing session", False)
except RuntimeError as e:
    check("mesh_native refuses to kill a live meshing session",
          "already live" in str(e))
fm._mesher = fm._MESH_CLAIM
r_sd = fm.shutdown()
check("shutdown leaves a launching claim with its owner",
      r_sd["status"] == "no session" and fm._mesher is fm._MESH_CLAIM)
fm._mesher = None

# gmsh fence: meshing tools serialize gmsh and restore the Win32 PATH
lk, sv_p, rs_p = fm._gmsh_fence()
got = lk.acquire(timeout=1)
if got:
    lk.release()
snap = sv_p()
rs_p(snap)
check("gmsh fence provides a usable lock and PATH snapshot/restore",
      got and (snap is None or isinstance(snap, str)))
probe = {"lock": threading.Lock(), "saved": [], "restored": [],
         "held": []}


def _probe_fence():
    def save():
        probe["saved"].append(1)
        return "SNAP"

    def rest(v):
        probe["restored"].append(v)
    return probe["lock"], save, rest


_real_fence = fm._gmsh_fence
_real_bdm = wf.build_domain_mesh
_real_g2f = wf.gmsh_to_fluent
fm._gmsh_fence = _probe_fence
wf.build_domain_mesh = lambda profiles, work, spec, domain_m=None: (
    probe["held"].append(probe["lock"].locked())
    or {"msh_path": str(TMP / "fake.msh"), "n_cells": 1})
wf.gmsh_to_fluent = lambda msh, work: str(TMP / "fake_fluent.msh")
try:
    r_mesh = fm.mesh_from_dxf(str(dxf_rect), {"mesher": "gmsh"})
finally:
    fm._gmsh_fence = _real_fence
    wf.build_domain_mesh = _real_bdm
    wf.gmsh_to_fluent = _real_g2f
check("mesh_from_dxf meshes under the gmsh fence (lock held, PATH "
      "snapshot restored)",
      probe["held"] == [True] and probe["saved"] == [1]
      and probe["restored"] == ["SNAP"]
      and r_mesh["mesh"]["mesher"] == "gmsh-bridge")

# ---- default-2d conventions: the manual workflow's true-2D recipe,
# made deterministic — explicit references, everything else left where
# Fluent starts it

t2_log = []
fm._solver = _Rec(t2_log)
fm._dim = 2
su_2d = fm.setup_external_aero(inlet_velocity_ms=12.0,
                               profile_zones=["profile"], chord_m=0.30,
                               slip_zones=["upper_bound"],
                               conventions="default-2d")
check("default-2d sets the explicit reference values (area 1 m^2, "
      "length 1 m, inlet velocity/density)",
      _has(t2_log, "setup.reference_values.area", 1.0)
      and _has(t2_log, "setup.reference_values.length", 1.0)
      and _has(t2_log, "setup.reference_values.velocity", 12.0)
      and _has(t2_log, "setup.reference_values.density", 1.225)
      and su_2d["reference_area_m2"] == 1.0
      and su_2d["reference_length_m"] == 1.0
      and su_2d["failed"] == [],
      f"(failed: {su_2d['failed']})")
check("default-2d leaves the viscous model and residual criteria "
      "untouched",
      not any("viscous" in p for p, _v in t2_log)
      and not any("check_convergence" in p for p, _v in t2_log)
      and any("viscous model left at Fluent defaults" in a
              for a in su_2d["applied"])
      and any("residual criteria left at Fluent defaults" in a
              for a in su_2d["applied"]))
check("default-2d keeps default material/turbulence and Fluent-default "
      "force vectors (+y lift, +x drag)",
      not any("turbulent_intensity" in p for p, _v in t2_log)
      and not any("materials" in p for p, _v in t2_log)
      and _has(t2_log, "[lift_coef].force_vector", [0, 1])
      and _has(t2_log, "[drag_coef].force_vector", [1, 0]))
check("default-2d applies the documented wall BCs (moving ground at "
      "inlet speed, shear-free upper bound, no-slip profile)",
      _has(t2_log, ".momentum.speed", 12.0)
      and _has(t2_log, ".momentum.direction", [1, 0])
      and _has(t2_log, ".momentum.shear_condition", "Specified Shear")
      and _has(t2_log, ".momentum.shear_condition", "No Slip"))
fm._dim = 3
check("studio still sets k-omega SST (regression around default-2d "
      "gating)",
      _has(setup_log, "setup.models.viscous.model", "k-omega")
      and _has(setup_log, "setup.models.viscous.k_omega_model", "sst"))
# the retired vocabulary is not accepted anywhere — no compatibility
# shim, so an old caller fails loudly instead of silently re-recipeing
for bad_conv in ("gui", "team", "team-doc", "team-2d"):
    try:
        fm.setup_external_aero(inlet_velocity_ms=1.0,
                               profile_zones=["p"], chord_m=0.3,
                               conventions=bad_conv)
        check(f"conventions {bad_conv!r} is rejected", False)
    except ValueError:
        check(f"conventions {bad_conv!r} is rejected", True)

# the untouched slip default resolves against the loaded mesh: the 2D
# chain names the upper boundary "upper_bound", the slab routes "top"
_real_zones = fm._zones
fb_log = []
fm._solver = _Rec(fb_log)
fm._dim = 2
fm._zones = lambda: {"velocity_inlet": ["inlet"],
                     "pressure_outlet": ["outlet"],
                     "wall": ["ground", "upper_bound", "profile"]}
try:
    su_fb = fm.setup_external_aero(inlet_velocity_ms=10.0,
                                   profile_zones=["profile"],
                                   chord_m=0.30,
                                   conventions="default-2d")
finally:
    fm._zones = _real_zones
    fm._dim = 3
check("untouched slip default resolves to upper_bound on a 2D chain "
      "mesh, never touching 'top'",
      _has(fb_log, "[upper_bound].momentum.shear_condition",
           "Specified Shear")
      and not any("[top]" in p for p, _v in fb_log)
      and any("resolved to upper_bound" in a for a in su_fb["applied"]))
fb2_log = []
fm._solver = _Rec(fb2_log)
fm._zones = lambda: {"wall": ["top", "ground", "w1"]}
try:
    fm.setup_external_aero(inlet_velocity_ms=10.0, profile_zones=["w1"],
                           chord_m=0.30)
finally:
    fm._zones = _real_zones
check("slab meshes keep the 'top' slip default",
      _has(fb2_log, "[top].momentum.shear_condition", "Specified Shear")
      and not any("upper_bound" in p for p, _v in fb2_log))

# ---- mesh_2d + run_case dimension=2: the Workbench chain, faked ----
# fluent2d_workflow may not exist yet (built against the same pinned
# contract) — a fake module through sys.modules stands in either way

import types  # noqa: E402
import scripts  # noqa: E402


class _F2DErr(RuntimeError):
    def __init__(self, stage, detail):
        super().__init__(f"{stage}: {detail}")
        self.stage, self.detail = stage, detail


chain_calls = []


def _fake_sizing(mode, cfg):
    if mode == "default":
        return {"edge_size_mm": 0.1, "first_layer_mm": 1.0,
                "n_layers": 10, "growth": 1.2}
    return {"edge_size_mm": 0.7, "first_layer_mm": 0.02,
            "n_layers": 30, "growth": 1.2}


def _fake_chain(dxf, work, **kw):
    chain_calls.append((str(dxf), kw))
    return {"msh_path": str(TMP / "FFF.msh"), "n_cells": 90000,
            "zones": ["fluid", "inlet", "outlet", "ground",
                      "upper_bound", "profile"],
            "stage_s": {"spaceclaim": 1.0, "workbench": 2.0},
            "project_dir": str(TMP)}


fake_wf2 = types.ModuleType("scripts.fluent2d_workflow")
fake_wf2.Fluent2DError = _F2DErr
fake_wf2.mesh_sizing = _fake_sizing
fake_wf2.run_chain = _fake_chain
_saved_mod = sys.modules.get("scripts.fluent2d_workflow")
_saved_attr = getattr(scripts, "fluent2d_workflow", None)
sys.modules["scripts.fluent2d_workflow"] = fake_wf2
scripts.fluent2d_workflow = fake_wf2
try:
    r2d = fm.mesh_2d(str(dxf_rect), sizing="default")
    check("mesh_2d hands the default sizing numbers to run_chain",
          chain_calls[-1][1]["edge_size_mm"] == 0.1
          and chain_calls[-1][1]["first_layer_mm"] == 1.0
          and chain_calls[-1][1]["n_layers"] == 10
          and chain_calls[-1][1]["growth"] == 1.2
          and r2d["mesher"] == "ansys-2d" and r2d["n_cells"] == 90000
          and "profile" in r2d["zones"],
          f"(kw {chain_calls[-1][1]})")
    check("mesh_2d leaves the stage budgets to the chain by default",
          "sc_budget_s" not in chain_calls[-1][1]
          and "wb_budget_s" not in chain_calls[-1][1])
    fm.mesh_2d(str(dxf_rect), sc_budget_s=600.0, wb_budget_s=3600.0)
    check("mesh_2d passes explicit stage budgets through to run_chain",
          chain_calls[-1][1]["sc_budget_s"] == 600.0
          and chain_calls[-1][1]["wb_budget_s"] == 3600.0)
    for bad_kw in ({"sc_budget_s": 10.0}, {"sc_budget_s": 8000.0},
                   {"wb_budget_s": 30.0}, {"wb_budget_s": 50000.0}):
        try:
            fm.mesh_2d(str(dxf_rect), **bad_kw)
            check(f"out-of-range {bad_kw} is rejected", False)
        except ValueError:
            check(f"out-of-range {bad_kw} is rejected", True)
    check("mesh_2d's recipe docstring names the 2D slip zone",
          'slip_zones=["upper_bound"]' in (fm.mesh_2d.__doc__ or ""))
    check("mesh_2d reports ground provenance for the walkthrough DXF "
          "(rectangle floor on the ground line)",
          r2d["ground"] is True)
    # free-air layout: the rectangle's floor mirrored well below the
    # section — write_dxf_2d's ground=False geometry
    doc_fa = ezdxf.new("R2010")
    doc_fa.header["$INSUNITS"] = 4
    msp_fa = doc_fa.modelspace()
    msp_fa.add_spline(fit_points=[(x, y, 0) for x, y in
                                  np.vstack([prof_mm, prof_mm[:1]])])
    msp_fa.add_lwpolyline([(-300, -90), (800, -90), (800, 250),
                           (-300, 250)], close=True)
    dxf_free = TMP / "free_air.dxf"
    doc_fa.saveas(dxf_free)
    r2f = fm.mesh_2d(str(dxf_free))
    check("mesh_2d reads a mirrored-floor DXF as free air",
          r2f["ground"] is False)
    check("_dxf_ground_2d agrees on both layouts directly",
          fm._dxf_ground_2d(dxf_rect) is True
          and fm._dxf_ground_2d(dxf_free) is False)
    for bad_sizing in ("ultra", "team"):
        try:
            fm.mesh_2d(str(dxf_rect), sizing=bad_sizing)
            check(f"mesh_2d rejects sizing {bad_sizing!r}", False)
        except ValueError:
            check(f"mesh_2d rejects sizing {bad_sizing!r}", True)

    def _boom(dxf, work, **kw):
        raise _F2DErr("workbench", "mesh stage wrote no result")
    fake_wf2.run_chain = _boom
    try:
        fm.mesh_2d(str(dxf_rect))
        check("mesh_2d propagates Fluent2DError from the chain", False)
    except _F2DErr as e:
        check("mesh_2d propagates Fluent2DError from the chain",
              e.stage == "workbench")
    fake_wf2.run_chain = _fake_chain

    # run_case dimension=2 routes through the chain with default-2d
    # conventions, a 2D launch and the recipe's zone names
    launch2: list = []
    setup2: dict = {}
    solve2: list = []
    mesh2d_calls: list = []

    def _fk_launch(dimension=3, precision="double", processors=4,
                   version=""):
        launch2.append(int(dimension))
        fm._session_dir = TMP / "mcp_sess3"
        fm._session_dir.mkdir(exist_ok=True)
        fm._launch_params = {"dimension": int(dimension),
                             "precision": precision,
                             "processors": int(processors)}
        return {"launched_in_s": 0.0}

    def _fk_setup(**kw):
        setup2.clear()
        setup2.update(kw)
        return {"applied": [], "failed": []}

    def _fk_solve(iterations=500, initialize=True):
        solve2.append(int(iterations))
        return {"iterations_requested": int(iterations)}

    fk_ground = [True]

    def _fk_mesh2d(dxf_path, sizing="default", config={}, version="",
                   **budgets):
        mesh2d_calls.append((str(dxf_path), sizing, dict(budgets)))
        return {"msh_path": str(TMP / "FFF.msh"), "n_cells": 90000,
                "zones": ["fluid", "inlet", "outlet", "ground",
                          "upper_bound", "profile"],
                "stage_s": {}, "sizing": sizing, "mesher": "ansys-2d",
                "ground": fk_ground[0]}

    _real2 = {n: getattr(fm, n) for n in
              ("launch", "read_mesh", "setup_external_aero", "solve",
               "mesh_2d")}
    fm.launch = _fk_launch
    fm.read_mesh = lambda p: {"zones": {}}
    fm.setup_external_aero = _fk_setup
    fm.solve = _fk_solve
    fm.mesh_2d = _fk_mesh2d
    try:
        fm._solver = None
        out2 = fm.run_case({"dimension": 2,
                            "geometry": {"dxf_path": "walk.dxf"},
                            "solve": {"iterations": 500}})
        check("run_case dimension=2 routes to the Workbench chain "
              "and a 2D launch",
              mesh2d_calls == [("walk.dxf", "default", {})]
              and launch2 == [2] and solve2 == [500]
              and out2["dimension"] == 2)
        check("dimension=2 defaults: default-2d conventions, profile "
              "zone, upper_bound slip, moving ground",
              setup2["conventions"] == "default-2d"
              and setup2["profile_zones"] == ["profile"]
              and setup2["slip_zones"] == ["upper_bound"]
              and setup2["moving_ground"] is True
              and out2["conventions"] == "default-2d")
        fm.run_case({"dimension": 2,
                     "geometry": {"dxf_path": "walk.dxf"},
                     "mesh": {"sc_budget_s": 600.0,
                              "wb_budget_s": 3600.0}})
        check("dimension=2 passes the stage budgets down to mesh_2d",
              mesh2d_calls[-1][2] == {"sc_budget_s": 600.0,
                                      "wb_budget_s": 3600.0})
        fm.run_case({"dimension": 2,
                     "geometry": {"dxf_path": "walk.dxf"},
                     "mesh": {"sc_budget_s": None,
                              "wb_budget_s": None}})
        check("null stage budgets fall back to the documented defaults",
              mesh2d_calls[-1][2] == {})
        try:
            fm.run_case({"dimension": 2,
                         "geometry": {"dxf_path": "w.dxf"},
                         "mesh": {"mesher": "gmsh"}})
            check("dimension=2 rejects a non-ansys-2d mesher", False)
        except ValueError:
            check("dimension=2 rejects a non-ansys-2d mesher", True)
        try:
            fm.run_case({"dimension": 2,
                         "geometry": {"dxf_path": "w.dxf"},
                         "mesh": {"first_layer_m": 5e-6}})
            check("dimension=2 rejects unknown mesh knobs loudly", False)
        except ValueError as e:
            check("dimension=2 rejects unknown mesh knobs loudly",
                  "first_layer_m" in str(e))
        # a retired or unknown conventions name must be refused BEFORE
        # the meshing chain and the license checkout — the setup step
        # that used to be the only checker runs minutes and one solver
        # seat later
        for bad_conv, dim_spec in (("team-2d", {"dimension": 2}),
                                   ("team-doc", {"dimension": 2}),
                                   ("resolved", {"dimension": 2}),
                                   ("team", {})):
            n_mesh, n_launch = len(mesh2d_calls), len(launch2)
            spec_c = {**dim_spec, "conventions": bad_conv,
                      "geometry": ({"dxf_path": "w.dxf"}
                                   if dim_spec else MSH_SPEC["geometry"]),
                      "physics": MSH_SPEC.get("physics", {})}
            label = (f"run_case rejects conventions {bad_conv!r} before "
                     f"any meshing or license work")
            try:
                fm.run_case(spec_c)
                check(label, False)
            except ValueError as e:
                check(label, "conventions must be" in str(e)
                      and len(mesh2d_calls) == n_mesh
                      and len(launch2) == n_launch, f"({e})")

        # 2D provenance mirrors the 3D DXF route: a free-air rectangle
        # gets the slip floor, an explicit knob still wins
        fk_ground[0] = False
        out2f = fm.run_case({"dimension": 2,
                             "geometry": {"dxf_path": "free.dxf"}})
        check("dimension=2 free-air provenance defaults the floor to "
              "slip",
              setup2["moving_ground"] is False
              and out2f["ground_treatment"].startswith("shear-free"))
        fm.run_case({"dimension": 2,
                     "geometry": {"dxf_path": "free.dxf"},
                     "physics": {"moving_ground": True}})
        check("dimension=2 explicit moving_ground overrides provenance",
              setup2["moving_ground"] is True)
        fk_ground[0] = True
        # regression: the default stays 3/native for existing callers
        fm._solver = None
        out3 = fm.run_case(MSH_SPEC)
        check("dimension default remains 3 with studio conventions "
              "and the slab's top slip zone",
              launch2[-1] == 3 and setup2["conventions"] == "studio"
              and setup2["slip_zones"] == ["top"]
              and out3["dimension"] == 3)
    finally:
        for n, f in _real2.items():
            setattr(fm, n, f)
finally:
    if _saved_mod is None:
        sys.modules.pop("scripts.fluent2d_workflow", None)
    else:
        sys.modules["scripts.fluent2d_workflow"] = _saved_mod
    if _saved_attr is None:
        if hasattr(scripts, "fluent2d_workflow"):
            del scripts.fluent2d_workflow
    else:
        scripts.fluent2d_workflow = _saved_attr

fm._solver, fm._session_dir, fm._launch_params, fm._mesher = _saved_state

print(f"\n{sum(results)}/{len(results)} fluent-workflow checks passed")
sys.exit(0 if all(results) else 1)
