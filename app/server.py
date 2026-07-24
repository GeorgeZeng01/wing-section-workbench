"""REST API + static frontend for the wing-section design studio.

Run:  .venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8642
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException
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


@app.middleware("http")
async def _limit_body_size(request, call_next):
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            too_big = int(cl) > MAX_BODY_BYTES
        except ValueError:
            too_big = False
        if too_big:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "request body too large"},
                                status_code=413)
    return await call_next(request)


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


class CfdExportBody(BaseModel):
    config: dict
    mesh_size: Literal["coarse", "medium", "fine"] = "medium"


class RansStartBody(BaseModel):
    config: dict
    mesh_size: Literal["coarse", "medium", "fine"] = "coarse"
    # generous default: the runner stops on its own the moment the force
    # history flattens, so the cap only matters for runs that need it
    max_iters: int = Field(default=10000, ge=100, le=20000)


def _err_detail(e: BaseException) -> str:
    # KeyError stringifies with quotes around its message — strip them
    return str(e.args[0]) if (isinstance(e, KeyError) and e.args) else str(e)


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


@app.post("/api/rans/start")
def rans_start(body: RansStartBody):
    _cfg(body.config)   # validate before spawning the job
    from .core import cfd_run
    try:
        job_id = cfd_run.start(body.config, body.mesh_size, body.max_iters)
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


@app.get("/api/rans/{job_id}/flow")
def rans_flow(job_id: str, field: str = "umag"):
    """The solved section flow as a PNG — rendered once, cached in the
    case directory."""
    from .core import cfd_run, foam_post
    if field not in ("umag", "cp"):
        raise HTTPException(422, detail="field must be 'umag' or 'cp'")
    job = cfd_run.get(job_id)
    if job is None:
        raise HTTPException(404, detail="unknown job")
    if job.state != "done":
        raise HTTPException(409, detail="the run has not finished")
    png = job.case_dir / f"flow_{field}.png"
    if png.is_file():
        return Response(content=png.read_bytes(), media_type="image/png")
    try:
        data = foam_post.flow_png(job.case_dir, job.cfg, field)
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
            data = export.dxf_bytes(cfg, body.frame, body.entity)
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
            data = export.zip_bundle(cfg, result, body.entity)
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
    p = Path(body.path).resolve()
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
    while case.exists():
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
    return {"filename": case.name, "path": str(case),
            "dir": str(EXPORTS_DIR), "summary": summary}


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
    import time as _time
    data, _media, ext = _export_bytes(fmt, body)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    n = len(body.config.get("elements") or [1])
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    fname = f"wing_section_{n}element_{stamp}.{ext}"
    path = EXPORTS_DIR / fname
    k = 2
    while path.exists():
        path = EXPORTS_DIR / f"wing_section_{n}element_{stamp}-{k}.{ext}"
        k += 1
    path.write_bytes(data)
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


@app.get("/api/session")
def session_get():
    if not SESSION_FILE.exists():
        return {"state": None}
    try:
        import json as _json
        return {"state": _json.loads(SESSION_FILE.read_text(encoding="utf-8"))}
    except Exception:
        return {"state": None}


@app.post("/api/session")
def session_put(body: SessionBody):
    import json as _json
    import os as _os
    import time as _time
    data = _json.dumps(body.state)
    if len(data) > SESSION_MAX_BYTES:
        raise HTTPException(422, detail="session state too large to persist")
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    # unique tmp per writer: concurrent saves (debounced autosave racing the
    # pagehide beacon) must not collide on a shared tmp name, and os.replace
    # on Windows can transiently fail while another writer holds the target
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
    return {"ok": True, "bytes": len(data)}


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


@app.get("/api/rule-presets")
def rule_presets_get():
    if not RULE_PRESETS_FILE.exists():
        return {"presets": []}
    try:
        import json as _json
        data = _json.loads(RULE_PRESETS_FILE.read_text(encoding="utf-8"))
        return {"presets": data if isinstance(data, list) else []}
    except Exception:
        return {"presets": []}


@app.put("/api/rule-presets")
def rule_presets_put(body: RulePresetsBody):
    import dataclasses
    import json as _json
    import os as _os
    import time as _time
    if len(body.presets) > RULE_PRESETS_MAX:
        raise HTTPException(422,
                            detail=f"at most {RULE_PRESETS_MAX} rule presets")
    cleaned, seen = [], set()
    for p in body.presets:
        if not isinstance(p, dict):
            raise HTTPException(422, detail="each preset must be an object")
        name = str(p.get("name") or "").strip()
        if not (1 <= len(name) <= 60):
            raise HTTPException(422,
                                detail="each preset needs a name, 1..60 chars")
        if name.lower() in seen:
            raise HTTPException(422, detail=f"duplicate preset name {name!r}")
        seen.add(name.lower())
        try:
            env = geometry.validate_rule_envelope(p.get("envelope") or {})
        except (ValueError, TypeError) as e:
            raise HTTPException(422, detail=f"preset {name!r}: {e}")
        d = dataclasses.asdict(env)
        d.pop("preset_name", None)   # the name lives beside, not inside
        cleaned.append({"name": name, "envelope": d})
    RULE_PRESETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = _json.dumps(cleaned, indent=1)
    tmp = RULE_PRESETS_FILE.with_name(
        f".rule_presets.{_os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        for attempt in range(4):
            try:
                _os.replace(tmp, RULE_PRESETS_FILE)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                _time.sleep(0.05 * (attempt + 1))
    finally:
        tmp.unlink(missing_ok=True)
    return {"ok": True, "count": len(cleaned)}


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
