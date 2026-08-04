"""REST API + static frontend for the wing-section design studio.

Run:  .venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8642
"""

from __future__ import annotations

import math
import re
import sys
import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .core import airfoils, analysis, export, geometry, optimizer, screener, viscous
from .core.geometry import StackConfig

app = FastAPI(title="Wing Section Studio", docs_url="/api/docs",
              openapi_url="/api/openapi.json")


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")

import time as _time_mod

_last_activity = _time_mod.time()


def last_activity() -> float:
    """Wall-clock time of the most recent request — the desktop launcher's
    plain-browser fallback uses it to shut the server down once the tab is
    gone (the UI heartbeats once a minute)."""
    return _last_activity


@app.middleware("http")
async def _track_activity(request, call_next):
    global _last_activity
    _last_activity = _time_mod.time()
    return await call_next(request)


# Generous cap, comfortably above the 4 MB session state and 500 KB .dat
# upload. Rejects an oversized body before the whole thing is buffered and
# JSON-parsed into memory (the per-field caps and the session-size check both
# run only AFTER a full parse, so they can't bound the parse itself).
MAX_BODY_BYTES = 16_000_000


class _BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, detail="request body too large")


class _BodySizeLimitMiddleware:
    """ASGI-level body cap. The Content-Length header alone cannot bound
    the parse — a Transfer-Encoding: chunked request carries none — so the
    received stream itself is counted and cut off past the cap."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = 0
        for k, v in scope.get("headers") or ():
            if k.lower() == b"content-length":
                try:
                    declared = int(v)
                except ValueError:
                    pass
                break
        if declared > MAX_BODY_BYTES:
            from fastapi.responses import JSONResponse
            resp = JSONResponse({"detail": "request body too large"},
                                status_code=413)
            await resp(scope, receive, send)
            return

        received = 0
        tripped = False
        response_started = False

        async def _recv():
            nonlocal received, tripped
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    tripped = True
                    raise _BodyTooLarge()
            return message

        async def _send(message):
            nonlocal response_started
            if tripped:
                # whatever the app renders after the cutoff is a substitute
                # for the exception (the raise cannot cross BaseHTTPMiddleware
                # task plumbing intact, so FastAPI turns it into a generic
                # 400) — drop it; the definitive 413 is sent below
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, _recv, _send)
        except BaseException:
            # only the cutoff's own unwinding is swallowed — anything else
            # keeps propagating
            if not tripped:
                raise
        if tripped and not response_started:
            from fastapi.responses import JSONResponse
            resp = JSONResponse({"detail": "request body too large"},
                                status_code=413)
            await resp(scope, receive, send)


app.add_middleware(_BodySizeLimitMiddleware)


def _hostname(value: str) -> str:
    """The bare host of a Host/Origin value: strip scheme, port and (for
    IPv6) keep the bracketed literal."""
    v = value.strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]          # drop any path
    if v.startswith("["):
        return v.split("]", 1)[0] + "]"
    return v.rsplit(":", 1)[0] if ":" in v else v


@app.middleware("http")
async def _local_host_only(request, call_next):
    """Reject requests that are not same-origin loopback traffic.

    Two defenses, both against a hostile web page trying to drive this local
    server from the user's browser:

    * Host allowlist — the server binds 127.0.0.1, but a DNS-rebinding page
      (a domain that re-resolves to 127.0.0.1) keeps the attacker's Host
      header, so pinning the Host to a loopback name closes that hole.
    * Origin allowlist — a cross-site page's requests carry its own Origin.
      The JSON-only request bodies and the absent CORS headers already stop
      the realistic cross-site POST, but rejecting any present non-loopback
      Origin is cheap defense-in-depth and makes the boundary explicit.
      Same-origin traffic carries a loopback Origin (or none, for top-level
      navigations), so this never touches the app's own requests.
    """
    from fastapi.responses import JSONResponse
    host = _hostname(request.headers.get("host") or "")
    if host not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and _hostname(origin) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "forbidden origin"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _revalidate_ui_assets(request, call_next):
    """The UI ships with the server: force revalidation so an updated app
    never runs against stale cached scripts (304s keep it cheap)."""
    response = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response

STATIC = Path(__file__).resolve().parent / "static"


# ---------- request models ----------

class UploadBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    dat_text: str = Field(min_length=10, max_length=500_000)


class PolarBody(BaseModel):
    spec: str = Field(min_length=1, max_length=200)
    re: float = Field(gt=1e3, lt=1e9)
    ncrit: float = Field(default=9.0, ge=0.1, le=20)
    model_size: str = "xlarge"
    engine: str = "neuralfoil"           # neuralfoil | xfoil
    alpha_start: float = Field(default=-6.0, ge=-90, le=90)
    alpha_stop: float = Field(default=24.0, ge=-90, le=90)
    alpha_step: float = Field(default=0.5, gt=0.05)


class ConfigBody(BaseModel):
    config: dict


class OptimizeBody(BaseModel):
    config: dict
    options: dict = Field(default_factory=dict)


class ScreenBody(BaseModel):
    re: float = Field(gt=1e3, lt=1e9)
    ncrit: float = Field(default=9.0, ge=0.1, le=20)
    cl_ref: float = Field(default=1.5, ge=-2, le=4)
    thickness_pct_min: float = 0.0
    thickness_pct_max: float = 25.0
    include_low_confidence: bool = False


from typing import Literal


class ExportBody(BaseModel):
    config: dict
    frame: Literal["installed", "design"] = "installed"
    entity: Literal["spline", "polyline"] = "spline"
    include_analysis: bool = True
    include_hitbox: bool = False


class CfdExportBody(BaseModel):
    config: dict
    mesh_size: Literal["coarse", "medium", "fine"] = "medium"


# (name, kind, low, high, low_is_inclusive) for the ANSYS 2D per-run
# overrides. The request bodies enforce these through pydantic Fields; the
# preset library re-checks the same numbers by hand, so a stored preset can
# never carry a value a run would refuse. high None = unbounded above
# (finite is still required).
_ANSYS_OVERRIDE_RANGES = (
    ("edge_size_mm", float, 0.0, None, False),
    ("first_layer_mm", float, 0.0, None, False),
    ("n_layers", int, 0, 100, True),
    ("growth", float, 1.0, 3.0, False),
    ("front_l", float, 0.5, 20.0, True),
    ("back_l", float, 0.5, 40.0, True),
    ("top_h", float, 0.5, 20.0, True),
    ("sc_budget_s", float, 30.0, 7200.0, True),
    ("wb_budget_s", float, 60.0, 43200.0, True),
)
_ANSYS_OVERRIDE_NAMES = tuple(n for n, *_rest in _ANSYS_OVERRIDE_RANGES)


class _AnsysOverrides(BaseModel):
    """The ANSYS 2D knobs a request may override on top of its sizing
    recipe, shared by the verify start and the mesh export.

    null (or absent) means "take the recipe's value" for the mesh knobs
    and the documented default for the domain and the stage budgets. An
    override never changes the sizing label — the result reports the
    values actually used.
    """
    edge_size_mm: float | None = Field(default=None, gt=0,
                                       allow_inf_nan=False)
    first_layer_mm: float | None = Field(default=None, gt=0,
                                         allow_inf_nan=False)
    n_layers: int | None = Field(default=None, ge=0, le=100)
    growth: float | None = Field(default=None, gt=1.0, le=3.0,
                                 allow_inf_nan=False)
    # domain extents in chords: ahead of, behind and above the section
    front_l: float | None = Field(default=None, ge=0.5, le=20,
                                  allow_inf_nan=False)
    back_l: float | None = Field(default=None, ge=0.5, le=40,
                                 allow_inf_nan=False)
    top_h: float | None = Field(default=None, ge=0.5, le=20,
                                allow_inf_nan=False)
    # wall-clock budgets for the two meshing stages
    sc_budget_s: float | None = Field(default=None, ge=30, le=7200,
                                      allow_inf_nan=False)
    wb_budget_s: float | None = Field(default=None, ge=60, le=43200,
                                      allow_inf_nan=False)

    def overrides(self) -> dict:
        """The overrides actually set, ready for the 2D job/chain."""
        return {n: getattr(self, n) for n in _ANSYS_OVERRIDE_NAMES
                if getattr(self, n) is not None}


# iteration defaults, per engine: the drift-stopped engines take a
# generous cap they rarely reach, the ANSYS 2D engine the walkthrough's
# own 500 (the number its settings contract states everywhere else)
RANS_ITERS_DEFAULT = 10000
FLUENT2D_ITERS = 500


class RansStartBody(_AnsysOverrides):
    config: dict
    # openfoam/fluent take the studio mesh presets; for the fluent2d
    # engine this field is a SIZING mode (default | studio-yplus1). The
    # Literal admits every engine's values, so the pairing is
    # cross-checked in rans_start
    mesh_size: Literal["coarse", "medium", "fine",
                       "default", "studio-yplus1"] = "coarse"
    # null = the engine's own default, resolved in rans_start: 10000 for
    # the drift-stopped engines (generous — they stop themselves the
    # moment the force history flattens), 500 for the ANSYS 2D engine,
    # the documented walkthrough length its settings contract states
    # everywhere else. The floor is engine-dependent too: fluent2d admits
    # the 50-iteration runs that replicate the manual walkthrough, the
    # other engines keep 100 — cross-checked in rans_start alongside the
    # mesh/engine pairing
    max_iters: int | None = Field(default=None, ge=50, le=20000)
    # MPI ranks for the solve — an explicit opt-in; 1 (the default) is the
    # serial case this endpoint has always produced
    n_ranks: int = Field(default=1, ge=1, le=32)
    # openfoam (default): the Docker screening engine. fluent: a licensed
    # local ANSYS Fluent on the 3D slab (the documented revert path).
    # fluent2d: the true-2D ANSYS workflow (Workbench-meshed)
    engine: Literal["openfoam", "fluent", "fluent2d"] = "openfoam"
    # Fluent slab engine only: who cuts the cells. fluent (default) = ANSYS
    # Fluent Meshing on the identical section geometry (no Docker);
    # gmsh = the studio mesher + containerized conversion — the
    # identical-mesh cross-check against the OpenFOAM engine
    mesher: Literal["fluent", "gmsh"] = "fluent"
    # fluent2d only: default = the documented manual ANSYS workflow's
    # solver defaults (residual auto-stop live, Fluent-native
    # references); studio = the app's conventions (SST pinned,
    # force-drift stop, chord-referenced downforce-positive display).
    # Other engines ignore it.
    conventions: Literal["default", "studio"] = "default"


def _num_in(field: str, value, kind, lo, hi, lo_inclusive: bool):
    """One settings number, range-checked the way its pydantic Field is.

    bool is rejected explicitly — it passes isinstance(x, int), and
    True would otherwise sail through an n_layers check as 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if kind is int:
        if isinstance(value, float) and (not math.isfinite(value)
                                         or value != int(value)):
            raise ValueError(f"{field} must be a whole number")
        value = int(value)
    else:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{field} must be a finite number")
    if (value < lo if lo_inclusive else value <= lo) or (
            hi is not None and value > hi):
        raise ValueError(
            f"{field} must be {'>=' if lo_inclusive else '>'} {lo:g}"
            + (f" and <= {hi:g}" if hi is not None else ""))
    return value


def _validate_ansys_settings(raw) -> dict:
    """The ANSYS 2D settings object, normalized.

    The base recipe (sizing, conventions, iterations, ranks) plus the
    per-run overrides, each inside the range a run accepts. Unknown keys
    are dropped — a preset's name lives beside its settings, never
    inside — and an absent override stays null so it resolves against
    the sizing recipe at run time instead of freezing a number here."""
    if not isinstance(raw, dict):
        raise ValueError("settings must be an object")

    def _default(key, fallback):
        # null and absent both mean "the documented default" for EVERY
        # key — and only those two: a falsy WRONG value ("" or 0) still
        # answers its own refusal instead of being coerced into it
        v = raw.get(key)
        return fallback if v is None else v

    sizing = _default("sizing", "default")
    if sizing not in ("default", "studio-yplus1"):
        raise ValueError(f"sizing must be 'default' or 'studio-yplus1', "
                         f"not {sizing!r}")
    conventions = _default("conventions", "default")
    if conventions not in ("default", "studio"):
        raise ValueError(f"conventions must be 'default' or 'studio', "
                         f"not {conventions!r}")
    out = {
        "sizing": sizing,
        "conventions": conventions,
        "n_iters": _num_in("n_iters", _default("n_iters", FLUENT2D_ITERS),
                           int, 50, 20000, True),
        "n_ranks": _num_in("n_ranks", _default("n_ranks", 1),
                           int, 1, 32, True),
    }
    for name, kind, lo, hi, lo_inclusive in _ANSYS_OVERRIDE_RANGES:
        v = raw.get(name)
        out[name] = (None if v is None
                     else _num_in(name, v, kind, lo, hi, lo_inclusive))
    return out


def _err_detail(e: BaseException) -> str:
    # KeyError stringifies with quotes around its message — strip them
    msg = str(e.args[0]) if (isinstance(e, KeyError) and e.args) else str(e)
    return _scrub_paths(msg)


# machine paths must not ride out in an error body: an OSError from deep in
# the run tree carries the whole home directory, and these strings end up in
# screenshots and pasted issue reports. The run directory is the app's own
# scratch, so its absolute location tells a user nothing they can act on.
_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/]|\\\\|/(?:home|Users|mnt)/)[^\s'"()]*""")


def _scrub_paths(msg: str) -> str:
    """Replace absolute filesystem paths with their final component."""
    def keep_tail(m: re.Match) -> str:
        tail = re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1]
        return tail or "a file"
    return _PATH_RE.sub(keep_tail, msg)


def _finite_safe(obj):
    """A validation-error payload with every non-finite float stringified.

    JSON's Infinity/NaN literals parse (Python's decoder accepts them) but
    do NOT encode — the response encoder refuses them. A rejected field
    echoes its input back in the 422 detail, so a body carrying Infinity
    would fail on the way out and become a 500."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return repr(obj)
    if isinstance(obj, dict):
        return {k: _finite_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite_safe(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request, exc):
    from fastapi.encoders import jsonable_encoder
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=422,
        content={"detail": _finite_safe(jsonable_encoder(exc.errors()))})


def _cfg(config: dict) -> StackConfig:
    try:
        return StackConfig.from_dict(config)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, detail=_err_detail(e))


# ---------- airfoils ----------

@app.get("/api/airfoils")
def list_airfoils(q: str = "", limit: int = 60):
    return {"library": airfoils.search_library(q, min(limit, 500)),
            "custom": airfoils.list_custom()}


@app.get("/api/airfoil")
def airfoil_geometry(spec: str):
    try:
        name, raw = airfoils.resolve(spec)
        coords = airfoils.normalize(raw)
    except (KeyError, ValueError) as e:
        raise HTTPException(404, detail=str(e))
    return {"spec": spec, "name": name,
            "coords": np.round(coords, 6).tolist(),
            "info": airfoils.geometry_info(coords)}


@app.post("/api/airfoils/upload")
def upload_airfoil(body: UploadBody):
    try:
        # dat_text is untrusted request content — never let a single-line
        # ".dat"-looking value be interpreted as a filesystem path to read
        name, coords = airfoils.read_dat(body.dat_text, body.name,
                                         allow_path=False)
        coords = airfoils.normalize(coords)
        if len(coords) > 800:
            raise ValueError(f"{len(coords)} points — limit is 800")
        spec = airfoils.register_custom(name, coords)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
    # report the stored display name — it may have been disambiguated when a
    # same-named upload with different geometry already exists
    stored = next((c["name"] for c in airfoils.list_custom()
                   if c["spec"] == spec), name)
    return {"spec": spec, "name": stored,
            "info": airfoils.geometry_info(coords)}


# ---------- polars ----------

@app.post("/api/polar")
def polar(body: PolarBody):
    if body.alpha_stop <= body.alpha_start:
        raise HTTPException(422, detail="alpha_stop must exceed alpha_start")
    alphas = (body.alpha_start, body.alpha_stop, body.alpha_step)
    try:
        if body.engine == "xfoil":
            p = viscous.xfoil_polar(body.spec, body.re, body.ncrit,
                                    (body.alpha_start, body.alpha_stop,
                                     max(body.alpha_step, 0.5)))
            out = {k: np.asarray(v).tolist() for k, v in p.items()}
            out["engine"] = "xfoil"
            return out
        p = viscous.polar(body.spec, body.re, body.ncrit, body.model_size,
                          alphas)
    except FileNotFoundError:
        raise HTTPException(424, detail="xfoil.exe not found — download it "
                                        "from the official XFOIL page and "
                                        "place it in the xfoil/ folder (see "
                                        "README), or use the NeuralFoil "
                                        "engine")
    except (KeyError, ValueError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    except Exception as e:
        raise HTTPException(500, detail=f"polar failed: {e}")
    return {"engine": "neuralfoil",
            **{k: np.asarray(v).tolist() for k, v in p.items()},
            "metrics": viscous.polar_metrics(p)}


# ---------- geometry / analysis ----------

@app.post("/api/geometry")
def geometry_only(body: ConfigBody):
    cfg = _cfg(body.config)
    try:
        return geometry.geometry_report(cfg)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, detail=_err_detail(e))


@app.post("/api/analyze")
def analyze(body: ConfigBody):
    cfg = _cfg(body.config)
    try:
        return analysis.analyze(cfg)
    except (ValueError, KeyError) as e:
        # KeyError: an element airfoil spec that no longer resolves (e.g. a
        # custom upload lost to a restart) — a client-fixable condition
        raise HTTPException(422, detail=_err_detail(e))
    except np.linalg.LinAlgError:
        raise HTTPException(422, detail="panel system is singular — geometry "
                                        "may be self-intersecting or touching "
                                        "the ground")


class SweepBody(BaseModel):
    config: dict
    variable: str
    values: list[float] = Field(min_length=2, max_length=40)


@app.post("/api/sweep")
def sweep(body: SweepBody):
    """Operating map: the design evaluated across ride height or speed."""
    if body.variable not in analysis.SWEEP_VARIABLES:
        raise HTTPException(422, detail=f"variable must be one of "
                                        f"{list(analysis.SWEEP_VARIABLES)}")
    cfg = _cfg(body.config)
    try:
        return analysis.sweep(cfg, body.variable, body.values)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    except np.linalg.LinAlgError:
        raise HTTPException(422, detail="panel system is singular — geometry "
                                        "may be self-intersecting or touching "
                                        "the ground")


# ---------- optimizer ----------

@app.post("/api/optimize")
def optimize_start(body: OptimizeBody):
    _cfg(body.config)  # validate before spawning the job
    try:
        job_id = optimizer.start(body.config, body.options)
    except RuntimeError as e:
        # a search is already running — the same refusal shape as the RANS
        # start path, so the client can tell "busy" from "bad request"
        raise HTTPException(409, detail=str(e))
    except (ValueError, TypeError, KeyError) as e:
        # malformed options (wrong-typed bounds, bogus keys) are client
        # errors, whatever exception type they surface as
        raise HTTPException(422, detail=str(e))
    return {"job_id": job_id}


@app.get("/api/optimize/current")
def optimize_current():
    """Active/most-recent optimizer job id — a reloaded page re-attaches
    through this instead of orphaning a CPU-bound search it can no longer
    see or cancel. Declared before /api/optimize/{job_id}."""
    return optimizer.current()


@app.get("/api/optimize/{job_id}")
def optimize_status(job_id: str):
    job = optimizer.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    return job.snapshot()


@app.post("/api/optimize/{job_id}/cancel")
def optimize_cancel(job_id: str):
    job = optimizer.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    job.cancel()
    return {"ok": True}


# ---------- RANS verification (Docker) ----------
#
# Opt-in truth runs: nothing here executes unless the user clicks Verify.
# The routes with literal paths must be declared before /api/rans/{job_id}.

@app.get("/api/rans/availability")
def rans_availability(refresh: bool = False):
    from .core import cfd_run
    return cfd_run.availability(refresh)


@app.get("/api/rans/current")
def rans_current():
    """Active/most-recent job id — a reloaded page re-attaches through this."""
    from .core import cfd_run
    return cfd_run.current()


def _fluent2d_workflow():
    """scripts.fluent2d_workflow, loaded lazily off the project root —
    the Workbench chain and its templates load only when the 2D engine
    or its mesh export is actually used (same lazy pattern as
    fluent_run's scripts imports)."""
    import importlib
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    return importlib.import_module("scripts.fluent2d_workflow")


@app.get("/api/fluent2d/availability")
def fluent2d_availability():
    """Can the ANSYS 2D workflow run here? Both halves are required:
    the Workbench meshing chain (SpaceClaim + Mechanical) and a
    licensed Fluent for the solve."""
    from .core import fluent_run
    fl = fluent_run.availability()
    try:
        wb = _fluent2d_workflow().availability()
    except Exception:
        # a missing/broken scripts module reads as unavailable, never a 500
        wb = {"available": False, "awp_root": "", "version": "",
              "detail": "the ANSYS 2D meshing tools are not installed "
                        "with this app"}
    ok = bool(fl.get("available")) and bool(wb.get("available"))
    detail = "" if ok else "; ".join(
        d for d in (wb.get("detail"), fl.get("detail")) if d)
    return {"available": ok, "detail": detail,
            "fluent": fl, "workbench": wb}


# single-run and queue starts guard each other through two different module
# locks (rans_queue._lock vs cfd_run's job lock), so both guard+start
# sequences must serialize here — otherwise two concurrent starts can each
# pass its own check and the loser aborts the whole shortlist verification
_rans_start_lock = threading.Lock()


@app.post("/api/rans/start")
def rans_start(body: RansStartBody):
    _cfg(body.config)   # validate before spawning the job
    # cross-field engine/mesh validation: a mesh preset handed to the
    # wrong engine must answer an actionable 422 here, not a deep
    # ValueError out of a job constructor
    if body.engine == "fluent2d":
        if body.mesh_size not in ("default", "studio-yplus1"):
            raise HTTPException(422, detail=(
                f"the Fluent 2D engine takes sizing 'default' or "
                f"'studio-yplus1' — {body.mesh_size!r} is a mesh preset "
                f"for the OpenFOAM and Fluent engines"))
    elif body.mesh_size not in ("coarse", "medium", "fine"):
        raise HTTPException(422, detail=(
            f"{body.mesh_size!r} is a Fluent 2D sizing mode — the "
            f"{body.engine} engine takes mesh 'coarse', 'medium' or "
            f"'fine'"))
    max_iters = (body.max_iters if body.max_iters is not None
                 else (FLUENT2D_ITERS if body.engine == "fluent2d"
                       else RANS_ITERS_DEFAULT))
    if body.engine != "fluent2d" and max_iters < 100:
        raise HTTPException(422, detail=(
            f"the {body.engine} engine needs at least 100 iterations — "
            f"values down to 50 are only for the Fluent 2D runs that "
            f"replicate the manual walkthrough"))
    # the mesh/domain/budget overrides steer the ANSYS 2D chain only:
    # accepting one silently on another engine would report a sizing the
    # solve never used
    settings = body.overrides()
    if settings and body.engine != "fluent2d":
        raise HTTPException(422, detail=(
            f"the {body.engine} engine takes no ANSYS 2D overrides "
            f"({', '.join(sorted(settings))}) — drop them, or set "
            f"engine 'fluent2d' to use them"))
    from .core import cfd_run, rans_queue
    with _rans_start_lock:
        # the guard must be two-directional: a single run started in the gap
        # between two queue items would make the queue's next start fail and
        # abort the whole shortlist verification
        q = rans_queue.get_current()
        if q is not None and q.state in ("pending", "running"):
            raise HTTPException(409, detail="a shortlist verification queue "
                                            "is running — cancel it or wait "
                                            "for it to finish")
        try:
            job_id = cfd_run.start(body.config, body.mesh_size,
                                   max_iters, body.n_ranks,
                                   body.engine, body.mesher,
                                   body.conventions, settings=settings)
        except RuntimeError as e:
            raise HTTPException(409, detail=str(e))
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422, detail=_err_detail(e))
    return {"job_id": job_id}


@app.get("/api/rans/{job_id}")
def rans_status(job_id: str):
    from .core import cfd_run
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    return job.snapshot()


@app.post("/api/rans/{job_id}/cancel")
def rans_cancel(job_id: str):
    from .core import cfd_run
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    job.cancel()
    return {"ok": True}


class RansQueueBody(BaseModel):
    items: list[dict]
    mesh_size: Literal["coarse", "medium", "fine"] = "medium"
    max_iters: int = Field(default=10000, ge=100, le=20000)
    # opt-in parallelism: defaults reproduce the sequential serial queue
    # exactly. n_ranks x max_concurrent is admission-checked against the
    # machine's core budget (422 when over).
    n_ranks: int = Field(default=1, ge=1, le=32)
    max_concurrent: int = Field(default=1, ge=1, le=8)


@app.post("/api/rans-queue/start")
def rans_queue_start(body: RansQueueBody):
    """Verify an optimizer shortlist with RANS and re-rank by measured
    downforce. One queue at a time; shares the single-run solver guard.
    Sequential serial solves by default — concurrency is an explicit
    opt-in via n_ranks/max_concurrent."""
    from .core import rans_queue
    try:
        with _rans_start_lock:
            qid = rans_queue.start(body.items, body.mesh_size,
                                   body.max_iters,
                                   n_ranks=body.n_ranks,
                                   max_concurrent=body.max_concurrent)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    except RuntimeError as e:
        raise HTTPException(409, detail=str(e))
    return {"queue_id": qid}


@app.get("/api/rans-queue/current")
def rans_queue_current():
    from .core import rans_queue
    q = rans_queue.get_current()
    return {"queue": q.snapshot() if q is not None else None}


@app.post("/api/rans-queue/cancel")
def rans_queue_cancel():
    from .core import rans_queue
    q = rans_queue.get_current()
    if q is None:
        raise HTTPException(404, detail="no verification queue")
    q.cancel()
    return {"ok": True}


@app.post("/api/rans/{job_id}/stop")
def rans_stop(job_id: str):
    """Stop-and-keep-fields: graceful writeNow stop so the velocity and
    pressure fields of the partial run stay viewable. Distinct from
    cancel, which hard-kills the container and keeps nothing."""
    from .core import cfd_run
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if not job.stop_graceful():
        # the refusal reasons differ per engine: Fluent (slab and 2D)
        # writes fields only at the end, so there is never a partial
        # state worth keeping — "not running yet" would be a lie mid-solve
        if job.snapshot().get("engine") in ("fluent", "fluent2d",
                                            "polish"):
            raise HTTPException(409, detail="the Fluent engine writes "
                                            "fields only at the end — "
                                            "there is no keep-fields "
                                            "stop; use Cancel instead")
        raise HTTPException(409, detail="the solver is not running yet — "
                                        "there are no fields to keep; use "
                                        "Cancel instead")
    return {"ok": True}


@app.get("/api/rans/{job_id}/flow")
def rans_flow(job_id: str, field: str = "umag", theme: str = "dark",
              cmap: str = "auto", vmin: float | None = None,
              vmax: float | None = None, streamlines: bool = True,
              extent: str = "section"):
    """The solved section flow as a PNG — rendered per view-settings
    combination, cached in the case directory. theme/cmap/vmin/vmax/
    streamlines are the contour-dialog knobs the manual GUI offers."""
    from .core import cfd_run, foam_post
    if field not in ("umag", "cp"):
        raise HTTPException(422, detail="field must be 'umag' or 'cp'")
    if theme not in ("dark", "light"):
        raise HTTPException(422, detail="theme must be 'dark' or 'light'")
    if cmap != "auto" and cmap not in foam_post.FLOW_CMAPS:
        raise HTTPException(
            422, detail=f"cmap must be auto or one of "
                        f"{foam_post.FLOW_CMAPS}")
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if job.state != "done":
        raise HTTPException(409, detail="the run has not finished")
    if extent not in ("section", "domain"):
        raise HTTPException(422, detail="extent must be 'section' or "
                                        "'domain'")
    key = (f"{field}_{theme}_{cmap}_{vmin if vmin is not None else 'a'}_"
           f"{vmax if vmax is not None else 'a'}_{int(streamlines)}_"
           f"{extent}").replace(".", "p").replace("-", "m")
    png = (job.case_dir / f"flow_{field}.png"
           if (theme, cmap, vmin, vmax, streamlines, extent)
           == ("dark", "auto", None, None, True, "section")
           else job.case_dir / f"flow_{key}.png")
    if png.is_file():
        return Response(content=png.read_bytes(), media_type="image/png")
    try:
        data = foam_post.flow_png(
            job.case_dir, job.cfg, field, theme=theme,
            cmap=None if cmap == "auto" else cmap,
            vmin=vmin, vmax=vmax, streamlines=streamlines,
            extent=extent)
    except foam_post.PostError as e:
        raise HTTPException(422, detail=str(e))
    except (OSError, ValueError, KeyError) as e:
        # a deleted case dir, a malformed field file — client-visible
        # conditions, not server faults
        raise HTTPException(422, detail=f"flow rendering failed: {e}")
    # publish atomically: a concurrent request must never read a torn file
    # (renders are serialized inside foam_post; both writers produce the
    # same image, so last-write-wins is fine)
    import os as _os
    tmp = png.with_name(f".{png.name}.{_os.getpid()}.{threading.get_ident()}")
    try:
        tmp.write_bytes(data)
        _os.replace(tmp, png)
    except OSError:
        tmp.unlink(missing_ok=True)
    return Response(content=data, media_type="image/png")


@app.get("/api/rans/{job_id}/flowfield")
def rans_flowfield(job_id: str, nx: int = 320,
                   extent: str = "section",
                   x0: float | None = None, y0: float | None = None,
                   x1: float | None = None, y1: float | None = None,
                   fields: str = "umag"):
    """The solved velocity field as JSON for the animated flow view —
    gridded, interior-masked, cached beside the case like the PNG.
    extent: "section" (working window) or "domain" (the whole box).
    x0/y0/x1/y1 (all four, meters) grid an arbitrary window instead —
    the animated view's level-of-detail path, so a zoomed-in region is
    re-sampled at full grid resolution. fields="umag,cp" adds the Cp
    field on the same grid (the animation's pressure backdrop)."""
    from .core import cfd_run, foam_post
    nx = max(120, min(int(nx), 640))
    if extent not in ("section", "domain"):
        raise HTTPException(422, detail="extent must be 'section' or "
                                        "'domain'")
    want = {f.strip() for f in fields.split(",") if f.strip()}
    if not want or not want <= {"umag", "cp"}:
        raise HTTPException(422, detail="fields must be a comma list "
                                        "from: umag, cp")
    include_cp = "cp" in want
    window = None
    coords = (x0, y0, x1, y1)
    if any(v is not None for v in coords):
        if any(v is None for v in coords):
            raise HTTPException(422, detail="a window needs all four of "
                                            "x0, y0, x1, y1")
        if not (x1 > x0 and y1 > y0):
            raise HTTPException(422, detail="window must have positive "
                                            "spans")
        window = (x0, y0, x1, y1)
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if job.state != "done":
        raise HTTPException(409, detail="the run has not finished")
    cache = job.case_dir / (f"flow_field_{nx}_{extent}"
                            + ("_cp" if include_cp else "") + ".json")
    if window is None and cache.is_file():
        return Response(content=cache.read_bytes(),
                        media_type="application/json")
    try:
        data = foam_post.flow_field_json(job.case_dir, job.cfg, nx,
                                         extent, window,
                                         include_cp=include_cp)
    except foam_post.PostError as e:
        raise HTTPException(422, detail=str(e))
    except (OSError, ValueError, KeyError) as e:
        raise HTTPException(422, detail=f"flow field extraction failed: {e}")
    import json as _json
    # allow_nan=False: a NaN that slips past the extractor's masking must
    # fail loudly here, not reach the browser as invalid JSON
    payload = _json.dumps(data, separators=(",", ":"),
                          allow_nan=False).encode()
    # publish atomically, same as the PNG cache above; windowed
    # (level-of-detail) responses are arbitrary boxes and are not cached
    if window is None:
        import os as _os
        tmp = cache.with_name(
            f".{cache.name}.{_os.getpid()}.{threading.get_ident()}")
        try:
            tmp.write_bytes(payload)
            _os.replace(tmp, cache)
        except OSError:
            tmp.unlink(missing_ok=True)
    return Response(content=payload, media_type="application/json")


@app.post("/api/rans/{job_id}/export/fluent")
def rans_export_fluent(job_id: str):
    """Copy a finished Fluent run's solved case into exports/ so it can
    be opened in ANSYS (Fluent: File > Read > Case & Data; CFD-Post and
    ParaView read the .h5 pair too). The run directory keeps its own
    copy; this creates the durable, revealable one under exports/."""
    import shutil
    import time as _time

    from .core import cfd_run
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    # state gate BEFORE the snapshot: a snapshot taken while the job is
    # still running carries result=None, and a running->done flip between
    # the two reads would export a README full of placeholders
    if job.state == "failed":
        raise HTTPException(409, detail="the run failed — there is no "
                                        "solved case to export")
    if job.state == "cancelled":
        raise HTTPException(409, detail="the run was cancelled — there "
                                        "is no solved case to export")
    if job.state != "done":
        raise HTTPException(409, detail="the run has not finished")
    snap = job.snapshot()
    if snap.get("engine") not in ("fluent", "fluent2d", "polish"):
        raise HTTPException(422, detail="this run was solved by the "
                                        "OpenFOAM engine — the ANSYS "
                                        "export applies to Fluent runs")
    cas = job.case_dir / "case.cas.h5"
    dat = job.case_dir / "case.dat.h5"
    if not cas.is_file():
        raise HTTPException(422, detail="this run carries no "
                                        "case.cas.h5 — the case write "
                                        "failed (or predates the ANSYS "
                                        "export); re-run the "
                                        "verification")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest = EXPORTS_DIR / f"fluent_case_{stamp}"
    k = 2
    # mkdir is the claim — same two-writers guard as the OpenFOAM case
    # export above
    while True:
        try:
            dest.mkdir()
            break
        except FileExistsError:
            dest = EXPORTS_DIR / f"fluent_case_{stamp}-{k}"
            k += 1
    try:
        files = []
        for src in (cas, dat, job.case_dir / "config.json"):
            if src.is_file():
                shutil.copy2(src, dest / src.name)
                files.append(src.name)
        r = snap.get("result") or {}
        mesh = snap.get("mesh") or {}
        if snap.get("engine") == "fluent2d":
            # the 2D engine's conventions are the run's own choice —
            # report what was actually solved, never the slab recipe
            conv_block = (
                "Conventions (as solved - ANSYS Fluent 2D, "
                f"{r.get('conventions', 'default')} conventions):\n"
                + (f"  - {r['ref_note']}\n" if r.get("ref_note") else "")
                + "  - true-2D case meshed by the ANSYS Workbench "
                "chain\n\n")
        elif snap.get("engine") == "polish":
            conv_block = (
                "Conventions (adjoint polish - gradient-based "
                "shape-opt):\n"
                "  - the seed verification run's 2D mesh, MORPHED by "
                "Fluent's optimizer\n"
                "  - solved state is the delivered polished shape; "
                "numbers on the morphed\n"
                "    mesh are provisional until the re-verification "
                "on a fresh mesh\n\n")
        else:
            conv_block = (
                "Conventions (as solved - the studio recipe):\n"
                "  - reported lift is DOWNFORCE-POSITIVE (force vector "
                "(0,-1,0))\n"
                "  - coefficients are chord-referenced (area = chord x "
                "slab depth)\n"
                "  - residual auto-stop was DISABLED; convergence was "
                "judged on\n"
                "    the force-history drift criterion\n"
                "  - one-cell 2D slab solved in 3D with symmetry "
                "z-planes\n\n")
        (dest / "README.txt").write_text(
            "Wing Section Studio - ANSYS Fluent case export\n"
            "==============================================\n\n"
            "Open in Fluent (2024 R2+): File > Read > Case & Data,\n"
            "pick case.cas.h5 (case.dat.h5 loads with it). CFD-Post,\n"
            "EnSight and ParaView read the pair as well.\n\n"
            + conv_block +
            f"Run summary: mesh {snap.get('mesh_size', '?')} "
            f"({mesh.get('n_cells', '?')} cells), "
            f"{r.get('n_iters_run', '?')} iterations, "
            f"Cl {r.get('cl_rans', '?')}, Cd {r.get('cd_rans', '?')}, "
            f"converged: {r.get('converged', '?')}\n"
            f"Exported {_time.strftime('%Y-%m-%d %H:%M:%S')} from run "
            f"{job.id}\n",
            encoding="utf-8")
        files.append("README.txt")
    except BaseException:
        # never leave a half-copied export folder looking complete
        shutil.rmtree(dest, ignore_errors=True)
        raise
    total = sum((dest / f).stat().st_size for f in files)
    return {"filename": dest.name, "path": str(dest),
            "dir": str(EXPORTS_DIR), "files": files,
            "size_bytes": total, "data_included": dat.is_file()}


# ---------- adjoint polish (final stage) ----------

class PolishStartBody(BaseModel):
    """Knobs for the final-stage adjoint polish. run_id names the
    FINISHED ANSYS 2D verification run whose solved case seeds the
    polish; the ranges mirror adjoint_run.resolve_options exactly, so a
    request the job would refuse is refused here with a field name."""
    run_id: str
    drag_exchange_k: float = Field(default=0.25, ge=0.0, le=10.0,
                                   allow_inf_nan=False)
    design_iters: int = Field(default=12, ge=1, le=60)
    flow_iters: int = Field(default=300, ge=100, le=5000)
    adjoint_iters: int = Field(default=250, ge=50, le=5000)
    margin_pct: float = Field(default=6.0, ge=1.0, le=15.0,
                              allow_inf_nan=False)
    settle_iters: int = Field(default=200, ge=50, le=2000)


@app.post("/api/polish/start")
def polish_start(body: PolishStartBody):
    """Start the final-stage adjoint polish: Fluent's gradient-based
    shape optimizer on a finished 2D verification run's solved case,
    objective downforce - k*drag, morph bounded by the rule envelope.
    Registers in the same cross-engine registry as every solver job, so
    status/cancel ride the /api/rans/{job_id} endpoints."""
    from .core import adjoint_run, cfd_run, rans_queue
    seed = cfd_run.get(body.run_id)
    if seed is None:
        raise HTTPException(404, detail=(
            "unknown run — the polish seeds from an ANSYS 2D "
            "verification run of this app session; run one first"))
    with _rans_start_lock:
        q = rans_queue.get_current()
        if q is not None and q.state in ("pending", "running"):
            raise HTTPException(409, detail="a shortlist verification "
                                            "queue is running — cancel it "
                                            "or wait for it to finish")
        try:
            job = adjoint_run.AdjointPolishJob(seed, {
                "drag_exchange_k": body.drag_exchange_k,
                "design_iters": body.design_iters,
                "flow_iters": body.flow_iters,
                "adjoint_iters": body.adjoint_iters,
                "margin_pct": body.margin_pct,
                "settle_iters": body.settle_iters,
            })
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422, detail=_err_detail(e))
        try:
            job_id = cfd_run.admit_and_launch(job)
        except RuntimeError as e:
            raise HTTPException(409, detail=str(e))
    return {"job_id": job_id}


def _polish_profiles(job) -> list:
    """The delivered polished polylines of a done polish job, or an
    HTTP-shaped refusal naming exactly what is missing."""
    if job.state != "done":
        raise HTTPException(409, detail="the polish has not finished")
    r = job.result or {}
    art = r.get("artifacts") or {}
    if not r.get("improved") or not art.get("profiles_json"):
        raise HTTPException(409, detail=(
            "the polish found no compliant improvement — there is no "
            "polished shape to use"))
    path = Path(job.case_dir) / art["profiles_json"]
    if not path.is_file():
        raise HTTPException(409, detail=(
            "the polished profiles were pruned from disk — re-run the "
            "polish"))
    import json as _json
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        polys = data["elements"]
    except (OSError, ValueError, KeyError) as e:
        raise HTTPException(422, detail=f"polished profiles unreadable: "
                                        f"{e}")
    if not polys:
        raise HTTPException(422, detail="polished profiles are empty")
    return polys


@app.post("/api/polish/{job_id}/reverify")
def polish_reverify(job_id: str):
    """Re-verify the polished shape on a FRESH mesh through the standard
    ANSYS 2D chain (studio conventions): the honest measure of the
    polish gain, free of morphed-mesh quality effects. The run carries
    profiles_override, so the panel comparison and the calibration
    harvest are disabled on it."""
    from .core import cfd_run, fluent2d_run, rans_queue
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if job.snapshot().get("engine") != "polish":
        raise HTTPException(422, detail="this run is not an adjoint "
                                        "polish")
    polys = _polish_profiles(job)
    # the seed's resolved mesh recipe, replayed as overrides (requested
    # values where the chain capped them — it re-caps against the
    # polished clearances itself); conventions are ALWAYS studio for a
    # re-verify — the drift doctrine judges convergence
    seed_settings = dict(getattr(job, "seed_settings", {}) or {})
    overrides = {}
    for name in _ANSYS_OVERRIDE_NAMES:
        v = seed_settings.get(f"{name}_requested",
                              seed_settings.get(name))
        if v is not None:
            overrides[name] = v
    sizing = seed_settings.get("sizing") or "default"
    n_iters = int(seed_settings.get("n_iters") or FLUENT2D_ITERS)
    with _rans_start_lock:
        q = rans_queue.get_current()
        if q is not None and q.state in ("pending", "running"):
            raise HTTPException(409, detail="a shortlist verification "
                                            "queue is running — cancel it "
                                            "or wait for it to finish")
        try:
            rjob = fluent2d_run.Fluent2DJob(
                job.config, sizing, n_iters, job.n_ranks,
                "studio", settings=overrides,
                profiles_override=polys)
        except (ValueError, TypeError, KeyError) as e:
            raise HTTPException(422, detail=_err_detail(e))
        try:
            rid = cfd_run.admit_and_launch(rjob)
        except RuntimeError as e:
            raise HTTPException(409, detail=str(e))
    return {"job_id": rid, "polish_id": job_id}


@app.post("/api/polish/{job_id}/export/dxf")
def polish_export_dxf(job_id: str):
    """The polished profiles as a true-scale DXF in the exports folder —
    exact closed polylines per element (never smoothing splines), ground
    line included."""
    import time as _time

    from .core import cfd_run
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if job.snapshot().get("engine") != "polish":
        raise HTTPException(422, detail="this run is not an adjoint "
                                        "polish")
    polys = _polish_profiles(job)
    try:
        data = export.polished_dxf_bytes(polys)
    except Exception as e:
        raise HTTPException(422, detail=f"DXF generation failed: "
                                        f"{_err_detail(e)}")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest = EXPORTS_DIR / f"polished_section_{stamp}.dxf"
    k = 2
    while dest.exists():
        dest = EXPORTS_DIR / f"polished_section_{stamp}-{k}.dxf"
        k += 1
    dest.write_bytes(data)
    return {"filename": dest.name, "path": str(dest),
            "dir": str(EXPORTS_DIR), "size_bytes": len(data)}


# ---------- screener ----------

@app.post("/api/screen")
def screen(body: ScreenBody):
    rows = screener.screen(body.re, body.ncrit, body.cl_ref,
                           thickness_pct_max=body.thickness_pct_max,
                           thickness_pct_min=body.thickness_pct_min,
                           include_low_confidence=body.include_low_confidence)
    return {"rows": rows, "count": len(rows)}


# ---------- export ----------

_EXPORT_TYPES = {
    "dxf": ("application/dxf", "dxf"),
    "svg": ("image/svg+xml", "svg"),
    "csv": ("text/csv", "csv"),
    "zip": ("application/zip", "zip"),
}

# WSS_EXPORTS_DIR override: test servers must not drop files into the real
# exports folder (same isolation contract as WSS_DATA_DIR for the session)
import os as _os_env

EXPORTS_DIR = Path(_os_env.environ.get(
    "WSS_EXPORTS_DIR", Path(__file__).resolve().parents[1] / "exports"))


def _export_bytes(fmt: str, body: ExportBody) -> tuple[bytes, str, str]:
    """(data, media_type, extension) for an export request."""
    if fmt not in _EXPORT_TYPES:
        raise HTTPException(404, detail=f"format {fmt!r} not supported")
    cfg = _cfg(body.config)
    media, ext = _EXPORT_TYPES[fmt]
    try:
        if fmt == "dxf":
            data = export.dxf_bytes(cfg, body.frame, body.entity,
                                    include_hitbox=body.include_hitbox)
        elif fmt == "svg":
            data = export.svg_bytes(cfg, body.frame)
        elif fmt == "csv":
            data = export.csv_text(cfg, body.frame).encode()
        else:
            result = None
            if body.include_analysis:
                try:
                    result = analysis.analyze(cfg, include_geometry=False)
                except Exception:
                    result = None
            data = export.zip_bundle(cfg, result, body.entity,
                                     include_hitbox=body.include_hitbox)
    except (ValueError, KeyError, np.linalg.LinAlgError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    return data, media, ext


class RevealBody(BaseModel):
    path: str


@app.post("/api/export/reveal")   # must be declared before /api/export/{fmt}
def export_reveal(body: RevealBody):
    """Open the system file manager with the exported file selected.

    Restricted to files inside the exports folder."""
    import subprocess
    # resolve() itself rejects a malformed path (an embedded NUL raises
    # ValueError on Windows) — that is a bad request, not a server fault
    try:
        p = Path(body.path).resolve()
    except (ValueError, OSError):
        raise HTTPException(422, detail="that is not a usable file path")
    try:
        p.relative_to(EXPORTS_DIR.resolve())
    except ValueError:
        raise HTTPException(403, detail="path is outside the exports folder")
    if not p.exists():
        raise HTTPException(404, detail="file not found — was it moved?")
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(p)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent)])
    return {"ok": True}


@app.post("/api/export/cfd/save")   # before /api/export/{fmt}/save
def export_cfd_save(body: CfdExportBody):
    """Generate a ready-to-run OpenFOAM 2D RANS case under exports/.

    gmsh meshing runs behind cfd's module lock — the library is global
    C state and calls must not overlap."""
    import time as _time

    from .core import cfd
    cfg = _cfg(body.config)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    case = EXPORTS_DIR / f"cfd_case_{stamp}"
    k = 2
    # mkdir is the claim: an exists() probe would let two same-second saves
    # pick one directory and interleave their writes into it
    while True:
        try:
            case.mkdir()
            break
        except FileExistsError:
            case = EXPORTS_DIR / f"cfd_case_{stamp}-{k}"
            k += 1
    try:
        summary = cfd.build_case(cfg, case, body.mesh_size)
    except (ValueError, KeyError, cfd.MeshError) as e:
        import shutil
        shutil.rmtree(case, ignore_errors=True)   # no half-written cases
        detail = _err_detail(e)
        if isinstance(e, cfd.MeshError):
            detail = f"mesh generation failed: {detail}"
        raise HTTPException(422, detail=detail)
    except BaseException:
        # OSError, RuntimeError, ... still surface (as a 500) — but a case
        # folder missing half its files must not be left looking exportable
        import shutil
        shutil.rmtree(case, ignore_errors=True)
        raise
    return {"filename": case.name, "path": str(case),
            "dir": str(EXPORTS_DIR), "summary": summary}


@app.post("/api/export/fluent-mesh/save")   # before /api/export/{fmt}
def export_fluent_mesh_save(body: CfdExportBody):
    """Generate the ANSYS Fluent native-mesh case under exports/:
    slab.stl geometry + native.msh.h5 (cut by Fluent Meshing at export
    time — needs a licensed local Fluent and holds a seat for the few
    minutes it runs) + a conventions README. The solved-case export
    lives on a finished Fluent verify run."""
    from .core import cfd_run, fluent_run, rans_queue
    # config problems answer 422 before any license is touched
    _cfg(body.config)
    av = fluent_run.availability()
    if not av["available"]:
        raise HTTPException(424, detail=f"ANSYS Fluent unavailable: "
                                        f"{av['detail']}")
    # the mesher holds a license and gmsh state — never alongside a
    # running verification or queue. The check alone is one-directional:
    # the export must also be REGISTERED as the exclusive claim in
    # cfd_run so a verify/queue (or second export) started during the
    # multi-minute meshing is refused rather than exiting the export's
    # live Fluent session. Guard + claim run under the start mutex so a
    # concurrent rans_start cannot slip between them.
    with _rans_start_lock:
        q = rans_queue.get_current()
        if q is not None and q.state in ("pending", "running"):
            raise HTTPException(409, detail="the shortlist queue is "
                                            "running — wait for it or "
                                            "cancel it first")
        try:
            # refuses while any job is pending/running, or while another
            # export holds the claim
            cfd_run.claim_exclusive("a Fluent mesh export")
        except RuntimeError as e:
            raise HTTPException(409, detail=str(e))
    try:
        return fluent_run.export_native_case(body.config,
                                             body.mesh_size,
                                             EXPORTS_DIR)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    except Exception as e:
        # a failed launch/mesh is a client-visible condition (license,
        # install, geometry) — surface the reason, not a bare 500
        raise HTTPException(422, detail=f"native meshing failed: {e}")
    finally:
        cfd_run.release_exclusive()


class Fluent2DMeshBody(_AnsysOverrides):
    config: dict
    # default = the documented manual ANSYS workflow's sizing exactly
    # (0.1 mm profile edges, 1 mm first layer x 10); studio-yplus1 = a
    # resolved wall (y+ ~ 1 first layer, chord-scaled edge sizing).
    # The inherited overrides ride on top of whichever recipe is picked.
    sizing: Literal["default", "studio-yplus1"] = "default"


@app.post("/api/export/fluent2d-mesh/save")   # before /api/export/{fmt}
def export_fluent2d_mesh_save(body: Fluent2DMeshBody):
    """Generate the ANSYS 2D mesh bundle under exports/: the profile +
    domain DXF plus the true-2D Fluent mesh cut by the Workbench chain
    (SpaceClaim -> Mechanical — holds ANSYS seats for the minutes it
    runs) and a README with the solve recipe. The solved-case export
    lives on a finished verify run."""
    import shutil
    import time as _time

    from .core import cfd, cfd_run, rans_queue
    # config problems answer 422 before any ANSYS process is touched
    cfg = _cfg(body.config)
    try:
        wf = _fluent2d_workflow()
    except Exception:
        raise HTTPException(424, detail="the ANSYS 2D meshing tools are "
                                        "not installed with this app")
    av = wf.availability()
    if not av["available"]:
        raise HTTPException(424, detail=f"ANSYS Workbench tools "
                                        f"unavailable: {av['detail']}")
    # the chain holds SpaceClaim/Workbench seats for minutes — never
    # alongside a running verification or queue. Same wiring as the
    # Fluent mesh export above: guard + exclusive claim under the start
    # mutex, work outside it, release in a finally.
    with _rans_start_lock:
        q = rans_queue.get_current()
        if q is not None and q.state in ("pending", "running"):
            raise HTTPException(409, detail="the shortlist queue is "
                                            "running — wait for it or "
                                            "cancel it first")
        try:
            cfd_run.claim_exclusive("an ANSYS 2D mesh export")
        except RuntimeError as e:
            raise HTTPException(409, detail=str(e))
    try:
        # POLYS ONLY from the shared geometry source: the 2D domain
        # rectangle is write_dxf_2d's own, not the slab box
        g = cfd.section_geometry(cfg, "coarse")
        # the recipe first, then whatever the request overrode: the label
        # stays the recipe's, the numbers reported below are the ones the
        # chain actually ran with
        over = body.overrides()
        sizing = dict(wf.mesh_sizing(body.sizing, cfg))
        sizing.update({k: v for k, v in over.items() if k in sizing})
        domain = {k: over[k] for k in ("front_l", "back_l", "top_h")
                  if k in over}
        budgets = {k: over[k] for k in ("sc_budget_s", "wb_budget_s")
                   if k in over}
        # the clearances the inflation stack faces, exactly as the verify
        # job computes them: without them the chain cannot cap a stack
        # that physically cannot fit, and Mechanical's whole generation
        # collapses (measured: 0 elements in a narrow slot)
        slot_gaps = [e.get("slot_gap_pct") for e in
                     (body.config.get("elements") or [])
                     if isinstance(e, dict) and e.get("slot_gap_pct")]
        clear = {"slot_gap_mm": (min(slot_gaps) / 100.0 * cfg.chord_m
                                 * 1000.0 if slot_gaps else None),
                 "ground_clear_mm": min(float(p[:, 1].min())
                                        for p, _b in g["polys"]) * 1000.0}
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        dest = EXPORTS_DIR / f"fluent2d_mesh_{stamp}"
        k = 2
        # mkdir is the claim — same two-writers guard as the sibling
        # case exports
        while True:
            try:
                dest.mkdir()
                break
            except FileExistsError:
                dest = EXPORTS_DIR / f"fluent2d_mesh_{stamp}-{k}"
                k += 1
        try:
            dxf = wf.write_dxf_2d([p for p, _b in g["polys"]],
                                  dest / "section_2d.dxf", **domain)
            work = dest / "_mesh_work"
            chain = wf.run_chain(dxf["dxf_path"], work,
                                 edge_size_mm=sizing["edge_size_mm"],
                                 first_layer_mm=sizing["first_layer_mm"],
                                 n_layers=sizing["n_layers"],
                                 growth=sizing["growth"],
                                 **clear, **budgets)
            # the mesh's own inflation, not the request: a capped or
            # dropped stack must not be documented as the one asked for
            inflation = chain.get("inflation") or {}
            meshed = dict(sizing)
            for k in ("first_layer_mm", "n_layers"):
                if inflation.get(k) is not None:
                    meshed[k] = inflation[k]
            bl_note = inflation.get("note")
            shutil.copy2(chain["msh_path"], dest / "FFF.msh")
            shutil.rmtree(work, ignore_errors=True)
            import json as _json
            (dest / "config.json").write_text(
                _json.dumps(body.config, indent=1), encoding="utf-8")
            (dest / "README.txt").write_text(
                "Wing Section Studio - ANSYS Fluent 2D mesh export\n"
                "=================================================\n\n"
                "FFF.msh is a true-2D Fluent mesh cut by the ANSYS "
                "Workbench chain\n(SpaceClaim geometry -> Mechanical "
                "mesh) from section_2d.dxf; the DXF\nis included so the "
                "geometry can be re-imported or re-meshed by hand.\n\n"
                "Open in Fluent (2D, double precision): File > Read > "
                "Mesh, pick\nFFF.msh. Zones arrive named: fluid, inlet, "
                "outlet, ground,\nupper_bound, profile.\n\n"
                "Solve recipe (the documented manual ANSYS workflow):\n"
                f"  - velocity inlet at the design speed "
                f"({cfg.speed_ms:g} m/s); pressure outlet 0 Pa\n"
                "  - profile: no-slip stationary wall\n"
                "  - upper_bound: specified shear = 0\n"
                "  - ground: moving wall, +x at the inlet speed\n"
                "  - reference values: compute from inlet, then set "
                "area 1 m2 and\n    length 1 m explicitly (2D "
                "coefficients per meter depth)\n"
                "  - report definitions: lift_coef and drag_coef on the "
                "profile zone\n    (Fluent default force vectors)\n"
                "  - hybrid initialization; run 500 iterations, residual "
                "criteria at\n    the Fluent defaults\n\n"
                f"Mesh sizing ({body.sizing}"
                + (" + request overrides" if over else "") + "):\n"
                f"  - profile edge sizing "
                f"{meshed['edge_size_mm']:g} mm\n"
                + (f"  - no inflation layers on the profile boundary\n"
                   if not meshed["n_layers"] else
                   f"  - inflation from the profile boundary: first layer "
                   f"{meshed['first_layer_mm']:g} mm,\n    up to "
                   f"{meshed['n_layers']} layers, growth "
                   f"{meshed['growth']:g}\n")
                + (f"  - {bl_note}\n" if bl_note else "") + "\n"
                f"Cells: {chain.get('n_cells', '?')}; exported "
                f"{_time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                encoding="utf-8")
        except BaseException:
            # never leave a half-built export folder looking complete
            shutil.rmtree(dest, ignore_errors=True)
            raise
        files = sorted(p.name for p in dest.iterdir())
        return {"filename": dest.name, "path": str(dest),
                "dir": str(EXPORTS_DIR), "files": files,
                "n_cells": chain.get("n_cells"),
                "zones": chain.get("zones"),
                # the recipe label plus the values it actually ran with:
                # mesh_sizing is what the mesh GOT, requested_sizing what
                # was asked, inflation the chain's own cap/degrade verdict
                "sizing": body.sizing, "mesh_sizing": meshed,
                "requested_sizing": sizing,
                "inflation": chain.get("inflation"),
                "overrides": over}
    except HTTPException:
        raise
    except (ValueError, KeyError) as e:
        raise HTTPException(422, detail=_err_detail(e))
    except Exception as e:
        # a failed stage is a client-visible condition (install, license,
        # geometry) — surface the reason, not a bare 500
        raise HTTPException(422, detail=f"2D meshing failed: {e}")
    finally:
        cfd_run.release_exclusive()


@app.post("/api/export/{fmt}")
def export_file(fmt: str, body: ExportBody):
    data, media, ext = _export_bytes(fmt, body)
    n = len(body.config.get("elements") or [1])
    fname = f"wing_section_{n}element.{ext}"
    return Response(content=data, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/export/{fmt}/save")
def export_save(fmt: str, body: ExportBody):
    """Write the export to the project's exports/ folder and report where.

    The server runs on this machine, so saving directly gives the user a
    real, linkable file location instead of a browser download that lands
    who-knows-where."""
    import os as _os
    import time as _time
    data, _media, ext = _export_bytes(fmt, body)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    n = len(body.config.get("elements") or [1])
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    path = EXPORTS_DIR / f"wing_section_{n}element_{stamp}.{ext}"
    k = 2
    # O_EXCL is the claim: an exists() probe would let two same-second
    # saves pick one name and the second silently replace the first
    # (same guard as the mkdir loops in the directory exports above)
    flags = (_os.O_CREAT | _os.O_EXCL | _os.O_WRONLY
             | getattr(_os, "O_BINARY", 0))
    while True:
        try:
            fd = _os.open(path, flags)
            break
        except FileExistsError:
            path = EXPORTS_DIR / f"wing_section_{n}element_{stamp}-{k}.{ext}"
            k += 1
    with _os.fdopen(fd, "wb") as f:
        f.write(data)
    return {"filename": path.name, "path": str(path),
            "dir": str(EXPORTS_DIR), "size_bytes": len(data)}


# ---------- presets ----------

PRESETS = [
    {
        "name": "Two-element baseline",
        "description": "S1223 main with a 35% S1223 flap — a proven starting "
                       "point for a 350 mm front-wing section, trimmed to "
                       "pass its own loading budget.",
        "config": {
            "elements": [
                {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0},
            ],
            "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
            "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
            "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        },
        "target_downforce_n": 250,
    },
    {
        "name": "Three-element aggressive",
        "description": "S1223 main with two slotted flaps — the most "
                       "sectional load the loading budget will sign off on; "
                       "let the optimizer push it to a target.",
        "config": {
            "elements": [
                {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "s1223", "chord_ratio": 0.28, "deflection_deg": 8,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 1.5},
                {"airfoil": "s1223", "chord_ratio": 0.20, "deflection_deg": 20,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 1.5},
            ],
            "stack_aoa_deg": -2.0, "ride_height_mm": 30, "chord_mm": 350,
            "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
            "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        },
        "target_downforce_n": 300,
    },
    {
        "name": "Low-drag two-element",
        "description": "E423 main and flap — softer camber, better "
                       "efficiency, more forgiving off-design.",
        "config": {
            "elements": [
                {"airfoil": "e423", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "e423", "chord_ratio": 0.33, "deflection_deg": 10,
                 "slot_gap_pct": 2.0, "slot_overlap_pct": 3.5},
            ],
            "stack_aoa_deg": 0.0, "ride_height_mm": 35, "chord_mm": 350,
            "span_mm": 1400, "speed_ms": 18, "ncrit": 7,
            "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        },
        "target_downforce_n": 200,
    },
]


@app.get("/api/presets")
def presets():
    return {"presets": PRESETS}


# ---------- session persistence ----------
#
# The working state must survive an app restart. localStorage cannot carry
# that in the desktop shell: it is keyed on the origin, and the desktop
# window runs the server on a fresh random port each launch (and WebView2's
# default profile is in-private besides). The state therefore lives with the
# server, in a JSON file next to the project.

# WSS_DATA_DIR override: test servers must not touch the real working state
import os as _os_mod

SESSION_FILE = Path(_os_mod.environ.get(
    "WSS_DATA_DIR", Path(__file__).resolve().parents[1] / "app_data")
) / "session.json"
SESSION_MAX_BYTES = 4_000_000


class SessionBody(BaseModel):
    state: dict
    # optimistic-concurrency token: a client that echoes the rev it loaded
    # gets a 409 (carrying the newer state) instead of silently overwriting
    # a save it never saw; clients that omit it keep last-write-wins
    rev: int | None = None


_session_lock = threading.Lock()


def _session_read() -> tuple[int, dict | None]:
    """Stored (rev, state); (0, None) when no session exists yet.

    A transient PermissionError (a reader colliding with os.replace, or an
    indexer/AV hold on Windows) is retried and then surfaced — reporting it
    as "no session" would hand a freshly opened window a blank state that
    its autosave then writes over the real one."""
    import json as _json
    import time as _time
    raw = None
    for attempt in range(4):
        try:
            raw = SESSION_FILE.read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            return 0, None
        except PermissionError:
            if attempt == 3:
                raise HTTPException(503, detail="session file is briefly "
                                                "locked — try again")
            _time.sleep(0.05 * (attempt + 1))
    try:
        data = _json.loads(raw)
    except ValueError:
        return 0, None
    if (isinstance(data, dict) and set(data) == {"rev", "state"}
            and isinstance(data["rev"], int)):
        return data["rev"], data["state"]
    return 0, data   # pre-rev file: the bare state


@app.get("/api/session")
def session_get():
    rev, state = _session_read()
    return {"state": state, "rev": rev}


@app.post("/api/session")
def session_put(body: SessionBody):
    import json as _json
    import os as _os
    import time as _time
    with _session_lock:
        cur_rev, cur_state = _session_read()
        if body.rev is not None and body.rev != cur_rev:
            raise HTTPException(409, detail={
                "message": "the session was saved by another window since "
                           "this one loaded it",
                "rev": cur_rev, "state": cur_state})
        rev = cur_rev + 1
        data = _json.dumps({"rev": rev, "state": body.state})
        if len(data) > SESSION_MAX_BYTES:
            raise HTTPException(422,
                                detail="session state too large to persist")
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        # unique tmp per writer: concurrent saves (debounced autosave racing
        # the pagehide beacon) must not collide on a shared tmp name, and
        # os.replace on Windows can transiently fail while an outside reader
        # holds the target
        tmp = SESSION_FILE.with_name(
            f".session.{_os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(data, encoding="utf-8")
            for attempt in range(4):
                try:
                    _os.replace(tmp, SESSION_FILE)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    _time.sleep(0.05 * (attempt + 1))
        finally:
            tmp.unlink(missing_ok=True)
    return {"ok": True, "bytes": len(data), "rev": rev}


# ---------- preset libraries ----------
#
# A preset PUT carries the client's whole library, so two windows editing
# presets would otherwise silently drop each other's saves. The libraries
# therefore carry the session file's optimistic-concurrency token: a client
# that echoes the rev it loaded gets a 409 (with the newer library) instead
# of overwriting a save it never saw; a client that omits it keeps
# last-write-wins.

_presets_lock = threading.Lock()


def _presets_read(path: Path, key: str = "presets") -> tuple[int, list]:
    """Stored (rev, entries); (0, []) when the library does not exist or
    cannot be read. A pre-rev file is a bare list, at rev 0. `key` is the
    on-disk (and 409-payload) name of the entry list, so a library of pins
    is not stored under a misnamed field forever."""
    import json as _json
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0, []
    except OSError:
        return 0, []
    try:
        data = _json.loads(raw)
    except ValueError:
        return 0, []
    if (isinstance(data, dict) and set(data) == {"rev", key}
            and isinstance(data["rev"], int)
            and isinstance(data[key], list)):
        return data["rev"], data[key]
    return 0, data if isinstance(data, list) else []


def _presets_write(path: Path, entries: list, rev: int, stem: str,
                   key: str = "presets") -> None:
    """The library at its new rev, atomically. Unique tmp per writer, and
    os.replace on Windows can transiently fail under an outside reader."""
    import json as _json
    import os as _os
    import time as _time
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(
        f".{stem}.{_os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(_json.dumps({"rev": rev, key: entries},
                                   indent=1), encoding="utf-8")
        for attempt in range(4):
            try:
                _os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                _time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)


def _clean_text(v, what: str, lo: int = 1, hi: int = 60) -> str:
    """Validated text — never coerced: str(["x"]) would store a repr."""
    if not isinstance(v, str):
        raise HTTPException(422, detail=f"{what} must be text")
    v = v.strip()
    if not (lo <= len(v) <= hi):
        raise HTTPException(422, detail=f"{what} must be {lo}..{hi} chars")
    return v


def _preset_name(p: dict, seen: set) -> str:
    """One preset's name, validated (never coerced: str(["x"]) would
    store a repr) and unique case-insensitively."""
    name = p.get("name")
    if not isinstance(name, str):
        raise HTTPException(422, detail="each preset name must be text")
    name = name.strip()
    if not (1 <= len(name) <= 60):
        raise HTTPException(422,
                            detail="each preset needs a name, 1..60 chars")
    if name.lower() in seen:
        raise HTTPException(422, detail=f"duplicate preset name {name!r}")
    seen.add(name.lower())
    return name


def _presets_stale(rev, cur_rev: int, cur_entries: list, what: str,
                   key: str = "presets") -> None:
    """409 when the client echoes a rev the library has moved past."""
    if rev is not None and rev != cur_rev:
        raise HTTPException(409, detail={
            "message": f"the {what} were saved by another window "
                       f"since this one loaded them",
            "rev": cur_rev, key: cur_entries})


# ---------- rule presets ----------
#
# Named rule envelopes ("FSAE 2026", ...) are a machine-level library — the
# same rulebook applies across projects, so they live next to the session
# state rather than inside any one project file. The ACTIVE envelope still
# travels inside the config (and therefore inside project files/sessions).

RULE_PRESETS_FILE = SESSION_FILE.parent / "rule_presets.json"
RULE_PRESETS_MAX = 50


class RulePresetsBody(BaseModel):
    presets: list[dict]
    rev: int | None = None   # optimistic-concurrency token; see above


@app.get("/api/rule-presets")
def rule_presets_get():
    rev, presets = _presets_read(RULE_PRESETS_FILE)
    return {"presets": presets, "rev": rev}


@app.put("/api/rule-presets")
def rule_presets_put(body: RulePresetsBody):
    import dataclasses
    if len(body.presets) > RULE_PRESETS_MAX:
        raise HTTPException(422,
                            detail=f"at most {RULE_PRESETS_MAX} rule presets")
    cleaned, seen = [], set()
    for p in body.presets:
        if not isinstance(p, dict):
            raise HTTPException(422, detail="each preset must be an object")
        name = _preset_name(p, seen)
        try:
            env = geometry.validate_rule_envelope(p.get("envelope") or {})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, detail=f"preset {name!r}: {e}")
        d = dataclasses.asdict(env)
        d.pop("preset_name", None)   # the name lives beside, not inside
        cleaned.append({"name": name, "envelope": d})
    # read-check-write under the lock: the stale check is worthless if
    # another writer can land between it and the replace
    with _presets_lock:
        cur_rev, cur = _presets_read(RULE_PRESETS_FILE)
        _presets_stale(body.rev, cur_rev, cur, "rule presets")
        rev = cur_rev + 1
        _presets_write(RULE_PRESETS_FILE, cleaned, rev, "rule_presets")
    return {"ok": True, "count": len(cleaned), "rev": rev}


# ---------- ANSYS settings presets ----------
#
# Named ANSYS 2D settings ("walkthrough parity", "resolved wall", ...) are
# a machine-level library for the same reason the rule presets are: the
# meshing recipe belongs to the install and its ANSYS version, not to any
# one section. The settings a run USED still travel with that run's result.

ANSYS_PRESETS_FILE = SESSION_FILE.parent / "ansys_presets.json"
ANSYS_PRESETS_MAX = 50


class AnsysPresetsBody(BaseModel):
    presets: list[dict]
    rev: int | None = None   # optimistic-concurrency token


@app.get("/api/ansys-presets")
def ansys_presets_get():
    rev, presets = _presets_read(ANSYS_PRESETS_FILE)
    return {"presets": presets, "rev": rev}


@app.put("/api/ansys-presets")
def ansys_presets_put(body: AnsysPresetsBody):
    if len(body.presets) > ANSYS_PRESETS_MAX:
        raise HTTPException(
            422, detail=f"at most {ANSYS_PRESETS_MAX} ANSYS presets")
    cleaned, seen = [], set()
    for p in body.presets:
        if not isinstance(p, dict):
            raise HTTPException(422, detail="each preset must be an object")
        name = _preset_name(p, seen)
        try:
            # the same ranges a run enforces: a preset that could not be
            # started is not worth storing
            st = _validate_ansys_settings(p.get("settings") or {})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, detail=f"preset {name!r}: {e}")
        cleaned.append({"name": name, "settings": st})
    with _presets_lock:
        cur_rev, cur = _presets_read(ANSYS_PRESETS_FILE)
        _presets_stale(body.rev, cur_rev, cur, "ANSYS presets")
        rev = cur_rev + 1
        _presets_write(ANSYS_PRESETS_FILE, cleaned, rev, "ansys_presets")
    return {"ok": True, "count": len(cleaned), "rev": rev}


# ---------- pinned designs library ----------
#
# The durable, machine-level record of pinned designs. Workspace pins are
# deliberately project-scoped (wiped on preset switch, replaced on project
# open — they belong to their design context and feed that project's
# report); every pin is ALSO mirrored here, and this library survives all
# of it. Same storage discipline as the preset libraries above: one JSON
# file beside the session, optimistic rev token, atomic replace,
# read-check-write under the shared lock. A pin embeds everything a later
# session needs to rebuild the design — including uploaded airfoil .dat
# text, because uploads live in server memory and die with the process —
# so a pin whose upload is gone is still restorable, and a config
# referencing an unregistered custom: spec must PASS validation here.

PINNED_FILE = SESSION_FILE.parent / "pinned_designs.json"
PINNED_MAX = 48                  # 4x the workspace cap of 12
# Its own file; the session cap is untouched. Must clear MAX_BODY_BYTES
# (16 MB) with JSON headroom, since a PUT carries the whole library.
PINNED_MAX_BYTES = 12_000_000
_PIN_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_PIN_CUSTOM_RE = re.compile(r"^custom:[a-z0-9_-]+$")
# verification runs captured with the pin: per engine channel, the full
# terminal status (verbatim — the same object the session/project restore
# path renders) plus compare-width flow images in the exact shape the
# client's safeFlow() accepts, each image byte-capped (a ~640px JPEG runs
# 40-90 KB)
_PIN_RUN_KEYS = ("rans", "fl2d", "polish")
_PIN_RUN_IMG_KEYS = ("umag", "cp")
_PIN_IMG_DATA_RE = re.compile(
    r"^data:image/(png|jpe?g|webp);base64,[A-Za-z0-9+/=]+$")
_PIN_RUN_IMG_MAX = 250_000
_PIN_DAT_MAX = 120_000           # an 800-pt .dat (the upload cap) is ~25 KB


def _pin_clean(p, seen_ids: set) -> dict:
    """One pin, validated field by field; unknown keys are dropped so junk
    cannot accumulate in the library. The config itself travels VERBATIM
    (harvest philosophy) — parse-validated, never round-tripped through the
    dataclass, which would materialize defaults the user never set."""
    if not isinstance(p, dict):
        raise HTTPException(422, detail="each pin must be an object")
    pid = p.get("id")
    if not (isinstance(pid, str) and _PIN_ID_RE.match(pid)):
        raise HTTPException(422, detail="each pin needs a hex id (8..32)")
    if pid in seen_ids:
        raise HTTPException(422, detail=f"duplicate pin id {pid!r}")
    seen_ids.add(pid)
    label = _clean_text(p.get("label"), "each pin's label")
    out = {"id": pid, "v": 1, "label": label}
    if p.get("t") is not None:
        out["t"] = _clean_text(p["t"], f"pin {label!r}: timestamp", 1, 40)
    if p.get("note") is not None:
        out["note"] = _clean_text(p["note"], f"pin {label!r}: note", 0, 500)
    ctx = p.get("context")
    if isinstance(ctx, dict):
        c = {}
        if isinstance(ctx.get("rule_preset_name"), str):
            c["rule_preset_name"] = ctx["rule_preset_name"][:60]
        if isinstance(ctx.get("project_hint"), str):
            c["project_hint"] = ctx["project_hint"][:120]
        out["context"] = c
    tgt = p.get("target")
    if tgt is not None:
        if isinstance(tgt, bool) or not isinstance(tgt, (int, float)) \
                or not math.isfinite(float(tgt)):
            raise HTTPException(
                422, detail=f"pin {label!r}: target must be a finite number")
        out["target"] = float(tgt)
    cfg = p.get("config")
    if not isinstance(cfg, dict):
        raise HTTPException(422, detail=f"pin {label!r}: config must be an "
                                        f"object")
    try:
        StackConfig.from_dict(cfg)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(422, detail=f"pin {label!r}: {e}")
    out["config"] = cfg
    ca = p.get("custom_airfoils")
    if ca is not None:
        if not isinstance(ca, dict) or len(ca) > 8:
            raise HTTPException(
                422, detail=f"pin {label!r}: custom_airfoils must be an "
                            f"object with at most 8 entries")
        for k, v in ca.items():
            if not (isinstance(k, str) and _PIN_CUSTOM_RE.match(k)):
                raise HTTPException(
                    422, detail=f"pin {label!r}: bad custom airfoil key")
            if not (isinstance(v, str) and 0 < len(v) <= _PIN_DAT_MAX):
                raise HTTPException(
                    422, detail=f"pin {label!r}: custom airfoil data must "
                                f"be text up to {_PIN_DAT_MAX} chars")
        out["custom_airfoils"] = ca
    an = p.get("airfoil_names")
    if an is not None:
        if (not isinstance(an, dict) or len(an) > 16
                or not all(isinstance(k, str) and len(k) <= 200
                           and isinstance(v, str) and len(v) <= 120
                           for k, v in an.items())):
            raise HTTPException(
                422, detail=f"pin {label!r}: airfoil_names must map short "
                            f"text to short text (at most 16)")
        out["airfoil_names"] = an
    hl = p.get("headline")
    if hl is not None:
        if not isinstance(hl, dict):
            raise HTTPException(
                422, detail=f"pin {label!r}: headline must be an object")
        out["headline"] = hl   # rendered via textContent/numf client-side;
                               # the whole-payload byte cap bounds it
    ol = p.get("outline")
    if ol is not None:
        ok = (isinstance(ol, list) and len(ol) <= 4
              and all(isinstance(poly, list) and len(poly) <= 120
                      and all(isinstance(pt, list) and len(pt) == 2
                              and all(isinstance(c, (int, float))
                                      and not isinstance(c, bool)
                                      and math.isfinite(float(c))
                                      for c in pt)
                              for pt in poly)
                      for poly in ol))
        if not ok:
            raise HTTPException(
                422, detail=f"pin {label!r}: outline must be up to 4 "
                            f"polylines of up to 120 finite [x, y] points")
        out["outline"] = ol
    rn = p.get("runs")
    if rn is not None:
        if not isinstance(rn, dict) \
                or any(k not in _PIN_RUN_KEYS for k in rn):
            raise HTTPException(
                422, detail=f"pin {label!r}: runs allows only "
                            f"{', '.join(_PIN_RUN_KEYS)}")
        out_runs = {}
        for k, v in rn.items():
            if not isinstance(v, dict):
                raise HTTPException(
                    422, detail=f"pin {label!r}: runs.{k} must be an object")
            st = v.get("status")
            if not isinstance(st, dict):
                raise HTTPException(
                    422, detail=f"pin {label!r}: runs.{k}.status must be "
                                f"an object (the run's terminal status)")
            # the status travels verbatim like the config: it is rendered
            # by the same restore path project files already flow through
            entry = {"status": st}
            im = v.get("images")
            if im is not None:
                if not isinstance(im, dict) \
                        or any(kk not in _PIN_RUN_IMG_KEYS for kk in im):
                    raise HTTPException(
                        422, detail=f"pin {label!r}: runs.{k}.images "
                                    f"allows only "
                                    f"{', '.join(_PIN_RUN_IMG_KEYS)}")
                for kk, vv in im.items():
                    if not (isinstance(vv, str)
                            and len(vv) <= _PIN_RUN_IMG_MAX
                            and _PIN_IMG_DATA_RE.match(vv)):
                        raise HTTPException(
                            422,
                            detail=f"pin {label!r}: runs.{k}.{kk} must be "
                                   f"a data-URI image up to "
                                   f"{_PIN_RUN_IMG_MAX} bytes")
                entry["images"] = im
            out_runs[k] = entry
        out["runs"] = out_runs
    return out


class PinnedDesignsBody(BaseModel):
    pins: list[dict]
    rev: int | None = None   # optimistic-concurrency token; see presets


@app.get("/api/pinned-designs")
def pinned_designs_get():
    rev, pins = _presets_read(PINNED_FILE, key="pins")
    return {"pins": pins, "rev": rev}


@app.put("/api/pinned-designs")
def pinned_designs_put(body: PinnedDesignsBody):
    import json as _json
    if len(body.pins) > PINNED_MAX:
        raise HTTPException(422,
                            detail=f"at most {PINNED_MAX} pinned designs")
    cleaned, seen_ids = [], set()
    for p in body.pins:
        cleaned.append(_pin_clean(p, seen_ids))
    try:
        size = len(_json.dumps(cleaned, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise HTTPException(
            422, detail="pinned designs must be JSON-serializable")
    if size > PINNED_MAX_BYTES:
        raise HTTPException(422, detail="pinned library too large to "
                                        "persist — delete some pins")
    with _presets_lock:
        cur_rev, cur = _presets_read(PINNED_FILE, key="pins")
        _presets_stale(body.rev, cur_rev, cur, "pinned designs", key="pins")
        rev = cur_rev + 1
        _presets_write(PINNED_FILE, cleaned, rev, "pinned_designs",
                       key="pins")
    return {"ok": True, "count": len(cleaned), "rev": rev}


@app.get("/api/health")
def health():
    return {"ok": True, "library_size": len(airfoils.library_names())}


# ---------- static frontend ----------

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico")
def favicon():
    ico = STATIC / "favicon.ico"
    if ico.exists():
        return FileResponse(ico)
    raise HTTPException(404)


app.mount("/static", StaticFiles(directory=STATIC), name="static")
