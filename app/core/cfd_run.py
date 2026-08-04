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

One INTERACTIVE job at a time: the verify tab drives a single attended
run, and start() keeps that UX by refusing a second concurrent start (the
API turns that into a 409). The old justification here — "simpleFoam is
CPU-bound on every core it gets" — was wrong: OpenFOAM's FV solvers have
no threading, a solve occupies exactly one core per MPI rank. Within-solve
parallelism is the opt-in n_ranks knob (decomposePar/mpirun in the
generated run.sh); the shortlist queue can additionally run several solves
at once through start_pooled() under core_budget(). Serial single-run
remains the default everywhere, and the serial case is byte-identical to
what this module always produced.

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
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

from . import analysis, cfd, foam_post
from .geometry import StackConfig

DOCKER_IMAGE = "opencfd/openfoam-run:2406"
TAIL_MEAN_ROWS = 500          # matches run.sh's "mean of last 500 iterations"
POLL_S = 1.0
MAX_WALL_S = 4 * 3600         # hard stop — a fine mesh at full iterations
                              # fits comfortably; anything longer is hung
# finished run directories retained on disk — sized so a queue's worth of
# recent verifications plus a case a saved project still points at survive
# routine housekeeping (registered protectors guard live references; the
# count guards recent history)
KEEP_RUN_DIRS = 6

# ---- opt-in parallelism ----
# Ranks within a solve (cfd.build_case n_ranks -> decomposePar/mpirun in
# run.sh) and, for the queue only, several solves at once. Serial is the
# default on every path; parallel is chosen per run.
MAX_RANKS = 32


def core_budget() -> int:
    """Cores the queue may schedule against (ranks x concurrent solves).

    cpu_count() reports LOGICAL processors and SMT contributes ~nothing to
    a memory-bandwidth-bound FV solve, so half the logical count is the
    useful ceiling. Override with WSS_CORE_BUDGET for unusual machines."""
    try:
        v = int(os.environ.get("WSS_CORE_BUDGET", "0"))
    except ValueError:
        v = 0
    return v if v > 0 else max(1, (os.cpu_count() or 8) // 2)


# Attachment-verdict lines (reversed-face fraction of a wing patch) and the
# knife-edge bands around them. Decomposition changes the solver's iteration
# path — the same case solved at different rank counts settles on slightly
# different reversed fractions near separation onset — so a fraction within
# the band is REPORTED as knife-edge rather than trusted as a stable side
# of the line: a verdict that would flip with core count marks a knife-edge
# candidate, which is information, not noise. Widths from the measured
# cross-rank spread on the wall-truth case at 1/4/8/16 ranks (2026-07-25,
# DECISIONS.md): the separating element's fraction moved 0.217 -> 0.259
# (spread 0.042) while attached elements held within 0.02 — separation
# onset is where the path sensitivity lives, so the demotion line carries
# the wide band and the attached line the narrow one.
SEP_ATTACHED_MAX = 0.10
SEP_PARTIAL_MAX = 0.20
SEP_KNIFE_BAND_ATTACHED = 0.02   # around the 0.10 line
SEP_KNIFE_BAND = 0.05            # around the 0.20 demotion line

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
# Four defenses against premature verdicts, each closing a measured hole:
# the first FORCE_STOP_SKIP rows are excluded from every drift decision (a
# decay-then-recover startup — the usual potentialFoam-initialized shape on
# a separated high-lift case — has a mean-crossing where the window means
# cancel while the run still trends); the drift compares THREE consecutive
# windows (two alone read flat at every zero-crossing of the window-mean
# difference — one window past an overshoot peak, or a node of a slow
# oscillation riding a climb); the minimum iteration gate keeps all three
# windows fully post-transient before the detector may fire; and the
# criterion must hold across FORCE_STOP_POLLS evaluations spaced at least
# FORCE_STOP_REARM_ROWS NEW iterations apart — a fine mesh advances only
# a few iterations per poll second, so consecutive 1 s polls would
# re-judge essentially the same data.
FORCE_STOP_SKIP = 500         # rows never included in a drift decision
FORCE_STOP_WINDOW = 800       # window size (rows) for the drift means
FORCE_STOP_MIN_ITERS = FORCE_STOP_SKIP + 3 * FORCE_STOP_WINDOW   # = 2900
FORCE_STOP_POLLS = 3          # flat evaluations required before stopping
FORCE_STOP_REARM_ROWS = 100   # new rows required between evaluations
FORCE_STOP_CL_TOL = 0.003     # relative Cl drift between windows
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
    """Largest relative disagreement between the means of the last THREE
    windows — ~0 once the history is flat (a bounded limit cycle averages
    out), and of the order of the per-window climb rate while trending.
    Two windows alone read ~0 at every zero-crossing of the window-mean
    difference (one window past an overshoot peak, at any node of a slow
    oscillation riding a climb) — a quadratic transient zeroes one gap but
    not both, so the third window closes that false-plateau hole. None
    until enough rows exist to judge."""
    w = min(window, len(vals) // 3)
    if w < 100:
        return None
    m1 = sum(vals[-3 * w:-2 * w]) / w
    m2 = sum(vals[-2 * w:-w]) / w
    m3 = sum(vals[-w:]) / w
    ref = max(abs(m3), 0.05)
    return max(abs(m3 - m2), abs(m2 - m1)) / ref


def _tail_stats(vals: list[float], n: int = TAIL_MEAN_ROWS
                ) -> tuple[float, float, int]:
    """(mean, DETRENDED population std, rows used) over the tail of the
    history — steady RANS on a loaded high-lift section often ends in a
    bounded limit cycle, and the tail mean/std are the number to trust and
    its amplitude. The std is measured around the tail's least-squares
    line, not around the mean: on a drifting tail the raw std IS the ramp
    (range/sqrt(12)) and would dress a moving number up as a precise one —
    the drift is reported separately, never as scatter.
    The window never covers more than the second half of the run, so a
    short run's startup transient cannot bias the mean (a flat 500-row
    window would average mostly transient on a 300-iteration run)."""
    window = min(n, max(1, len(vals) // 2)) if len(vals) < 2 * n else n
    tail = vals[-window:]
    m = sum(tail) / len(tail)
    k = len(tail)
    if k < 3:
        return m, 0.0, k
    xm = (k - 1) / 2.0
    sxx = sum((i - xm) ** 2 for i in range(k))
    slope = sum((i - xm) * (v - m) for i, v in enumerate(tail)) / sxx
    var = sum((v - (m + slope * (i - xm))) ** 2
              for i, v in enumerate(tail)) / k
    return m, math.sqrt(var), k


CALIBRATION_GRADE_MESH = "fine"


def mesh_below_calibration_grade(mesh_size: str) -> bool:
    """Is a result on this mesh a screening number rather than a datum?

    ONE definition, shared with the shortlist queue's verdict grading —
    the two disagreeing would let a k_g be pinned from a mesh the queue
    calls unusable for exactly that purpose. The 2026-07 campaign measured
    the coarse mesh reading validated operating points 22-35% below
    fine-mesh truth on the two-element baseline (under-resolved venturi
    gap) and found medium scattering just as widely at racing height, so
    only fine calibrates.
    """
    return mesh_size != CALIBRATION_GRADE_MESH


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


# Field-channel attachment verdict — ADOPTED 2026-08-03 after the wall-
# pairing rounds (docs/calibration/LOG.md). Two modes with disjoint sensors,
# each line sitting in a measured gap of the wall-labeled record:
#   mode A (confluence collapse): worst per-element near-wall reversed
#     fraction. Every probe reading >= 0.26 was wall-separated; quiet
#     elements read <= 0.22. Line at 0.28, knife band 0.26-0.30.
#   mode B (main/open separation): total cells in reattaches=False regions.
#     The failing tier reads 3459-8272 cells, everything else <= 377; the
#     probe reads near zero on exactly these (the mode it cannot see).
#     Line at 1000, knife band 500-1500.
# A verdict from this channel is a FIELD reading: the probe under-reads the
# wall everywhere (worst in the partial band), so "clean" here means "no
# field evidence of separation", never "attached". Wall shear, where it
# exists, outranks it; the queue treats either channel's separated as
# grounds for demotion.
FIELD_SEP_PROBE = 0.28
FIELD_PROBE_KNIFE = (0.26, 0.30)
FIELD_SEP_OPEN_CELLS = 1000
FIELD_OPEN_KNIFE = (500, 1500)


def wall_verdict_text(wall: dict | None) -> tuple:
    """(verdict prose, knife flag) from a wall report — ONE grading for
    every engine that produces the wall channel, so the OpenFOAM parser
    and the Fluent x-wall-shear export cannot drift apart on what the
    fractions mean. Returns (None, None) when there is nothing to grade."""
    if not (wall and wall.get("separation")):
        return None, None
    parts = []
    knife = False
    for patch in sorted(wall["separation"]):
        frac = wall["separation"][patch]["reversed_frac"]
        state = ("attached" if frac <= SEP_ATTACHED_MAX
                 else "partial separation" if frac <= SEP_PARTIAL_MAX
                 else "separated")
        # within the band of either line the side it landed on is
        # rank-count luck, not a stable classification — say so
        near = (abs(frac - SEP_ATTACHED_MAX) <= SEP_KNIFE_BAND_ATTACHED
                or abs(frac - SEP_PARTIAL_MAX) <= SEP_KNIFE_BAND)
        knife = knife or near
        parts.append(f"{patch.replace('wing_', '')} {state} "
                     f"({frac * 100:.0f}% reversed"
                     f"{', knife-edge' if near else ''})")
    return ", ".join(parts), knife


def field_verdict(recirc: dict | None) -> dict | None:
    """Grade a recirculation report with the adopted two-mode lines.

    Returns {"verdict": str, "separated": bool, "knife_edge": bool,
    "worst_probe": float|None, "open_cells": int, "mode": "probe"|"census"|
    "both"|None} — or None when there is no measured report to grade (an
    unmeasurable case is never a pass, so its absence must read as absence).
    """
    if not recirc or recirc.get("status") != "measured":
        return None
    probes = [e.get("near_wall_reversed_frac")
              for e in (recirc.get("elements") or [])]
    probes = [p for p in probes if p is not None]
    worst = max(probes) if probes else None
    open_cells = sum(g["n_cells"] for g in (recirc.get("regions") or [])
                     if g.get("reattaches") is False)

    probe_sep = worst is not None and worst >= FIELD_SEP_PROBE
    open_sep = open_cells >= FIELD_SEP_OPEN_CELLS
    knife = ((worst is not None
              and FIELD_PROBE_KNIFE[0] <= worst < FIELD_PROBE_KNIFE[1])
             or FIELD_OPEN_KNIFE[0] <= open_cells < FIELD_OPEN_KNIFE[1])

    mode = ("both" if probe_sep and open_sep else
            "probe" if probe_sep else "census" if open_sep else None)
    if probe_sep or open_sep:
        parts = []
        if probe_sep:
            parts.append(f"near-wall reversal {worst * 100:.0f}%")
        if open_sep:
            parts.append(f"{open_cells} cells of non-reattaching "
                         f"reversed flow")
        verdict = ("field: separated (" + ", ".join(parts)
                   + (", knife-edge)" if knife else ")"))
    elif knife:
        verdict = ("field: knife-edge (near the "
                   + ("census" if open_cells >= FIELD_OPEN_KNIFE[0]
                      else "probe") + " line — not a stable side)")
    else:
        verdict = ("field: no separation evidence (the field channel "
                   "under-reads the wall; this is not 'attached')")
    return {"verdict": verdict, "separated": bool(probe_sep or open_sep),
            "knife_edge": bool(knife),
            "worst_probe": None if worst is None else round(worst, 4),
            "open_cells": int(open_cells), "mode": mode}


def delta_cd(cd_rans: float, panel: dict | None) -> tuple:
    """Measured profile drag against the attached-flow estimate.

    Returns (delta_pct, is_upper_bound); (None, False) when the estimate
    is missing or degenerate.

    NOT A SEPARATION DETECTOR. Measured and refuted -- read this before
    using it as one, because the column was introduced believing it was.

    Six converged Fluent 2D solves, delta_cd_pct against the measured
    attachment state:

        ATTACHED  validated baseline 30mm   +626.1%
        separated 859e79a7486d              +582.1%
        ATTACHED  validated baseline 40mm   +524.2%
        separated 1c4bf2d726c3              +523.9%
        separated 09c6de53265a              +442.7%
        separated df1865bb82a3              +395.5%

    The classes overlap completely and the healthiest design in the set
    reads HIGHEST. What this column actually measures is how far the
    attached-flow polar estimate falls short of a loaded ground-effect
    stack's real drag, and that shortfall is 4-6x whatever the flow is
    doing -- note is_upper_bound came back True on all six, i.e. the polar
    lookup was capped every time. The cap dominates; separation does not
    move it enough to see.

    It remains worth reporting: a 5x gap says the estimate's drag is not
    usable for this design, which is real information. It is just
    information about the ESTIMATE, not about the flow.

    Basis: 2D RANS Cd is profile (pressure + friction) drag, compared
    against CD_profile_stack and never the total — induced drag is a 3D
    effect the case cannot see (module docstring).

    Bound semantics: when the estimate's polar lookup is capped, an
    element carried past its isolated CL_max has no honest drag on that
    polar at all, so the estimate UNDERSTATES drag and the ratio
    OVERSTATES the excess. The delta is then an upper bound on the true
    excess, not a result, and the caller must label it as one.

    No verdict is derived here. What excess means for attachment is a
    calibration question against the wall-shear record, not a constant to
    guess in the runner.
    """
    if not panel:
        return None, False
    cd_est = panel.get("cd_profile")
    if cd_est is None or abs(cd_est) < 1e-9:
        return None, False
    return (round((cd_rans / cd_est - 1) * 100, 1),
            bool(panel.get("cd_profile_is_lower_bound")))


def worst_reversed(result: dict) -> float | None:
    """Highest measured reversed wall-shear fraction across the elements.

    One definition, shared by the queue's ranking and the harvest sink, so
    "how separated was this run" cannot mean two things."""
    sep = ((result.get("wall_report") or {}).get("separation") or {})
    fracs = [p.get("reversed_frac") for p in sep.values()
             if p.get("reversed_frac") is not None]
    return max(fracs) if fracs else None


def _harvest_path() -> Path:
    """Where accumulated (design -> measured truth) rows land.

    Under the data dir rather than docs/calibration: the app appends on
    every finished solve, and a version-controlled target would dirty the
    working tree on every run and sweep unreviewed rows into commits.
    scripts/promote_harvest.py lifts a reviewed extract into
    docs/calibration when it is worth keeping. Run-dir housekeeping only
    removes case directories, so rows here outlive the cases they describe
    -- which is the whole point: the previous calibration record had to be
    hand-extracted precisely because the cases were already gone."""
    explicit = os.environ.get("WSS_HARVEST_PATH")
    if explicit:
        return Path(explicit)
    base = Path(os.environ.get(
        "WSS_DATA_DIR", Path(__file__).resolve().parents[2] / "app_data"))
    return base / "harvest.jsonl"


def harvest_row(result: dict, config: dict, engine: str,
                mesh_size: str | None) -> dict:
    """One (design -> prediction -> measured truth) row.

    Carries the cheap screen's prediction and the expensive measurement
    side by side, which is the pairing a calibration needs and the thing
    no artifact on disk currently holds. The config travels verbatim so a
    row stays reproducible after its case directory is deleted.

    Nothing is graded here. The row records what was predicted and what was
    measured; deciding where the line between them falls is the job of a
    regression over many such rows."""
    recirc = result.get("recirc_report") or {}
    r_elems = recirc.get("elements") or []
    panel = result.get("panel") or {}
    case_dir = result.get("case_dir")
    return {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "case_id": Path(case_dir).name if case_dir else None,
        "engine": engine,
        "mesh_size": mesh_size,
        # -- run trustworthiness: a bound is not a result
        "converged": result.get("converged"),
        "user_stopped": result.get("user_stopped"),
        "stop_reason": result.get("stop_reason"),
        "n_iters_run": result.get("n_iters_run"),
        # -- forces, and both comparison columns
        "cl_rans": result.get("cl_rans"),
        "cd_rans": result.get("cd_rans"),
        "delta_cl_pct": result.get("delta_cl_pct"),
        "delta_cd_pct": result.get("delta_cd_pct"),
        "delta_cd_is_upper_bound": result.get("delta_cd_is_upper_bound"),
        # -- the CHEAP screen's prediction (what the optimizer saw)
        "shadow_mins": panel.get("shadow_mins"),
        "c_est": panel.get("c_est"),
        "cd_profile_est": panel.get("cd_profile"),
        # -- the EXPENSIVE measurement (what RANS found)
        "wall_reversed": {k: v.get("reversed_frac") for k, v in
                          (((result.get("wall_report") or {})
                            .get("separation") or {}).items())} or None,
        "worst_reversed": worst_reversed(result),
        "recirc_status": recirc.get("status"),
        "recirc_bound": recirc.get("bound"),
        "field_verdict": result.get("field_verdict"),
        "recirc_near_wall_frac": [e.get("near_wall_reversed_frac")
                                  for e in r_elems] or None,
        "recirc_closes": [[r.get("closes")
                           for s in (e.get("sides") or {}).values()
                           for r in s.get("runs", [])]
                          for e in r_elems] or None,
        "config": config,
    }


def append_harvest(row: dict) -> bool:
    """Append one row. Never raises: losing a calibration row must not fail
    a solve that already succeeded."""
    try:
        p = _harvest_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, allow_nan=False) + "\n")
        return True
    except (OSError, TypeError, ValueError):
        return False


# ---------- the job ----------

class RansJob:
    def __init__(self, config: dict, mesh_size: str = "coarse",
                 n_iters: int = cfd.N_ITERS, n_ranks: int = 1):
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
        self.n_ranks = int(n_ranks)
        if not (1 <= self.n_ranks <= MAX_RANKS):
            raise ValueError(f"n_ranks must be between 1 and {MAX_RANKS}")
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
        self._cl_drift: float | None = None   # latest window drifts
        self._cd_drift: float | None = None
        self._flat_polls = 0                  # consecutive flat evaluations
        self._n_rows = 0                      # coefficient rows seen so far
        self._armed_rows = 0                  # rows at the last evaluation
        self._force_stop = False              # runner requested writeNow
        self._user_stop = threading.Event()   # user asked to stop-and-keep
        self._stopped_by_user = False         # the user request took effect
        self._rows_at_user_stop: int | None = None
        self._stop_flip_failures = 0          # writeNow flips that failed
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
            self._n_rows = n
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
            # whitespace-tolerant: an exact-string match would silently
            # break the Stop button the day cfd.py's template is respaced
            new, n = re.subn(r"stopAt\s+endTime;",
                             "stopAt          writeNow;", text, count=1)
            if not n:
                return False
            tmp = cd_path.with_suffix(".tmp")
            tmp.write_text(new, encoding="utf-8", newline="\n")
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
            new, n = re.subn(r"stopAt\s+writeNow;",
                             "stopAt          endTime;", text, count=1)
            if n:
                cd_path.write_text(new, encoding="utf-8", newline="\n")
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
            # a writeNow flip must not outlive the job on ANY exit path —
            # the failure message points the user at the retained case, and
            # a still-flipped controlDict makes a manual rerun stop after
            # one iteration with no error
            if self._force_stop:
                self._restore_controldict()

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
                                  self.mesh_size, self.n_iters,
                                  self.n_ranks)
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
                    # user stop-and-keep: same writeNow mechanism as the
                    # auto stop, but the finalize verdict stays honest
                    # ("stopped by user", never "converged", no k_g). The
                    # row count at the flip is recorded so finalize can
                    # tell a stop that actually took effect from one that
                    # landed after the solver had already quit on its own.
                    if self._user_stop.is_set() and not self._force_stop:
                        if self._request_graceful_stop():
                            self._force_stop = True
                            self._stopped_by_user = True
                            self._rows_at_user_stop = self._n_rows
                            with self._lock:
                                self.phase = ("stopping at your request — "
                                              "writing final fields")
                        else:
                            # the flip is a file edit that can keep failing
                            # (template drift, AV holding controlDict) —
                            # after a few failures say so instead of letting
                            # the solver run for hours past a Stop click
                            self._stop_flip_failures += 1
                            if self._stop_flip_failures >= 5:
                                with self._lock:
                                    self.phase = (
                                        "stop request could not be applied "
                                        "(controlDict not writable?) — the "
                                        "solver is still running; use "
                                        "Cancel to kill it")
                    # a single flat evaluation can be a noise minimum, and
                    # 1 s polls re-judge nearly identical data on a slow
                    # mesh — require the criterion to persist across
                    # evaluations separated by genuinely new history
                    if (not self._force_stop
                            and self._n_rows - self._armed_rows
                            >= FORCE_STOP_REARM_ROWS):
                        self._armed_rows = self._n_rows
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
        # history actually flat? EVERY branch is gated on the final
        # history — the stop mechanism explains the exit, it never
        # certifies the result (a false plateau can fire the force stop;
        # an early rc==0 exit is not evidence of anything). The drift
        # excludes the startup transient, same as the stop decision.
        cl_drift = drift(data["cl"][FORCE_STOP_SKIP:])
        history_flat = cl_drift is not None and cl_drift < CONVERGED_CL_TOL
        # drift() needs SKIP + 3*100 rows before it can judge anything. Not
        # measurable is not the same as measurably trending: a short run
        # must not be told its history is climbing when nothing measured it.
        history_unknown = cl_drift is None
        # "the user stop took effect" requires the solver to have quit
        # below the cap AND to have advanced past the flip — a stop that
        # landed after the solver already exited on its own must not
        # relabel that exit's verdict
        user_applied = (self._stopped_by_user and n_run < self.n_iters
                        and (self._rows_at_user_stop is None
                             or len(data["cl"]) > self._rows_at_user_stop))
        if user_applied:
            # a user stop is a preview, not a result: never "converged",
            # never a k_g suggestion — a hand-stopped tail must not feed
            # calibration, however flat it happens to look
            converged = False
            stop_reason = "stopped by user (fields written)"
        elif self._force_stop and not self._stopped_by_user \
                and n_run < self.n_iters:
            converged = history_flat
            stop_reason = ("force history converged" if converged else
                           "force stop fired on a false plateau — history "
                           "still trending")
        elif n_run < self.n_iters:
            converged = history_flat
            stop_reason = (
                "residuals converged" if converged else
                f"solver stopped early after {n_run} iterations — too few "
                f"to verify the force history" if history_unknown else
                "solver stopped early with a still-trending force history")
        else:
            converged = history_flat
            stop_reason = ("iteration cap reached" if not history_unknown else
                           f"iteration cap reached after {n_run} iterations — "
                           f"too few to verify the force history")

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
                # the drag comparison is only as honest as the polar
                # lookup behind it — carry the cap flag with the number
                "cd_profile_is_lower_bound": fo[
                    "drag_profile_is_lower_bound"],
                "cd_capped_roles": fo["drag_capped_roles"],
                # what the CHEAP screen predicted for this design, kept
                # beside the expensive measurement so a calibration row can
                # pair them without re-running the panel model
                "shadow_mins": [e.get("shadow_min") for e in r["elements"]],
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

        # signed trend across the last two windows: the drifting-run note
        # tells the user WHICH WAY the number is still moving (a rising
        # history makes the tail mean a lower bound)
        # measured on the same post-transient slice the verdict used: a
        # direction read across the startup decay would point the wrong way
        post = data["cl"][FORCE_STOP_SKIP:]
        w = min(FORCE_STOP_WINDOW, max(1, len(post) // 3))
        trend_note = None
        if not converged and not history_unknown and len(post) >= 2 * w:
            m_prev = sum(post[-2 * w:-w]) / w
            m_last = sum(post[-w:]) / w
            signed = (m_last - m_prev) / max(abs(m_last), 0.05)
            if abs(signed) >= 0.001:
                direction = "rising" if signed > 0 else "falling"
                bound = "lower" if signed > 0 else "upper"
                trend_note = (
                    f"Cl is still {direction} ~{abs(signed) * 100:.1f}% per "
                    f"{w}-iteration window — treat {cl_mean:.2f} as a "
                    f"{bound} bound, not a result")
        elif not converged and history_unknown:
            trend_note = (
                f"only {len(data['cl'])} iterations of force history — too "
                f"few to tell whether {cl_mean:.2f} has settled; raise Max "
                f"iterations and rerun")

        # measured wall state (y+ + separation) from the diagnostics the
        # case writes beside every field set; absent on cases generated
        # before those function objects existed
        wall = None
        wall_verdict = None
        sep_knife_edge = None
        try:
            wall = foam_post.wall_report(self.case_dir)
        except Exception:
            wall = None
        wall_verdict, sep_knife_edge = wall_verdict_text(wall)

        # field-side recirculation structure, from the same solved field the
        # flow view draws. Ungraded by construction (see foam_post): it says
        # how much and where, and the line that would turn that into a
        # verdict does not exist yet. Costs about one flow_png, so it runs
        # once here at result assembly and never on a UI poll.
        try:
            recirc = foam_post.recirculation_report(
                self.case_dir, self.cfg, converged=converged,
                user_stopped=user_applied, wall=wall)
        except Exception:
            recirc = None

        d_cd, d_cd_bound = delta_cd(cd_mean, panel)

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
                # a delta computed from a drifting tail moves with the
                # history — the UI must not present it as the design's
                # measured error
                "delta_cl_provisional": not converged,
                # NOT a separation signal: measured, an ATTACHED
                # validated baseline reads +626% and a separated case
                # +396%, so the classes overlap and the healthy design
                # reads highest (see cfd_run.delta_cd). It measures the
                # capped polar estimate's shortfall against a loaded
                # stack's real drag -- information about the estimate,
                # not about the flow.
                "delta_cd_pct": d_cd,
                "delta_cd_is_upper_bound": d_cd_bound,
                "cl_trend_note": trend_note,
                "wall_report": wall,
                "wall_verdict": wall_verdict,
                "sep_knife_edge": sep_knife_edge,
                "recirc_report": recirc,
                # the ADOPTED field grading rides beside the wall channel;
                # on this engine both exist and wall outranks field
                "field_verdict": field_verdict(recirc),
                "n_ranks": self.n_ranks,
                "estimate_scope_note": (
                    "the estimate's k_g realization curve is calibrated on "
                    "the two-element baseline at moderate loading; on "
                    "multi-element, heavily loaded stacks fine-mesh RANS "
                    "has measured 35-65% more downforce than the estimate "
                    "(this section: k_g realizes only "
                    f"{panel['k_g_used']:.2f} of the inviscid gain). The "
                    "gap is estimate conservatism, not a RANS fault — on a "
                    "converged run, apply the suggested k_g to recalibrate "
                    "the session."
                ) if (panel and len(self.cfg.elements) >= 3
                      and abs(panel["c_est"]) > 1e-9
                      and cl_mean / panel["c_est"] - 1 > 0.25) else None,
                "suggested_k_g": suggestion,
                "mesh_caution": mesh_below_calibration_grade(self.mesh_size),
                "user_stopped": user_applied,
                "case_dir": str(self.case_dir),
            }
            self.phase = None
            self.state = "done"

        # every finished solve becomes a calibration row. The previous
        # record had to be hand-extracted from case directories because
        # housekeeping had already deleted most of what it described; rows
        # written here outlive their cases by construction.
        append_harvest(harvest_row(self.result, self.config, "openfoam",
                                   self.mesh_size))

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
                "mesh_size": self.mesh_size, "n_ranks": self.n_ranks,
                "engine": "openfoam",
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

# Non-job workloads that must own the solver slot exclusively — the Fluent
# mesh export holds an ANSYS seat and gmsh state for minutes, and a job
# started meanwhile would exit its live Meshing session mid-workflow. The
# claim lives beside _jobs under _jobs_lock so start()/start_pooled() and
# the claim itself decide against one consistent picture.
_exclusive_claim: str | None = None


def claim_exclusive(tag: str) -> None:
    """Claim the solver slot for a non-job workload. The tag carries its
    own article (e.g. "a Fluent mesh export") — refusal messages
    interpolate it sentence-initially. Refused while any registered job
    is pending/running or another claim is held; the caller must
    release_exclusive() in a finally around the whole workload."""
    global _exclusive_claim
    with _jobs_lock:
        if _exclusive_claim is not None:
            raise RuntimeError(
                f"{_exclusive_claim} is already running — wait for it "
                f"to finish")
        for j in _jobs.values():
            if j.state in ("pending", "running"):
                raise RuntimeError(
                    "a RANS verification is already running — cancel it "
                    "or wait for it to finish")
        _exclusive_claim = tag


def release_exclusive() -> None:
    global _exclusive_claim
    with _jobs_lock:
        _exclusive_claim = None


def exclusive_claim() -> str | None:
    """The tag holding the solver slot, if any — the shortlist queue
    checks it at start so an export-blocked queue answers an immediate
    refusal instead of a started-then-failed first row."""
    with _jobs_lock:
        return _exclusive_claim


def _pid_alive(pid: int) -> bool:
    """Is a process with this id still running on this machine?"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            # ERROR_ACCESS_DENIED: the pid EXISTS but belongs to another
            # user or elevation — calling that "dead" would let the orphan
            # sweep kill a live neighbour's solve
            return k32.GetLastError() == 5
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
    """Force-remove wss-rans-* (and wedged wss-fluent-mesh-*) containers
    whose owning server is gone.

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
        # the gmsh->Fluent bridge containers (scripts/fluent_workflow)
        # carry their owner pid in the NAME, not a label:
        # wss-fluent-mesh-<pid>-<ts>. --rm cleans up the normal case;
        # sweep only the wedged ones whose owner is gone. Any live owner
        # is left alone — including this process, whose own conversion
        # may be in flight on a job thread and is not in active_names.
        r = _docker(["ps", "--filter", "name=wss-fluent-mesh-",
                     "--format", "{{.Names}}"], 15)
        if r.returncode == 0:
            for name in (ln.strip() for ln in r.stdout.splitlines()):
                if not name.startswith("wss-fluent-mesh-"):
                    continue
                try:
                    owner = int(name.split("-")[3])
                except (IndexError, ValueError):
                    owner = 0
                if not owner or not _pid_alive(owner):
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
        try:
            j.cancel()
            # FluentJob duck-types the registry surface but has no docker
            # container to kill; one wedged job must not abort the reap
            # of the jobs after it
            kill = getattr(j, "_kill_container", None)
            if kill is not None:
                kill(15)
        except Exception:
            pass


atexit.register(_reap_at_exit)


_protect_lock = threading.Lock()
_protected_providers: list = []   # callables returning iterables of dirs


def register_protected_dirs(provider) -> None:
    """Register a callable returning case directories that must never be
    pruned — the RANS queue's shortlist rows and the session's retained
    verification case reference dirs long after their jobs left the
    registry, and pruning one leaves a dangling case_dir in saved state."""
    with _protect_lock:
        _protected_providers.append(provider)


def _protected_dirs() -> set[Path]:
    with _protect_lock:
        providers = list(_protected_providers)
    out: set[Path] = set()
    for provider in providers:
        try:
            out |= {Path(d).resolve() for d in provider() if d}
        except Exception:
            pass   # a broken provider must not block job starts
    return out


def _prune_run_dirs(active: set[Path],
                    live_other_names: set[str] = frozenset()) -> None:
    """Keep the newest KEEP_RUN_DIRS finished run directories; a solve can
    write hundreds of MB of fields, and verification runs are working
    artifacts, not user exports. Directories backing another instance's
    live container, or claimed by a registered protector (queue rows,
    session-referenced cases), are never pruned out from under it."""
    root = _runs_dir()
    protected = {name[len("wss-rans-"):] for name in live_other_names}
    if not root.is_dir():
        return
    keep = active | _protected_dirs()
    dirs = sorted((d for d in root.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_RUN_DIRS:]:
        if d.resolve() not in keep and d.name not in protected:
            shutil.rmtree(d, ignore_errors=True)


def start(config: dict, mesh_size: str = "coarse",
          n_iters: int = cfd.N_ITERS, n_ranks: int = 1,
          engine: str = "openfoam", mesher: str = "fluent",
          conventions: str = "default",
          settings: dict | None = None) -> str:
    """Start one interactive verification. engine="openfoam" (default)
    is the Docker screening engine; engine="fluent" runs a licensed
    local ANSYS Fluent on the 3D slab (the documented revert path),
    meshed natively by Fluent Meshing (mesher="fluent", the default) or
    by the studio's gmsh for an identical-mesh cross-check
    (mesher="gmsh"); engine="fluent2d" runs the true-2D ANSYS workflow
    (Workbench-meshed — see fluent2d_run), where mesh_size is a SIZING
    mode ("default" | "studio-yplus1") and conventions picks the solver
    doctrine ("default" | "studio"). mesher applies to the Fluent slab
    engine only; conventions and settings — the 2D panel's mesh, domain
    and stage-budget overrides, resolved by the job constructor — to the
    2D engine only. All engines share this registry, so the
    one-at-a-time guard spans them."""
    if engine not in ("openfoam", "fluent", "fluent2d"):
        raise ValueError(
            "engine must be 'openfoam', 'fluent' or 'fluent2d'")
    if engine == "fluent":
        from . import fluent_run
        job = fluent_run.FluentJob(config, mesh_size, n_iters,
                                   n_ranks, mesher)   # validates eagerly
    elif engine == "fluent2d":
        from . import fluent2d_run
        job = fluent2d_run.Fluent2DJob(config, mesh_size, n_iters,
                                       n_ranks, conventions,
                                       settings)   # validates eagerly
    else:
        job = RansJob(config, mesh_size, n_iters,
                      n_ranks)   # validates eagerly
    return admit_and_launch(job)


def admit_and_launch(job) -> str:
    """Registry admission + housekeeping + thread launch, shared by every
    interactively started engine job: start() builds its job from a
    config; the adjoint polish builds its own job object and registers
    it here, so the one-at-a-time guard, the orphan sweep and the
    run-dir pruning span it identically."""
    with _jobs_lock:
        if _exclusive_claim is not None:
            raise RuntimeError(
                f"{_exclusive_claim} is running — wait for it to finish")
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


def start_pooled(config: dict, mesh_size: str = "coarse",
                 n_iters: int = cfd.N_ITERS, n_ranks: int = 1,
                 conventions: str = "default") -> str:
    """Queue-owned start: registers and launches a job WITHOUT the
    interactive one-at-a-time guard. The caller (the shortlist queue) owns
    admission — how many pooled jobs run at once, within core_budget() —
    and runs batch_housekeep() once per batch instead of per start.
    start() still refuses while pooled jobs are active, so the verify tab
    cannot land a second workload on top of a running queue. conventions
    is accepted for signature parity with start(); the queue solves
    OpenFOAM only, where it does not apply."""
    job = RansJob(config, mesh_size, n_iters, n_ranks)
    with _jobs_lock:
        if _exclusive_claim is not None:
            raise RuntimeError(
                f"{_exclusive_claim} is running — wait for it to finish")
        done_ids = [k for k, j in _jobs.items()
                    if j.state in ("done", "failed", "cancelled")]
        for k in done_ids[:-4]:
            _jobs.pop(k, None)
        _jobs[job.id] = job
    threading.Thread(target=job.run, name=f"rans-{job.id}",
                     daemon=True).start()
    return job.id


def batch_housekeep() -> None:
    """The orphan sweep + run-dir prune start() performs, once per queue
    batch. Raises RuntimeError when another app instance's solver container
    is live — the queue fails eagerly instead of colliding with it."""
    with _jobs_lock:
        active_names = {j._container for j in _jobs.values()}
        active_dirs = {j.case_dir.resolve() for j in _jobs.values()}
    live_others = _sweep_orphan_containers(active_names)
    if live_others:
        raise RuntimeError(
            "a RANS verification started by another app window or "
            "instance is still running — cancel it there or wait for "
            "it to finish")
    _prune_run_dirs(active_dirs, live_others)


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
