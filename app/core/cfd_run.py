"""In-app OpenFOAM RANS verification runs (Docker) — opt-in truth checks.

The Export tab has always produced a ready-to-run case for WSL; this module
is the "verify this design now" path behind the RANS verify tab: it builds
the same case into a working directory under app_data/rans/, runs it in a
Docker container, streams convergence out of postProcessing/ while the
solver iterates, and finishes with the tail-mean force coefficients next to
the panel-model estimate — including the k_g the estimate would need to
reproduce the RANS sectional load, closing the calibration loop the model
notes ask for.

Image choice: the case files are written in the ESI (v2xxx) dialect and
run.sh sources /usr/lib/openfoam/openfoam*/etc/bashrc — exactly the layout
the official opencfd/openfoam-* images ship, so the SAME run.sh runs
verbatim in the container and in WSL. Foundation images (openfoam/openfoamN)
moved to foamRun/momentumTransport and cannot run these cases. Override
with the WSS_OPENFOAM_IMAGE environment variable if a different ESI-layout
image is preferred.

One job at a time: simpleFoam is CPU-bound on every core it gets; queueing
a second solve beside it helps nobody. start() raises RuntimeError while a
job is active — the API turns that into a 409.

Comparison semantics: the case's forceCoeffs is referenced to the main
chord with liftDir (0 -1 0), so its Cl is the downforce-positive sectional
coefficient in ground effect — directly comparable to the studio's C_est.
Its Cd is profile (pressure + friction) drag of the 2D section; induced
drag is a 3D effect the slab case cannot see, so it is compared against
CD_profile_stack, never the total.
"""

from __future__ import annotations

import atexit
import copy
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import analysis, cfd
from .geometry import StackConfig

DOCKER_IMAGE = "opencfd/openfoam-run:2406"
TAIL_MEAN_ROWS = 500          # matches run.sh's "mean of last 500 iterations"
POLL_S = 1.0
MAX_WALL_S = 4 * 3600         # hard stop — a fine mesh at full iterations
                              # fits comfortably; anything longer is hung
KEEP_RUN_DIRS = 4             # finished run directories retained on disk

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _image() -> str:
    return os.environ.get("WSS_OPENFOAM_IMAGE", DOCKER_IMAGE)


def _runs_dir() -> Path:
    # same override the server's session store honors: tests must not write
    # into the real app_data
    base = Path(os.environ.get(
        "WSS_DATA_DIR", Path(__file__).resolve().parents[2] / "app_data"))
    return base / "rans"


def _docker(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # env passed explicitly: os.environ is Python's pristine snapshot, and
    # the real process environment may have been clobbered by gmsh (see
    # cfd._real_env_path) — belt and braces on top of cfd's restore
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, creationflags=_CREATE_NO_WINDOW,
                          env={**os.environ})


# indirection so tests can fake the solver container without touching the
# global subprocess module (other libraries in-process use it too)
_popen = subprocess.Popen


# ---------- availability ----------

_avail_lock = threading.Lock()
_avail_cache: dict = {"t": 0.0, "value": None}
AVAIL_TTL_S = 15.0


def availability(refresh: bool = False) -> dict:
    """Can this machine run a verification right now?

    {"available", "docker" (server version or None), "image",
     "image_present", "detail"} — cached briefly; the UI polls it."""
    with _avail_lock:
        now = time.time()
        if (not refresh and _avail_cache["value"] is not None
                and now - _avail_cache["t"] < AVAIL_TTL_S):
            return _avail_cache["value"]
        out = {"available": False, "docker": None, "image": _image(),
               "image_present": False, "detail": ""}
        try:
            r = _docker(["version", "--format", "{{.Server.Version}}"], 10)
            if r.returncode == 0 and r.stdout.strip():
                out["docker"] = r.stdout.strip().splitlines()[0]
                out["available"] = True
            else:
                out["detail"] = ((r.stderr or r.stdout or "").strip()
                                 .splitlines() or ["docker not responding"])[0]
        except FileNotFoundError:
            out["detail"] = "docker CLI not found"
        except subprocess.TimeoutExpired:
            out["detail"] = "docker did not respond within 10 s"
        except OSError as e:
            out["detail"] = str(e)
        if out["available"]:
            try:
                r = _docker(["image", "inspect", "--format", "ok",
                             _image()], 10)
                out["image_present"] = r.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                pass
        _avail_cache.update(t=now, value=out)
        return out


# ---------- coefficient.dat ----------

def parse_coefficient_dat(text: str) -> dict:
    """{"iters", "cd", "cl"} (parallel lists) from a forceCoeffs
    coefficient.dat. Column indices come from the last header line before
    the data (names vary a little across versions); the fallback matches the
    v2xxx layout run.sh's awk uses (Time=1st, Cd=2nd, Cl=5th)."""
    header: list[str] | None = None
    in_data = False
    i_cd, i_cl = 1, 4
    iters: list[float] = []
    cd: list[float] = []
    cl: list[float] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if not in_data:
                header = s.lstrip("#").split()
            continue
        if not in_data:
            in_data = True
            if header and "Cd" in header and "Cl" in header:
                i_cd, i_cl = header.index("Cd"), header.index("Cl")
        parts = s.split()
        try:
            t, d, l = float(parts[0]), float(parts[i_cd]), float(parts[i_cl])
        except (ValueError, IndexError):
            continue
        iters.append(t)
        cd.append(d)
        cl.append(l)
    return {"iters": iters, "cd": cd, "cl": cl}


def _tail_stats(vals: list[float], n: int = TAIL_MEAN_ROWS
                ) -> tuple[float, float, int]:
    """(mean, population std, rows used) over the tail of the history —
    steady RANS on a loaded high-lift section often ends in a bounded limit
    cycle, and the tail mean/std are the number to trust and its amplitude.
    The window never covers more than the second half of the run, so a
    short run's startup transient cannot bias the mean (a flat 500-row
    window would average mostly transient on a 300-iteration run)."""
    window = min(n, max(1, len(vals) // 2)) if len(vals) < 2 * n else n
    tail = vals[-window:]
    m = sum(tail) / len(tail)
    var = sum((v - m) ** 2 for v in tail) / len(tail)
    return m, math.sqrt(var), len(tail)


def suggested_k_g(cl_rans: float, c_free: float, c_ground: float,
                  cfg: StackConfig) -> float | None:
    """The pinned k_g that makes the studio's C_est reproduce the RANS Cl.

    Inverts C_est = eta*(C_f + cap*tanh(k_g*g_inv/cap)*choke) for k_g.
    None when the RANS result sits outside the model's reachable range
    (saturated cap, no inviscid gain, or k_g outside (0, 1])."""
    g_inv = c_ground - c_free
    cap = cfg.gain_cap_ratio * max(abs(c_free), 1e-9)
    choke = math.tanh(cfg.ride_height_c / cfg.choke_h_c)
    if abs(g_inv) < 1e-9 or choke < 1e-9 or cfg.viscous_efficiency < 1e-9:
        return None
    g_needed = cl_rans / cfg.viscous_efficiency - c_free
    u = g_needed / (cap * choke)
    if not (0.0 < u < 0.999):
        return None
    k = cap * math.atanh(u) / g_inv
    return round(k, 3) if 0.0 < k <= 1.0 else None


# ---------- the job ----------

class RansJob:
    def __init__(self, config: dict, mesh_size: str = "coarse",
                 n_iters: int = cfd.N_ITERS):
        if mesh_size not in cfd.MESH_PRESETS:
            raise ValueError(
                f"mesh_size must be one of {sorted(cfd.MESH_PRESETS)}")
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(config)
        self.cfg = StackConfig.from_dict(config)   # validates eagerly
        self.mesh_size = mesh_size
        self.n_iters = int(n_iters)
        if not (100 <= self.n_iters <= 20_000):
            raise ValueError("max_iters must be between 100 and 20000")
        self.state = "pending"   # pending | running | done | failed | cancelled
        self.phase: str | None = None
        self.error: str | None = None
        self.iteration = 0
        self.latest: dict | None = None    # {"iter", "cl", "cd"}
        self.history: list[dict] = []      # downsampled full-run convergence
        self.mesh: dict | None = None      # build_case summary
        self.result: dict | None = None
        self.case_dir = _runs_dir() / self.id
        self.t_start: float | None = None
        self.t_end: float | None = None
        self._container = f"wss-rans-{self.id}"
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ---- progress plumbing ----

    def _latest_coefficient_file(self) -> Path | None:
        root = self.case_dir / "postProcessing" / "forceCoeffs1"
        if not root.is_dir():
            return None
        files = sorted(root.glob("*/coefficient.dat"),
                       key=lambda p: p.parent.name)
        return files[-1] if files else None

    def _poll_progress(self) -> None:
        coef = self._latest_coefficient_file()
        if coef is None:
            # pre-solve stages: surface run.sh's own "== stage" markers
            log = self.case_dir / "log.docker"
            try:
                stages = [ln[3:].strip() for ln
                          in log.read_text(errors="replace").splitlines()
                          if ln.startswith("== ")]
                if stages:
                    names = {"gmshToFoam": "converting mesh",
                             "checkMesh": "checking mesh",
                             "potentialFoam": "initializing (potential flow)",
                             "simpleFoam": "solving"}
                    with self._lock:
                        self.phase = names.get(stages[-1], stages[-1])
            except OSError:
                pass
            return
        try:
            data = parse_coefficient_dat(coef.read_text(errors="replace"))
        except OSError:
            return
        if not data["iters"]:
            return
        # rebuild a bounded-size history over the WHOLE run each poll, so
        # the convergence chart always shows the full picture
        n = len(data["iters"])
        step = max(1, n // 300)
        idx = list(range(0, n, step))
        if idx[-1] != n - 1:
            idx.append(n - 1)
        hist = [{"iter": data["iters"][i], "cl": round(data["cl"][i], 4),
                 "cd": round(data["cd"][i], 5)} for i in idx]
        with self._lock:
            self.iteration = int(data["iters"][-1])
            self.latest = hist[-1]
            self.history = hist
            self.phase = "solving"

    # ---- runner ----

    def _kill_container(self, timeout: float = 60) -> None:
        """Force-remove the solver container, retrying once — a cancel can
        race the daemon registering the name."""
        for _ in range(2):
            try:
                r = _docker(["rm", "-f", self._container], timeout)
                if r.returncode == 0:
                    return
            except Exception:
                pass
            time.sleep(1.0)

    def run(self) -> None:
        with self._lock:
            self.state = "running"
            self.t_start = time.time()
        try:
            self._run_inner()
        except Exception as e:
            with self._lock:
                self.state = "failed"
                self.error = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            with self._lock:
                self.t_end = time.time()
            # belt and braces: never leave a container running past the job
            if self.state in ("failed", "cancelled"):
                self._kill_container(30)

    def _run_inner(self) -> None:
        with self._lock:
            self.phase = "meshing"
        self.case_dir.mkdir(parents=True, exist_ok=True)
        try:
            mesh = cfd.build_case(self.cfg, self.case_dir,
                                  self.mesh_size, self.n_iters)
            with self._lock:
                self.mesh = mesh
        except (ValueError, cfd.MeshError) as e:
            shutil.rmtree(self.case_dir, ignore_errors=True)
            with self._lock:
                self.state = "failed"
                self.error = f"case generation failed: {e}"
            return
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return

        avail = availability(refresh=True)
        if not avail["available"]:
            with self._lock:
                self.state = "failed"
                self.error = (f"Docker is unavailable ({avail['detail']}) — "
                              f"the generated case is still complete at "
                              f"{self.case_dir}; run it in WSL with run.sh")
            return
        if not avail["image_present"]:
            with self._lock:
                self.phase = "pulling OpenFOAM image (one-time download)"
            # Popen + poll instead of a blocking run(): a pull can take many
            # minutes and cancel must stay responsive throughout
            pull_log = self.case_dir / "log.pull"
            with open(pull_log, "wb") as fh:
                pull = _popen(["docker", "pull", _image()], stdout=fh,
                              stderr=subprocess.STDOUT,
                              creationflags=_CREATE_NO_WINDOW,
                              env={**os.environ})
                t_pull = time.time()
                while pull.poll() is None:
                    if self._cancel.is_set() or time.time() - t_pull > 1800:
                        pull.kill()
                        pull.wait(timeout=30)
                        break
                    time.sleep(POLL_S)
            if self._cancel.is_set():
                with self._lock:
                    self.state = "cancelled"
                return
            if pull.returncode != 0:
                tail = ""
                try:
                    tail = pull_log.read_text(errors="replace").strip()[-400:]
                except OSError:
                    pass
                with self._lock:
                    self.state = "failed"
                    self.error = f"docker pull failed: {tail}"
                return
            availability(refresh=True)
        if self._cancel.is_set():
            with self._lock:
                self.state = "cancelled"
            return

        with self._lock:
            self.phase = "starting container"
        log_path = self.case_dir / "log.docker"
        cmd = ["docker", "run", "--rm", "--name", self._container,
               "-v", f"{self.case_dir}:/case", "-w", "/case",
               "--entrypoint", "/bin/bash", _image(), "run.sh"]
        with open(log_path, "wb") as log_fh:
            proc = _popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                          creationflags=_CREATE_NO_WINDOW,
                          env={**os.environ})
            try:
                while True:
                    rc = proc.poll()
                    if self._cancel.is_set():
                        self._kill_container()
                        try:
                            proc.wait(timeout=60)
                        except Exception:
                            proc.kill()
                        with self._lock:
                            self.state = "cancelled"
                        return
                    self._poll_progress()
                    if rc is not None:
                        break
                    if time.time() - self.t_start > MAX_WALL_S:
                        self._kill_container()
                        try:
                            proc.wait(timeout=60)
                        except Exception:
                            proc.kill()
                        with self._lock:
                            self.state = "failed"
                            self.error = (f"run exceeded the "
                                          f"{MAX_WALL_S // 3600} h wall-clock "
                                          f"limit and was stopped")
                        return
                    time.sleep(POLL_S)
            finally:
                if proc.poll() is None:
                    proc.kill()

        if proc.returncode != 0:
            tail = ""
            try:
                lines = log_path.read_text(errors="replace").splitlines()
                tail = "\n".join(ln for ln in lines if ln.strip())[-800:]
            except OSError:
                pass
            with self._lock:
                self.state = "failed"
                self.error = (f"solver container exited with code "
                              f"{proc.returncode}:\n{tail}\n"
                              f"(full logs in {self.case_dir})")
            return

        self._finalize()

    def _finalize(self) -> None:
        self._poll_progress()   # pick up the final rows (sets phase itself)
        with self._lock:
            self.phase = "extracting results"
        coef = self._latest_coefficient_file()
        if coef is None:
            with self._lock:
                self.state = "failed"
                self.error = ("the solver wrote no force coefficients — see "
                              f"log.simpleFoam in {self.case_dir}")
            return
        data = parse_coefficient_dat(coef.read_text(errors="replace"))
        if not data["iters"]:
            with self._lock:
                self.state = "failed"
                self.error = "coefficient.dat contains no data rows"
            return
        cl_mean, cl_std, n_tail = _tail_stats(data["cl"])
        cd_mean, cd_std, _ = _tail_stats(data["cd"])
        n_run = int(data["iters"][-1])

        # panel-model numbers for the same config, full pipeline
        panel = None
        panel_error = None
        suggestion = None
        try:
            r = analysis.analyze(self.cfg, include_geometry=False)
            co, fo = r["coefficients"], r["forces"]
            panel = {
                "c_est": co["C_downforce_estimated"],
                "c_free": co["C_downforce_inviscid_free"],
                "c_ground": co["C_downforce_inviscid_ground"],
                "cd_profile": co["CD_profile_stack"],
                "k_g_used": co["k_ground_realization"],
                "downforce_n": fo["downforce_n"],
            }
            suggestion = suggested_k_g(cl_mean, panel["c_free"],
                                       panel["c_ground"], self.cfg)
        except Exception as e:
            # the RANS numbers stand on their own; say why the comparison
            # column is missing instead of leaving it blank
            panel_error = f"{type(e).__name__}: {e}"

        q = self.cfg.q_pa
        area = self.cfg.chord_m * (self.cfg.span_mm / 1000.0)
        with self._lock:
            self.result = {
                "cl_rans": round(cl_mean, 4),
                "cl_rans_std": round(cl_std, 4),
                "cd_rans": round(cd_mean, 5),
                "cd_rans_std": round(cd_std, 5),
                "tail_rows": n_tail,
                "n_iters_run": n_run,
                "residual_stop": n_run < self.n_iters,
                "downforce_n_at_rans_cl": round(
                    q * area * cl_mean * self.cfg.efficiency_3d, 1),
                "panel": panel,
                "panel_error": panel_error,
                "delta_cl_pct": round((cl_mean / panel["c_est"] - 1) * 100, 1)
                    if panel and abs(panel["c_est"]) > 1e-9 else None,
                "suggested_k_g": suggestion,
                "case_dir": str(self.case_dir),
            }
            self.phase = None
            self.state = "done"

    # ---- API surface ----

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = ((self.t_end or time.time())
                       - (self.t_start or time.time()))
            if self.state in ("done", "failed", "cancelled"):
                progress = 1.0
            elif self.iteration:
                progress = min(0.97, 0.05 + 0.92 * self.iteration
                               / max(self.n_iters, 1))
            elif self.mesh is not None:
                progress = 0.05
            else:
                progress = 0.01
            return {
                "id": self.id, "state": self.state, "phase": self.phase,
                "error": self.error,
                "progress": round(progress, 3),
                "iteration": self.iteration, "n_iters": self.n_iters,
                "mesh_size": self.mesh_size,
                "elapsed_s": round(elapsed, 1),
                "latest": self.latest,
                "history": list(self.history),
                "mesh": self.mesh,
                "result": self.result if self.state == "done" else None,
                "case_dir": str(self.case_dir),
            }

    def cancel(self) -> None:
        self._cancel.set()


# ---------- registry ----------

_jobs: dict[str, RansJob] = {}
_jobs_lock = threading.Lock()


def _sweep_orphan_containers(active_names: set[str]) -> None:
    """Force-remove wss-rans-* containers this process does not own.

    The one-job-at-a-time guard and the run-dir pruning are process-local;
    a server restart mid-solve would otherwise leave a full-CPU container
    running unsupervised (--rm only cleans up after it finishes on its own)
    and later prune the case directory out from under it."""
    try:
        r = _docker(["ps", "--filter", "name=wss-rans-",
                     "--format", "{{.Names}}"], 15)
        if r.returncode != 0:
            return
        for name in r.stdout.split():
            if name.startswith("wss-rans-") and name not in active_names:
                _docker(["rm", "-f", name], 30)
    except Exception:
        pass   # sweeping is best-effort; docker may simply be down


def _reap_at_exit() -> None:
    """Clean interpreter shutdown must not leave the solver running: the
    worker is a daemon thread and dies without its finally block."""
    with _jobs_lock:
        active = [j for j in _jobs.values()
                  if j.state in ("pending", "running")]
    for j in active:
        j.cancel()
        j._kill_container(15)


atexit.register(_reap_at_exit)


def _prune_run_dirs(active: set[Path]) -> None:
    """Keep the newest KEEP_RUN_DIRS finished run directories; a solve can
    write hundreds of MB of fields, and verification runs are working
    artifacts, not user exports."""
    root = _runs_dir()
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_RUN_DIRS:]:
        if d.resolve() not in active:
            shutil.rmtree(d, ignore_errors=True)


def start(config: dict, mesh_size: str = "coarse",
          n_iters: int = cfd.N_ITERS) -> str:
    with _jobs_lock:
        for j in _jobs.values():
            if j.state in ("pending", "running"):
                raise RuntimeError(
                    "a RANS verification is already running — cancel it or "
                    "wait for it to finish")
        job = RansJob(config, mesh_size, n_iters)
        _sweep_orphan_containers({j._container for j in _jobs.values()}
                                 | {job._container})
        _prune_run_dirs({j.case_dir.resolve() for j in _jobs.values()}
                        | {job.case_dir.resolve()})
        done_ids = [k for k, j in _jobs.items()
                    if j.state in ("done", "failed", "cancelled")]
        for k in done_ids[:-4]:
            _jobs.pop(k, None)
        _jobs[job.id] = job
    threading.Thread(target=job.run, name=f"rans-{job.id}",
                     daemon=True).start()
    return job.id


def get(job_id: str) -> RansJob | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def current() -> dict:
    """The active (or, failing that, most recent) job — lets a reloaded UI
    or a second window rediscover and re-attach to the run it cannot see
    in its own state."""
    with _jobs_lock:
        jobs = list(_jobs.values())
    for j in jobs:
        if j.state in ("pending", "running"):
            return {"job_id": j.id, "state": j.state}
    if jobs:
        last = max(jobs, key=lambda j: j.t_start or 0)
        return {"job_id": last.id, "state": last.state}
    return {"job_id": None, "state": None}
