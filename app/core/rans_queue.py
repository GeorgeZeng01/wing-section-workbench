"""Sequential RANS verification of an optimizer shortlist.

The panel model navigates; RANS measures. This queue takes the finalists
(winner, candidates, a Pareto pick) and runs each through the existing
cfd_run pipeline one at a time — the one-job guard is real: Docker/WSL2
is a single-lane resource — then re-ranks the rows under the objective
the shortlist was optimized for (measured drag at the target level for
target mode, measured downforce for max-downforce) and classifies each
delta against the recorded calibration classes. Each row also carries
the per-element wall-shear attachment verdict, and any row measuring an
element separated ranks behind every attached row — high forces from a
separated flow state are not a podium.

The queue never spends solver hours on an illegal design: every item is
rule-envelope-checked eagerly at submission, same gate as the optimizer.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid

from . import analysis, cfd_run, geometry
from .geometry import StackConfig

POLL_S = 1.0
MAX_ITEMS = 8

# verdict classes against the fine-mesh record (docs/calibration): the
# validated baseline measured ~0%, clean winners about -14%, the
# warned/flagged classes -23..-42%, and the mid-height conservative band
# up to +67%. -20% splits the recorded clean cluster from the recorded
# over-claim cluster with margin on both sides.
# FINE-MESH classes only: the same record measured coarse reading a
# validated point 29% LOW at racing height but 33% HIGH at 90 mm, and
# medium scattering -2..-20% across tiny geometry changes ("medium is not
# calibration-grade at racing height"). The bias flips sign with ride
# height, so no shifted/widened band pair can correct it — coarse/medium
# verdicts are demoted to screening wording instead of re-thresholded.
# PROVENANCE: every runs.csv row behind these two numbers was solved in the
# 8-chord-tall domain used before 2026-07-24. The taller domain shipped that
# day measured ~5% less Cl on an aggressive 3-element case (blockage, not
# physics — docs/calibration/LOG.md), and delta_cl_pct scales with Cl, so
# the recorded deltas sit that much high. Applying the measured shift moves
# the recorded clean cluster from -14.2/-14.4 to about -18.5/-18.7: still
# inside the band, but with ~1.5 points of margin rather than 6. These
# thresholds are therefore an EXTRAPOLATION of pre-change evidence, not a
# re-measurement, and the affected verdicts are advisory labels rather than
# gates. Re-anchor them (anchor-h30-fine, stage2_clean-fine,
# stage2_flagged-fine at the new domain) before treating a near-band
# verdict as decisive.
HEALTHY_LO = -20.0
CONSERVATIVE_HI = 5.0

# target-mode ranking: a row within this fraction of the target counts as
# on-level and competes on measured drag. Wider than the optimizer's 3%
# construction tolerance because the measured side carries the fine-mesh
# limit-cycle band (~±7% at racing height, docs/calibration).
RANK_TARGET_TOL = 0.05


def classify(result: dict, mesh_size: str = "fine") -> str:
    if not result.get("converged"):
        return "no verdict (not converged)"
    d = result.get("delta_cl_pct")
    if d is None:
        return "no panel comparison"
    if cfd_run.mesh_below_calibration_grade(mesh_size):
        return "screening only (mesh below calibration grade)"
    if d < HEALTHY_LO:
        return "over-claims"
    if d > CONSERVATIVE_HI:
        return "conservative"
    return "healthy band"


def _shortlist_objective() -> tuple[str, float | None]:
    """The objective of the optimizer run this shortlist came from.

    The API does not carry it, but the queue and the optimizer share a
    process: the most recent run that produced candidates is the run the
    'verify shortlist' button reads its items from. Falls back to
    measured-downforce ranking when no such run exists (e.g. a shortlist
    restored from a saved session after a server restart)."""
    try:
        from . import optimizer
        job = optimizer.last_candidate_job()
        if job is not None and job.objective_mode == "target":
            # the level the run actually held: D* when the target was
            # unreachable and the run said so, else the target itself
            level = (float(job.dstar) if job.dstar is not None
                     else float(job.options.get("target_downforce_n",
                                                200.0)))
            return "target", level
    except Exception:
        pass
    return "max_downforce", None


class QueueJob:
    def __init__(self, items: list[dict], mesh_size: str = "medium",
                 max_iters: int = 10000, objective: str | None = None,
                 target_downforce_n: float | None = None):
        self.id = uuid.uuid4().hex[:12]
        if not isinstance(items, list) or not items:
            raise ValueError("nothing to verify — the queue needs at least "
                             "one design")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} designs per queue")
        if mesh_size not in ("coarse", "medium", "fine"):
            raise ValueError("mesh_size must be coarse, medium or fine")
        if objective not in (None, "target", "max_downforce"):
            raise ValueError("objective must be 'target' or 'max_downforce'")
        self.mesh_size = str(mesh_size)
        self.max_iters = int(max_iters)
        self.objective = objective
        self.target_downforce_n = (float(target_downforce_n)
                                   if target_downforce_n is not None
                                   else None)
        if self.objective is None:
            self.objective, inferred = _shortlist_objective()
            if self.target_downforce_n is None:
                self.target_downforce_n = inferred
        self.rows: list[dict] = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                raise ValueError(f"item {i + 1} must be an object")
            label = str(it.get("label") or f"design {i + 1}")[:60]
            cfg_d = it.get("config")
            if not isinstance(cfg_d, dict):
                raise ValueError(f"{label}: config must be an object")
            cfg = StackConfig.from_dict(cfg_d)   # raises ValueError -> 422
            installed = geometry.install_stack(geometry.build_stack(cfg),
                                               cfg.ride_height_c)
            rules = geometry.envelope_check(installed, cfg)
            if rules is not None and not rules["ok"]:
                v = rules["violations"][0]
                raise ValueError(
                    f"{label}: violates rule '{v['rule']}' by "
                    f"{v['by_mm']:.1f} mm — fix or drop it before queueing")
            try:
                ev = analysis.quick_objective_eval(cfg)
                claim = (round(float(ev["downforce_n"]), 1)
                         if ev.get("feasible") else None)
            except Exception:
                claim = None
            self.rows.append({
                "label": label, "config": copy.deepcopy(cfg_d),
                "state": "queued", "job_id": None,
                "panel_downforce_n": claim,
                "cl_rans": None, "cd_rans": None, "rans_downforce_n": None,
                "drag_rans_n": None, "case_dir": None,
                "delta_cl_pct": None, "converged": None, "stop_reason": None,
                "mesh_caution": None, "verdict": None, "error": None,
                "wall_verdict": None, "worst_reversed": None,
                "rank": None,
            })
        self.state = "pending"   # pending | running | done | failed
                                 # | cancelled
        self.error: str | None = None
        self.active: int | None = None
        self.t_start: float | None = None
        self.t_end: float | None = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---- runner ----

    def run(self) -> None:
        with self._lock:
            self.state = "running"
            self.t_start = time.time()
        try:
            for i, row in enumerate(self.rows):
                if self._cancel.is_set():
                    with self._lock:
                        row["state"] = "skipped"
                    continue
                with self._lock:
                    self.active = i
                    row["state"] = "running"
                try:
                    jid = cfd_run.start(row["config"], self.mesh_size,
                                        self.max_iters)
                except (RuntimeError, ValueError) as e:
                    # a run started by another window mid-queue: stop the
                    # whole queue with a diagnosable cause rather than
                    # failing every remaining row one by one
                    with self._lock:
                        row["state"] = "failed"
                        row["error"] = str(e)
                        self.error = (f"queue stopped at {row['label']}: "
                                      f"{e}")
                        for r2 in self.rows[i + 1:]:
                            if r2["state"] == "queued":
                                r2["state"] = "skipped"
                    break
                # job_id is recorded BEFORE the first registry read: the
                # crash-recovery path below can only cancel an in-flight
                # solve it can name
                with self._lock:
                    row["job_id"] = jid
                job = cfd_run.get(jid)
                if job is None:   # registry eviction between start and get
                    with self._lock:
                        row["state"] = "failed"
                        row["error"] = ("solver job vanished from the "
                                        "registry before it could be polled")
                    continue
                while True:
                    if self._cancel.is_set():
                        job.cancel()
                    s = job.snapshot()
                    if s["state"] in ("done", "failed", "cancelled"):
                        break
                    time.sleep(POLL_S)
                with self._lock:
                    row["state"] = s["state"]
                    if s["state"] == "done" and s.get("result"):
                        r = s["result"]
                        cl, cd = r["cl_rans"], r["cd_rans"]
                        dn = r["downforce_n_at_rans_cl"]
                        # same q*S_ref as the downforce, so D = L*cd/cl
                        drag = (round(abs(dn) * abs(cd) / abs(cl), 2)
                                if None not in (cl, cd, dn)
                                and abs(cl) > 1e-9 else None)
                        row.update(
                            cl_rans=cl, cd_rans=cd,
                            rans_downforce_n=dn, drag_rans_n=drag,
                            delta_cl_pct=r["delta_cl_pct"],
                            converged=r["converged"],
                            stop_reason=r["stop_reason"],
                            mesh_caution=bool(r["mesh_caution"]) or
                            cfd_run.mesh_below_calibration_grade(
                                self.mesh_size),
                            case_dir=r.get("case_dir"))
                        row["verdict"] = classify(r, self.mesh_size)
                        # measured attachment state: a separated row must
                        # not outrank an attached one, whatever its forces
                        # say — the ranking demotes on this
                        row["wall_verdict"] = r.get("wall_verdict")
                        wall = r.get("wall_report") or {}
                        sep = wall.get("separation") or {}
                        fracs = [p.get("reversed_frac") for p in sep.values()
                                 if p.get("reversed_frac") is not None]
                        row["worst_reversed"] = (max(fracs) if fracs
                                                 else None)
                    elif s["state"] == "failed":
                        row["error"] = s.get("error")
            ranked = self._ranked_rows()
            with self._lock:
                for k, r in enumerate(ranked, 1):
                    r["rank"] = k
                # an aborted queue is a failure with a cause, not "done"
                self.state = ("cancelled" if self._cancel.is_set()
                              else "failed" if self.error else "done")
        except Exception as e:  # never leave a zombie "running" queue
            with self._lock:
                self.state = "failed"
                self.error = str(e)
                jid = (self.rows[self.active]["job_id"]
                       if self.active is not None else None)
                for r2 in self.rows:
                    if r2["state"] == "running":
                        r2["state"] = "failed"
                        r2["error"] = r2.get("error") or str(e)
                    elif r2["state"] == "queued":
                        r2["state"] = "skipped"
            # the in-flight solve must not keep burning Docker/WSL hours
            # inside a dead queue — and its one-job guard would refuse
            # every new start until someone found and cancelled it by hand
            if jid:
                try:
                    j = cfd_run.get(jid)
                    if j is not None:
                        j.cancel()
                except Exception:
                    pass
        finally:
            with self._lock:
                self.t_end = time.time()
                self.active = None

    def _ranked_rows(self) -> list[dict]:
        """Converged rows in rank order, consistent with the objective the
        shortlist was optimized under. Target-mode candidates sit on one
        downforce level by construction — the optimizer differentiates
        them by drag at that level — so ranking by raw downforce would
        order them by overshoot: instead rows within RANK_TARGET_TOL of
        the target rank by measured drag, and rows that missed the level
        rank after them by distance to it. Max-downforce (and unknown-
        provenance) shortlists rank by measured downforce.

        Measured separation outranks everything: a row whose wall shear
        grades any element separated (reversed fraction past the in-app
        0.20 line) ranks behind every attached row under either
        objective — its forces are the product of a flow state the
        screening model does not describe, so they cannot buy it a
        podium. Rows without wall diagnostics (older cases) keep tier 0
        rather than being punished for missing data."""
        rows = [r for r in self.rows
                if r["converged"] and r["rans_downforce_n"] is not None]

        def sep_tier(r):
            w = r.get("worst_reversed")
            return 1 if w is not None and w > 0.20 else 0

        t = self.target_downforce_n
        if self.objective == "target" and t:
            scale = max(abs(t), 1.0)

            def key(r):
                miss = abs(r["rans_downforce_n"] - t) / scale
                drag = r.get("drag_rans_n")
                if miss <= RANK_TARGET_TOL and drag is not None:
                    return (sep_tier(r), 0, drag, miss)
                return (sep_tier(r), 1, miss,
                        drag if drag is not None else 1e9)

            return sorted(rows, key=key)
        return sorted(rows, key=lambda r: (sep_tier(r),
                                           -r["rans_downforce_n"]))

    # ---- API surface ----

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = ((self.t_end or time.time())
                       - (self.t_start or time.time()))
            active_jid = (self.rows[self.active]["job_id"]
                          if self.active is not None else None)
            return {
                "id": self.id, "state": self.state, "error": self.error,
                "mesh_size": self.mesh_size, "active": self.active,
                "active_job_id": active_jid,
                "objective": self.objective,
                "target_downforce_n": self.target_downforce_n,
                "elapsed_s": round(elapsed, 1),
                "rows": copy.deepcopy(self.rows),
            }

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            jid = (self.rows[self.active]["job_id"]
                   if self.active is not None else None)
        if jid:
            job = cfd_run.get(jid)
            if job is not None:
                job.cancel()


# ---------- registry (one queue at a time) ----------

_current: QueueJob | None = None
_lock = threading.Lock()


def start(items: list[dict], mesh_size: str = "medium",
          max_iters: int = 10000, objective: str | None = None,
          target_downforce_n: float | None = None) -> str:
    global _current
    job = QueueJob(items, mesh_size, max_iters, objective,
                   target_downforce_n)   # validates eagerly
    with _lock:
        if _current is not None and _current.state in ("pending", "running"):
            raise RuntimeError("a verification queue is already running — "
                               "cancel it or wait for it to finish")
        cur = cfd_run.current()
        if cur["state"] in ("pending", "running"):
            raise RuntimeError("a RANS verification is already running — "
                               "the queue shares the solver; cancel it or "
                               "wait for it to finish")
        _current = job
    threading.Thread(target=job.run, name=f"rans-queue-{job.id}",
                     daemon=True).start()
    return job.id


def get_current() -> QueueJob | None:
    with _lock:
        return _current


def _queue_case_dirs():
    q = get_current()
    if q is None:
        return ()
    with q._lock:
        return [r["case_dir"] for r in q.rows if r.get("case_dir")]


# registered once at import: cfd_run's run-dir housekeeping keeps only the
# newest few finished cases, and a shortlist longer than that would lose
# its early rows' retained case directories while the later rows solve
cfd_run.register_protected_dirs(_queue_case_dirs)
