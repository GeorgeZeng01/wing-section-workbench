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

# Force-based convergence: residualControl alone under-serves this case —
# heavily loaded fine meshes can iterate for thousands of steps with the
# lift STILL CLIMBING at a steady rate (measured: +0.49 Cl per 500
# iterations at the old 3000 cap on a 96k-cell stack — the reported tail
# mean was mid-transient, not a result). The runner therefore watches the
# force history itself: when the half-window means of Cl and Cd agree
# within the tolerances below, it flips controlDict to `stopAt writeNow`
# (runTimeModifiable; ESI builds re-check by mtime each step, which
# propagates through the bind mount) and the solver writes and exits
# cleanly. Runs that hit the iteration cap while still drifting are
# reported as NOT converged, and no k_g calibration is offered from them.
#
# Three defenses against premature verdicts, each closing a measured hole:
# the first FORCE_STOP_SKIP rows are excluded from every drift decision (a
# decay-then-recover startup — the usual potentialFoam-initialized shape on
# a separated high-lift case — has a mean-crossing where the half-window
# means cancel while the run still trends); the minimum iteration gate is
# SKIP + 2*WINDOW so both half-windows are fully post-transient before the
# detector may fire; and the criterion must hold on several CONSECUTIVE
# polls, so a single noise minimum cannot trigger the stop.
FORCE_STOP_SKIP = 500         # rows never included in a drift decision
FORCE_STOP_WINDOW = 800       # half-window size (rows) for the drift means
FORCE_STOP_MIN_ITERS = FORCE_STOP_SKIP + 2 * FORCE_STOP_WINDOW   # = 2100
FORCE_STOP_POLLS = 3          # consecutive flat polls before stopping
FORCE_STOP_CL_TOL = 0.003     # relative Cl drift between half-windows
FORCE_STOP_CD_TOL = 0.010     # Cd converges last; keep a looser bar
CONVERGED_CL_TOL = 0.006      # post-hoc verdict for cap-limited runs

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _image() -> str:
    return os.environ.get("WSS_OPENFOAM_IMAGE", DOCKER_IMAGE)


def _runs_dir() -> Path:
    # same override the server's session store honors: tests must not write
    # into the real app_data
    base = Path(os.environ.get(
        "WSS_DATA_DIR", Path(__file__).resolve().parents[2] / "app_data"))
    return base / "rans"


_docker_exe_cached: str | None = None


def _docker_exe() -> str:
    """Absolute path to the docker CLI.

    Windows CreateProcess resolves a bare "docker" through the LIVE process
    PATH — which gmsh replaces wholesale while a mesh is being built (see
    cfd._real_env_path), so a concurrent availability/cancel call during
    that window would fail with FileNotFoundError even though docker is
    installed. Resolving to an absolute path once (falling back to the real
    Win32 PATH if the snapshot is already clobbered) makes every docker
    launch independent of the live PATH."""
    global _docker_exe_cached
    if _docker_exe_cached:
        return _docker_exe_cached
    import shutil as _shutil
    exe = _shutil.which("docker")
    if exe is None and sys.platform == "win32":
        from . import cfd
        real = cfd._real_env_path()
        if real:
            exe = _shutil.which("docker", path=real)
    if exe:
        _docker_exe_cached = exe
        return exe
    return "docker"   # unresolvable: let the subprocess raise cleanly


def _docker(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    # env passed explicitly: os.environ is Python's pristine snapshot, and
    # the real process environment may have been clobbered by gmsh (see
    # cfd._real_env_path) — belt and braces on top of cfd's restore
    return subprocess.run([_docker_exe(), *args], capture_output=True,
                          text=True, timeout=timeout,
                          creationflags=_CREATE_NO_WINDOW,
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


def drift(vals: list[float], window: int = FORCE_STOP_WINDOW) -> float | None:
    """Relative disagreement between the means of the last two half-windows
    — ~0 once the history is flat (a bounded limit cycle averages out), and
    of the order of the per-window climb rate while still trending. None
    until enough rows exist to judge."""
    w = min(window, len(vals) // 2)
    if w < 100:
        return None
    m1 = sum(vals[-2 * w:-w]) / w
    m2 = sum(vals[-w:]) / w
    return abs(m2 - m1) / max(abs(m2), 0.05)


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
    cap = analysis.gain_cap(c_free, cfg)   # same cap the estimate uses
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
        self._cl_drift: float | None = None   # latest half-window drifts
        self._cd_drift: float | None = None
        self._flat_polls = 0                  # consecutive flat polls
        self._force_stop = False              # runner requested writeNow
        self._user_stop = threading.Event()   # user asked to stop-and-keep
        self._stopped_by_user = False         # the user request took effect
        self._solving = False                 # solver container is running

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
            if not self._force_stop:
                # keep the "writing final fields" label during the drain
                self.phase = "solving"
            # decision drifts exclude the startup transient entirely
            self._cl_drift = drift(data["cl"][FORCE_STOP_SKIP:])
            self._cd_drift = drift(data["cd"][FORCE_STOP_SKIP:])

    # ---- runner ----

    def _force_converged(self) -> bool:
        return (self.iteration >= FORCE_STOP_MIN_ITERS
                and self._cl_drift is not None
                and self._cl_drift < FORCE_STOP_CL_TOL
                and self._cd_drift is not None
                and self._cd_drift < FORCE_STOP_CD_TOL)

    def _request_graceful_stop(self) -> bool:
        """Flip controlDict to `stopAt writeNow` — the solver notices at the
        next time step (runTimeModifiable, mtime-checked), writes the fields
        and exits 0, so run.sh still extracts results normally."""
        cd_path = self.case_dir / "system" / "controlDict"
        try:
            text = cd_path.read_text(encoding="utf-8")
            if "stopAt          endTime;" not in text:
                return False
            tmp = cd_path.with_suffix(".tmp")
            tmp.write_text(text.replace("stopAt          endTime;",
                                        "stopAt          writeNow;", 1),
                           encoding="utf-8", newline="\n")
            os.replace(tmp, cd_path)
            return True
        except OSError:
            return False

    def _restore_controldict(self) -> None:
        """Undo the writeNow flip once the solver has exited — the retained
        case must stay runnable as-is (a manual WSL rerun of a flipped case
        would stop after a single iteration)."""
        cd_path = self.case_dir / "system" / "controlDict"
        try:
            text = cd_path.read_text(encoding="utf-8")
            if "stopAt          writeNow;" in text:
                cd_path.write_text(text.replace("stopAt          writeNow;",
                                                "stopAt          endTime;", 1),
                                   encoding="utf-8", newline="\n")
        except OSError:
            pass

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
            import json
            # the exact config this case was built from — makes a retained
            # run dir diagnosable long after the job left the registry
            (self.case_dir / "config.json").write_text(
                json.dumps(self.config, indent=1), encoding="utf-8")
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
                pull = _popen([_docker_exe(), "pull", _image()], stdout=fh,
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
        # the pid label lets the orphan sweep tell a crashed server's
        # leftover apart from a live run owned by ANOTHER app instance
        cmd = [_docker_exe(), "run", "--rm", "--name", self._container,
               "--label", f"wss.pid={os.getpid()}",
               "-v", f"{self.case_dir}:/case", "-w", "/case",
               "--entrypoint", "/bin/bash", _image(), "run.sh"]
        with open(log_path, "wb") as log_fh:
            proc = _popen(cmd, stdout=log_fh, stderr=subprocess.STDOUT,
                          creationflags=_CREATE_NO_WINDOW,
                          env={**os.environ})
            self._solving = True
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
                    # a single flat poll can be a noise minimum — require
                    # the criterion to persist before stopping the solver.
                    # (The docker client is still alive during run.sh's
                    # post-solve steps, so a stop requested in that window
                    # is a no-op on an already-exited solver — harmless,
                    # and the finalize verdict re-checks the outcome.)
                    # user stop-and-keep: same writeNow mechanism as the
                    # auto stop, but the finalize verdict stays honest
                    # ("stopped by user", never "converged", no k_g)
                    if (self._user_stop.is_set() and not self._force_stop
                            and self._request_graceful_stop()):
                        self._force_stop = True
                        self._stopped_by_user = True
                        with self._lock:
                            self.phase = ("stopping at your request — "
                                          "writing final fields")
                    if not self._force_stop:
                        self._flat_polls = (self._flat_polls + 1
                                            if self._force_converged() else 0)
                        if (self._flat_polls >= FORCE_STOP_POLLS
                                and self._request_graceful_stop()):
                            self._force_stop = True
                            with self._lock:
                                self.phase = ("force-converged — writing "
                                              "final fields")
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
        if self._force_stop:
            self._restore_controldict()
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

        # convergence verdict: what stopped the run, and is the force
        # history actually flat? A cap-limited run with a drifting tail is
        # a transient snapshot, not a result — say so, loudly. The drift
        # excludes the startup transient, same as the stop decision.
        cl_drift = drift(data["cl"][FORCE_STOP_SKIP:])
        # "the user stop took effect" requires the solver to have quit
        # below the cap — a request the solver never noticed must not
        # relabel a cap-limited run (its verdict judges the history), and
        # the result flag must mirror the verdict actually applied
        user_applied = self._stopped_by_user and n_run < self.n_iters
        if user_applied:
            # a user stop is a preview, not a result: never "converged",
            # never a k_g suggestion — a hand-stopped tail must not feed
            # calibration, however flat it happens to look
            converged = False
            stop_reason = "stopped by user (fields written)"
        elif self._force_stop and n_run < self.n_iters:
            # the request demonstrably took effect (the solver quit early)
            converged, stop_reason = True, "force history converged"
        elif n_run < self.n_iters:
            converged, stop_reason = True, "residuals converged"
        else:
            # ran to the cap — including the case where a writeNow request
            # was silently never noticed by the solver: judge the history,
            # not the request
            converged = cl_drift is not None and cl_drift < CONVERGED_CL_TOL
            stop_reason = "iteration cap reached"

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
            # a calibration constant fitted to a mid-transient Cl would be
            # confidently wrong — only converged runs may suggest one
            if converged:
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
                "residual_stop": (n_run < self.n_iters
                                  and not self._force_stop),
                "converged": converged,
                "stop_reason": stop_reason,
                "cl_drift": round(cl_drift, 5) if cl_drift is not None
                            else None,
                "downforce_n_at_rans_cl": round(
                    q * area * cl_mean * self.cfg.efficiency_3d, 1),
                "panel": panel,
                "panel_error": panel_error,
                "delta_cl_pct": round((cl_mean / panel["c_est"] - 1) * 100, 1)
                    if panel and abs(panel["c_est"]) > 1e-9 else None,
                "suggested_k_g": suggestion,
                # the 2026-07 calibration campaign (docs/calibration) measured
                # the coarse mesh reading validated operating points 22-35%
                # below fine-mesh truth through the racing and mid-height
                # bands (under-resolved venturi gap) — a coarse result is a
                # screening number, and a k_g pinned from one can bake that
                # bias into every estimate in the session
                "mesh_caution": self.mesh_size == "coarse",
                "user_stopped": user_applied,
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
            elif self._force_stop:
                progress = 0.97   # converged; the solver is writing out
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

    def stop_graceful(self) -> bool:
        """Stop-and-keep-fields: flip controlDict to writeNow at the next
        poll, let run.sh finish its post-processing (writeCellCentres and
        the results extraction), and finalize normally — so the flow view
        works on the partial run. The verdict says "stopped by user" and
        offers no k_g. Returns False while the solver is not running yet
        (nothing worth keeping — that is what cancel is for)."""
        with self._lock:
            if not (self._solving and self.state == "running"):
                return False
        if self._force_stop:
            return False   # a writeNow is already pending or done
        self._user_stop.set()
        return True


# ---------- registry ----------

_jobs: dict[str, RansJob] = {}
_jobs_lock = threading.Lock()


def _pid_alive(pid: int) -> bool:
    """Is a process with this id still running on this machine?"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259    # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _sweep_orphan_containers(active_names: set[str]) -> set[str]:
    """Force-remove wss-rans-* containers whose owning server is gone.

    The one-job-at-a-time guard and the run-dir pruning are process-local;
    a server restart mid-solve would otherwise leave a full-CPU container
    running unsupervised (--rm only cleans up after it finishes on its own)
    and later prune the case directory out from under it. Containers whose
    recorded owner pid is still alive belong to ANOTHER app instance and
    are left untouched — killing a neighbour's live solve is worse than
    tolerating a shared machine. Returns the names left running that this
    process does not own."""
    live_others: set[str] = set()
    try:
        r = _docker(["ps", "--filter", "name=wss-rans-", "--format",
                     '{{.Names}}\t{{.Label "wss.pid"}}'], 15)
        if r.returncode != 0:
            return live_others
        for line in r.stdout.splitlines():
            parts = line.strip().split("\t")
            name = parts[0] if parts else ""
            if not name.startswith("wss-rans-") or name in active_names:
                continue
            try:
                owner = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            except ValueError:
                owner = 0
            if owner and owner != os.getpid() and _pid_alive(owner):
                live_others.add(name)
                continue
            _docker(["rm", "-f", name], 30)
    except Exception:
        pass   # sweeping is best-effort; docker may simply be down
    return live_others


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


def _prune_run_dirs(active: set[Path],
                    live_other_names: set[str] = frozenset()) -> None:
    """Keep the newest KEEP_RUN_DIRS finished run directories; a solve can
    write hundreds of MB of fields, and verification runs are working
    artifacts, not user exports. Directories backing another instance's
    live container are never pruned out from under it."""
    root = _runs_dir()
    protected = {name[len("wss-rans-"):] for name in live_other_names}
    if not root.is_dir():
        return
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_RUN_DIRS:]:
        if d.resolve() not in active and d.name not in protected:
            shutil.rmtree(d, ignore_errors=True)


def start(config: dict, mesh_size: str = "coarse",
          n_iters: int = cfd.N_ITERS) -> str:
    job = RansJob(config, mesh_size, n_iters)   # validates eagerly
    with _jobs_lock:
        for j in _jobs.values():
            if j.state in ("pending", "running"):
                raise RuntimeError(
                    "a RANS verification is already running — cancel it or "
                    "wait for it to finish")
        # claim the slot under the lock so two concurrent starts cannot both
        # pass the guard; the slow docker housekeeping happens OUTSIDE the
        # lock — holding it through CLI calls would stall every status,
        # cancel and current request whenever the daemon is slow
        done_ids = [k for k, j in _jobs.items()
                    if j.state in ("done", "failed", "cancelled")]
        for k in done_ids[:-4]:
            _jobs.pop(k, None)
        _jobs[job.id] = job
        active_names = {j._container for j in _jobs.values()}
        active_dirs = {j.case_dir.resolve() for j in _jobs.values()}
    try:
        live_others = _sweep_orphan_containers(active_names)
        if live_others:
            with _jobs_lock:
                _jobs.pop(job.id, None)
            raise RuntimeError(
                "a RANS verification started by another app window or "
                "instance is still running — cancel it there or wait for "
                "it to finish")
        _prune_run_dirs(active_dirs, live_others)
    except RuntimeError:
        raise
    except Exception:
        pass   # housekeeping is best-effort
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
