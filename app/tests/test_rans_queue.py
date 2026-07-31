"""RANS re-rank queue: sequential shortlist verification with
objective-consistent re-ranking, verdict classes tied to the recorded
calibration bands (fine mesh only — coarse/medium demote to screening),
an eager rule gate, a single-queue guard, crash reconciliation, and
prune protection for every row's retained case. All solver interaction
is faked through the cfd_run._popen seam — no docker involved.

Run directly:  python app/tests/test_rans_queue.py
"""
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# isolation BEFORE any app.core import: cfd_run resolves its runs dir from
# WSS_DATA_DIR at call time, and without this the suite writes fabricated
# run dirs into the real app_data/rans and PRUNES the user's retained cases
_SCRATCH = Path(tempfile.mkdtemp(prefix="wss-rans-queue-test-")).resolve()
os.environ["WSS_DATA_DIR"] = str(_SCRATCH)
atexit.register(shutil.rmtree, _SCRATCH, ignore_errors=True)
from app.core import cfd_run, rans_queue  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

_RUNS = cfd_run._runs_dir().resolve()
_REAL = (ROOT / "app_data").resolve()
if _RUNS == _REAL or _REAL in _RUNS.parents:
    print(f"FAIL  isolation: runs dir {_RUNS} resolves inside the repo's "
          f"real app_data — refusing to run")
    sys.exit(1)

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


def downforce_at_cl(cl):
    cfg = StackConfig.from_dict(CFG_D)
    area = cfg.chord_m * (cfg.span_mm / 1000.0)
    return cfg.q_pa * area * cl * cfg.efficiency_3d


def fake_build_case(cfg, case_dir, mesh_size, n_iters=10000, n_ranks=1):
    case_dir.mkdir(parents=True, exist_ok=True)
    return {"n_cells": 1000, "mesh_size": mesh_size, "n_iters": n_iters,
            "n_ranks": n_ranks}


def fake_availability(refresh=False):
    return {"available": True, "docker": "0.0-test", "image": "test",
            "image_present": True, "detail": ""}


class FlatProc:
    """Fake solver: writes a flat force history (converges by residuals,
    below the cap) and exits cleanly after a few polls. wall_frac, when
    given, also writes a wallShearStress field whose wing patches read
    that reversed-flow fraction (20 faces per element)."""
    def __init__(self, case_dir, cl, cd=0.2, polls=3, wall_frac=None):
        self.case = case_dir
        self.cl = cl
        self.cd = cd
        self.wall_frac = wall_frac
        self.polls_to_exit = polls
        self.returncode = None
        self._polls = 0

    def poll(self):
        self._polls += 1
        cdir = self.case / "postProcessing" / "forceCoeffs1" / "0"
        cdir.mkdir(parents=True, exist_ok=True)
        lines = ["# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r)"]
        # long enough that the drift verdict sees a flat history past the
        # FORCE_STOP_SKIP transient exclusion
        lines += [f"{i + 1} {self.cd:.4f} 0.1 0.1 {self.cl:.4f} 1 1"
                  for i in range(1500)]
        (cdir / "coefficient.dat").write_text("\n".join(lines) + "\n")
        if self.wall_frac is not None:
            n = 20
            n_rev = round(self.wall_frac * n)
            vecs = " ".join(["(1 0 0)"] * n_rev + ["(-1 0 0)"] * (n - n_rev))
            blocks = "".join(
                f"    wing_e{k}\n    {{\n        type calculated;\n"
                f"        value nonuniform List<vector> {n}({vecs});\n"
                f"    }}\n" for k in (1, 2))
            tdir = self.case / "500"
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / "wallShearStress").write_text(
                "internalField nonuniform List<vector> 1((0 0 0));\n"
                "boundaryField\n{\n" + blocks + "}\n")
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
    def __init__(self, runs=((2.5, 0.2), (3.0, 0.2)), polls=3):
        self.runs = list(runs)
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
            run = self.runs[min(self.calls, len(self.runs) - 1)]
            cl, cd = run[0], run[1]
            wall = run[2] if len(run) > 2 else None
            self.calls += 1
            # the -v mount argument carries the case dir
            case = Path(next(a for a in cmd if ":/case" in str(a))
                        .split(":/case")[0])
            return FlatProc(case, cl, cd, self.polls, wall_frac=wall)
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


def wait_solver_idle(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cfd_run.current()["state"] not in ("pending", "running"):
            return
        time.sleep(0.02)


def main():
    # the import guard already refuses to run inside the real app_data; this
    # is the positive form — the runs dir must land in THIS suite's scratch
    # dir, not merely somewhere else
    check("runs dir resolves inside the suite's scratch data dir",
          _RUNS == _SCRATCH or _SCRATCH in _RUNS.parents,
          f"({_RUNS} vs {_SCRATCH})")

    # ---- verdict classification against the recorded bands ----
    check("classify: healthy band (fine)",
          rans_queue.classify({"converged": True, "delta_cl_pct": -14.2},
                              "fine") == "healthy band")
    check("classify: over-claims (fine)",
          rans_queue.classify({"converged": True, "delta_cl_pct": -30.0},
                              "fine") == "over-claims")
    check("classify: conservative (fine)",
          rans_queue.classify({"converged": True, "delta_cl_pct": 35.0},
                              "fine") == "conservative")
    check("classify: no verdict without convergence",
          rans_queue.classify({"converged": False, "delta_cl_pct": -14.0},
                              "fine").startswith("no verdict"))
    # the calibrated bands are fine-mesh classes: coarse read a validated
    # point 29% low (and 33% high at 90 mm), medium scattered -2..-20% —
    # a clean -14% design would be branded "over-claims" on either
    check("classify: coarse demotes to screening wording",
          rans_queue.classify({"converged": True, "delta_cl_pct": -43.0},
                              "coarse").startswith("screening only"))
    check("classify: medium demotes to screening wording",
          rans_queue.classify({"converged": True, "delta_cl_pct": -14.2},
                              "medium").startswith("screening only"))

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
    try:
        rans_queue.QueueJob([{"config": CFG_D}], objective="fastest")
        check("unknown ranking objective is refused", False)
    except ValueError:
        check("unknown ranking objective is refused", True)

    # ---- mesh-export exclusive claim: the queue must refuse at start
    # (endpoint maps RuntimeError -> 409), not accept and then fail row 0
    # inside start_pooled ----
    cfd_run.claim_exclusive("a Fluent mesh export")
    try:
        try:
            rans_queue.start([{"config": CFG_D}], "medium", 300)
            check("queue start refused while a mesh-export claim is held",
                  False)
        except RuntimeError as e:
            check("queue start refused while a mesh-export claim is held",
                  str(e).startswith("a Fluent mesh export is running"),
                  f"({e})")
        check("refused queue registers nothing",
              rans_queue.get_current() is None)
    finally:
        cfd_run.release_exclusive()

    # ---- end to end: two items, no source run in-process, so the queue
    # falls back to measured-downforce ranking ----
    with _Patched(runs=((2.5, 0.2), (3.0, 0.2))):
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
    check("rows carry the measured drag force",
          all(r["drag_rans_n"] is not None and r["drag_rans_n"] > 0
              for r in rows))
    check("panel claim column is populated",
          all(r["panel_downforce_n"] is not None for r in rows))
    check("fallback re-rank follows MEASURED downforce (B solved higher)",
          rows[1]["rank"] == 1 and rows[0]["rank"] == 2,
          f"(ranks {[r['rank'] for r in rows]}, "
          f"cl {[r['cl_rans'] for r in rows]})")
    check("medium mesh rows carry the screening caution",
          all(r["mesh_caution"] is True for r in rows))
    check("medium mesh verdicts are screening-grade, not calibrated",
          all(r["verdict"].startswith("screening only") for r in rows))
    check("snapshot names the ranking objective",
          snap["objective"] in ("target", "max_downforce"))

    # ---- target-mode shortlist: rank by measured drag AT the level, not
    # by overshoot. C overshoots the target 20% on the lowest Cd and must
    # NOT outrank the on-level rows; among those, B wins on drag. ----
    target = downforce_at_cl(2.5)
    with _Patched(runs=((2.50, 0.20), (2.52, 0.15), (3.00, 0.10))):
        rans_queue.start([{"label": "A", "config": CFG_D},
                          {"label": "B", "config": CFG_D2},
                          {"label": "C", "config": CFG_D}],
                         "fine", 300, objective="target",
                         target_downforce_n=target)
        snap_t = wait_queue()
    rt = snap_t["rows"]
    check("target queue completes",
          snap_t["state"] == "done"
          and all(r["state"] == "done" for r in rt),
          f"(state {snap_t['state']})")
    check("target re-rank: lowest drag ON the level wins",
          rt[1]["rank"] == 1 and rt[0]["rank"] == 2,
          f"(ranks {[r['rank'] for r in rt]}, "
          f"drag {[r['drag_rans_n'] for r in rt]})")
    check("target re-rank: the overshooter ranks last despite lowest Cd",
          rt[2]["rank"] == 3,
          f"(C dn {rt[2]['rans_downforce_n']} vs target {target:.1f})")
    check("fine mesh rows carry calibrated verdicts and no caution",
          all(r["mesh_caution"] is False
              and not r["verdict"].startswith("screening") for r in rt))

    # ---- measured separation demotes: B posts the higher downforce but
    # its wall shear grades it separated (40% reversed faces); under the
    # fallback measured-downforce ranking it must still rank BEHIND the
    # attached A — forces from a separated flow state are not a podium ----
    wait_solver_idle()
    with _Patched(runs=((2.5, 0.2, 0.05), (3.0, 0.2, 0.40))):
        rans_queue.start([{"label": "att", "config": CFG_D},
                          {"label": "sep", "config": CFG_D2}],
                         "medium", 300)
        snap_w = wait_queue()
    rw = snap_w["rows"]
    check("wall verdict: rows carry the measured attachment state",
          rw[0]["worst_reversed"] == 0.05 and rw[1]["worst_reversed"] == 0.4
          and "separated" in (rw[1]["wall_verdict"] or ""),
          f"(worst {[r['worst_reversed'] for r in rw]})")
    check("wall verdict: separated row is demoted below the attached one",
          rw[0]["rank"] == 1 and rw[1]["rank"] == 2,
          f"(ranks {[r['rank'] for r in rw]}, "
          f"cl {[r['cl_rans'] for r in rw]})")
    check("wall verdict: rows without diagnostics stay un-penalized "
          "(earlier queues ranked on forces alone)",
          all(r["worst_reversed"] is None for r in rows))

    # ---- the demotion line is cfd_run.SEP_PARTIAL_MAX, not a copied
    # literal: re-anchor the constant for one queue and a 15%-reversed row
    # must demote under the 0.10 line even though it clears the shipped
    # 0.20 ----
    wait_solver_idle()
    _sep_saved = cfd_run.SEP_PARTIAL_MAX
    try:
        cfd_run.SEP_PARTIAL_MAX = 0.10
        with _Patched(runs=((2.5, 0.2, 0.05), (3.0, 0.2, 0.15))):
            rans_queue.start([{"label": "att", "config": CFG_D},
                              {"label": "mild", "config": CFG_D2}],
                             "medium", 300)
            snap_c = wait_queue()
    finally:
        cfd_run.SEP_PARTIAL_MAX = _sep_saved
    rc = snap_c["rows"]
    check("queue demotion follows a re-anchored SEP_PARTIAL_MAX",
          rc[0]["rank"] == 1 and rc[1]["rank"] == 2,
          f"(ranks {[r['rank'] for r in rc]}, "
          f"worst {[r['worst_reversed'] for r in rc]})")

    # ---- crash mid-queue: the in-flight solve is cancelled and the
    # remaining rows are reconciled instead of staying 'queued' inside a
    # 'failed' queue ----
    wait_solver_idle()
    real_get = cfd_run.get
    state = {"calls": 0, "cancelled": []}

    def boom_get(jid):
        job = real_get(jid)
        if job is None:
            return None
        state["calls"] += 1
        if state["calls"] >= 2:
            class Boom:
                def snapshot(self):
                    raise RuntimeError("registry evicted mid-poll")

                def cancel(self):
                    state["cancelled"].append(jid)
                    job.cancel()
            return Boom()
        return job

    cfd_run.get = boom_get
    try:
        with _Patched(runs=((2.5, 0.2),) * 3):
            rans_queue.start([{"label": "r1", "config": CFG_D},
                              {"label": "r2", "config": CFG_D2},
                              {"label": "r3", "config": CFG_D}],
                             "medium", 300)
            snap_c = wait_queue()
    finally:
        cfd_run.get = real_get
    rc = snap_c["rows"]
    check("crashed queue fails with the cause named",
          snap_c["state"] == "failed"
          and "registry evicted" in (snap_c["error"] or ""),
          f"({(snap_c['error'] or '')[:60]})")
    check("crash reconciliation: running row failed, later rows skipped",
          [r["state"] for r in rc] == ["done", "failed", "skipped"],
          f"({[r['state'] for r in rc]})")
    check("crash path cancelled the in-flight solver job",
          len(state["cancelled"]) >= 1, f"({state['cancelled']})")
    wait_solver_idle()

    # ---- registry eviction between start and first poll: the row fails
    # alone (no AttributeError crash), earlier work and ranking survive.
    # The vanished row is LAST so the check does not race the solver's
    # one-job guard on a following start. ----
    state2 = {"calls": 0}

    def none_get(jid):
        state2["calls"] += 1
        return None if state2["calls"] == 2 else real_get(jid)

    cfd_run.get = none_get
    try:
        with _Patched(runs=((2.5, 0.2), (2.6, 0.2))):
            rans_queue.start([{"label": "n1", "config": CFG_D},
                              {"label": "n2", "config": CFG_D2}],
                             "medium", 300)
            snap_n = wait_queue()
    finally:
        cfd_run.get = real_get
    rn = snap_n["rows"]
    check("vanished job fails its row only; queue finishes",
          snap_n["state"] == "done"
          and [r["state"] for r in rn] == ["done", "failed"]
          and "vanished" in (rn[1]["error"] or ""),
          f"(state {snap_n['state']}, rows {[r['state'] for r in rn]})")
    check("surviving row is still ranked",
          rn[0]["rank"] == 1 and rn[1]["rank"] is None)
    wait_solver_idle()

    # ---- prune protection: a shortlist longer than KEEP_RUN_DIRS must
    # keep every row's retained case on disk to the end ----
    n_rows = cfd_run.KEEP_RUN_DIRS + 2
    with _Patched(runs=tuple((2.0 + 0.1 * i, 0.2) for i in range(n_rows))):
        rans_queue.start([{"label": f"p{i}", "config": CFG_D}
                          for i in range(n_rows)], "coarse", 300)
        snap_p = wait_queue(timeout=60.0)
    rp = snap_p["rows"]
    check("long queue completes",
          snap_p["state"] == "done"
          and all(r["state"] == "done" for r in rp),
          f"(state {snap_p['state']}: {[r['state'] for r in rp]})")
    check("every row keeps its case_dir reference",
          all(r["case_dir"] for r in rp))
    kept = [r for r in rp if r["case_dir"] and Path(r["case_dir"]).is_dir()]
    check("no row's retained case was pruned mid-queue "
          f"({n_rows} rows > KEEP_RUN_DIRS={cfd_run.KEEP_RUN_DIRS})",
          len(kept) == n_rows,
          f"({len(kept)}/{n_rows} on disk)")
    check("queue rows are registered as prune-protected dirs",
          {Path(r["case_dir"]).resolve() for r in rp}
          <= cfd_run._protected_dirs())

    # ---- opt-in concurrency: budget admission + true overlap ----
    # A pinned budget makes the admission check deterministic on any
    # machine; core_budget() reads the env at call time.
    wait_solver_idle()
    _budget_prev = os.environ.pop("WSS_CORE_BUDGET", None)
    os.environ["WSS_CORE_BUDGET"] = "8"
    try:
        try:
            rans_queue.QueueJob([{"config": CFG_D}], n_ranks=4,
                                max_concurrent=4)
            check("over-budget parallel request is refused", False)
        except ValueError as e:
            check("over-budget parallel request is refused",
                  "core budget" in str(e), f"({str(e)[:70]})")

        conc = {"open": 0, "max_open": 0}

        class TrackedProc(FlatProc):
            """FlatProc that records how many fake solvers are open at
            once — the proof the queue actually overlapped solves."""
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                conc["open"] += 1
                conc["max_open"] = max(conc["max_open"], conc["open"])
                self._closed = False

            def poll(self):
                rc = super().poll()
                if rc is not None and not self._closed:
                    self._closed = True
                    conc["open"] -= 1
                return rc

        with _Patched(runs=((2.5, 0.2), (2.6, 0.2), (3.0, 0.2)),
                      polls=6) as p:
            real_popen = cfd_run._popen

            def tracked_popen(cmd, **kw):
                run = p.runs[min(p.calls, len(p.runs) - 1)]
                p.calls += 1
                case = Path(next(a for a in cmd if ":/case" in str(a))
                            .split(":/case")[0])
                return TrackedProc(case, run[0], run[1], p.polls)
            cfd_run._popen = tracked_popen
            try:
                rans_queue.start(
                    [{"label": "c1", "config": CFG_D},
                     {"label": "c2", "config": CFG_D2},
                     {"label": "c3", "config": CFG_D}],
                    "medium", 300, n_ranks=2, max_concurrent=2)
                snap_cc = wait_queue()
            finally:
                cfd_run._popen = real_popen
        rcc = snap_cc["rows"]
        check("concurrent queue completes with every row done",
              snap_cc["state"] == "done"
              and all(r["state"] == "done" for r in rcc),
              f"(state {snap_cc['state']}: {[r['state'] for r in rcc]})")
        check("solves actually overlapped (2 open at once)",
              conc["max_open"] >= 2, f"(max_open {conc['max_open']})")
        check("snapshot reports the parallel configuration",
              snap_cc["n_ranks"] == 2 and snap_cc["max_concurrent"] == 2)
        check("concurrent rows still rank by measured downforce",
              rcc[2]["rank"] == 1 and rcc[1]["rank"] == 2
              and rcc[0]["rank"] == 3,
              f"(ranks {[r['rank'] for r in rcc]})")
    finally:
        os.environ.pop("WSS_CORE_BUDGET", None)
        if _budget_prev is not None:
            os.environ["WSS_CORE_BUDGET"] = _budget_prev

    print(f"\n{sum(results)}/{len(results)} rans-queue checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
