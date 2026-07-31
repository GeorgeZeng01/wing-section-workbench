"""In-app Fluent engine — everything except Fluent/Docker themselves.

FluentJob's state machine through the faked session seams (_mcp and
_bridge), the chunked drift-stop, honest verdicts (converged only on a
flat history, no k_g otherwise), cancellation with guaranteed license
release, and registry integration: the one-job-at-a-time guard spans
both engines.

Run directly:  python app/tests/test_fluent_run.py
"""
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="wss_fluent_run_")
os.environ["WSS_DATA_DIR"] = _TMP
os.environ["WSS_EXPORTS_DIR"] = str(Path(_TMP) / "exports")

from app.core import cfd_run, fluent_run  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG_D = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "ride_height_mm": 30, "chord_mm": 350, "span_mm": 1400,
    "speed_ms": 15, "ncrit": 7, "n_panels_per_side": 50,
}


class FakeFM:
    """Stands in for the scripts.fluent_mcp session layer."""

    def __init__(self, row_fn, fail_setup=False, hold: threading.Event
                 | None = None, n_cells_none=False, cd_fn=None):
        self.row_fn = row_fn
        self.fail_setup = fail_setup
        self.hold = hold
        self.n_cells_none = n_cells_none
        self.cd_fn = cd_fn
        self.cl: list[float] = []
        self.cd: list[float] = []
        self.calls: list[str] = []
        self.shutdowns = 0
        self.job_ref = None
        self.state_at_export = None

    def launch(self, **kw):
        self.calls.append("launch")
        return {"launched_in_s": 0.1, **kw}

    def mesh_native(self, stl_path, **kw):
        self.calls.append("mesh_native")
        self.mesh_native_kw = kw
        assert Path(stl_path).is_file(), "mesh_native ran before the STL"
        work = Path(stl_path).parent
        p = work / "native.msh.h5"
        p.write_bytes(b"h5")
        # the real meshing session drops scratch into its cwd — the
        # export path must sweep exactly this litter
        (work / "FM_TEST_1234").mkdir(exist_ok=True)
        (work / "fluent-20990101-000000-1.trn").write_text(
            "t", encoding="utf-8")
        (work / "cleanup-fluent-TEST-1.bat").write_text(
            "rem", encoding="utf-8")
        (work / "native_workflow_files").mkdir(exist_ok=True)
        return {"msh_path": str(p),
                "n_cells": None if self.n_cells_none else 54321,
                "quality": {"max_skewness": 0.83},
                "applied": ["surface mesh", "volume mesh"], "failed": [],
                "meshed_in_s": 0.2, "mesher": "fluent-meshing"}

    def tui(self, command):
        self.calls.append(f"tui:{command}")
        return ("Mesh Size\n\n"
                "Level    Cells    Faces    Nodes   Partitions\n"
                "    0   651119  3456862  2427894            4\n")

    def read_mesh(self, path):
        self.calls.append("read_mesh")
        return {"zones": {"wall": ["wing_e1", "wing_e2"]}}

    def setup_external_aero(self, **kw):
        self.calls.append("setup")
        self.setup_kw = kw
        if self.fail_setup:
            return {"failed": [{"step": "viscous k-omega SST",
                                "error": "Boom: nope"}], "applied": []}
        return {"failed": [], "applied": ["all"],
                "conventions": kw.get("conventions")}

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}")
        if self.hold is not None:
            self.hold.wait(timeout=10)
        n0 = len(self.cl)
        for i in range(n0, n0 + iterations):
            self.cl.append(self.row_fn(i))
            self.cd.append(self.cd_fn(i) if self.cd_fn else 0.2)
        return {}

    def _report_histories(self):
        return {"lift_coef": list(self.cl), "drag_coef": list(self.cd)}

    def write_case_data(self, stem):
        self.calls.append("write")
        self.case_stem = stem
        # the real tool writes <stem>.cas.h5 + <stem>.dat.h5 exactly
        # where the (absolute) stem points
        p = Path(f"{stem}.cas.h5")
        p.write_bytes(b"CASH5")
        p.with_name(p.name[:-len(".cas.h5")] + ".dat.h5").write_bytes(
            b"DATH5")
        return {"written": str(p), "data_file": str(p)}

    def export_ascii(self, filename, quantities=None, location=None):
        """Writes a plausible cell-centre export: a small ring of points
        around the section with a uniform-ish flow."""
        self.calls.append("export")
        # the ordering contract: the job must NOT be "done" yet when the
        # export runs — done-before-export made the UI's first flow
        # fetch race the files
        if self.job_ref is not None:
            self.state_at_export = self.job_ref.state
        lines = ["cellnumber, x-coordinate, y-coordinate, z-coordinate,"
                 " x-velocity, y-velocity, pressure"]
        n = 400
        for i in range(n):
            a = 2 * math.pi * i / n
            r = 0.3 + 0.25 * (i % 7) / 7
            x, y = 0.2 + r * math.cos(a), 0.25 + abs(r * math.sin(a))
            # two pressure plateaus so the Cp normalization is pinned
            # to an analytic value (a uniform field only ever read the
            # hardcoded cp_p98 floor back)
            p_pa = -61.25 if i < n // 2 else -250.0
            lines.append(f"{i+1}, {x:.6e}, {y:.6e}, 1.75e-02, "
                         f"{15.0 + math.sin(a):.6e}, {0.5:.6e}, "
                         f"{p_pa:.6e}")
        Path(filename).write_text("\n".join(lines) + "\n",
                                  encoding="utf-8")
        return {"written": str(filename), "rows": n}

    def shutdown(self):
        self.shutdowns += 1
        return {"status": "exited"}


def fake_build_case(cfg, out_dir, mesh_size, n_iters=3000, n_ranks=1):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return {"n_cells": 12345, "mesh_size": mesh_size, "n_iters": n_iters,
            "bl_mode": "per-element"}


def make_bridge(fake):
    def fake_bridge(msh, work):
        fake.calls.append("bridge")
        return str(Path(work) / "case.msh")
    return fake_bridge


def run_job(row_fn, n_iters=3000, fail_setup=False, cancel_after=None,
            hold=None, mesher="fluent", n_cells_none=False, cd_fn=None,
            fm_cls=FakeFM, precancel=False):
    fake = fm_cls(row_fn, fail_setup=fail_setup, hold=hold,
                  n_cells_none=n_cells_none, cd_fn=cd_fn)
    real = (fluent_run._mcp, fluent_run._bridge,
            fluent_run.cfd.build_case)
    fluent_run._mcp = lambda: fake
    fluent_run._bridge = make_bridge(fake)
    fluent_run.cfd.build_case = fake_build_case
    job = fluent_run.FluentJob(CFG_D, "coarse", n_iters, 4, mesher)
    fake.job_ref = job
    try:
        if precancel:
            job.cancel()
        if cancel_after is not None:
            threading.Timer(cancel_after, job.cancel).start()
        job.run()
    finally:
        (fluent_run._mcp, fluent_run._bridge,
         fluent_run.cfd.build_case) = real
    return job, fake


# ---- happy path: flat history stops on drift, honest verdict ----

job, fake = run_job(lambda i: 2.5, n_iters=6000)
snap = job.snapshot()
r = snap["result"]
check("flat history: done + converged by force drift",
      snap["state"] == "done" and r and r["converged"] is True
      and r["stop_reason"] == "force history converged",
      f"({snap['state']}: {snap['error'] or (r and r['stop_reason'])})")
check("drift stop fires before the cap (chunked solving)",
      r and r["n_iters_run"] < 6000 and r["n_iters_run"]
      >= fluent_run.DRIFT_SKIP + 3 * cfd_run.FORCE_STOP_WINDOW,
      f"(stopped at {r and r['n_iters_run']})")
# floor 2600 -> first eligible boundary 2810, persistence requires a
# second consecutive hit -> the stop lands exactly at 3060
check("stop waits for three full windows plus a repeat boundary",
      r and r["n_iters_run"] == 3060,
      f"(stopped at {r and r['n_iters_run']})")
check("result carries the Cd drift alongside the Cl drift",
      r and r["cd_drift"] is not None
      and r["cd_drift"] < cfd_run.FORCE_STOP_CD_TOL,
      f"(cd_drift {r and r['cd_drift']})")
check("result mirrors the OpenFOAM engine's surface",
      r and r["engine"] == "fluent" and r["cl_rans"] == 2.5
      and r["cd_rans"] == 0.2 and r["wall_report"] is None
      and "engine_note" in r and r["mesh_caution"] is True
      and r["n_ranks"] == 4)
check("panel comparison and delta computed",
      r and r["panel"] and r["delta_cl_pct"] is not None)
check("license released exactly once", fake.shutdowns == 1)
check("history downsampled for the chart",
      2 <= len(snap["history"]) <= 320
      and snap["history"][-1]["iter"] == r["n_iters_run"])
check("snapshot names the engine", snap["engine"] == "fluent")

# ---- native mesher is the default: all-ANSYS, no Docker anywhere ----

check("native mesher is the default and never touches the bridge",
      job.mesher == "fluent" and "mesh_native" in fake.calls
      and "bridge" not in fake.calls, f"({fake.calls[:4]})")
check("real slab STL written from the shared section geometry",
      (job.case_dir / "fluent_native" / "slab.stl").is_file())
check("mesh summary carries native provenance",
      snap["mesh"]["mesher"] == "fluent-meshing"
      and snap["mesh"]["n_cells"] == 54321
      and snap["mesh"]["bl_mode"] == "native-prisms",
      f"({snap['mesh']})")
check("native sizing handed to Fluent Meshing from the studio preset",
      fake.mesh_native_kw["edge_size_m"] > 0
      and fake.mesh_native_kw["n_layers"] >= 4
      and fake.mesh_native_kw["wall_zones"] == ["wing_e1", "wing_e2"]
      and fake.mesh_native_kw["processors"] == 4)
check("solver reference depth follows the native slab",
      abs(fake.setup_kw["depth_m"]
          - fluent_run.cfd.DZ_C * 0.35) < 1e-12,
      f"({fake.setup_kw['depth_m']})")
check("snapshot and result name the mesher",
      snap["mesher"] == "fluent" and r["mesher"] == "fluent")

# ---- gmsh option: the identical-mesh cross-check route still works ----

job_gm, fake_gm = run_job(lambda i: 2.5, n_iters=3000, mesher="gmsh")
sg = job_gm.snapshot()
check("gmsh mesher option bridges through Docker as before",
      job_gm.state == "done" and "bridge" in fake_gm.calls
      and "mesh_native" not in fake_gm.calls
      and sg["mesh"]["mesher"] == "gmsh-bridge"
      and sg["mesh"]["n_cells"] == 12345
      and sg["result"]["mesher"] == "gmsh",
      f"({job_gm.state}: {job_gm.error})")
try:
    fluent_run.FluentJob(CFG_D, "coarse", 3000, 4, "ansys")
    check("unknown mesher is rejected", False)
except ValueError:
    check("unknown mesher is rejected", True)

# ---- exported flow field: readable by the studio's own pipeline ----

from app.core import foam_post  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

tdir = foam_post.latest_time_dir(job.case_dir)
check("Fluent run writes an OpenFOAM-format time directory",
      tdir.name == str(r["n_iters_run"])
      and (tdir / "C").is_file() and (tdir / "U").is_file()
      and (tdir / "p").is_file(), f"({tdir.name})")
_c = foam_post.parse_internal_field((tdir / "C").read_text())
_u = foam_post.parse_internal_field((tdir / "U").read_text())
_p = foam_post.parse_internal_field((tdir / "p").read_text())
check("exported fields parse through foam_post",
      _c.shape == (400, 3) and _u.shape == (400, 3)
      and _p.shape == (400,))
check("pressure converted to the kinematic convention (p/rho)",
      abs(float(_p[0]) - (-61.25 / 1.225)) < 1e-6, f"({_p[0]})")
_ffj = foam_post.flow_field_json(job.case_dir,
                                 StackConfig.from_dict(CFG_D), nx=120)
check("animation field JSON builds from a Fluent run",
      _ffj["nx"] == 120 and _ffj["iter"] == str(r["n_iters_run"])
      and any(x is not None for x in _ffj["umag"]))
_ffc = foam_post.flow_field_json(job.case_dir,
                                 StackConfig.from_dict(CFG_D), nx=120,
                                 include_cp=True)
check("include_cp adds the Cp backdrop on the identical grid",
      len(_ffc["cp"]) == len(_ffc["umag"]) == _ffc["nx"] * _ffc["ny"]
      and _ffc["cp_p98"] >= 0.5
      and any(x is not None for x in _ffc["cp"])
      and "cp" not in _ffj,
      f"(cp_p98 {_ffc.get('cp_p98')})")
# the -250 Pa plateau pins the whole normalization chain: Cp =
# (p/rho)/(0.5 V^2) = (-250/1.225)/112.5 — a rho or q slip fails this
_cp_min = min(x for x in _ffc["cp"] if x is not None)
check("Cp normalization matches the analytic plateau value",
      abs(_cp_min - (-250.0 / 1.225 / (0.5 * 15.0 ** 2))) < 0.02,
      f"(min cp {_cp_min})")
check("engine note says the flow view works",
      "flow view and animation" in r["engine_note"].lower()
      and "unavailable" not in r["engine_note"].lower())
check("done is deferred until the flow export has run",
      fake.state_at_export == "running",
      f"(state at export: {fake.state_at_export})")

# ---- trending history: cap reached, provisional, no k_g ----

job_t, fake_t = run_job(lambda i: 2.0 + 0.001 * i, n_iters=1500)
rt = job_t.snapshot()["result"]
check("trending history: NOT converged at the cap",
      job_t.state == "done" and rt and rt["converged"] is False
      and rt["stop_reason"] == "iteration cap reached"
      and rt["n_iters_run"] == 1500)
check("no k_g from an unconverged Fluent run",
      rt and rt["suggested_k_g"] is None
      and rt["delta_cl_provisional"] is True
      and rt["cl_trend_note"] is not None)
check("trending run still released the license", fake_t.shutdowns == 1)

# ---- setup failure surfaces the failed step ----

job_f, fake_f = run_job(lambda i: 2.5, fail_setup=True)
check("failed setup step fails the job with the step named",
      job_f.state == "failed" and "viscous k-omega SST"
      in (job_f.error or ""), f"({(job_f.error or '')[:60]})")
check("failed setup still released the license", fake_f.shutdowns == 1)

# ---- raise AFTER launch: the session must not outlive the job ----


class _ReadBoomFM(FakeFM):
    """read_mesh raises — a corrupt bridge mesh, post-launch."""

    def read_mesh(self, path):
        self.calls.append("read_mesh")
        raise RuntimeError("corrupt mesh file")


class _SolveBoomFM(FakeFM):
    """solve raises with NO cancel in flight — a diverged AMG solve."""

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}")
        raise RuntimeError("AMG solver diverged")


job_rb, fake_rb = run_job(lambda i: 2.5, fm_cls=_ReadBoomFM)
check("read_mesh raise fails the job AND releases the license",
      job_rb.state == "failed" and "corrupt mesh file"
      in (job_rb.error or "") and fake_rb.shutdowns == 1,
      f"({job_rb.state}, shutdowns {fake_rb.shutdowns})")
job_sb, fake_sb = run_job(lambda i: 2.5, fm_cls=_SolveBoomFM)
check("uncancelled solve raise fails the job AND releases the license",
      job_sb.state == "failed" and "AMG solver diverged"
      in (job_sb.error or "") and fake_sb.shutdowns == 1,
      f"({job_sb.state}, shutdowns {fake_sb.shutdowns})")

# ---- force-stop doctrine: Cd gate, window floor, persistence ----

# flat Cl over a still-climbing Cd must NOT force-stop (Cd converges
# last — the OpenFOAM runner's combined criterion applies here too)
job_cd, fake_cd = run_job(lambda i: 2.5, n_iters=3000,
                          cd_fn=lambda i: 0.2 + 0.0005 * i)
rcd = job_cd.snapshot()["result"]
check("flat Cl over climbing Cd runs to the cap (Cd gate holds)",
      job_cd.state == "done" and rcd
      and rcd["n_iters_run"] == 3000
      and rcd["stop_reason"] == "iteration cap reached",
      f"({rcd and rcd['stop_reason']} at {rcd and rcd['n_iters_run']})")
check("climbing Cd is reported as drift, not hidden",
      rcd and rcd["cd_drift"] is not None
      and rcd["cd_drift"] > cfd_run.FORCE_STOP_CD_TOL,
      f"(cd_drift {rcd and rcd['cd_drift']})")
# the false plateau that fooled the 370-row adaptive window: a slow
# climb with an oscillation reads drift ~0.0024 at iter 1310 but
# ~0.005 on full 800-row windows — the floor must refuse the early stop
job_fp, fake_fp = run_job(
    lambda i: 2.3 + 1.5e-5 * i + 0.03 * math.sin(2 * math.pi * i / 370),
    n_iters=3000)
rfp = job_fp.snapshot()["result"]
check("slow climb + oscillation is never stopped as converged",
      job_fp.state == "done" and rfp and rfp["n_iters_run"] == 3000
      and rfp["stop_reason"] == "iteration cap reached",
      f"({rfp and rfp['stop_reason']} at {rfp and rfp['n_iters_run']})")

# ---- torn drag rfile: cd one row short of cl must not crash ----


class _TornCdFM(FakeFM):
    """The drag rfile's last line was mid-flush when read — the parser
    drops it, leaving cd one row shorter than cl."""

    def _report_histories(self):
        return {"lift_coef": list(self.cl),
                "drag_coef": list(self.cd[:-1])}


job_tc, fake_tc = run_job(lambda i: 2.5, n_iters=3000, fm_cls=_TornCdFM)
check("torn drag row: run completes instead of IndexError-failing",
      job_tc.state == "done" and fake_tc.shutdowns == 1,
      f"({job_tc.state}: {job_tc.error})")
check("torn drag row: chart rows stay paired (no lift-derived index)",
      all(h["cd"] is not None for h in job_tc.snapshot()["history"]))

# ---- cancel before the pipeline starts: lands at the first boundary ----

job_pc, fake_pc = run_job(lambda i: 2.5, precancel=True)
check("pre-set cancel lands before any geometry or license work",
      job_pc.state == "cancelled" and fake_pc.calls == []
      and fake_pc.shutdowns == 0,
      f"({job_pc.state}, calls {fake_pc.calls})")

# ---- wall-clock backstop: a wedged session fails honestly ----

real_wall = fluent_run.WALL_LIMIT_S
fluent_run.WALL_LIMIT_S = -1.0
try:
    job_w, fake_w = run_job(lambda i: 2.5, n_iters=3000)
finally:
    fluent_run.WALL_LIMIT_S = real_wall
check("past the wall limit the job fails with the honest reason",
      job_w.state == "failed" and "wall-clock backstop"
      in (job_w.error or ""), f"({job_w.state}: {job_w.error})")
check("wall-stopped run still released the license",
      fake_w.shutdowns == 1, f"({fake_w.shutdowns})")

# ---- cancellation between chunks ----

hold = threading.Event()
holder = {}


def cancel_then_release():
    # cancel only once the solver is actually iterating (the native
    # meshing stages run real geometry code and take real time) — a
    # cancel before launch correctly has no license to release
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            c.startswith("solve:") for c in fake_c.calls):
        time.sleep(0.01)
    holder["job"].cancel()
    hold.set()


t = threading.Thread(target=cancel_then_release, daemon=True)
fake_c = FakeFM(lambda i: 2.5, hold=hold)
real = (fluent_run._mcp, fluent_run._bridge, fluent_run.cfd.build_case)
fluent_run._mcp = lambda: fake_c
fluent_run._bridge = lambda msh, work: str(Path(work) / "case.msh")
fluent_run.cfd.build_case = fake_build_case
job_c = fluent_run.FluentJob(CFG_D, "coarse", 3000, 2)
holder["job"] = job_c
try:
    t.start()
    job_c.run()
finally:
    (fluent_run._mcp, fluent_run._bridge,
     fluent_run.cfd.build_case) = real
check("cancel mid-solve lands in cancelled",
      job_c.state == "cancelled", f"({job_c.state})")
# cancel now hard-kills the live session so blocking calls land within
# seconds; the finally sweep repeats the (idempotent) shutdown
check("cancelled run released the license",
      fake_c.shutdowns >= 1, f"({fake_c.shutdowns})")
check("cancel itself killed the session (not just the flag)",
      fake_c.shutdowns >= 2, f"({fake_c.shutdowns})")

# ---- cancel during a BLOCKING call: the kill converts to cancelled ----


class KilledFM(FakeFM):
    """A session whose blocked solve raises once released — what a real
    hard-killed gRPC session does."""

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}")
        if self.hold is not None:
            self.hold.wait(timeout=10)
            raise RuntimeError("RPC channel closed (session killed)")
        return {}


hold_k = threading.Event()
fake_k = KilledFM(lambda i: 2.5, hold=hold_k)
real = (fluent_run._mcp, fluent_run._bridge, fluent_run.cfd.build_case)
fluent_run._mcp = lambda: fake_k
fluent_run._bridge = make_bridge(fake_k)
fluent_run.cfd.build_case = fake_build_case
job_k = fluent_run.FluentJob(CFG_D, "coarse", 3000, 2)


def kill_when_solving():
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            c.startswith("solve:") for c in fake_k.calls):
        time.sleep(0.01)
    job_k.cancel()      # sets the flag AND shuts the session down
    hold_k.set()        # the blocked call now raises


try:
    threading.Thread(target=kill_when_solving, daemon=True).start()
    job_k.run()
finally:
    (fluent_run._mcp, fluent_run._bridge,
     fluent_run.cfd.build_case) = real
check("cancel kills a BLOCKING solve within the chunk (not after it)",
      job_k.state == "cancelled" and job_k.error is None,
      f"({job_k.state}: {job_k.error})")
check("the kill went through the session shutdown",
      fake_k.shutdowns >= 1, f"({fake_k.shutdowns})")

# ---- missing transcript count: solver-side size-info fallback ----

job_nc, fake_nc = run_job(lambda i: 2.5, n_iters=1500,
                          n_cells_none=True)
snap_nc = job_nc.snapshot()
check("missing meshing count falls back to the solver's size-info",
      job_nc.state == "done"
      and snap_nc["mesh"]["n_cells"] == 651119,
      f"({snap_nc['mesh']})")
check("big native meshes solve in short chunks after a small first one",
      "solve:60" in fake_nc.calls and "solve:100" in fake_nc.calls
      and "solve:250" not in fake_nc.calls, f"({fake_nc.calls[:8]})")
check("normal meshes keep the standard chunk after the first",
      "solve:60" in fake.calls and "solve:250" in fake.calls)

# ---- Export tab: native mesh case under exports/ ----

exp2 = Path(_TMP) / "exports_mesh"
fake_e = FakeFM(lambda i: 2.5)
real = (fluent_run._mcp, fluent_run._bridge, fluent_run.cfd.build_case)
fluent_run._mcp = lambda: fake_e
try:
    out_e = fluent_run.export_native_case(CFG_D, "coarse", exp2)
finally:
    (fluent_run._mcp, fluent_run._bridge,
     fluent_run.cfd.build_case) = real
dest_e = Path(out_e["path"])
check("fluent mesh export: deliverables only, session scratch swept",
      set(out_e["files"]) == {"README.txt", "config.json",
                              "native.msh.h5", "slab.stl"}
      and dest_e.parent == exp2 and out_e["n_cells"] == 54321,
      f"({out_e['files']})")
readme_e = (dest_e / "README.txt").read_text()
check("mesh-export README carries the recipe and provenance",
      "DOWNFORCE-POSITIVE" in readme_e and "coarse" in readme_e
      and "54321" in readme_e and "k-omega SST" in readme_e
      and "Read > Mesh" in readme_e)
bad_cfg = {**CFG_D, "ride_height_mm": 100000}
try:
    fluent_run.export_native_case(bad_cfg, "coarse", exp2)
    check("mesh export refuses impossible geometry", False)
except (ValueError, KeyError):
    check("mesh export refuses impossible geometry",
          len(list(exp2.glob("fluent_mesh_*"))) == 1,
          f"({sorted(p.name for p in exp2.iterdir())})")


class _MeshBoomFM(FakeFM):
    """Meshing dies AFTER the export dir exists and the STL is written
    — the realistic license/meshing failure the rmtree sweep is for."""

    def mesh_native(self, stl_path, **kw):
        self.calls.append("mesh_native")
        raise RuntimeError("license lost mid-workflow")


fake_mb = _MeshBoomFM(lambda i: 2.5)
real = (fluent_run._mcp, fluent_run._bridge, fluent_run.cfd.build_case)
fluent_run._mcp = lambda: fake_mb
try:
    try:
        fluent_run.export_native_case(CFG_D, "coarse", exp2)
        check("mesh failure after the dir claim propagates", False)
    except RuntimeError:
        check("mesh failure after the dir claim propagates", True)
finally:
    (fluent_run._mcp, fluent_run._bridge,
     fluent_run.cfd.build_case) = real
check("half-written export dir swept from exports/ on failure",
      len(list(exp2.glob("fluent_mesh_*"))) == 1,
      f"({sorted(p.name for p in exp2.iterdir())})")

# ---- registry: the one-job guard spans engines ----

gate = threading.Event()
fake_g = FakeFM(lambda i: 2.5, hold=gate)
real = (fluent_run._mcp, fluent_run._bridge, fluent_run.cfd.build_case)
fluent_run._mcp = lambda: fake_g
fluent_run._bridge = lambda msh, work: str(Path(work) / "case.msh")
fluent_run.cfd.build_case = fake_build_case
real_avail = cfd_run.availability
cfd_run.availability = lambda refresh=False: {
    "available": True, "docker": "t", "image": "t",
    "image_present": True, "detail": ""}
# start() sweeps orphan containers through the real Docker CLI — stub
# it like test_cfd_run does, so the offline suite never touches a real
# container (or NameErrors when another live instance is solving)
real_docker = cfd_run._docker
cfd_run._docker = lambda args, timeout: subprocess.CompletedProcess(
    args, 0, "", "")
jid = jobg = None
try:
    jid = cfd_run.start(CFG_D, "coarse", 3000, 2, engine="fluent")
    jobg = cfd_run.get(jid)
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            c.startswith("solve:") for c in fake_g.calls):
        time.sleep(0.01)
    check("fluent engine registers in the shared registry",
          jobg is not None and jobg.state == "running"
          and cfd_run.current()["job_id"] == jid)
    refused = {"of": False, "fl": False}
    for key, eng in (("of", "openfoam"), ("fl", "fluent")):
        try:
            cfd_run.start(CFG_D, engine=eng)
        except RuntimeError:
            refused[key] = True
    check("one-at-a-time guard refuses BOTH engines while it runs",
          refused["of"] and refused["fl"], f"({refused})")
finally:
    gate.set()
    deadline = time.time() + 15
    while (time.time() < deadline and jobg is not None
           and jobg.state == "running"):
        time.sleep(0.02)
    (fluent_run._mcp, fluent_run._bridge,
     fluent_run.cfd.build_case) = real
    cfd_run.availability = real_avail
    cfd_run._docker = real_docker
check("gated fluent job finishes after release",
      jobg is not None and jobg.state == "done",
      f"({jobg and jobg.state}: {jobg and jobg.error})")

# ---- ANSYS export: case+data beside the run, then into exports/ ----

check("case write targets the run directory, not the session dir",
      Path(f"{fake.case_stem}.cas.h5")
      == job.case_dir / "case.cas.h5",
      f"(stem: {fake.case_stem})")
# the registry tests above ran housekeeping that may have pruned this
# finished run's directory — restore the written artifacts so the
# export test exercises the copy, not the pruning
job.case_dir.mkdir(parents=True, exist_ok=True)
(job.case_dir / "case.cas.h5").write_bytes(b"CASH5")
(job.case_dir / "case.dat.h5").write_bytes(b"DATH5")
(job.case_dir / "config.json").write_text("{}", encoding="utf-8")

from fastapi import HTTPException  # noqa: E402

from app import server  # noqa: E402

with cfd_run._jobs_lock:
    cfd_run._jobs[job.id] = job
out = server.rans_export_fluent(job.id)
dest = Path(out["path"])
check("fluent export lands under exports/ with all artifacts",
      dest.parent == Path(os.environ["WSS_EXPORTS_DIR"])
      and set(out["files"]) == {"case.cas.h5", "case.dat.h5",
                                "config.json", "README.txt"}
      and out["data_included"] is True
      and all((dest / f).is_file() for f in out["files"]),
      f"({out['files']})")
readme = (dest / "README.txt").read_text()
check("export README states the conventions and the run summary",
      "DOWNFORCE-POSITIVE" in readme and "chord-referenced" in readme
      and "Case & Data" in readme and str(r["cl_rans"]) in readme)
try:
    server.rans_export_fluent("no-such-job")
    check("export of unknown job -> 404", False)
except HTTPException as e:
    check("export of unknown job -> 404", e.status_code == 404)


class _StubOpenFoamJob:
    state = "done"
    case_dir = job.case_dir
    id = "stub"

    def snapshot(self):
        return {"engine": "openfoam", "state": "done"}


with cfd_run._jobs_lock:
    cfd_run._jobs["stub-of"] = _StubOpenFoamJob()
try:
    server.rans_export_fluent("stub-of")
    check("export of an OpenFOAM run -> 422", False)
except HTTPException as e:
    check("export of an OpenFOAM run -> 422", e.status_code == 422,
          f"({e.detail})")
(job.case_dir / "case.cas.h5").unlink()
try:
    server.rans_export_fluent(job.id)
    check("export without case files -> 422 with the reason", False)
except HTTPException as e:
    check("export without case files -> 422 with the reason",
          e.status_code == 422 and "case.cas.h5" in str(e.detail))
with cfd_run._jobs_lock:
    cfd_run._jobs.pop("stub-of", None)

# ---- validation + availability ----

try:
    cfd_run.start(CFG_D, engine="ansys")
    check("unknown engine is rejected", False)
except (ValueError, RuntimeError) as e:
    check("unknown engine is rejected", isinstance(e, ValueError),
          f"({e})")
try:
    fluent_run.FluentJob(CFG_D, "coarse", 3000, 64)
    check("out-of-range processor count is rejected", False)
except ValueError:
    check("out-of-range processor count is rejected", True)
av = fluent_run.availability()
check("availability reports shape without launching",
      isinstance(av["available"], bool) and "detail" in av
      and isinstance(av["pyfluent"], bool))

print(f"\n{sum(results)}/{len(results)} fluent-run checks passed")
sys.exit(0 if all(results) else 1)
