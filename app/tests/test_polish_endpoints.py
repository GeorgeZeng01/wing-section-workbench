"""Polish endpoints — gates, refusal shapes and the re-verify handoff,
in-process (the FastAPI endpoint functions called directly, fabricated
jobs planted in cfd_run's registry, the ANSYS seams faked). No uvicorn,
no ANSYS, no Docker.

Run directly:  python app/tests/test_polish_endpoints.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TMP = tempfile.mkdtemp(prefix="wss_polish_api_")
os.environ["WSS_DATA_DIR"] = _TMP
os.environ["WSS_EXPORTS_DIR"] = str(Path(_TMP) / "exports")

import numpy as np  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import app.server as server  # noqa: E402
from app.core import adjoint_run, cfd_run, fluent2d_run  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def expect_http(label, status, fn, frag=""):
    try:
        fn()
        check(label, False, "(no HTTPException)")
    except HTTPException as e:
        check(label, e.status_code == status
              and (not frag or frag in str(e.detail)),
              f"({e.status_code}: {e.detail})")


CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "ride_height_mm": 30, "chord_mm": 350, "span_mm": 1400,
    "speed_ms": 15, "ncrit": 7, "n_panels_per_side": 50,
}


def ring(cx, cy, rx, ry, n=48):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([cx + rx * np.cos(a),
                            cy + ry * np.sin(a)])


OVR = [ring(0.10, 0.060, 0.10, 0.012).round(7).tolist(),
       ring(0.28, 0.045, 0.04, 0.006).round(7).tolist()]


class StubJob:
    """Just enough job for the registry-facing endpoints."""

    def __init__(self, engine="polish", state="done", result=None,
                 config=CFG, seed_settings=None, n_ranks=1):
        self.id = "stub" + os.urandom(4).hex()
        self._engine = engine
        self.state = state
        self.result = result
        self.config = json.loads(json.dumps(config))
        self.case_dir = Path(_TMP) / f"case-{self.id}"
        self.case_dir.mkdir(parents=True, exist_ok=True)
        self.seed_settings = seed_settings or {}
        self.n_ranks = n_ranks
        self._container = ""

    def snapshot(self):
        return {"engine": self._engine, "id": self.id,
                "state": self.state,
                "result": self.result if self.state == "done" else None,
                "mesh_size": "default", "mesh": {"n_cells": 1}}


def plant(job):
    with cfd_run._jobs_lock:
        cfd_run._jobs[job.id] = job
    return job


def unplant(job):
    with cfd_run._jobs_lock:
        cfd_run._jobs.pop(job.id, None)


# ---------- body validation mirrors the job's ranges ----------

b = server.PolishStartBody(run_id="x")
opts = adjoint_run.resolve_options(None)
check("body: defaults are exactly the job's defaults",
      b.drag_exchange_k == opts["drag_exchange_k"]
      and b.design_iters == opts["design_iters"]
      and b.flow_iters == opts["flow_iters"]
      and b.adjoint_iters == opts["adjoint_iters"]
      and b.margin_pct == opts["margin_pct"]
      and b.settle_iters == opts["settle_iters"])
for field, val in [("drag_exchange_k", -0.1), ("drag_exchange_k", 10.1),
                   ("design_iters", 0), ("design_iters", 61),
                   ("flow_iters", 99), ("adjoint_iters", 49),
                   ("margin_pct", 0.5), ("settle_iters", 2001)]:
    try:
        server.PolishStartBody(run_id="x", **{field: val})
        check(f"body: {field}={val} refused", False)
    except ValidationError:
        check(f"body: {field}={val} refused", True)


# ---------- polish start gates ----------

expect_http("start: unknown run is a 404", 404,
            lambda: server.polish_start(
                server.PolishStartBody(run_id="nope")),
            "run one first")

seed_wrong = plant(StubJob(engine="openfoam", state="done",
                           result={"cl_rans": 2.0}))
expect_http("start: an OpenFOAM seed is a 422 naming the engine", 422,
            lambda: server.polish_start(
                server.PolishStartBody(run_id=seed_wrong.id)),
            "openfoam")
unplant(seed_wrong)

seed_running = plant(StubJob(engine="fluent2d", state="running"))
expect_http("start: an unfinished seed is a 422", 422,
            lambda: server.polish_start(
                server.PolishStartBody(run_id=seed_running.id)),
            "not finished")
unplant(seed_running)


# ---------- reverify + export gates ----------

expect_http("reverify: unknown job is a 404", 404,
            lambda: server.polish_reverify("nope"))

not_polish = plant(StubJob(engine="fluent2d", state="done",
                           result={"cl_rans": 2.0}))
expect_http("reverify: a non-polish run is a 422", 422,
            lambda: server.polish_reverify(not_polish.id),
            "not an adjoint polish")
unplant(not_polish)

running_polish = plant(StubJob(engine="polish", state="running"))
expect_http("reverify: an unfinished polish is a 409", 409,
            lambda: server.polish_reverify(running_polish.id),
            "not finished")
unplant(running_polish)

no_gain = plant(StubJob(engine="polish", state="done", result={
    "improved": False, "artifacts": {"profiles_json": None}}))
expect_http("reverify: no compliant improvement is a 409", 409,
            lambda: server.polish_reverify(no_gain.id),
            "no compliant improvement")
expect_http("export-dxf: same refusal shape", 409,
            lambda: server.polish_export_dxf(no_gain.id),
            "no compliant improvement")
unplant(no_gain)

pruned = plant(StubJob(engine="polish", state="done", result={
    "improved": True, "artifacts": {"profiles_json": "gone.json"}}))
expect_http("reverify: pruned profiles are a 409", 409,
            lambda: server.polish_reverify(pruned.id), "pruned")
unplant(pruned)


# ---------- a real polish artifact: export + reverify handoff ----------

def make_done_polish():
    job = StubJob(engine="polish", state="done", seed_settings={
        "sizing": "default", "n_iters": 700, "edge_size_mm": 0.1,
        "first_layer_mm": 0.4, "first_layer_mm_requested": 1.0,
        "n_layers": 10, "growth": 1.2})
    (job.case_dir / "polished_profiles.json").write_text(json.dumps({
        "polish_id": job.id, "seed_id": "seedxyz", "iteration": 2,
        "k_exchange": 0.25, "elements": OVR}), encoding="utf-8")
    job.result = {
        "engine": "polish", "improved": True,
        "artifacts": {"profiles_json": "polished_profiles.json",
                      "case_written": True},
        "seed": {"id": "seedxyz", "cl_rans": 2.1}}
    return plant(job)


done_polish = make_done_polish()
out = server.polish_export_dxf(done_polish.id)
dxf_path = Path(out["path"])
check("export-dxf: writes a DXF into the exports dir",
      dxf_path.is_file() and dxf_path.suffix == ".dxf"
      and out["size_bytes"] > 500
      and str(dxf_path.parent) == str(server.EXPORTS_DIR))
txt = dxf_path.read_text(errors="replace")
check("export-dxf: polished layers and closed polylines present",
      "E1_MAIN_POLISHED" in txt and "E2_FLAP1_POLISHED" in txt
      and "LWPOLYLINE" in txt and "GROUND" in txt)

# the reverify handoff runs the REAL Fluent2DJob against the fluent2d
# suite's seam doctrine: fake mcp/dxf/sizing/chain, then wait for done
SIZING_FAKE = {"default": {"edge_size_mm": 0.1, "first_layer_mm": 1.0,
                           "n_layers": 10, "growth": 1.2}}
rec = {}


class FakeFM2:
    def __init__(self):
        self.calls = []
        self.cl = []
        self.cd = []
        self.shutdowns = 0

    def launch(self, **kw):
        self.calls.append("launch")
        self.launch_kw = kw
        return {}

    def read_mesh(self, path):
        return {"zones": {"wall": ["profile", "ground"]}}

    def setup_external_aero(self, **kw):
        self.setup_kw = kw
        return {"failed": [], "applied": ["all"]}

    def solve(self, iterations, initialize=True):
        n0 = len(self.cl)
        for i in range(n0, n0 + iterations):
            self.cl.append(2.4)
            self.cd.append(0.07)
        return {}

    def _report_histories(self):
        return {"lift_coef": list(self.cl), "drag_coef": list(self.cd)}

    def write_case_data(self, stem):
        p = Path(f"{stem}.cas.h5")
        p.write_bytes(b"CASH5")
        p.with_name("case.dat.h5").write_bytes(b"DATH5")
        return {"written": str(p)}

    def export_ascii(self, filename, quantities=None, location=None,
                     surfaces=None):
        import math as _m
        lines = ["cellnumber, x-coordinate, y-coordinate,"
                 " x-velocity, y-velocity, pressure"]
        for i in range(400):
            a = 2 * _m.pi * i / 400
            r = 0.3 + 0.25 * (i % 7) / 7
            lines.append(f"{i+1}, {0.2 + r * _m.cos(a):.6e}, "
                         f"{0.25 + abs(r * _m.sin(a)):.6e}, "
                         f"{15.0:.6e}, {0.5:.6e}, {-100.0:.6e}")
        Path(filename).write_text("\n".join(lines) + "\n",
                                  encoding="utf-8")
        return {"written": str(filename), "rows": 400}

    def shutdown(self):
        self.shutdowns += 1
        return {}


fake = FakeFM2()


def fake_dxf(profiles_m, out_path, **kw):
    rec["dxf_profiles"] = [np.asarray(p) for p in profiles_m]
    Path(out_path).write_bytes(b"DXF")
    return {"dxf_path": str(out_path), "domain_m": (-1, 0, 3, 1),
            "n_profiles": len(profiles_m), "n_points": 1}


def fake_chain(dxf_path, work_dir, **kw):
    rec["chain_kw"] = kw
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    msh = work / "FFF.msh"
    msh.write_bytes(b"(2 2)")
    return {"msh_path": str(msh), "n_cells": 5000,
            "zones": ["fluid", "inlet", "outlet", "ground",
                      "upper_bound", "profile"], "stage_s": {}}


real = (fluent2d_run._mcp, fluent2d_run._dxf, fluent2d_run._sizing,
        fluent2d_run._chain)
fluent2d_run._mcp = lambda: fake
fluent2d_run._dxf = fake_dxf
fluent2d_run._sizing = lambda mode, cfg: dict(SIZING_FAKE["default"])
fluent2d_run._chain = fake_chain
try:
    r = server.polish_reverify(done_polish.id)
    rjob = cfd_run.get(r["job_id"])
    check("reverify: starts a real Fluent2DJob and echoes both ids",
          rjob is not None and r["polish_id"] == done_polish.id)
    for _ in range(600):
        if rjob.state in ("done", "failed", "cancelled"):
            break
        time.sleep(0.05)
    snap = rjob.snapshot()
    check("reverify: the run completes offline through the fakes",
          snap["state"] == "done", f"({snap['state']}: {snap['error']})")
    check("reverify: solves the POLISHED polylines under studio "
          "conventions",
          snap["conventions"] == "studio"
          and snap["profiles_override"] is True
          and len(rec["dxf_profiles"]) == 2
          and np.allclose(rec["dxf_profiles"][0], np.asarray(OVR[0])))
    check("reverify: replays the seed's mesh recipe, requested values "
          "first",
          rjob.settings["edge_size_mm"] == 0.1
          and rjob.settings["first_layer_mm"] == 1.0
          and rjob.settings["n_iters"] == 700,
          str({k: rjob.settings[k] for k in
               ("edge_size_mm", "first_layer_mm", "n_iters")}))
finally:
    (fluent2d_run._mcp, fluent2d_run._dxf, fluent2d_run._sizing,
     fluent2d_run._chain) = real

# with the reverify job now DONE in the registry, a second reverify is
# admitted cleanly; a RUNNING one must 409 — fabricate that state
runner = plant(StubJob(engine="fluent2d", state="running"))
expect_http("reverify: refused while another job runs", 409,
            lambda: server.polish_reverify(done_polish.id),
            "already running")
unplant(runner)

# ---------- shared endpoints treat polish as a Fluent engine ----------

polish_case = plant(StubJob(engine="polish", state="done", result={
    "improved": True, "conventions": "studio",
    "artifacts": {"profiles_json": "polished_profiles.json"}}))
(polish_case.case_dir / "case.cas.h5").write_bytes(b"CAS")
(polish_case.case_dir / "case.dat.h5").write_bytes(b"DAT")
(polish_case.case_dir / "config.json").write_text("{}",
                                                  encoding="utf-8")
out = server.rans_export_fluent(polish_case.id)
readme = (Path(out["path"]) / "README.txt").read_text(encoding="utf-8")
check("ANSYS export: accepts a polish run and states its conventions",
      "adjoint polish" in readme and "provisional" in readme)
unplant(polish_case)


class StopStub(StubJob):
    def stop_graceful(self):
        return False


stopper = plant(StopStub(engine="polish", state="running"))
expect_http("stop: polish has no keep-fields stop (Fluent doctrine)",
            409, lambda: server.rans_stop(stopper.id), "Cancel instead")
unplant(stopper)

check("pins: the polish run channel is admitted",
      "polish" in server._PIN_RUN_KEYS)

print(f"\n{sum(results)}/{len(results)} polish-endpoint checks passed")
sys.exit(0 if all(results) else 1)
