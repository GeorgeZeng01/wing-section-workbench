"""RANS-runner validation — everything except Docker itself.

Covers: coefficient.dat parsing across header layouts, tail statistics,
the k_g inversion (must round-trip the analysis model exactly), n_iters
plumbing into the case files, job state machine with a faked container
(progress, success, failure, cancellation short-circuit), the one-at-a-time
registry guard and run-directory pruning. No docker binary is invoked.

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


def fake_build_case(cfg, out_dir, mesh_size, n_iters=3000):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.sh").write_text("#!/bin/bash\n")
    return {**FAKE_SUMMARY, "n_iters": n_iters}


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
