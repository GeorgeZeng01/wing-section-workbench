"""REST API + static frontend for the wing-section design studio.

Run:  .venv\\Scripts\\python.exe -m uvicorn app.server:app --port 8642
"""

from __future__ import annotations

import sys
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


@app.middleware("http")
async def _local_host_only(request, call_next):
    """Reject requests whose Host header is not a loopback name.

    The server binds to 127.0.0.1, but a hostile web page can still reach it
    via DNS rebinding (a domain that resolves to 127.0.0.1 keeps the
    attacker's Host header). Pinning the Host closes that hole."""
    raw = (request.headers.get("host") or "").lower()
    host = (raw.split("]")[0] + "]") if raw.startswith("[") \
        else raw.rsplit(":", 1)[0]
    if host not in _LOCAL_HOSTS:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
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
    spec: str
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


def _cfg(config: dict) -> StackConfig:
    try:
        return StackConfig.from_dict(config)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, detail=str(e))


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
        name, coords = airfoils.read_dat(body.dat_text, body.name)
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
    except (KeyError, ValueError) as e:
        raise HTTPException(422, detail=str(e))
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
        raise HTTPException(422, detail=str(e))


@app.post("/api/analyze")
def analyze(body: ConfigBody):
    cfg = _cfg(body.config)
    try:
        return analysis.analyze(cfg)
    except ValueError as e:
        raise HTTPException(422, detail=str(e))
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

EXPORTS_DIR = Path(__file__).resolve().parents[1] / "exports"


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
    except (ValueError, np.linalg.LinAlgError) as e:
        raise HTTPException(422, detail=str(e))
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
                       "point for a 350 mm front-wing section.",
        "config": {
            "elements": [
                {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0},
            ],
            "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
            "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
            "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        },
        "target_downforce_n": 250,
    },
    {
        "name": "Three-element aggressive",
        "description": "S1223 main with two slotted flaps for maximum "
                       "sectional load at low speed.",
        "config": {
            "elements": [
                {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "s1223", "chord_ratio": 0.30, "deflection_deg": 22,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0},
                {"airfoil": "s1223", "chord_ratio": 0.22, "deflection_deg": 45,
                 "slot_gap_pct": 1.5, "slot_overlap_pct": 2.0},
            ],
            "stack_aoa_deg": 2.0, "ride_height_mm": 30, "chord_mm": 350,
            "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
            "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        },
        "target_downforce_n": 350,
    },
    {
        "name": "Low-drag two-element",
        "description": "E423 main and flap — softer camber, better "
                       "efficiency, more forgiving off-design.",
        "config": {
            "elements": [
                {"airfoil": "e423", "chord_ratio": 1.0, "deflection_deg": 0},
                {"airfoil": "e423", "chord_ratio": 0.33, "deflection_deg": 18,
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
