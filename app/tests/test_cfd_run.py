"""RANS-runner validation — everything except Docker itself.

Covers: coefficient.dat parsing across header layouts, tail statistics,
the k_g inversion (must round-trip the analysis model exactly), n_iters
plumbing into the case files, job state machine with a faked container
(progress, success, failure, cancellation short-circuit), the one-at-a-time
registry guard, the mesh-export exclusive claim, the atexit reaper and
run-directory pruning. No docker binary is invoked.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_cfd_run.py
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# isolate every run directory this suite creates
_TMP = tempfile.mkdtemp(prefix="wss_cfd_run_")
os.environ["WSS_DATA_DIR"] = _TMP

from app.core import cfd, cfd_run, geometry  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

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
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}
CFG = StackConfig.from_dict(CFG_D)

# ---- coefficient.dat parsing ----

V2XXX = """\
# Force coefficients
# dragDir : (1 0 0)
# Time    Cd    Cd(f)    Cd(r)    Cl    Cl(f)    Cl(r)
1 0.5 0.3 0.2 1.0 0.6 0.4
2 0.4 0.25 0.15 1.5 0.9 0.6
3 0.3 0.2 0.1 2.0 1.2 0.8
"""
d = cfd_run.parse_coefficient_dat(V2XXX)
check("v2xxx header: columns resolved by name",
      d["iters"] == [1.0, 2.0, 3.0] and d["cd"] == [0.5, 0.4, 0.3]
      and d["cl"] == [1.0, 1.5, 2.0], f"({d})")

REORDERED = """\
# Time    Cl    Cd
1 2.5 0.11
2 2.6 0.12
"""
d = cfd_run.parse_coefficient_dat(REORDERED)
check("reordered header still lands on the named columns",
      d["cl"] == [2.5, 2.6] and d["cd"] == [0.11, 0.12])

NO_NAMES = """\
# no column names here
1 0.5 0 0 1.25 0 0
"""
d = cfd_run.parse_coefficient_dat(NO_NAMES)
check("nameless header falls back to the v2xxx layout (Cd=2nd, Cl=5th)",
      d["cd"] == [0.5] and d["cl"] == [1.25])

d = cfd_run.parse_coefficient_dat("# header only\n")
check("empty data parses to empty lists", d["iters"] == [])

d = cfd_run.parse_coefficient_dat("# Time Cd Cl\n1 0.5 1.0\nbroken row x\n2 0.6 1.2\n")
check("malformed rows are skipped, not fatal", d["cl"] == [1.0, 1.2])

d = cfd_run.drift([1.0] * 2000)
check("drift on a flat history is ~0", d is not None and d < 1e-12)
ramp = [2.0 + 0.001 * i for i in range(3000)]
d = cfd_run.drift(ramp)
check("drift on the measured climbing case is far above the stop bar",
      d is not None and d > 0.05, f"({d})")
check("drift needs enough rows to judge", cfd_run.drift([1.0] * 100) is None)
cyc = [3.0 + (0.2 if i % 2 else -0.2) for i in range(3000)]
d = cfd_run.drift(cyc)
check("a bounded limit cycle counts as flat", d is not None
      and d < cfd_run.FORCE_STOP_CL_TOL, f"({d})")

m, s, n = cfd_run._tail_stats([1.0] * 600 + [2.0] * 500)
check("tail stats average the last 500 rows", m == 2.0 and s == 0.0 and n == 500)
m, s, n = cfd_run._tail_stats([5.0] * 50 + [1.0] * 50)
check("short runs average only the second half (startup transient excluded)",
      m == 1.0 and s == 0.0 and n == 50, f"(mean {m}, n {n})")
m, s, n = cfd_run._tail_stats([1.0, 3.0])
check("tail stats survive minimal histories", m == 3.0 and n == 1)

# ---- k_g inversion round-trip against the real analysis model ----

from app.core import analysis  # noqa: E402

c_free, c_ground = 2.0, 3.6
for k_true in (0.3, 0.6, 0.85):
    cfg_k = StackConfig.from_dict({**CFG_D, "k_g": k_true})
    c_est, _, _ = analysis.corrected_downforce(c_free, c_ground, cfg_k)
    k_back = cfd_run.suggested_k_g(c_est, c_free, c_ground, cfg_k)
    check(f"k_g inversion round-trips ({k_true})",
          k_back is not None and abs(k_back - k_true) <= 2e-3,
          f"(got {k_back})")

check("k_g inversion: saturated result -> None",
      cfd_run.suggested_k_g(99.0, c_free, c_ground, CFG) is None)
check("k_g inversion: no inviscid gain -> None",
      cfd_run.suggested_k_g(2.0, c_free, c_free, CFG) is None)
check("k_g inversion: below free-air load -> None",
      cfd_run.suggested_k_g(0.5 * CFG.viscous_efficiency * c_free,
                            c_free, c_ground, CFG) is None)

# ---- n_iters plumbing (no meshing needed) ----

cd_text = cfd._controldict(CFG, 0.035, 1234)
check("controlDict carries the requested iteration cap",
      "endTime         1234;" in cd_text)
try:
    cfd.build_case(CFG, Path(_TMP) / "never", "coarse", 5)
    check("build_case rejects out-of-range n_iters", False)
except ValueError:
    check("build_case rejects out-of-range n_iters", True)

# ---- job state machine with a faked container ----

FAKE_SUMMARY = {"mesh_size": "coarse", "n_iters": 300, "n_cells": 12345,
                "n_bl_quads": 100, "boundary_layer": True,
                "first_layer_mm": 0.02, "y_plus_est": 1.0,
                "re_main_chord": 350000, "patches": [], "files": []}


def fake_build_case(cfg, out_dir, mesh_size, n_iters=3000, n_ranks=1):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.sh").write_text("#!/bin/bash\n")
    return {**FAKE_SUMMARY, "n_iters": n_iters, "n_ranks": n_ranks}


def fake_availability(refresh=False):
    return {"available": True, "docker": "0.0-test", "image": "test",
            "image_present": True, "detail": ""}


class FakeProc:
    """Stands in for the docker-run Popen: writes convergence rows on a
    schedule, exits cleanly (or not) after a few polls."""
    def __init__(self, case_dir: Path, rows: int, rc: int):
        self.case = case_dir
        self.rows = rows
        self.rc = rc
        self.returncode = None
        self._polls = 0

    def poll(self):
        self._polls += 1
        cdir = self.case / "postProcessing" / "forceCoeffs1" / "0"
        cdir.mkdir(parents=True, exist_ok=True)
        upto = min(self.rows, self._polls * (self.rows // 2))
        lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
        lines += [f"{i + 1} 0.2 0.1 0.1 {2.0 + 0.001 * i:.4f} 1 1"
                  for i in range(upto)]
        (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
        if self._polls >= 3:
            self.returncode = self.rc
            return self.rc
        return None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def run_fake_job(rc=0, cancel_after=None):
    job = cfd_run.RansJob(CFG_D, "coarse", 300)
    real_popen = cfd_run._popen
    real_build = cfd_run.cfd.build_case
    real_avail = cfd_run.availability
    real_poll_s = cfd_run.POLL_S
    real_docker = cfd_run._docker
    cfd_run.cfd.build_case = fake_build_case
    cfd_run.availability = fake_availability
    cfd_run.POLL_S = 0.01
    cfd_run._docker = lambda args, timeout: type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    cfd_run._popen = lambda cmd, **kw: FakeProc(job.case_dir, 300, rc)
    try:
        if cancel_after is not None:
            threading.Timer(cancel_after, job.cancel).start()
        job.run()
    finally:
        cfd_run._popen = real_popen
        cfd_run.cfd.build_case = real_build
        cfd_run.availability = real_avail
        cfd_run.POLL_S = real_poll_s
        cfd_run._docker = real_docker
    return job


job = run_fake_job(rc=0)
snap = job.snapshot()
check("fake solve completes as done", snap["state"] == "done",
      f"({snap['state']}: {snap['error']})")
r = snap["result"]
# 300 rows of cl = 2.0 + 0.001*i -> tail window 150 rows (half), mean 2.2245
check("result carries RANS tail means",
      r and abs(r["cl_rans"] - 2.2245) < 1e-3 and r["cd_rans"] == 0.2
      and r["tail_rows"] == 150,
      f"({r and (r['cl_rans'], r['cd_rans'], r['tail_rows'])})")
check("result compares against the panel model",
      r and r["panel"] and r["panel"]["c_est"] > 0
      and r["delta_cl_pct"] is not None,
      f"(panel {r and r['panel']}, err {r and r.get('panel_error')})")
check("result reports downforce at the RANS Cl",
      r and abs(r["downforce_n_at_rans_cl"]
                - (CFG.q_pa * CFG.chord_m * 1.4 * r["cl_rans"] * 0.9)) < 2.0)
check("history is downsampled and monotone",
      2 <= len(snap["history"]) <= 320
      and snap["history"][-1]["iter"] == 300)
check("progress hits 1.0 at the end", snap["progress"] == 1.0)

job_f = run_fake_job(rc=7)
check("container failure -> failed with exit code in the error",
      job_f.state == "failed" and "7" in (job_f.error or ""),
      f"({job_f.error})")

job_c = run_fake_job(rc=0, cancel_after=0.0)
check("cancellation lands in a terminal cancelled state",
      job_c.state == "cancelled", f"({job_c.state})")

# ---- convergence verdict + graceful stop ----

def finalize_with(cl_rows, n_iters, force_stop=False, mesh="coarse"):
    job = cfd_run.RansJob(CFG_D, mesh, n_iters)
    cdir = job.case_dir / "postProcessing" / "forceCoeffs1" / "0"
    cdir.mkdir(parents=True, exist_ok=True)
    lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
    lines += [f"{i + 1} 0.2 0.1 0.1 {v:.5f} 1 1"
              for i, v in enumerate(cl_rows)]
    (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
    job.t_start = time.time()
    job._force_stop = force_stop
    job._finalize()
    return job.result


ramp_r = finalize_with([2.0 + 0.001 * i for i in range(3000)], 3000)
check("cap-limited trending run is reported NOT converged",
      ramp_r and ramp_r["converged"] is False
      and ramp_r["stop_reason"] == "iteration cap reached"
      and ramp_r["cl_drift"] > 0.05,
      f"({ramp_r and (ramp_r['converged'], ramp_r['stop_reason'])})")
check("no k_g calibration is offered from an unconverged run",
      ramp_r and ramp_r["suggested_k_g"] is None)

# a force stop that demonstrably took effect: the solver quit below the cap
flat_r = finalize_with([2.0] * 500 + [3.2] * 2000, 3000, force_stop=True)
check("force-stopped run (below cap) is reported converged",
      flat_r and flat_r["converged"] is True
      and flat_r["stop_reason"] == "force history converged")

# a writeNow that the solver silently never noticed: the run went all the
# way to the cap with a drifting tail — the verdict must judge the history,
# not trust the request
ghost_r = finalize_with([2.0 + 0.001 * i for i in range(3000)], 3000,
                        force_stop=True)
check("ineffective writeNow at the cap is NOT trusted as converged",
      ghost_r and ghost_r["converged"] is False
      and ghost_r["stop_reason"] == "iteration cap reached"
      and ghost_r["suggested_k_g"] is None,
      f"({ghost_r and (ghost_r['converged'], ghost_r['stop_reason'])})")

early_r = finalize_with([2.5] * 1500, 3000)
check("residual-stopped run (below cap) is reported converged",
      early_r and early_r["converged"] is True
      and early_r["stop_reason"] == "residuals converged"
      and early_r["residual_stop"] is True)

# the calibration campaign measured coarse-mesh results 22-35% below
# fine-mesh truth at racing/mid ride heights — a coarse result must carry
# the caution flag the UI renders next to the k_g suggestion, and a
# finer mesh must not
check("coarse-mesh result carries mesh_caution",
      early_r and early_r["mesh_caution"] is True)
fine_r = finalize_with([2.5] * 1500, 3000, mesh="fine")
check("fine-mesh result carries no mesh_caution",
      fine_r and fine_r["mesh_caution"] is False)

# a run too short for drift() to judge must say SO, not assert a trend it
# never measured (drift needs FORCE_STOP_SKIP + 3*100 rows)
short_r = finalize_with([2.5] * 700, 3000)
check("short flat run is not accused of trending",
      short_r and short_r["cl_drift"] is None
      and "too few" in short_r["stop_reason"]
      and "trending" not in short_r["stop_reason"]
      and short_r["suggested_k_g"] is None,
      f"({short_r and short_r['stop_reason']})")
check("short run's trend note reports the shortfall, not a direction",
      short_r and short_r["cl_trend_note"] is not None
      and "too few" in short_r["cl_trend_note"]
      and "rising" not in short_r["cl_trend_note"]
      and "falling" not in short_r["cl_trend_note"])
short_cap_r = finalize_with([2.5] * 700, 700)
check("short cap-limited run also declines to claim a trend",
      short_cap_r and "too few" in short_cap_r["stop_reason"]
      and short_cap_r["converged"] is False)

# the mesh-grade policy is ONE definition shared with the queue's grading
check("only the fine mesh is calibration grade",
      cfd_run.mesh_below_calibration_grade("coarse")
      and cfd_run.mesh_below_calibration_grade("medium")
      and not cfd_run.mesh_below_calibration_grade("fine"))
med_r = finalize_with([2.5] * 1500, 3000, mesh="medium")
check("medium single run carries the mesh caution the queue also applies",
      med_r and med_r["mesh_caution"] is True)

# an early rc==0 exit with a DRIFTING history must not mint a verdict (or
# a k_g): the stop mechanism explains the exit, only the history certifies
early_drift_r = finalize_with([2.0 + 0.001 * i for i in range(1500)], 3000)
check("early exit with a trending history is NOT converged",
      early_drift_r and early_drift_r["converged"] is False
      and "still-trending" in early_drift_r["stop_reason"]
      and early_drift_r["suggested_k_g"] is None,
      f"({early_drift_r and early_drift_r['stop_reason']})")

# a force stop that fired on a false plateau: below the cap, still trending
fp_r = finalize_with([2.0 + 0.001 * i for i in range(2500)], 3000,
                     force_stop=True)
check("force stop on a false plateau is NOT converged",
      fp_r and fp_r["converged"] is False
      and "false plateau" in fp_r["stop_reason"]
      and fp_r["suggested_k_g"] is None)

# drifting results are provisional and say WHICH WAY they are moving
check("drifting result is provisional with a rising-trend note",
      ramp_r and ramp_r["delta_cl_provisional"] is True
      and ramp_r["cl_trend_note"] is not None
      and "rising" in ramp_r["cl_trend_note"]
      and "lower bound" in ramp_r["cl_trend_note"])
check("converged result is not provisional and carries no trend note",
      flat_r and flat_r["delta_cl_provisional"] is False
      and flat_r["cl_trend_note"] is None)
check("legacy case without wall diagnostics reports no wall_report",
      ramp_r and ramp_r["wall_report"] is None
      and ramp_r["wall_verdict"] is None)

# the tail std is measured around the tail's own trend line: a pure ramp
# must read ~zero scatter (its raw std IS the drift, not precision)
_m, _s, _n = cfd_run._tail_stats([1.0 + 0.001 * i for i in range(600)])
check("tail std is detrended (ramp reads ~0 scatter)", _s < 1e-9,
      f"(std {_s})")

# three-window drift: a flattening overshoot zeroes the two-window gap
# near its peak but not both gaps — the detector must see through it
_peak = [8.8 - 0.3 * ((3000 - i) / 3000) ** 2 for i in range(2900)]
_d3 = cfd_run.drift(_peak)
check("drift sees through a flattening overshoot",
      _d3 is not None and _d3 > cfd_run.FORCE_STOP_CL_TOL, f"({_d3})")

# a user stop that landed AFTER the solver already exited on its own must
# not relabel that exit — attribution requires rows past the flip
noeff = cfd_run.RansJob(CFG_D, "coarse", 3000)
_cd = noeff.case_dir / "postProcessing" / "forceCoeffs1" / "0"
_cd.mkdir(parents=True, exist_ok=True)
_rows = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
_rows += [f"{i + 1} 0.2 0.1 0.1 2.50000 1 1" for i in range(1500)]
(_cd / "coefficient.dat").write_text("\n".join(_rows) + "\n")
noeff.t_start = time.time()
noeff._force_stop = True
noeff._stopped_by_user = True
noeff._rows_at_user_stop = 1500     # flip landed after the last row
noeff._finalize()
check("user stop after solver exit does not claim the verdict",
      noeff.result and noeff.result["user_stopped"] is False
      and noeff.result["stop_reason"] == "residuals converged",
      f"({noeff.result and noeff.result['stop_reason']})")

# housekeeping must never prune a directory a registered protector claims
_runs = cfd_run._runs_dir()
_runs.mkdir(parents=True, exist_ok=True)
_dirs = []
for i in range(cfd_run.KEEP_RUN_DIRS + 2):
    d = _runs / f"prunetest{i}"
    d.mkdir(exist_ok=True)
    os.utime(d, (time.time() - 1000 + i, time.time() - 1000 + i))
    _dirs.append(d)
cfd_run.register_protected_dirs(lambda: [_dirs[0]])
cfd_run._prune_run_dirs(set())
check("protected case dir survives pruning while an unprotected peer dies",
      _dirs[0].is_dir() and not _dirs[1].is_dir(),
      f"(kept {[d.name for d in _dirs if d.is_dir()]})")

job_g = cfd_run.RansJob(CFG_D, "coarse", 3000)
sysd = job_g.case_dir / "system"
sysd.mkdir(parents=True, exist_ok=True)
(sysd / "controlDict").write_text(
    "stopAt          endTime;\nendTime         3000;\n", encoding="utf-8")
ok = job_g._request_graceful_stop()
txt = (sysd / "controlDict").read_text()
check("graceful stop flips controlDict to writeNow",
      ok and "stopAt          writeNow;" in txt and "endTime;" not in txt)
check("graceful stop is refused when already flipped",
      job_g._request_graceful_stop() is False)
job_g._restore_controldict()
txt = (sysd / "controlDict").read_text()
check("finalize restores the retained case to a runnable state",
      "stopAt          endTime;" in txt and "writeNow" not in txt)

job_f = cfd_run.RansJob(CFG_D, "coarse", 5000)
job_f.iteration = cfd_run.FORCE_STOP_MIN_ITERS + 100
job_f._cl_drift, job_f._cd_drift = 0.001, 0.005
check("force-converged detector fires on flat histories",
      job_f._force_converged())

# ---- user stop-and-keep-fields ----

job_ns = cfd_run.RansJob(CFG_D, "coarse", 3000)
check("stop-and-keep is refused before the solver runs",
      job_ns.stop_graceful() is False)

# verdict: a hand-stopped run below the cap is a preview — never
# "converged", never a k_g source, but a normal done state (flow view)
job_u = cfd_run.RansJob(CFG_D, "coarse", 3000)
cdir_u = job_u.case_dir / "postProcessing" / "forceCoeffs1" / "0"
cdir_u.mkdir(parents=True, exist_ok=True)
_lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
_lines += [f"{i + 1} 0.2 0.1 0.1 {2.0 + 0.001 * i:.5f} 1 1"
           for i in range(1500)]
(cdir_u / "coefficient.dat").write_text("\n".join(_lines) + "\n")
job_u.t_start = time.time()
job_u._force_stop = True
job_u._stopped_by_user = True
job_u._finalize()
ur = job_u.result
check("user-stopped run: done with an honest verdict",
      job_u.state == "done" and ur
      and ur["stop_reason"] == "stopped by user (fields written)"
      and ur["converged"] is False and ur["user_stopped"] is True,
      f"({ur and (ur['stop_reason'], ur['converged'])})")
check("user-stopped run offers no k_g suggestion",
      ur and ur["suggested_k_g"] is None)


# end to end: the request lands mid-solve, flips controlDict, and the
# finalize keeps the case runnable
class SlowFakeProc(FakeProc):
    def poll(self):
        self._polls += 1
        cdir = self.case / "postProcessing" / "forceCoeffs1" / "0"
        cdir.mkdir(parents=True, exist_ok=True)
        upto = min(self.rows, self._polls * 40)
        lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
        lines += [f"{i + 1} 0.2 0.1 0.1 {2.0 + 0.001 * i:.4f} 1 1"
                  for i in range(upto)]
        (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
        if self._polls >= 10:
            self.returncode = self.rc
            return self.rc
        return None


def run_user_stop_job():
    job = cfd_run.RansJob(CFG_D, "coarse", 1000)   # cap far above the rows

    def build_with_controldict(cfg, case_dir, mesh, iters, n_ranks=1):
        out = fake_build_case(cfg, case_dir, mesh, iters, n_ranks)
        sysd = case_dir / "system"
        sysd.mkdir(parents=True, exist_ok=True)
        (sysd / "controlDict").write_text(
            "stopAt          endTime;\nendTime         1000;\n",
            encoding="utf-8")
        return out

    def stopper():
        for _ in range(2000):
            if job.stop_graceful():
                return
            time.sleep(0.002)

    real = (cfd_run._popen, cfd_run.cfd.build_case, cfd_run.availability,
            cfd_run.POLL_S, cfd_run._docker)
    cfd_run.cfd.build_case = build_with_controldict
    cfd_run.availability = fake_availability
    cfd_run.POLL_S = 0.01
    cfd_run._docker = lambda args, timeout: type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    cfd_run._popen = lambda cmd, **kw: SlowFakeProc(job.case_dir, 400, 0)
    t = threading.Thread(target=stopper, daemon=True)
    try:
        t.start()
        job.run()
    finally:
        (cfd_run._popen, cfd_run.cfd.build_case, cfd_run.availability,
         cfd_run.POLL_S, cfd_run._docker) = real
    t.join(timeout=5)
    return job


job_us = run_user_stop_job()
us = job_us.result
check("live user stop lands: done, user-stopped verdict",
      job_us.state == "done" and us and us["user_stopped"] is True
      and us["stop_reason"] == "stopped by user (fields written)",
      f"(state {job_us.state}, {us and us['stop_reason']})")
check("live user stop restored the retained controlDict",
      "stopAt          endTime;" in
      (job_us.case_dir / "system" / "controlDict").read_text())
job_f._cl_drift = 0.02
check("force-converged detector holds while Cl still trends",
      not job_f._force_converged())
job_f._cl_drift, job_f.iteration = 0.001, 1500
check("force-converged detector never judges the startup transient",
      not job_f._force_converged())

# the decay-then-recover startup (the usual potentialFoam-initialized shape
# on a separated case) has a mean-crossing where naive half-windows cancel;
# the transient-excluded decision drift must stay above the stop bar there
DECAY = ([3.5 - (i / 400) for i in range(400)]
         + [2.5 + 0.0007 * i for i in range(1700)])
d_naive = cfd_run.drift(DECAY)
d_decision = cfd_run.drift(DECAY[cfd_run.FORCE_STOP_SKIP:])
check("transient exclusion defeats the decay-then-recover false positive",
      d_naive is not None and d_decision is not None
      and d_decision > cfd_run.FORCE_STOP_CL_TOL,
      f"(naive {d_naive:.4f}, decision {d_decision:.4f})")

# ---- flow-field post-processing ----

from app.core import foam_post  # noqa: E402

SCALAR_FIELD = """\
FoamFile
{
    class       volScalarField;
    object      p;
}
internalField   nonuniform List<scalar>
3
(
1.5
-2.5
0.75
)
;
"""
v = foam_post.parse_internal_field(SCALAR_FIELD)
check("scalar field parses", list(v) == [1.5, -2.5, 0.75])

VECTOR_FIELD = SCALAR_FIELD.replace("List<scalar>", "List<vector>").replace(
    "1.5\n-2.5\n0.75", "(1 2 3)\n(4 5 6)\n(7 8 9)").replace(
    "volScalarField", "volVectorField")
v = foam_post.parse_internal_field(VECTOR_FIELD)
check("vector field parses", v.shape == (3, 3) and v[1][2] == 6.0)

try:
    foam_post.parse_internal_field("internalField   uniform (15 0 0);\n")
    check("uniform field is rejected", False)
except foam_post.PostError:
    check("uniform field is rejected", True)

flow_case = Path(_TMP) / "flow_case"
for tname in ("500", "3000"):
    td = flow_case / tname
    td.mkdir(parents=True, exist_ok=True)
    import numpy as _np
    xs, ys = _np.meshgrid(_np.linspace(-0.2, 0.8, 40),
                          _np.linspace(0.005, 0.35, 24))
    pts = _np.column_stack([xs.ravel(), ys.ravel(),
                            _np.full(xs.size, 0.0175)])
    def _vec_file(obj, rows):
        body = "\n".join(f"({r[0]:.6g} {r[1]:.6g} {r[2]:.6g})" for r in rows)
        return (f"FoamFile{{class volVectorField; object {obj};}}\n"
                f"internalField   nonuniform List<vector> \n{len(rows)}\n"
                f"(\n{body}\n)\n;\n")
    (td / "C").write_text(_vec_file("C", pts))
    u = _np.column_stack([_np.full(pts.shape[0], 15.0),
                          _np.zeros(pts.shape[0]), _np.zeros(pts.shape[0])])
    (td / "U").write_text(_vec_file("U", u))
    (td / "p").write_text(
        "FoamFile{class volScalarField; object p;}\n"
        "internalField   nonuniform List<scalar> \n"
        f"{pts.shape[0]}\n(\n" +
        "\n".join("0.0" for _ in range(pts.shape[0])) + "\n)\n;\n")
check("latest_time_dir picks 3000 over 500 (numeric, not lexicographic)",
      foam_post.latest_time_dir(flow_case).name == "3000")
png = foam_post.flow_png(flow_case, CFG, "umag")
check("flow field renders a PNG", png[:8] == b"\x89PNG\r\n\x1a\n"
      and len(png) > 20_000, f"({len(png)} bytes)")

# the contour-dialog knobs: theme chrome, colormap, range clamp,
# streamline toggle — distinct settings must produce distinct renders
png_light = foam_post.flow_png(flow_case, CFG, "umag", theme="light")
check("light theme renders and differs from dark",
      png_light[:8] == b"\x89PNG\r\n\x1a\n" and png_light != png)
png_set = foam_post.flow_png(flow_case, CFG, "umag", cmap="turbo",
                             vmax=10.0, streamlines=False)
check("cmap/clamp/streamline settings render and differ",
      png_set[:8] == b"\x89PNG\r\n\x1a\n" and png_set != png
      and png_set != png_light)
for bad in (dict(theme="sepia"), dict(cmap="jet"),
            dict(vmin=5.0, vmax=1.0)):
    try:
        foam_post.flow_png(flow_case, CFG, "umag", **bad)
        check(f"bad view setting rejected {bad}", False)
    except foam_post.PostError:
        check(f"bad view setting rejected {bad}", True)

# the animated view's field payload: same extraction path as the PNG,
# JSON-clean (masked cells are null, never NaN), row-major with metadata
ffj = foam_post.flow_field_json(flow_case, CFG, nx=160)
check("flow field json: grid shape, metadata, element outlines",
      ffj["nx"] == 160 and ffj["ny"] >= 40
      and len(ffj["u"]) == len(ffj["v"]) == len(ffj["umag"])
      == ffj["nx"] * ffj["ny"]
      and ffj["iter"] == "3000" and ffj["speed_ms"] == CFG.speed_ms
      and len(ffj["polys"]) == len(CFG.elements)
      and ffj["x1"] > ffj["x0"] and ffj["y1"] > 0,
      f"(nx {ffj['nx']}, ny {ffj['ny']}, polys {len(ffj['polys'])})")
import json as _json  # noqa: E402
try:
    _json.dumps(ffj, allow_nan=False)
    check("flow field json carries no NaN (masked cells are null)", True)
except ValueError as e:
    check("flow field json carries no NaN (masked cells are null)", False,
          f"({e})")
check("flow field json masks the element interiors",
      any(x is None for x in ffj["umag"]))
check("flow field json clamps the grid size",
      foam_post.flow_field_json(flow_case, CFG, nx=50)["nx"] == 120)
# "domain" extent spans exactly the solved cell-centre cloud (the whole
# solve box in production; the synthetic fixture's cloud here) rather
# than the section crop's fixed chord-margins
ffd = foam_post.flow_field_json(flow_case, CFG, nx=160, extent="domain")
check("domain extent follows the solved cloud's bounds",
      abs(ffd["x0"] - (-0.2)) < 0.02 and abs(ffd["x1"] - 0.8) < 0.02
      and ffd["x0"] != ffj["x0"] and ffd["x1"] != ffj["x1"],
      f"(domain x {ffd['x0']}..{ffd['x1']} vs section "
      f"{ffj['x0']}..{ffj['x1']})")
try:
    foam_post.flow_field_json(flow_case, CFG, extent="galaxy")
    check("bad extent rejected", False)
except foam_post.PostError:
    check("bad extent rejected", True)
# windowed (level-of-detail) extraction: an arbitrary box re-gridded at
# full resolution, clamped to the solved cloud
ffw = foam_post.flow_field_json(flow_case, CFG, nx=160,
                                window=(0.1, 0.05, 0.5, 0.25))
check("windowed field grids exactly the requested box",
      abs(ffw["x0"] - 0.1) < 1e-6 and abs(ffw["x1"] - 0.5) < 1e-6
      and abs(ffw["y0"] - 0.05) < 1e-6 and abs(ffw["y1"] - 0.25) < 1e-6
      and ffw["nx"] == 160,
      f"({ffw['x0']}..{ffw['x1']}, {ffw['y0']}..{ffw['y1']})")
check("window clamps to the solved cloud",
      foam_post.flow_field_json(flow_case, CFG, nx=160,
                                window=(-5, 0.05, 5, 0.25))["x0"]
      >= -0.21)
try:
    foam_post.flow_field_json(flow_case, CFG, window=(9, 9, 10, 10))
    check("window outside the domain rejected", False)
except foam_post.PostError:
    check("window outside the domain rejected", True)

# ---- registry: one at a time, rediscovery ----

blocker = cfd_run.RansJob(CFG_D, "coarse", 300)
blocker.state = "running"
cfd_run._jobs[blocker.id] = blocker
try:
    cfd_run.start(CFG_D)
    check("second concurrent start is refused", False)
except RuntimeError:
    check("second concurrent start is refused", True)
cur = cfd_run.current()
check("current() rediscovers the active job",
      cur["job_id"] == blocker.id and cur["state"] == "running", f"({cur})")
blocker.state = "cancelled"
cfd_run._jobs.pop(blocker.id, None)

check("current() with no jobs reports none",
      cfd_run.current()["job_id"] is None)

check("unknown job id -> None", cfd_run.get("nope") is None)

# ---- exclusive claim: the mesh export owns the slot, both directions ----

from app.core import fluent_run as _flu_mod  # noqa: E402

cfd_run.claim_exclusive("a Fluent mesh export")
try:
    check("exclusive_claim() reports the held tag",
          cfd_run.exclusive_claim() == "a Fluent mesh export")
    try:
        cfd_run.start(CFG_D)
        check("start refused while the mesh-export claim is held", False)
    except RuntimeError as e:
        # the tag carries its own article — the message must open with
        # it verbatim, never a doubled "a a ..." / "a an ..."
        check("start refused while the mesh-export claim is held",
              str(e).startswith("a Fluent mesh export is running"), f"({e})")
    try:
        cfd_run.start_pooled(CFG_D, "coarse", 300)
        check("pooled start refused while the claim is held", False)
    except RuntimeError:
        check("pooled start refused while the claim is held", True)
    # the Fluent engine registers through the same start(), so the claim
    # must refuse it too. Stub the docker CLI and the pyfluent session
    # factory: the refusal fires before either, but a regression here
    # must not reach the real CLI or launch a licensed Fluent
    _claim_docker = cfd_run._docker
    _claim_mcp = _flu_mod._mcp
    cfd_run._docker = lambda args, timeout: type(
        "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    _flu_mod._mcp = lambda: (_ for _ in ()).throw(
        RuntimeError("must not launch"))
    try:
        _leak = cfd_run.start(CFG_D, engine="fluent")
        cfd_run._jobs.pop(_leak, None)
        check("Fluent-engine start refused while the claim is held", False)
    except RuntimeError:
        check("Fluent-engine start refused while the claim is held", True)
    finally:
        cfd_run._docker = _claim_docker
        _flu_mod._mcp = _claim_mcp
    try:
        cfd_run.claim_exclusive("a Fluent mesh export")
        check("second concurrent claim refused", False)
    except RuntimeError as e:
        check("second concurrent claim refused",
              str(e).startswith("a Fluent mesh export is already running"),
              f"({e})")
finally:
    cfd_run.release_exclusive()

_cl_blocker = cfd_run.RansJob(CFG_D, "coarse", 300)
_cl_blocker.state = "running"
cfd_run._jobs[_cl_blocker.id] = _cl_blocker
try:
    cfd_run.claim_exclusive("a Fluent mesh export")
    cfd_run.release_exclusive()
    check("claim refused while a job runs", False)
except RuntimeError:
    check("claim refused while a job runs", True)
finally:
    _cl_blocker.state = "cancelled"
    cfd_run._jobs.pop(_cl_blocker.id, None)

check("released claim leaves the slot free", cfd_run._exclusive_claim is None)
check("exclusive_claim() reports a free slot as None",
      cfd_run.exclusive_claim() is None)

# ---- fluent2d dispatch: constructor plumbing, guard, claim ----
# the module is stubbed through the package attribute (same seam the
# lazy `from . import fluent2d_run` resolves), so the suite stays
# offline whether or not the real module exists yet

import types as _types  # noqa: E402

import app.core as _core_pkg  # noqa: E402

_f2d_calls = []


class _StubFluent2DJob:
    """Registry-shaped stand-in for the ANSYS 2D job — records its
    constructor args and finishes at once."""

    def __init__(self, config, mesh_size, n_iters, n_ranks, conventions,
                 settings=None):
        _f2d_calls.append((mesh_size, n_iters, n_ranks, conventions,
                           settings))
        self.id = f"f2dstub{len(_f2d_calls)}"
        self.state = "pending"
        self.case_dir = cfd_run._runs_dir() / self.id
        self._container = ""
        self.t_start = None
        self.t_end = None

    def run(self):
        self.state = "done"

    def cancel(self):
        pass


_f2d_saved_attr = getattr(_core_pkg, "fluent2d_run", None)
_core_pkg.fluent2d_run = _types.SimpleNamespace(
    Fluent2DJob=_StubFluent2DJob)
_f2d_docker = cfd_run._docker
cfd_run._docker = lambda args, timeout: type(
    "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
try:
    # the constructor's positional order is a contract: the sizing mode,
    # the iteration cap, the ranks, the conventions, then the settings
    # object the job resolves against its recipe
    _f2d_settings = {"sizing": "default", "edge_size_mm": 0.4,
                     "first_layer_mm": None, "n_iters": 500,
                     "n_ranks": 2, "conventions": "studio"}
    _jid2d = cfd_run.start(CFG_D, "default", 500, 2, engine="fluent2d",
                           conventions="studio",
                           settings=_f2d_settings)
    _j2d = cfd_run.get(_jid2d)
    _deadline = time.time() + 5
    while time.time() < _deadline and _j2d.state != "done":
        time.sleep(0.01)
    check("fluent2d start dispatches sizing, conventions and settings "
          "verbatim",
          _f2d_calls == [("default", 500, 2, "studio", _f2d_settings)]
          and _j2d.state == "done", f"({_f2d_calls}, {_j2d.state})")
    cfd_run._jobs.pop(_jid2d, None)

    _blk2d = cfd_run.RansJob(CFG_D, "coarse", 300)
    _blk2d.state = "running"
    cfd_run._jobs[_blk2d.id] = _blk2d
    try:
        _leak = cfd_run.start(CFG_D, "default", 500, engine="fluent2d")
        cfd_run._jobs.pop(_leak, None)
        check("one-at-a-time guard spans the fluent2d engine", False)
    except RuntimeError:
        check("one-at-a-time guard spans the fluent2d engine", True)
    finally:
        _blk2d.state = "cancelled"
        cfd_run._jobs.pop(_blk2d.id, None)

    # the 2D mesh export's exclusive claim must refuse fluent2d starts —
    # the export holds the same ANSYS seats the job would need
    cfd_run.claim_exclusive("an ANSYS 2D mesh export")
    try:
        _leak = cfd_run.start(CFG_D, "default", 500, engine="fluent2d")
        cfd_run._jobs.pop(_leak, None)
        check("fluent2d start refused while the 2D export claim is held",
              False)
    except RuntimeError as e:
        # "an ..." tags must scan too — no baked-in article upstream
        check("fluent2d start refused while the 2D export claim is held",
              str(e).startswith("an ANSYS 2D mesh export is running"),
              f"({e})")
    finally:
        cfd_run.release_exclusive()

    try:
        cfd_run.start(CFG_D, engine="starccm")
        check("unknown engine rejected with the full enum", False)
    except ValueError as e:
        check("unknown engine rejected with the full enum",
              "fluent2d" in str(e), f"({e})")
finally:
    cfd_run._docker = _f2d_docker
    if _f2d_saved_attr is None:
        del _core_pkg.fluent2d_run
    else:
        _core_pkg.fluent2d_run = _f2d_saved_attr

import inspect as _inspect  # noqa: E402

check("start_pooled accepts the conventions parameter",
      "conventions" in _inspect.signature(cfd_run.start_pooled).parameters)
_start_sig = _inspect.signature(cfd_run.start).parameters
check("start's 2D defaults are the neutral vocabulary, settings optional",
      _start_sig["conventions"].default == "default"
      and _inspect.signature(cfd_run.start_pooled)
      .parameters["conventions"].default == "default"
      and _start_sig["settings"].default is None
      and list(_start_sig)[-1] == "settings",
      f"({_start_sig['conventions'].default}, {list(_start_sig)[-1]})")

# ---- atexit reaper: registry entries beyond RansJob ----

class _ReapStub:
    """Minimal registry-shaped job — like FluentJob, no _kill_container."""

    def __init__(self, jid, boom=False):
        self.id = jid
        self.state = "running"
        self.boom = boom
        self.cancelled = False
        self._container = ""
        self.case_dir = cfd_run._runs_dir() / jid

    def cancel(self):
        self.cancelled = True
        if self.boom:
            raise RuntimeError("wedged")


class _ReapRans(_ReapStub):
    def __init__(self, jid):
        super().__init__(jid)
        self.killed = False

    def _kill_container(self, timeout=60):
        self.killed = True


_r_bad = _ReapStub("reap_bad", boom=True)   # first: must not abort the loop
_r_flu = _ReapStub("reap_flu")
_r_of = _ReapRans("reap_of")
for _j in (_r_bad, _r_flu, _r_of):
    cfd_run._jobs[_j.id] = _j
try:
    cfd_run._reap_at_exit()
    check("reaper survives a container-less job and a raising cancel, "
          "and still reaps the rest",
          _r_flu.cancelled and _r_of.cancelled and _r_of.killed)
except Exception as e:
    check("reaper survives a container-less job and a raising cancel, "
          "and still reaps the rest", False, f"({e!r})")
finally:
    for _j in (_r_bad, _r_flu, _r_of):
        cfd_run._jobs.pop(_j.id, None)

try:
    cfd_run.RansJob(CFG_D, "ultra", 300)
    check("bad mesh size is rejected at construction", False)
except ValueError:
    check("bad mesh size is rejected at construction", True)

try:
    cfd_run.RansJob(CFG_D, "coarse", 5)
    check("bad max_iters is rejected at construction", False)
except ValueError:
    check("bad max_iters is rejected at construction", True)

# ---- opt-in parallelism: case generation, plumbing, knife-edge band ----

_sh_par = cfd._run_sh(["wing_e1", "wing_e2"], 8)
check("parallel run.sh decomposes, solves -parallel, reconstructs, cleans",
      all(s in _sh_par for s in (
          "decomposePar -force", "--allow-run-as-root", "-np 8",
          "simpleFoam -parallel", "reconstructPar -latestTime",
          "rm -rf processor*"))
      and _sh_par.index("potentialFoam") < _sh_par.index("decomposePar -force")
      < _sh_par.index("simpleFoam -parallel")
      < _sh_par.index("reconstructPar -latestTime")
      < _sh_par.index("writeCellCentres"))
_sh_ser = cfd._run_sh(["wing_e1", "wing_e2"])
check("serial run.sh is the n_ranks=1 script and carries no MPI",
      _sh_ser == cfd._run_sh(["wing_e1", "wing_e2"], 1)
      and "mpirun" not in _sh_ser and "decomposePar" not in _sh_ser)
_dp = cfd._decomposepardict(6)
check("decomposeParDict carries the rank count and scotch",
      "numberOfSubdomains 6;" in _dp and "method          scotch;" in _dp)

try:
    cfd_run.RansJob(CFG_D, "coarse", 300, 40)
    check("RansJob rejects out-of-range n_ranks", False)
except ValueError:
    check("RansJob rejects out-of-range n_ranks", True)
_jr = cfd_run.RansJob(CFG_D, "coarse", 300, 8)
check("RansJob snapshot reports its rank count",
      _jr.snapshot()["n_ranks"] == 8)

_env_prev = os.environ.pop("WSS_CORE_BUDGET", None)
os.environ["WSS_CORE_BUDGET"] = "7"
check("core budget honors the env override", cfd_run.core_budget() == 7)
os.environ.pop("WSS_CORE_BUDGET", None)
check("core budget defaults to half the logical cores",
      cfd_run.core_budget() == max(1, (os.cpu_count() or 8) // 2))
if _env_prev is not None:
    os.environ["WSS_CORE_BUDGET"] = _env_prev


def finalize_with_wall(fracs):
    """Converged flat run whose wing patches read the given reversed
    fractions (20 faces per element -> 0.05 granularity)."""
    job = cfd_run.RansJob(CFG_D, "coarse", 3000)
    cdir = job.case_dir / "postProcessing" / "forceCoeffs1" / "0"
    cdir.mkdir(parents=True, exist_ok=True)
    lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
    lines += [f"{i + 1} 0.2 0.1 0.1 2.50000 1 1" for i in range(1500)]
    (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
    n = 20
    blocks = ""
    for k, f in enumerate(fracs, 1):
        n_rev = round(f * n)
        vecs = " ".join(["(1 0 0)"] * n_rev + ["(-1 0 0)"] * (n - n_rev))
        blocks += (f"    wing_e{k}\n    {{\n        type calculated;\n"
                   f"        value nonuniform List<vector> {n}({vecs});\n"
                   f"    }}\n")
    tdir = job.case_dir / "500"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "wallShearStress").write_text(
        "internalField nonuniform List<vector> 1((0 0 0));\n"
        "boundaryField\n{\n" + blocks + "}\n")
    job.t_start = time.time()
    job._finalize()
    return job.result


# e1 sits ON the partial/separated line: whichever side a given rank
# count computes, the verdict must say knife-edge rather than pretend
# the classification is stable
knife_r = finalize_with_wall([0.20, 0.40])
check("fraction on the 0.20 line is reported knife-edge",
      knife_r and knife_r["sep_knife_edge"] is True
      and "knife-edge" in (knife_r["wall_verdict"] or "")
      and "separated" in knife_r["wall_verdict"],
      f"({knife_r and knife_r['wall_verdict']})")
clean_r = finalize_with_wall([0.05, 0.30])
check("fractions clear of both lines carry no knife-edge marker",
      clean_r and clean_r["sep_knife_edge"] is False
      and "knife-edge" not in (clean_r["wall_verdict"] or ""))
check("legacy case without wall diagnostics has no knife-edge claim",
      ramp_r and ramp_r["sep_knife_edge"] is None)
check("result records the rank count that produced it",
      knife_r and knife_r["n_ranks"] == 1)

# ---- pooled starts: the queue's concurrency seam ----

_gate = threading.Event()


class GatedProc:
    """Fake solver container that holds until the test releases it."""
    def __init__(self, case_dir: Path):
        self.case = case_dir
        self.returncode = None

    def poll(self):
        cdir = self.case / "postProcessing" / "forceCoeffs1" / "0"
        cdir.mkdir(parents=True, exist_ok=True)
        lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
        lines += [f"{i + 1} 0.2 0.1 0.1 2.5000 1 1" for i in range(300)]
        (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
        if _gate.is_set():
            self.returncode = 0
            return 0
        return None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


_pooled_real = (cfd_run._popen, cfd_run.cfd.build_case,
                cfd_run.availability, cfd_run.POLL_S, cfd_run._docker)
cfd_run.cfd.build_case = fake_build_case
cfd_run.availability = fake_availability
cfd_run.POLL_S = 0.01
cfd_run._docker = lambda args, timeout: type(
    "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
cfd_run._popen = lambda cmd, **kw: GatedProc(
    Path(str(next(a for a in cmd if ":/case" in str(a))).split(":/case")[0]))
try:
    _gate.clear()
    jid_a = cfd_run.start_pooled(CFG_D, "coarse", 300, 2)
    jid_b = cfd_run.start_pooled(CFG_D, "coarse", 300, 2)
    ja, jb = cfd_run.get(jid_a), cfd_run.get(jid_b)
    deadline = time.time() + 10
    while time.time() < deadline and not (ja._solving and jb._solving):
        time.sleep(0.005)
    check("two pooled jobs solve concurrently",
          ja.state == "running" and jb.state == "running"
          and ja._solving and jb._solving,
          f"({ja.state}/{jb.state})")
    try:
        cfd_run.start(CFG_D)
        check("interactive start is refused while pooled jobs run", False)
    except RuntimeError:
        check("interactive start is refused while pooled jobs run", True)
    _gate.set()
    deadline = time.time() + 10
    while time.time() < deadline and not (
            ja.state in ("done", "failed")
            and jb.state in ("done", "failed")):
        time.sleep(0.005)
    check("pooled jobs finish independently",
          ja.state == "done" and jb.state == "done",
          f"({ja.state}: {ja.error} / {jb.state}: {jb.error})")
finally:
    _gate.set()
    (cfd_run._popen, cfd_run.cfd.build_case, cfd_run.availability,
     cfd_run.POLL_S, cfd_run._docker) = _pooled_real

# ---- orphan sweep: gmsh->Fluent bridge containers ----

# a pid that is genuinely dead: a child that has already exited
import subprocess  # noqa: E402
_swp = subprocess.Popen([sys.executable, "-c", "pass"])
_swp.wait()
_dead_pid = _swp.pid

_sw_rm: list = []
_sw_real = cfd_run._docker


def _sw_docker(args, timeout):
    if args[0] == "ps" and "name=wss-rans-" in args:
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    if args[0] == "ps" and "name=wss-fluent-mesh-" in args:
        out = (f"wss-fluent-mesh-{_dead_pid}-123\n"
               f"wss-fluent-mesh-{os.getpid()}-456\n")
        return type("R", (), {"returncode": 0, "stdout": out,
                              "stderr": ""})()
    if args[:2] == ["rm", "-f"]:
        _sw_rm.append(args[2])
    return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()


cfd_run._docker = _sw_docker
try:
    cfd_run._sweep_orphan_containers(set())
finally:
    cfd_run._docker = _sw_real
check("bridge sweep removes the dead-owner container, spares the live one",
      _sw_rm == [f"wss-fluent-mesh-{_dead_pid}-123"], f"({_sw_rm})")

# ---- run-directory pruning ----

runs = cfd_run._runs_dir()
runs.mkdir(parents=True, exist_ok=True)
now = time.time()
made = []
for i in range(7):
    p = runs / f"prune_test_{i}"
    p.mkdir(exist_ok=True)
    os.utime(p, (now - 1000 + i, now - 1000 + i))
    made.append(p)
keep_active = {made[0].resolve()}   # oldest, but active — must survive
cfd_run._prune_run_dirs(keep_active)
left = {p.name for p in runs.iterdir() if p.name.startswith("prune_test_")}
check("pruning keeps the newest dirs plus every active one",
      made[0].name in left and len(left) <= cfd_run.KEEP_RUN_DIRS + 1,
      f"({sorted(left)})")

print(f"\n{sum(results)}/{len(results)} cfd-run checks passed")
sys.exit(0 if all(results) else 1)
