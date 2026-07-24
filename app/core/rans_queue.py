"""Sequential RANS verification of an optimizer shortlist.

The panel model navigates; RANS measures. This queue takes the finalists
(winner, candidates, a Pareto pick) and runs each through the existing
cfd_run pipeline one at a time — the one-job guard is real: Docker/WSL2
is a single-lane resource — then re-ranks by MEASURED downforce and
classifies each delta against the recorded calibration classes.

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
HEALTHY_LO = -20.0
CONSERVATIVE_HI = 5.0


def classify(result: dict) -> str:
    if not result.get("converged"):
        return "no verdict (not converged)"
    d = result.get("delta_cl_pct")
    if d is None:
        return "no panel comparison"
    if d < HEALTHY_LO:
        return "over-claims"
    if d > CONSERVATIVE_HI:
        return "conservative"
    return "healthy band"


class QueueJob:
    def __init__(self, items: list[dict], mesh_size: str = "medium",
                 max_iters: int = 10000):
        self.id = uuid.uuid4().hex[:12]
        if not isinstance(items, list) or not items:
            raise ValueError("nothing to verify — the queue needs at least "
                             "one design")
        if len(items) > MAX_ITEMS:
            raise ValueError(f"at most {MAX_ITEMS} designs per queue")
        if mesh_size not in ("coarse", "medium", "fine"):
            raise ValueError("mesh_size must be coarse, medium or fine")
        self.mesh_size = str(mesh_size)
        self.max_iters = int(max_iters)
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
                "delta_cl_pct": None, "converged": None, "stop_reason": None,
                "mesh_caution": None, "verdict": None, "error": None,
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
                    break
                job = cfd_run.get(jid)
                with self._lock:
                    row["job_id"] = jid
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
                        row.update(
                            cl_rans=r["cl_rans"], cd_rans=r["cd_rans"],
                            rans_downforce_n=r["downforce_n_at_rans_cl"],
                            delta_cl_pct=r["delta_cl_pct"],
                            converged=r["converged"],
                            stop_reason=r["stop_reason"],
                            mesh_caution=r["mesh_caution"])
                        row["verdict"] = classify(r)
                    elif s["state"] == "failed":
                        row["error"] = s.get("error")
            ranked = sorted(
                (r for r in self.rows if r["converged"]
                 and r["rans_downforce_n"] is not None),
                key=lambda r: -r["rans_downforce_n"])
            with self._lock:
                for k, r in enumerate(ranked, 1):
                    r["rank"] = k
                self.state = ("cancelled" if self._cancel.is_set()
                              else "done")
        except Exception as e:  # never leave a zombie "running" queue
            with self._lock:
                self.state = "failed"
                self.error = str(e)
        finally:
            with self._lock:
                self.t_end = time.time()
                self.active = None

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
          max_iters: int = 10000) -> str:
    global _current
    job = QueueJob(items, mesh_size, max_iters)   # validates eagerly
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
