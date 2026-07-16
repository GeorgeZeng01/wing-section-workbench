"""Library-wide airfoil screening at a target Reynolds number.

Sweeps the bundled UIUC database (~2,170 sections) plus any uploaded
airfoils through NeuralFoil and reports selection metrics per foil. A full
sweep takes seconds; results are cached per operating point so re-sorting
and pagination are free.
"""

from __future__ import annotations

import threading

import numpy as np

from . import airfoils, viscous

_cache: dict[tuple, list[dict]] = {}
_cache_lock = threading.Lock()
_MAX_CACHE = 8


def _metrics_row(spec: str, re: float, ncrit: float, cl_ref: float,
                 model_size: str) -> dict | None:
    try:
        _, coords = airfoils.repaneled(spec, 80)
        info = airfoils.geometry_info(coords)
        p = viscous.polar(spec, re, ncrit, model_size)
        m = viscous.polar_metrics(p)
        op = viscous.operating_point(spec, re, cl_ref, ncrit, model_size)
    except Exception:
        return None
    if not np.isfinite(m["CL_max"]) or m["CL_max"] <= 0:
        return None
    return {
        "spec": spec,
        "CL_max": round(m["CL_max"], 3),
        "alpha_CL_max": round(m["alpha_CL_max"], 1),
        "LD_max": round(m["LD_max"], 1),
        "CL_at_LD_max": round(m["CL_at_LD_max"], 3),
        "CD_at_CL_ref": round(op["CD"], 5) if not op["clamped"] else None,
        "CD_min": round(m["CD_min"], 5) if m["CD_min"] else None,
        "thickness_pct": round(info["max_thickness"] * 100, 1),
        "camber_pct": round(info["max_camber"] * 100, 1),
        "confidence": round(m["confidence_at_CL_max"], 2),
    }


def metrics_for(spec: str, re: float, ncrit: float = 9.0, cl_ref: float = 1.5,
                model_size: str = "large") -> dict | None:
    """Selection metrics for one spec (used to rank shortlist additions that
    the confidence filter would otherwise hide). None when analysis fails."""
    return _metrics_row(spec, re, ncrit, cl_ref, model_size)


def screen(re: float, ncrit: float = 9.0, cl_ref: float = 1.5,
           model_size: str = "large", thickness_pct_max: float = 25.0,
           thickness_pct_min: float = 0.0, include_low_confidence: bool = False,
           ) -> list[dict]:
    custom_ids = tuple(sorted(c["spec"] for c in airfoils.list_custom()))
    key = (float(f"{re:.3g}"), round(ncrit, 2), round(cl_ref, 2), model_size,
           custom_ids)
    with _cache_lock:
        rows = _cache.get(key)
    if rows is None:
        rows = []
        specs = list(airfoils.library_names())
        specs += [c["spec"] for c in airfoils.list_custom()]
        for spec in specs:
            row = _metrics_row(spec, re, ncrit, cl_ref, model_size)
            if row is not None:
                rows.append(row)
        with _cache_lock:
            if len(_cache) >= _MAX_CACHE:
                _cache.pop(next(iter(_cache)))
            _cache[key] = rows
    out = [r for r in rows
           if thickness_pct_min <= r["thickness_pct"] <= thickness_pct_max
           and (include_low_confidence or r["confidence"] >= 0.5)]
    return out
