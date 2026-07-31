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
        # exact thickness kept beside the display-rounded one: the filter
        # must compare against the optimizer's buildable floor unrounded,
        # or boundary sections slip through / get dropped by 0.05 %c
        "_thickness_exact": float(info["max_thickness"] * 100),
        "CL_max": round(m["CL_max"], 3),
        # polar still climbing at the last analyzed angle: CL_max is a
        # lower bound, not a stall value — surfaced so consumers can say so
        "CL_max_lower_bound": bool(m["CL_max_at_grid_edge"]),
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
    # epsilon on both bounds: _thickness_exact = 100 * (already-rounded value)
    # carries ~1e-15 float noise, so a section at exactly the user's bound
    # (e.g. 7.0 % typed against a 7.000000000000001 value) would be dropped
    _te = 1e-6
    # copies with the private helper stripped: _thickness_exact exists only
    # for this boundary compare, and handing out the cached dicts would let
    # a caller mutate the cache through the returned rows
    out = [{k: v for k, v in r.items() if k != "_thickness_exact"}
           for r in rows
           if thickness_pct_min - _te <= r.get("_thickness_exact",
                                                r["thickness_pct"])
           <= thickness_pct_max + _te
           and (include_low_confidence or r["confidence"] >= 0.5)]
    return out
