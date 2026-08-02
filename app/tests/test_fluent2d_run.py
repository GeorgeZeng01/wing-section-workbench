"""ANSYS 2D engine — everything except ANSYS itself.

Fluent2DJob's state machine through the faked seams (_dxf, _sizing,
_chain, _mcp): the pipeline order, default-vs-studio iteration
semantics (fixed 500 with residual_stop detection vs the drift
doctrine), the raw-to-chord coefficient conversion, settings
resolution (override wins, empty falls back to the recipe, every range
refused at construction), cancellation at every stage including a
blocking meshing chain, license release on every raise path, and the
flow-field export round-tripping through foam_post.

Run directly:  python app/tests/test_fluent2d_run.py
"""
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="wss_fluent2d_run_")
os.environ["WSS_DATA_DIR"] = _TMP
os.environ["WSS_EXPORTS_DIR"] = str(Path(_TMP) / "exports")

from app.core import cfd_run, fluent2d_run  # noqa: E402

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

# what the workflow's mesh_sizing returns for each mode — the job must
# forward these to the chain untouched wherever no override replaces
# them
SIZING_FAKE = {
    "default": {"edge_size_mm": 0.1, "first_layer_mm": 1.0,
                "n_layers": 10, "growth": 1.2},
    "studio-yplus1": {"edge_size_mm": 0.7, "first_layer_mm": 0.02,
                      "n_layers": 30, "growth": 1.2},
}


class FakeFM:
    """Stands in for the scripts.fluent_mcp session layer (2D flavor:
    the ASCII export carries NO z column, like a real 2D session)."""

    # the zone vocabulary the 2D chain actually produces (matches
    # default_chain below) — the real setup fails every zone-role
    # kwarg naming a zone the loaded mesh does not have, and a fake
    # that accepts anything hides a wrong zone default entirely
    MESH_ZONES = ("fluid", "inlet", "outlet", "ground", "upper_bound",
                  "profile")

    def __init__(self, row_fn, fail_setup=False, hold: threading.Event
                 | None = None, cd_fn=None, iter_cap=None):
        self.row_fn = row_fn
        self.fail_setup = fail_setup
        self.hold = hold
        self.cd_fn = cd_fn
        self.iter_cap = iter_cap    # Fluent's residual criteria firing
        self.cl: list[float] = []
        self.cd: list[float] = []
        self.calls: list[str] = []
        self.shutdowns = 0
        self.job_ref = None
        self.state_at_export = None

    def launch(self, **kw):
        self.calls.append("launch")
        self.launch_kw = kw
        return {"launched_in_s": 0.1, **kw}

    def read_mesh(self, path):
        self.calls.append("read_mesh")
        self.read_mesh_path = path
        return {"zones": {"wall": ["profile", "ground"]}}

    def setup_external_aero(self, **kw):
        self.calls.append("setup")
        self.setup_kw = kw
        if self.fail_setup:
            return {"failed": [{"step": "reference values",
                                "error": "Boom: nope"}], "applied": []}
        # resolve every zone-role kwarg against the mesh with the real
        # signature's defaults applied, exactly like the real layer's
        # fenced set_zone_type/bc.wall loops
        roles = [kw.get("inlet", "inlet"), kw.get("outlet", "outlet"),
                 *kw.get("profile_zones", []),
                 *kw.get("slip_zones", ["top"])]
        if kw.get("ground", "ground"):
            roles.append(kw.get("ground", "ground"))
        bad = [z for z in roles if z not in self.MESH_ZONES]
        if bad:
            return {"failed": [{"step": f"zone-type {z} -> wall",
                                "error": f"KeyError: {z} not in mesh"}
                               for z in bad], "applied": []}
        return {"failed": [], "applied": ["all"],
                "conventions": kw.get("conventions")}

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}")
        if self.hold is not None:
            self.hold.wait(timeout=10)
        n0 = len(self.cl)
        n1 = n0 + iterations
        if self.iter_cap is not None:
            n1 = min(n1, self.iter_cap)
        for i in range(n0, n1):
            self.cl.append(self.row_fn(i))
            self.cd.append(self.cd_fn(i) if self.cd_fn else 0.07)
        return {}

    def _report_histories(self):
        return {"lift_coef": list(self.cl), "drag_coef": list(self.cd)}

    def write_case_data(self, stem):
        self.calls.append("write")
        self.case_stem = stem
        p = Path(f"{stem}.cas.h5")
        p.write_bytes(b"CASH5")
        p.with_name(p.name[:-len(".cas.h5")] + ".dat.h5").write_bytes(
            b"DATH5")
        return {"written": str(p), "data_file": str(p)}

    def export_ascii(self, filename, quantities=None, location=None):
        """A plausible 2D cell-centre export: NO z column, a ring of
        points with two pressure plateaus pinning the Cp chain."""
        self.calls.append("export")
        if self.job_ref is not None:
            self.state_at_export = self.job_ref.state
        lines = ["cellnumber, x-coordinate, y-coordinate,"
                 " x-velocity, y-velocity, pressure"]
        n = 400
        for i in range(n):
            a = 2 * math.pi * i / n
            r = 0.3 + 0.25 * (i % 7) / 7
            x, y = 0.2 + r * math.cos(a), 0.25 + abs(r * math.sin(a))
            p_pa = -61.25 if i < n // 2 else -250.0
            lines.append(f"{i+1}, {x:.6e}, {y:.6e}, "
                         f"{15.0 + math.sin(a):.6e}, {0.5:.6e}, "
                         f"{p_pa:.6e}")
        Path(filename).write_text("\n".join(lines) + "\n",
                                  encoding="utf-8")
        return {"written": str(filename), "rows": n}

    def shutdown(self):
        self.shutdowns += 1
        return {"status": "exited"}


def make_seams(fake, rec, chain_fn=None):
    """Fake _dxf/_sizing/_chain implementations recording into rec."""

    def fake_dxf(profiles_m, out_path, **kw):
        fake.calls.append("dxf")
        rec["dxf_profiles"] = profiles_m
        rec["dxf_kw"] = kw
        rec["dxf_path"] = Path(out_path)
        Path(out_path).write_bytes(b"DXF")
        return {"dxf_path": str(out_path),
                "domain_m": (-1.0, 0.0, 3.0, 1.0),
                "n_profiles": len(profiles_m),
                "n_points": sum(len(p) for p in profiles_m)}

    def fake_sizing(mode, cfg):
        fake.calls.append("sizing")
        rec["sizing_mode"] = mode
        rec["sizing_cfg"] = cfg
        return dict(SIZING_FAKE[mode])

    def default_chain(dxf_path, work_dir, **kw):
        fake.calls.append("chain")
        rec["chain_dxf"] = dxf_path
        rec["chain_kw"] = kw
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        msh = work / "FFF.msh"
        msh.write_bytes(b"(2 2)")
        return {"msh_path": str(msh), "n_cells": 84210,
                "zones": ["fluid", "inlet", "outlet", "ground",
                          "upper_bound", "profile"],
                "stage_s": {"spaceclaim": 1.0, "workbench": 2.0},
                "project_dir": str(work / "proj")}

    return fake_dxf, fake_sizing, chain_fn or default_chain


def run_job(row_fn, n_iters=500, conventions="default", sizing="default",
            n_ranks=2, fail_setup=False, cancel_after=None, hold=None,
            cd_fn=None, iter_cap=None, fm_cls=FakeFM, precancel=False,
            chain_fn=None, cfg=CFG_D, settings=None):
    fake = fm_cls(row_fn, fail_setup=fail_setup, hold=hold, cd_fn=cd_fn,
                  iter_cap=iter_cap)
    rec = {}
    real = (fluent2d_run._mcp, fluent2d_run._dxf,
            fluent2d_run._sizing, fluent2d_run._chain)
    fd, fs, fc = make_seams(fake, rec, chain_fn)
    fluent2d_run._mcp = lambda: fake
    fluent2d_run._dxf = fd
    fluent2d_run._sizing = fs
    fluent2d_run._chain = fc
    job = fluent2d_run.Fluent2DJob(cfg, sizing, n_iters, n_ranks,
                                   conventions, settings)
    fake.job_ref = job
    try:
        if precancel:
            job.cancel()
        if cancel_after is not None:
            threading.Timer(cancel_after, job.cancel).start()
        job.run()
    finally:
        (fluent2d_run._mcp, fluent2d_run._dxf,
         fluent2d_run._sizing, fluent2d_run._chain) = real
    return job, fake, rec


def make_job(sizing="default", n_iters=500, n_ranks=1,
             conventions="default", settings=None, cfg=CFG_D):
    """Construct a job with only the sizing recipe faked — the recipe
    itself lives in the scripts package, which this offline suite never
    loads."""
    real = fluent2d_run._sizing
    fluent2d_run._sizing = lambda mode, c: dict(SIZING_FAKE[mode])
    try:
        return fluent2d_run.Fluent2DJob(cfg, sizing, n_iters, n_ranks,
                                        conventions, settings)
    finally:
        fluent2d_run._sizing = real


# ---- the pure conversion math, against hand values ----

check("cl_chord flips the sign and divides by the chord",
      abs(fluent2d_run.cl_chord(-0.875, 0.35) - 2.5) < 1e-12
      and abs(fluent2d_run.cl_chord(0.5, 0.25) - (-2.0)) < 1e-12)
check("cd_chord keeps the sign and divides by the chord",
      abs(fluent2d_run.cd_chord(0.07, 0.35) - 0.2) < 1e-12
      and abs(fluent2d_run.cd_chord(0.014, 0.28) - 0.05) < 1e-12)

# ---- happy path, default mode: exactly 500 iterations, raw + chord ----

job, fake, rec = run_job(lambda i: -0.875, n_iters=500)
snap = job.snapshot()
r = snap["result"]
check("default run completes done after exactly the requested iterations",
      snap["state"] == "done" and r and r["n_iters_run"] == 500
      and r["stop_reason"] == "requested iterations completed",
      f"({snap['state']}: {snap['error'] or (r and r['stop_reason'])})")
check("raw coefficients carry Fluent's own signs",
      r and r["cl_raw"] == -0.875 and r["cd_raw"] == 0.07,
      f"({r and (r['cl_raw'], r['cd_raw'])})")
check("chord-referenced downforce-positive conversion applied",
      r and r["cl_chord"] == 2.5 and r["cd_chord"] == 0.2
      and r["cl_rans"] == 2.5 and r["cd_rans"] == 0.2,
      f"({r and (r['cl_chord'], r['cd_chord'])})")
check("ref_note states Fluent's own reference convention",
      r and "1 m2" in r["ref_note"]
      and "Fluent's own convention" in r["ref_note"]
      and "team" not in r["ref_note"].lower())
check("default mode: residual_stop False on a full run",
      r and r["residual_stop"] is False
      and r["conventions"] == "default")
check("flat 500-iteration history reads converged (honest post-hoc)",
      r and r["converged"] is True and r["cl_drift"] is not None
      and r["cl_drift"] < cfd_run.CONVERGED_CL_TOL)
check("license released exactly once", fake.shutdowns == 1)
# the recipe is read at CONSTRUCTION (settings resolve against it
# before any run dir or ANSYS process exists), the rest in order
check("pipeline order: sizing -> dxf -> chain -> launch",
      fake.calls[:4] == ["sizing", "dxf", "chain", "launch"],
      f"({fake.calls[:6]})")
check("Fluent launched 2D double precision on the requested cores",
      fake.launch_kw == {"dimension": 2, "precision": "double",
                         "processors": 2}, f"({fake.launch_kw})")
check("setup speaks the default-2d conventions to the profile zone",
      fake.setup_kw["conventions"] == "default-2d"
      and fake.setup_kw["profile_zones"] == ["profile"]
      and fake.setup_kw["depth_m"] == 1.0
      and abs(fake.setup_kw["chord_m"] - 0.35) < 1e-12
      and abs(fake.setup_kw["inlet_velocity_ms"] - 15) < 1e-12,
      f"({fake.setup_kw})")
check("setup names the 2D mesh's own zone roles, not the slab's",
      fake.setup_kw["slip_zones"] == ["upper_bound"]
      and fake.setup_kw["ground"] == "ground",
      f"({fake.setup_kw.get('slip_zones')}, "
      f"{fake.setup_kw.get('ground')})")
# the seam itself must reject the 3D slab's slip default the way the
# real layer does — a fake that accepts any zone name is exactly what
# let a wrong default through a passing suite
_probe = FakeFM(lambda i: 0.0).setup_external_aero(
    inlet_velocity_ms=15.0, profile_zones=["profile"], chord_m=0.35)
check("fake setup fails unknown zone roles like the real layer",
      _probe["failed"] and "top" in _probe["failed"][0]["error"]
      and _probe["applied"] == [], f"({_probe['failed']})")
check("default sizing forwarded to the chain untouched",
      rec["sizing_mode"] == "default"
      and rec["chain_kw"]["edge_size_mm"] == 0.1
      and rec["chain_kw"]["first_layer_mm"] == 1.0
      and rec["chain_kw"]["n_layers"] == 10
      and rec["chain_kw"]["growth"] == 1.2, f"({rec['chain_kw']})")
check("documented domain proportions and stage budgets used unasked",
      rec["dxf_kw"] == {"front_l": 3.0, "back_l": 7.0, "top_h": 3.0}
      and rec["chain_kw"]["sc_budget_s"] == 300.0
      and rec["chain_kw"]["wb_budget_s"] == 900.0,
      f"({rec['dxf_kw']}, {rec['chain_kw'].get('sc_budget_s')}, "
      f"{rec['chain_kw'].get('wb_budget_s')})")
check("chain receives the job's own cancel event",
      rec["chain_kw"]["cancel_evt"] is job._cancel)
check("chain receives the slot gap for the inflation cap (1.5%c)",
      abs(rec["chain_kw"]["slot_gap_mm"] - 0.015 * 350.0) < 1e-9,
      f"({rec['chain_kw'].get('slot_gap_mm')})")
check("chain receives the ground clearance for the inflation cap",
      0.0 < rec["chain_kw"]["ground_clear_mm"] < 60.0,
      f"({rec['chain_kw'].get('ground_clear_mm')})")
check("chain fed the DXF the job wrote",
      rec["chain_dxf"] == str(job.case_dir / "profile.dxf")
      and rec["dxf_path"].is_file())
check("DXF written from the two installed-section polylines in meters",
      len(rec["dxf_profiles"]) == 2
      and all(p.ndim == 2 and p.shape[1] == 2 and p.shape[0] > 50
              for p in rec["dxf_profiles"])
      and max(float(p[:, 0].max()) for p in rec["dxf_profiles"]) < 1.0)
check("mesh summary carries the 2D chain provenance",
      snap["mesh"]["mesher"] == "ansys-2d"
      and snap["mesh"]["n_cells"] == 84210
      and "profile" in snap["mesh"]["zones"]
      and snap["mesh"]["stage_s"]["workbench"] == 2.0,
      f"({snap['mesh']})")
check("the default-mode chart shows the raw Fluent monitor values",
      snap["history"][-1]["iter"] == 500
      and snap["history"][-1]["cl"] == -0.875
      and 2 <= len(snap["history"]) <= 320)
check("panel comparison and delta computed from the chord value",
      r and r["panel"] and r["delta_cl_pct"] is not None)
_kg_direct = cfd_run.suggested_k_g(2.5, r["panel"]["c_free"],
                                   r["panel"]["c_ground"], job.cfg)
check("k_g suggested from the chord-referenced downforce value",
      r and r["suggested_k_g"] == _kg_direct
      and r["suggested_k_g"] is not None,
      f"(suggested {r and r['suggested_k_g']}, direct {_kg_direct})")
check("mesh caution always on (no 2D sizing is calibration grade)",
      r and r["mesh_caution"] is True and r["sizing"] == "default")
check("engine note is honest about conventions and limits",
      r and "chord-referenced" in r["engine_note"]
      and "downforce-positive" in r["engine_note"]
      and "OpenFOAM-engine feature" in r["engine_note"]
      and "screening" in r["engine_note"]
      and "app/" not in r["engine_note"]
      and "scripts/" not in r["engine_note"])
check("the default sizing's wall-function regime stated in the note",
      r and "wall functions" in r["engine_note"]
      and "1 mm first layer" in r["engine_note"]
      and "y+" in r["engine_note"]
      and "not directly comparable" in r["engine_note"])
check("no run-facing text speaks of a team",
      r and "team" not in r["engine_note"].lower()
      and "team" not in r["stop_reason"].lower()
      and "team" not in r["ref_note"].lower())
check("config written beside the run",
      (job.case_dir / "config.json").is_file())

# ---- snapshot duck-typing: every FluentJob snapshot key present ----

FLUENT_SNAP_KEYS = {"id", "state", "phase", "error", "progress",
                    "iteration", "n_iters", "mesh_size", "n_ranks",
                    "engine", "mesher", "elapsed_s", "latest",
                    "history", "mesh", "result", "case_dir"}
check("snapshot carries every FluentJob key plus the engine identity",
      FLUENT_SNAP_KEYS <= set(snap)
      and snap["engine"] == "fluent2d" and snap["mesher"] == "ansys-2d"
      and snap["mesh_size"] == "default",
      f"(missing {FLUENT_SNAP_KEYS - set(snap)})")
SETTINGS_KEYS = {"sizing", "edge_size_mm", "first_layer_mm", "n_layers",
                 "growth", "front_l", "back_l", "top_h", "n_iters",
                 "n_ranks", "conventions", "sc_budget_s", "wb_budget_s"}
check("snapshot and result both report the resolved settings",
      set(snap["settings"]) == SETTINGS_KEYS
      and snap["settings"] == r["settings"]
      and snap["settings"] == {
          "sizing": "default", "edge_size_mm": 0.1,
          "first_layer_mm": 1.0, "n_layers": 10, "growth": 1.2,
          "front_l": 3.0, "back_l": 7.0, "top_h": 3.0,
          "n_iters": 500, "n_ranks": 2, "conventions": "default",
          "sc_budget_s": 300.0, "wb_budget_s": 900.0},
      f"({snap['settings']})")
check("the reported settings are a copy, not the live object",
      snap["settings"] is not job.settings
      and r["settings"] is not job.settings)
FLUENT_RESULT_KEYS = {"engine", "cl_rans", "cl_rans_std", "cd_rans",
                      "cd_rans_std", "tail_rows", "n_iters_run",
                      "residual_stop", "converged", "stop_reason",
                      "cl_drift", "cd_drift", "downforce_n_at_rans_cl",
                      "panel", "panel_error", "delta_cl_pct",
                      "delta_cl_provisional", "delta_cd_pct",
                      "delta_cd_is_upper_bound", "cl_trend_note",
                      "wall_report", "wall_verdict", "sep_knife_edge",
                      "engine_note", "suggested_k_g", "mesh_caution",
                      "mesher", "n_ranks", "user_stopped", "case_dir"}
check("result mirrors the FluentJob surface plus the 2D additions",
      FLUENT_RESULT_KEYS <= set(r)
      and {"cl_raw", "cd_raw", "cl_chord", "cd_chord", "ref_note",
           "conventions", "sizing", "settings"} <= set(r)
      and r["wall_report"] is None and r["wall_verdict"] is None
      and r["engine"] == "fluent2d" and r["n_ranks"] == 2,
      f"(missing {FLUENT_RESULT_KEYS - set(r)})")
check("registry-facing attributes present",
      job._container == "" and isinstance(job.t_created, float)
      and job.stop_graceful() is False)

# ---- exported flow field: 2D export, z and w synthesized as zeros ----

from app.core import foam_post  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

tdir = foam_post.latest_time_dir(job.case_dir)
check("2D run writes an OpenFOAM-format time directory",
      tdir.name == "500" and (tdir / "C").is_file()
      and (tdir / "U").is_file() and (tdir / "p").is_file(),
      f"({tdir.name})")
_c = foam_post.parse_internal_field((tdir / "C").read_text())
_u = foam_post.parse_internal_field((tdir / "U").read_text())
_p = foam_post.parse_internal_field((tdir / "p").read_text())
check("exported fields parse through foam_post",
      _c.shape == (400, 3) and _u.shape == (400, 3)
      and _p.shape == (400,))
check("no z column in the export -> z = 0 and w = 0 written",
      float(abs(_c[:, 2]).max()) == 0.0
      and float(abs(_u[:, 2]).max()) == 0.0)
check("pressure converted to the kinematic convention (p/rho)",
      abs(float(_p[0]) - (-61.25 / 1.225)) < 1e-6, f"({_p[0]})")
_ffj = foam_post.flow_field_json(job.case_dir,
                                 StackConfig.from_dict(CFG_D), nx=120)
check("animation field JSON builds from a 2D Fluent run",
      _ffj["nx"] == 120 and _ffj["iter"] == "500"
      and any(x is not None for x in _ffj["umag"]))
_ffc = foam_post.flow_field_json(job.case_dir,
                                 StackConfig.from_dict(CFG_D), nx=120,
                                 include_cp=True)
_cp_min = min(x for x in _ffc["cp"] if x is not None)
check("Cp normalization matches the analytic plateau value",
      abs(_cp_min - (-250.0 / 1.225 / (0.5 * 15.0 ** 2))) < 0.02,
      f"(min cp {_cp_min})")
check("done is deferred until the flow export has run",
      fake.state_at_export == "running",
      f"(state at export: {fake.state_at_export})")
check("case write targets the run directory",
      Path(f"{fake.case_stem}.cas.h5") == job.case_dir / "case.cas.h5"
      and (job.case_dir / "case.dat.h5").is_file(),
      f"(stem: {fake.case_stem})")

# ---- residual stop: Fluent's own criteria end a default run early ----

job_rs, fake_rs, _ = run_job(lambda i: -0.875, n_iters=500,
                             iter_cap=240)
rrs = job_rs.snapshot()["result"]
check("history shorter than the chunk reads as a residual stop",
      job_rs.state == "done" and rrs and rrs["residual_stop"] is True
      and rrs["n_iters_run"] == 240,
      f"({job_rs.state}: {rrs and rrs['n_iters_run']})")
check("residual stop is named, never dressed as a drift verdict",
      rrs and "residual criteria" in rrs["stop_reason"]
      and "240" in rrs["stop_reason"], f"({rrs and rrs['stop_reason']})")
check("residual-stopped run released the license",
      fake_rs.shutdowns == 1)
check("no further chunks after the residual stop",
      fake_rs.calls.count("solve:250") == 1
      and "solve:190" not in fake_rs.calls, f"({fake_rs.calls})")

# ---- torn LIFT rfile row: a tiny shortfall is not a residual stop ----


class _TornLiftFM(FakeFM):
    """The lift rfile's last row is torn at the instant of the first
    read after each chunk; the flush lands before the re-read."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._fresh = False
        self.rereads = 0

    def solve(self, iterations, initialize=True):
        out = super().solve(iterations, initialize)
        self._fresh = True
        return out

    def _report_histories(self):
        if self._fresh:
            self._fresh = False
            return {"lift_coef": list(self.cl[:-1]),
                    "drag_coef": list(self.cd)}
        self.rereads += 1
        return super()._report_histories()


real_settle = fluent2d_run.RESIDUAL_SETTLE_S
fluent2d_run.RESIDUAL_SETTLE_S = 0.0
try:
    job_tl, fake_tl, _ = run_job(lambda i: -0.875, n_iters=500,
                                 fm_cls=_TornLiftFM)
finally:
    fluent2d_run.RESIDUAL_SETTLE_S = real_settle
rtl = job_tl.snapshot()["result"]
check("torn lift row: the settled re-read averts a false residual stop",
      job_tl.state == "done" and rtl and rtl["residual_stop"] is False
      and rtl["n_iters_run"] == 500
      and rtl["stop_reason"] == "requested iterations completed"
      and fake_tl.rereads >= 1,
      f"({job_tl.state}: {rtl and rtl['stop_reason']}, "
      f"rereads {fake_tl.rereads})")

# a shortfall that SURVIVES the re-read is Fluent's own stop — even one
# row under the budget must still be reported honestly
real_settle = fluent2d_run.RESIDUAL_SETTLE_S
fluent2d_run.RESIDUAL_SETTLE_S = 0.0
try:
    job_p1, _, _ = run_job(lambda i: -0.875, n_iters=500, iter_cap=309)
finally:
    fluent2d_run.RESIDUAL_SETTLE_S = real_settle
rp1 = job_p1.snapshot()["result"]
check("persistent 1-row shortfall still reads as a residual stop",
      job_p1.state == "done" and rp1 and rp1["residual_stop"] is True
      and rp1["n_iters_run"] == 309
      and "residual criteria" in rp1["stop_reason"],
      f"({rp1 and (rp1['n_iters_run'], rp1['stop_reason'])})")

# ---- short default run: too few rows for a drift verdict, says so ----

job_sh, _, _ = run_job(lambda i: -0.875, n_iters=120)
rsh = job_sh.snapshot()["result"]
check("120-iteration run is honest about the missing verdict",
      job_sh.state == "done" and rsh and rsh["converged"] is False
      and "too few" in rsh["stop_reason"]
      and rsh["suggested_k_g"] is None
      and rsh["delta_cl_provisional"] is True,
      f"({rsh and rsh['stop_reason']})")

# ---- studio conventions: the OpenFOAM force-stop doctrine ----

# studio setup references the monitors to the chord with a downforce-
# positive lift vector AT THE SOURCE (fluent_mcp studio branch), so the
# fake emits chord-referenced values directly — the engine must NOT
# convert them a second time
job_s, fake_s, rec_s = run_job(lambda i: 2.5, n_iters=6000,
                               conventions="studio",
                               cd_fn=lambda i: 0.2)
ss = job_s.snapshot()
rs = ss["result"]
check("studio mode: flat history stops on drift with the verdict",
      job_s.state == "done" and rs and rs["converged"] is True
      and rs["stop_reason"] == "force history converged"
      and rs["residual_stop"] is False,
      f"({job_s.state}: {rs and rs['stop_reason']})")
check("stop respects the floor and the two-boundary persistence",
      rs and rs["n_iters_run"] == 3060
      and rs["n_iters_run"] >= fluent2d_run.DRIFT_SKIP
      + 3 * cfd_run.FORCE_STOP_WINDOW,
      f"(stopped at {rs and rs['n_iters_run']})")
check("studio setup speaks the studio conventions",
      fake_s.setup_kw["conventions"] == "studio")
check("studio chart passes the source-referenced monitors through",
      ss["history"][-1]["cl"] == 2.5 and ss["history"][-1]["cd"] == 0.2,
      f"({ss['history'][-1]})")
check("studio result never double-converts and carries no raw pair",
      rs and rs["cl_raw"] is None and rs["cd_raw"] is None
      and rs["cl_chord"] == 2.5 and rs["cd_chord"] == 0.2
      and rs["cl_rans"] == 2.5
      and rs["conventions"] == "studio",
      f"(raw {rs and rs['cl_raw']}, chord {rs and rs['cl_chord']})")
check("studio converged run also offers the k_g suggestion",
      rs and rs["suggested_k_g"] == _kg_direct
      and rs["suggested_k_g"] is not None)

# flat Cl over a still-climbing Cd must NOT force-stop (Cd converges
# last — the combined criterion applies here too)
job_cd, _, _ = run_job(lambda i: 2.5, n_iters=3000,
                       conventions="studio",
                       cd_fn=lambda i: 0.2 + 0.0006 * i)
rcd = job_cd.snapshot()["result"]
check("studio: flat Cl over climbing Cd runs to the cap (Cd gate)",
      job_cd.state == "done" and rcd and rcd["n_iters_run"] == 3000
      and rcd["stop_reason"] == "iteration cap reached",
      f"({rcd and rcd['stop_reason']} at {rcd and rcd['n_iters_run']})")

# ---- studio-yplus1 sizing flows through the sizing seam ----

job_y, _, rec_y = run_job(lambda i: -0.875, n_iters=500,
                          sizing="studio-yplus1")
ry = job_y.snapshot()["result"]
check("studio-yplus1 sizing forwarded to the chain",
      rec_y["sizing_mode"] == "studio-yplus1"
      and rec_y["chain_kw"]["first_layer_mm"] == 0.02
      and rec_y["chain_kw"]["n_layers"] == 30
      and ry["sizing"] == "studio-yplus1",
      f"({rec_y['chain_kw']})")
check("resolved-wall sizing carries no wall-function caveat",
      ry and "wall functions" not in ry["engine_note"])

# ---- settings: resolved once, then reported as what ran ----

FULL_SETTINGS = {"sizing": "default", "edge_size_mm": 0.4,
                 "first_layer_mm": 0.05, "n_layers": 22, "growth": 1.15,
                 "front_l": 4.0, "back_l": 9.0, "top_h": 2.5,
                 "n_iters": 500, "n_ranks": 2, "conventions": "default",
                 "sc_budget_s": 600.0, "wb_budget_s": 1800.0}

job_o, _, rec_o = run_job(lambda i: -0.875, settings=FULL_SETTINGS)
ro = job_o.snapshot()["result"]
check("every mesh override reaches the chain in place of the recipe",
      rec_o["chain_kw"]["edge_size_mm"] == 0.4
      and rec_o["chain_kw"]["first_layer_mm"] == 0.05
      and rec_o["chain_kw"]["n_layers"] == 22
      and rec_o["chain_kw"]["growth"] == 1.15,
      f"({rec_o['chain_kw']})")
check("domain proportions reach the DXF writer",
      rec_o["dxf_kw"] == {"front_l": 4.0, "back_l": 9.0, "top_h": 2.5},
      f"({rec_o['dxf_kw']})")
check("stage budgets reach the meshing chain",
      rec_o["chain_kw"]["sc_budget_s"] == 600.0
      and rec_o["chain_kw"]["wb_budget_s"] == 1800.0,
      f"({rec_o['chain_kw'].get('sc_budget_s')}, "
      f"{rec_o['chain_kw'].get('wb_budget_s')})")
check("an override never rewrites the sizing label",
      ro and ro["sizing"] == "default"
      and ro["settings"]["sizing"] == "default"
      and job_o.mesh_size == "default", f"({ro and ro['sizing']})")
check("the near-wall caveat follows the resolved first layer",
      ro and "wall functions" not in ro["engine_note"],
      f"({ro and ro['engine_note'][-120:]})")
check("the result reports the resolved set, not the request",
      ro and ro["settings"] == FULL_SETTINGS, f"({ro and ro['settings']})")

# an empty override is a request for the recipe value, never a zero
NULL_SETTINGS = {"sizing": "studio-yplus1", "edge_size_mm": None,
                 "first_layer_mm": None, "n_layers": None,
                 "growth": None, "front_l": None, "back_l": None,
                 "top_h": None, "n_iters": 500, "n_ranks": 1,
                 "conventions": "default", "sc_budget_s": None,
                 "wb_budget_s": None}
job_n, _, rec_n = run_job(lambda i: -0.875, sizing="studio-yplus1",
                          n_ranks=1, settings=NULL_SETTINGS)
rn = job_n.snapshot()["result"]
check("null mesh overrides fall back to the sizing recipe",
      rec_n["chain_kw"]["edge_size_mm"] == 0.7
      and rec_n["chain_kw"]["first_layer_mm"] == 0.02
      and rec_n["chain_kw"]["n_layers"] == 30
      and rec_n["chain_kw"]["growth"] == 1.2, f"({rec_n['chain_kw']})")
check("null domain and budget overrides fall back to the documented "
      "values",
      rec_n["dxf_kw"] == {"front_l": 3.0, "back_l": 7.0, "top_h": 3.0}
      and rec_n["chain_kw"]["sc_budget_s"] == 300.0
      and rec_n["chain_kw"]["wb_budget_s"] == 900.0,
      f"({rec_n['dxf_kw']})")
check("the fallback values are the ones reported",
      rn and rn["settings"] == {
          "sizing": "studio-yplus1", "edge_size_mm": 0.7,
          "first_layer_mm": 0.02, "n_layers": 30, "growth": 1.2,
          "front_l": 3.0, "back_l": 7.0, "top_h": 3.0, "n_iters": 500,
          "n_ranks": 1, "conventions": "default", "sc_budget_s": 300.0,
          "wb_budget_s": 900.0}, f"({rn and rn['settings']})")

# a settings object carrying the four run parameters is the authority:
# the panel sends one object, so a positional argument left behind must
# never silently win over it
job_a = make_job(sizing="default", n_iters=500, n_ranks=1,
                 conventions="default",
                 settings={"sizing": "studio-yplus1", "n_iters": 900,
                           "n_ranks": 4, "conventions": "studio"})
check("the settings object's own run parameters win over the positional",
      job_a.mesh_size == "studio-yplus1" and job_a.n_iters == 900
      and job_a.n_ranks == 4 and job_a.conventions == "studio"
      and job_a.settings["sizing"] == "studio-yplus1"
      and job_a.settings["n_iters"] == 900
      and job_a.settings["n_ranks"] == 4
      and job_a.settings["conventions"] == "studio",
      f"({job_a.mesh_size}, {job_a.n_iters}, {job_a.n_ranks}, "
      f"{job_a.conventions})")
check("no settings object at all resolves to the pure recipe",
      make_job().settings == {
          "sizing": "default", "edge_size_mm": 0.1,
          "first_layer_mm": 1.0, "n_layers": 10, "growth": 1.2,
          "front_l": 3.0, "back_l": 7.0, "top_h": 3.0, "n_iters": 500,
          "n_ranks": 1, "conventions": "default", "sc_budget_s": 300.0,
          "wb_budget_s": 900.0}, f"({make_job().settings})")

# ---- the mesh's OWN inflation, not the requested one ----
# the chain caps the stack to the clearances it faces, and drops it
# entirely on a degrade retry; a run that reported the request would
# document a boundary layer the mesh never had


def capped_chain(first_layer_mm, n_layers, note, degraded=False):
    """A chain that meshed something other than what it was asked for."""

    def chain(dxf_path, work_dir, **kw):
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        msh = work / "FFF.msh"
        msh.write_bytes(b"(2 2)")
        return {"msh_path": str(msh), "n_cells": 84210,
                "zones": ["fluid", "inlet", "outlet", "ground",
                          "upper_bound", "profile"],
                "stage_s": {"spaceclaim": 1.0, "workbench": 2.0},
                "project_dir": str(work / "proj"),
                "inflation": {"first_layer_mm": first_layer_mm,
                              "n_layers": n_layers, "capped": True,
                              "degraded": degraded, "note": note}}

    return chain


CAP_NOTE = ("inflation capped to the tightest clearance (layer budget "
            "1.6 mm): 10 layers -> 1")
job_c, _, _ = run_job(lambda i: -0.875,
                      chain_fn=capped_chain(1.0, 1, CAP_NOTE))
snap_c = job_c.snapshot()
rc = snap_c["result"]
check("a capped layer count is reported as the mesh got it, the request "
      "beside it",
      rc and rc["settings"]["n_layers"] == 1
      and rc["settings"]["n_layers_requested"] == 10
      and rc["settings"]["first_layer_mm"] == 1.0
      and rc["settings"]["first_layer_mm_requested"] == 1.0,
      f"({rc and rc['settings']})")
check("snapshot and result report the same reconciled set",
      snap_c["settings"] == rc["settings"] and rc["settings"]
      is not job_c.settings)
check("the cap note reaches the card (it is rendered nowhere else)",
      rc and CAP_NOTE in rc["engine_note"],
      f"({rc and rc['engine_note'][-160:]})")

FL_NOTE = ("inflation capped to fit the tightest clearance: first layer "
           "1 mm -> 0.4 mm, single layer")
job_fl, _, _ = run_job(lambda i: -0.875,
                       chain_fn=capped_chain(0.4, 1, FL_NOTE))
rfl = job_fl.snapshot()["result"]
check("the near-wall caveat quotes the MESHED first layer, not the ask",
      rfl and rfl["settings"]["first_layer_mm"] == 0.4
      and "0.4 mm first layer" in rfl["engine_note"]
      and "1 mm first layer" not in rfl["engine_note"],
      f"({rfl and rfl['engine_note'][-200:]})")

# a degrade retry meshes with NO inflation at all — on the resolved-wall
# recipe the first-layer height alone would read as a resolved wall
DEG_NOTE = ("the inflation layers failed on this geometry — meshed "
            "WITHOUT boundary layers (wall resolution degraded; treat "
            "forces as screening values)")
job_dg, _, _ = run_job(lambda i: -0.875, sizing="studio-yplus1",
                       chain_fn=capped_chain(0.02, 0, DEG_NOTE,
                                             degraded=True))
rdg = job_dg.snapshot()["result"]
check("a mesh with zero layers is never reported as resolved-wall",
      rdg and rdg["settings"]["n_layers"] == 0
      and rdg["settings"]["n_layers_requested"] == 30
      and "no inflation layers" in rdg["engine_note"]
      and "wall functions" in rdg["engine_note"]
      and "not directly comparable" in rdg["engine_note"],
      f"({rdg and rdg['engine_note'][-220:]})")
check("the degrade note is stated on the card too",
      rdg and DEG_NOTE in rdg["engine_note"])

# the same hole reachable by hand: layers 0 on the resolved-wall recipe
job_z, _, _ = run_job(lambda i: -0.875, sizing="studio-yplus1",
                      settings={"n_layers": 0})
rz = job_z.snapshot()["result"]
check("an n_layers=0 override carries the unresolved-wall caveat",
      rz and rz["settings"]["n_layers"] == 0
      and "no inflation layers" in rz["engine_note"]
      and "wall functions" in rz["engine_note"],
      f"({rz and rz['engine_note'][-220:]})")
check("an uncapped chain leaves the resolved set exactly as resolved",
      set(job_z.settings) == SETTINGS_KEYS,
      f"({sorted(set(job_z.settings) - SETTINGS_KEYS)})")

# ---- reference density follows the fluid the solve actually ran ----

# the default conventions leave the material at Fluent's own air, so the
# references and the Cp conversion must use 1.225 even when the
# configuration's air differs — anything else folds a silent
# rho/1.225 factor into every coefficient
job_r, fake_r, _ = run_job(lambda i: -0.875, n_iters=500,
                           cfg={**CFG_D, "rho": 1.0})
rr = job_r.snapshot()["result"]
check("default setup references Fluent's own air, not the config's",
      abs(fake_r.setup_kw["rho"] - 1.225) < 1e-12
      and abs(fake_r.setup_kw["mu"] - 1.7894e-05) < 1e-15,
      f"({fake_r.setup_kw.get('rho')}, {fake_r.setup_kw.get('mu')})")
_p_r = foam_post.parse_internal_field(
    (foam_post.latest_time_dir(job_r.case_dir) / "p").read_text())
check("default Cp export normalized by the solved (default-air) density",
      abs(float(_p_r[0]) - (-61.25 / 1.225)) < 1e-6, f"({_p_r[0]})")
check("default ref_note states the default-air reference",
      rr and "1.225" in rr["ref_note"])

# studio conventions set the material to the config's air — there the
# config values ARE the solved fluid
job_sr, fake_sr, _ = run_job(lambda i: 2.5, n_iters=3000,
                             conventions="studio", cd_fn=lambda i: 0.2,
                             cfg={**CFG_D, "rho": 1.0})
check("studio setup passes the config air to the material",
      abs(fake_sr.setup_kw["rho"] - 1.0) < 1e-12
      and abs(fake_sr.setup_kw["mu"]
              - 1.0 * job_sr.cfg.nu) < 1e-15,
      f"({fake_sr.setup_kw.get('rho')}, {fake_sr.setup_kw.get('mu')})")
_p_sr = foam_post.parse_internal_field(
    (foam_post.latest_time_dir(job_sr.case_dir) / "p").read_text())
check("studio Cp export normalized by the config density",
      job_sr.state == "done"
      and abs(float(_p_sr[0]) - (-61.25 / 1.0)) < 1e-6,
      f"({job_sr.state}, {_p_sr[0]})")

# ---- setup failure surfaces the failed step, license released ----

job_f, fake_f, _ = run_job(lambda i: -0.875, fail_setup=True)
check("failed setup step fails the job with the step named",
      job_f.state == "failed" and "reference values"
      in (job_f.error or ""), f"({(job_f.error or '')[:60]})")
check("failed setup still released the license", fake_f.shutdowns == 1)

# ---- raise AFTER launch: the session must not outlive the job ----


class _ReadBoomFM(FakeFM):
    """read_mesh raises — a corrupt chain mesh, post-launch."""

    def read_mesh(self, path):
        self.calls.append("read_mesh")
        raise RuntimeError("corrupt mesh file")


class _SolveBoomFM(FakeFM):
    """solve raises with NO cancel in flight — a diverged AMG solve."""

    def solve(self, iterations, initialize=True):
        self.calls.append(f"solve:{iterations}")
        raise RuntimeError("AMG solver diverged")


job_rb, fake_rb, _ = run_job(lambda i: -0.875, fm_cls=_ReadBoomFM)
check("read_mesh raise fails the job AND releases the license",
      job_rb.state == "failed" and "corrupt mesh file"
      in (job_rb.error or "") and fake_rb.shutdowns == 1,
      f"({job_rb.state}, shutdowns {fake_rb.shutdowns})")
job_sb, fake_sb, _ = run_job(lambda i: -0.875, fm_cls=_SolveBoomFM)
check("uncancelled solve raise fails the job AND releases the license",
      job_sb.state == "failed" and "AMG solver diverged"
      in (job_sb.error or "") and fake_sb.shutdowns == 1,
      f"({job_sb.state}, shutdowns {fake_sb.shutdowns})")

# ---- chain failure without a cancel: a friendly meshing error ----


def boom_chain(dxf_path, work_dir, **kw):
    raise RuntimeError("workbench: Mechanical mesh stage failed")


job_cb, fake_cb, _ = run_job(lambda i: -0.875, chain_fn=boom_chain)
check("chain failure fails the job before any license checkout",
      job_cb.state == "failed" and "ANSYS 2D meshing failed"
      in (job_cb.error or "") and "launch" not in fake_cb.calls
      and fake_cb.shutdowns == 0,
      f"({job_cb.state}: {(job_cb.error or '')[:60]})")

# ---- impossible geometry refused before any ANSYS work ----

# passes StackConfig validation but cannot fit the CFD domain
job_bg, fake_bg, _ = run_job(lambda i: -0.875,
                             cfg={**CFG_D, "ride_height_mm": 4900})
check("impossible geometry fails early with the reason",
      job_bg.state == "failed" and "case geometry failed"
      in (job_bg.error or "")
      # only the recipe lookup the constructor makes — no DXF, no chain,
      # no license
      and fake_bg.calls == ["sizing"]
      and not job_bg.case_dir.exists(),
      f"({job_bg.state}: {(job_bg.error or '')[:60]})")

# ---- cancel before the pipeline starts ----

job_pc, fake_pc, _ = run_job(lambda i: -0.875, precancel=True)
check("pre-set cancel lands before any geometry or license work",
      job_pc.state == "cancelled" and fake_pc.calls == ["sizing"]
      and fake_pc.shutdowns == 0,
      f"({job_pc.state}, calls {fake_pc.calls})")

# ---- cancel DURING the meshing chain: the chain's kill converts ----

rec_cc = {}


def blocking_chain(dxf_path, work_dir, **kw):
    # the real chain polls cancel_evt every ~2 s and kills the ANSYS
    # process tree, then raises — model exactly that
    rec_cc["cancel_evt"] = kw["cancel_evt"]
    got = kw["cancel_evt"].wait(timeout=10)
    if got:
        raise RuntimeError("stage killed: cancelled")
    raise RuntimeError("test never cancelled")


job_cc, fake_cc, _ = run_job(lambda i: -0.875, chain_fn=blocking_chain,
                             cancel_after=0.3)
check("cancel mid-chain lands in cancelled, no license ever taken",
      job_cc.state == "cancelled" and "launch" not in fake_cc.calls,
      f"({job_cc.state}: {job_cc.error})")
check("the chain was handed the job's cancel event",
      rec_cc.get("cancel_evt") is job_cc._cancel)

# ---- cancellation between chunks ----

hold = threading.Event()
holder = {}


def cancel_then_release():
    deadline = time.time() + 10
    while time.time() < deadline and not any(
            c.startswith("solve:") for c in holder["fake"].calls):
        time.sleep(0.01)
    holder["job"].cancel()
    hold.set()


fake_c = FakeFM(lambda i: -0.875, hold=hold)
rec_c = {}
real = (fluent2d_run._mcp, fluent2d_run._dxf,
        fluent2d_run._sizing, fluent2d_run._chain)
fd_c, fs_c, fc_c = make_seams(fake_c, rec_c)
fluent2d_run._mcp = lambda: fake_c
fluent2d_run._dxf = fd_c
fluent2d_run._sizing = fs_c
fluent2d_run._chain = fc_c
job_c = fluent2d_run.Fluent2DJob(CFG_D, "default", 3000, 2)
holder["job"], holder["fake"] = job_c, fake_c
try:
    threading.Thread(target=cancel_then_release, daemon=True).start()
    job_c.run()
finally:
    (fluent2d_run._mcp, fluent2d_run._dxf,
     fluent2d_run._sizing, fluent2d_run._chain) = real
check("cancel mid-solve lands in cancelled",
      job_c.state == "cancelled", f"({job_c.state})")
check("cancelled run released the license",
      fake_c.shutdowns >= 1, f"({fake_c.shutdowns})")
check("cancel itself killed the session (not just the flag)",
      fake_c.shutdowns >= 2, f"({fake_c.shutdowns})")

# ---- cancel during a BLOCKING solve: the kill converts ----


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
fake_k = KilledFM(lambda i: -0.875, hold=hold_k)
rec_k = {}
real = (fluent2d_run._mcp, fluent2d_run._dxf,
        fluent2d_run._sizing, fluent2d_run._chain)
fd_k, fs_k, fc_k = make_seams(fake_k, rec_k)
fluent2d_run._mcp = lambda: fake_k
fluent2d_run._dxf = fd_k
fluent2d_run._sizing = fs_k
fluent2d_run._chain = fc_k
job_k = fluent2d_run.Fluent2DJob(CFG_D, "default", 3000, 2)


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
    (fluent2d_run._mcp, fluent2d_run._dxf,
     fluent2d_run._sizing, fluent2d_run._chain) = real
check("cancel kills a BLOCKING solve within the chunk (not after it)",
      job_k.state == "cancelled" and job_k.error is None,
      f"({job_k.state}: {job_k.error})")
check("the kill went through the session shutdown",
      fake_k.shutdowns >= 1, f"({fake_k.shutdowns})")

# ---- wall-clock backstop: a wedged session fails honestly ----

real_wall = fluent2d_run.WALL_LIMIT_S
fluent2d_run.WALL_LIMIT_S = -1.0
try:
    job_w, fake_w, _ = run_job(lambda i: -0.875, n_iters=3000)
finally:
    fluent2d_run.WALL_LIMIT_S = real_wall
check("past the wall limit the job fails with the honest reason",
      job_w.state == "failed" and "wall-clock backstop"
      in (job_w.error or ""), f"({job_w.state}: {job_w.error})")
check("wall-stopped run still released the license",
      fake_w.shutdowns == 1, f"({fake_w.shutdowns})")

# ---- torn drag rfile: cd one row short of cl must not crash ----


class _TornCdFM(FakeFM):
    def _report_histories(self):
        return {"lift_coef": list(self.cl),
                "drag_coef": list(self.cd[:-1])}


job_tc, fake_tc, _ = run_job(lambda i: -0.875, n_iters=500,
                             fm_cls=_TornCdFM)
check("torn drag row: run completes instead of crashing",
      job_tc.state == "done" and fake_tc.shutdowns == 1,
      f"({job_tc.state}: {job_tc.error})")
check("torn drag row: chart rows stay paired",
      all(h["cd"] is not None for h in job_tc.snapshot()["history"]))

# ---- validation ----

for bad_kw, label in (
        (dict(mesh_size="coarse"), "preset names are not 2D sizings"),
        # the session layer's vocabulary is not this module's: its
        # walkthrough recipe has no meaning at the app level
        (dict(conventions="walkthrough"), "unknown conventions rejected"),
        # the retired vocabulary must not be accepted anywhere, in
        # either slot — there is no compatibility shim
        (dict(mesh_size="team"), "the retired sizing name is rejected"),
        (dict(conventions="team"), "the retired conventions name is "
                                   "rejected"),
        (dict(n_iters=30), "too few iterations rejected"),
        (dict(n_iters=30000), "too many iterations rejected"),
        (dict(n_ranks=64), "out-of-range processor count rejected")):
    kw = dict(mesh_size="default", n_iters=500, n_ranks=1,
              conventions="default")
    kw.update(bad_kw)
    try:
        make_job(kw["mesh_size"], kw["n_iters"], kw["n_ranks"],
                 kw["conventions"])
        check(label, False)
    except ValueError:
        check(label, True)
for bad_settings, label in (
        ({"sizing": "team"}, "the retired sizing name is rejected in "
                             "the settings too"),
        ({"conventions": "team"}, "the retired conventions name is "
                                  "rejected in the settings too")):
    try:
        make_job(settings=bad_settings)
        check(label, False)
    except ValueError:
        check(label, True)

# every settings range, at both edges: an impossible value is refused at
# construction, before any run directory or licensed process exists
for key, value, ok in (
        ("edge_size_mm", 0.0, False), ("edge_size_mm", -0.1, False),
        ("edge_size_mm", float("inf"), False),
        ("edge_size_mm", 1e-6, True),
        ("first_layer_mm", 0.0, False), ("first_layer_mm", -1.0, False),
        ("first_layer_mm", 1e-6, True),
        ("n_layers", -1, False), ("n_layers", 101, False),
        ("n_layers", 0, True), ("n_layers", 100, True),
        # the int-cast keys must refuse what they cannot represent
        # instead of raising OverflowError or truncating silently
        ("n_layers", float("inf"), False),
        ("n_layers", float("nan"), False),
        ("n_layers", 10.9, False), ("n_layers", -0.5, False),
        ("n_layers", 10.0, True),
        ("n_iters", float("inf"), False), ("n_iters", 500.5, False),
        ("n_ranks", float("inf"), False), ("n_ranks", 2.5, False),
        ("growth", 1.0, False), ("growth", 3.01, False),
        ("growth", 1.001, True), ("growth", 3.0, True),
        ("front_l", 0.49, False), ("front_l", 20.01, False),
        ("front_l", 0.5, True), ("front_l", 20.0, True),
        ("back_l", 0.49, False), ("back_l", 40.01, False),
        ("back_l", 0.5, True), ("back_l", 40.0, True),
        ("top_h", 0.49, False), ("top_h", 20.01, False),
        ("top_h", 0.5, True), ("top_h", 20.0, True),
        ("n_iters", 49, False), ("n_iters", 20001, False),
        ("n_iters", 50, True), ("n_iters", 20000, True),
        ("n_ranks", 0, False), ("n_ranks", 33, False),
        ("n_ranks", 1, True), ("n_ranks", 32, True),
        ("sc_budget_s", 29.9, False), ("sc_budget_s", 7200.1, False),
        ("sc_budget_s", 30.0, True), ("sc_budget_s", 7200.0, True),
        ("wb_budget_s", 59.9, False), ("wb_budget_s", 43200.1, False),
        ("wb_budget_s", 60.0, True), ("wb_budget_s", 43200.0, True),
        ("edge_size_mm", "wide", False), ("n_layers", None, True)):
    label = (f"settings {key}={value!r} "
             f"{'accepted' if ok else 'refused'}")
    try:
        j = make_job(settings={key: value})
        check(label, ok, f"(resolved {j.settings[key]!r})")
    except ValueError as e:
        check(label, not ok, f"({e})" if ok else "")
j50 = make_job("default", 50, 1, "default")
check("50-iteration parity run is constructible (the 2D floor)",
      j50.n_iters == 50 and j50.state == "pending")

print(f"\n{sum(results)}/{len(results)} fluent2d-run checks passed")
sys.exit(0 if all(results) else 1)
