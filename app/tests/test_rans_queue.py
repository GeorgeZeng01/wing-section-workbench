"""RANS re-rank queue: sequential shortlist verification with measured
re-ranking, verdict classes tied to the recorded calibration bands, an
eager rule gate, and a single-queue guard. All solver interaction is
faked through the cfd_run._popen seam — no docker involved.

Run directly:  python app/tests/test_rans_queue.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import cfd_run, rans_queue  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG_D = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 60,
}
CFG_D2 = {**CFG_D, "stack_aoa_deg": 1.0}


def fake_build_case(cfg, case_dir, mesh_size, n_iters=10000):
    case_dir.mkdir(parents=True, exist_ok=True)
    return {"n_cells": 1000, "mesh_size": mesh_size, "n_iters": n_iters}


def fake_availability(refresh=False):
    return {"available": True, "docker": "0.0-test", "image": "test",
            "image_present": True, "detail": ""}


class FlatProc:
    """Fake solver: writes a flat force history (converges by residuals,
    below the cap) and exits cleanly after a few polls."""
    def __init__(self, case_dir, cl, polls=3):
        self.case = case_dir
        self.cl = cl
        self.polls_to_exit = polls
        self.returncode = None
        self._polls = 0

    def poll(self):
        self._polls += 1
        cdir = self.case / "postProcessing" / "forceCoeffs1" / "0"
        cdir.mkdir(parents=True, exist_ok=True)
        lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
        lines += [f"{i + 1} 0.2 0.1 0.1 {self.cl:.4f} 1 1"
                  for i in range(150)]
        (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
        if self._polls >= self.polls_to_exit:
            self.returncode = 0
            return 0
        return None

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _Patched:
    """The cfd_run._popen convention, applied to queue runs."""
    def __init__(self, cl_by_call=(2.5, 3.0), polls=3):
        self.cl_by_call = list(cl_by_call)
        self.polls = polls
        self.calls = 0

    def __enter__(self):
        self.real = (cfd_run._popen, cfd_run.cfd.build_case,
                     cfd_run.availability, cfd_run.POLL_S, cfd_run._docker,
                     rans_queue.POLL_S)
        cfd_run.cfd.build_case = fake_build_case
        cfd_run.availability = fake_availability
        cfd_run.POLL_S = 0.01
        rans_queue.POLL_S = 0.02
        cfd_run._docker = lambda args, timeout: type(
            "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        def popen(cmd, **kw):
            cl = self.cl_by_call[min(self.calls,
                                     len(self.cl_by_call) - 1)]
            self.calls += 1
            # the -v mount argument carries the case dir
            case = Path(next(a for a in cmd if ":/case" in str(a))
                        .split(":/case")[0])
            return FlatProc(case, cl, self.polls)
        cfd_run._popen = popen
        return self

    def __exit__(self, *exc):
        (cfd_run._popen, cfd_run.cfd.build_case, cfd_run.availability,
         cfd_run.POLL_S, cfd_run._docker, rans_queue.POLL_S) = self.real
        return False


def wait_queue(timeout=30.0):
    q = rans_queue.get_current()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if q.state in ("done", "failed", "cancelled"):
            return q.snapshot()
        time.sleep(0.02)
    return q.snapshot()


def main():
    # ---- verdict classification against the recorded bands ----
    check("classify: healthy band",
          rans_queue.classify({"converged": True, "delta_cl_pct": -14.2})
          == "healthy band")
    check("classify: over-claims",
          rans_queue.classify({"converged": True, "delta_cl_pct": -30.0})
          == "over-claims")
    check("classify: conservative",
          rans_queue.classify({"converged": True, "delta_cl_pct": 35.0})
          == "conservative")
    check("classify: no verdict without convergence",
          rans_queue.classify({"converged": False, "delta_cl_pct": -14.0})
          .startswith("no verdict"))

    # ---- eager validation ----
    try:
        rans_queue.QueueJob([{"label": "bad", "config":
                              {**CFG_D, "rule_envelope":
                               {"max_height_mm": 60}}}])
        check("rule-violating item is refused at submission", False)
    except ValueError as e:
        check("rule-violating item is refused at submission",
              "violates rule" in str(e), f"({str(e)[:70]})")
    try:
        rans_queue.QueueJob([{"config": CFG_D}] * 9)
        check("oversized queue is refused", False)
    except ValueError:
        check("oversized queue is refused", True)
    try:
        rans_queue.QueueJob([])
        check("empty queue is refused", False)
    except ValueError:
        check("empty queue is refused", True)

    # ---- end to end: two items, ranked by measured downforce ----
    with _Patched(cl_by_call=(2.5, 3.0)):
        rans_queue.start([{"label": "cand A", "config": CFG_D},
                          {"label": "cand B", "config": CFG_D2}],
                         "medium", 300)
        # single-queue guard while the first is live
        try:
            rans_queue.start([{"config": CFG_D}], "medium", 300)
            check("second queue is refused while one runs", False)
        except RuntimeError:
            check("second queue is refused while one runs", True)
        snap = wait_queue()
    rows = snap["rows"]
    check("queue completes with every row done",
          snap["state"] == "done"
          and all(r["state"] == "done" for r in rows),
          f"(state {snap['state']}: {[r['state'] for r in rows]})")
    check("rows carry measured numbers and verdicts",
          all(r["cl_rans"] is not None and r["rans_downforce_n"] is not None
              and r["verdict"] is not None and r["converged"] for r in rows))
    check("panel claim column is populated",
          all(r["panel_downforce_n"] is not None for r in rows))
    check("re-rank follows MEASURED downforce (cand B solved higher Cl)",
          rows[1]["rank"] == 1 and rows[0]["rank"] == 2,
          f"(ranks {[r['rank'] for r in rows]}, "
          f"cl {[r['cl_rans'] for r in rows]})")
    check("medium mesh rows carry no coarse caution",
          all(r["mesh_caution"] is False for r in rows))

    print(f"\n{sum(results)}/{len(results)} rans-queue checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
