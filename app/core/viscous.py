"""Viscous airfoil data via NeuralFoil (fast surrogate) and XFOIL (reference).

NeuralFoil evaluates a full polar in milliseconds, which makes it usable
inside optimization loops and for library-wide screening. XFOIL runs are
offered as a slower cross-check through the same interface.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import airfoils

_nf_lock = threading.Lock()  # CasADi/Kulfan fitting is not proven thread-safe

# The grid reaches down to -8 deg so lightly-loaded elements interpolate a
# real operating point instead of clamping to the bottom edge; 24 deg is
# comfortably past stall for every high-lift section in the library.
DEFAULT_ALPHAS = (-8.0, 24.0, 0.5)
RE_FLOOR = 1e4   # NeuralFoil's trained envelope; lower Re is clamped up


def _alpha_grid(alphas=DEFAULT_ALPHAS) -> np.ndarray:
    a0, a1, da = alphas
    n = max(2, int(round((a1 - a0) / da)) + 1)
    if n > 800:
        raise ValueError(f"alpha grid of {n} points — narrow the range or "
                         f"increase the step")
    return np.linspace(a0, a1, n)


@lru_cache(maxsize=4096)   # shape sweeps generate many distinct specs
def _polar_cached(spec: str, re_key: float, ncrit: float, model_size: str,
                  alphas_key: tuple, _token: int) -> dict:
    import aerosandbox as asb
    import neuralfoil as nf
    _, coords = airfoils.repaneled(spec, 80)
    af = asb.Airfoil(name="af", coordinates=coords)
    grid = _alpha_grid(alphas_key)
    with _nf_lock:
        out = nf.get_aero_from_airfoil(airfoil=af, alpha=grid, Re=re_key,
                                       n_crit=ncrit, model_size=model_size)
    return {
        "alpha": grid,
        "CL": np.ravel(out["CL"]).astype(float),
        "CD": np.ravel(out["CD"]).astype(float),
        "CM": np.ravel(out["CM"]).astype(float),
        "confidence": np.ravel(out["analysis_confidence"]).astype(float),
        "Top_Xtr": np.ravel(out["Top_Xtr"]).astype(float),
        "Bot_Xtr": np.ravel(out["Bot_Xtr"]).astype(float),
    }


def polar(spec: str, re: float, ncrit: float = 9.0, model_size: str = "xlarge",
          alphas=DEFAULT_ALPHAS) -> dict:
    """NeuralFoil polar. Re is bucketed to 3 significant figures for caching
    and floored at RE_FLOOR (the surrogate's trained envelope); "re_used"
    reports the Reynolds number actually evaluated."""
    re_key = float(f"{max(re, RE_FLOOR):.3g}")
    p = _polar_cached(str(spec), re_key, round(float(ncrit), 2),
                      model_size, tuple(alphas), airfoils.spec_cache_token(spec))
    return {**p, "re_used": re_key, "re_clamped": bool(re < RE_FLOOR)}


def polar_metrics(p: dict) -> dict:
    cl, cd, al, conf = p["CL"], p["CD"], p["alpha"], p["confidence"]
    i = int(np.argmax(cl))
    valid = cd > 1e-6
    ld = np.where(valid, cl / np.maximum(cd, 1e-9), 0.0)
    j = int(np.argmax(ld))
    return {
        "CL_max": float(cl[i]),
        "alpha_CL_max": float(al[i]),
        # CL still climbing at the last grid angle: the polar never stalled
        # inside the alpha range, so "CL_max" is a grid edge, not a stall
        "CL_max_at_grid_edge": bool(i == len(cl) - 1),
        "LD_max": float(ld[j]),
        "CL_at_LD_max": float(cl[j]),
        "alpha_LD_max": float(al[j]),
        "CD_min": float(cd[valid].min()) if valid.any() else None,
        "confidence_min": float(conf.min()),
        "confidence_at_CL_max": float(conf[i]),
    }


def cl_limit(spec: str, re: float, ncrit: float = 9.0,
             model_size: str = "large") -> dict:
    """CL_max of the isolated airfoil (the element-loading sanity bound)."""
    p = polar(spec, re, ncrit, model_size)
    m = polar_metrics(p)
    return {"CL_max": m["CL_max"], "alpha_CL_max": m["alpha_CL_max"],
            "at_grid_edge": m["CL_max_at_grid_edge"],
            "confidence": m["confidence_at_CL_max"]}


def operating_point(spec: str, re: float, cl_target: float, ncrit: float = 9.0,
                    model_size: str = "large") -> dict:
    """Profile drag & alpha where the isolated polar reaches cl_target.

    cl_target is clamped to the pre-stall branch; `clamped` reports by how
    much (a loading indicator in its own right).
    """
    p = polar(spec, re, ncrit, model_size)
    cl, cd, al = p["CL"], p["CD"], p["alpha"]
    i_max = int(np.argmax(cl))
    branch = slice(0, i_max + 1)
    cl_b, cd_b, al_b = cl[branch], cd[branch], al[branch]
    order = np.argsort(cl_b)
    cl_b, cd_b, al_b = cl_b[order], cd_b[order], al_b[order]
    cl_used = float(np.clip(cl_target, cl_b[0], cl_b[-1]))
    return {
        "CD": float(np.interp(cl_used, cl_b, cd_b)),
        "alpha": float(np.interp(cl_used, cl_b, al_b)),
        "CL_used": cl_used,
        "clamped": bool(abs(cl_used - cl_target) > 1e-6),
        # which side: high = target beyond the pre-stall branch (drag will be
        # understated), low = target below the grid's bottom edge
        "clamped_high": bool(cl_target - cl_b[-1] > 1e-6),
        "clamped_low": bool(cl_b[0] - cl_target > 1e-6),
        "CL_max": float(cl[i_max]),
    }


def xfoil_polar(spec: str, re: float, ncrit: float = 9.0,
                alphas=(-6.0, 24.0, 1.0), timeout: int = 240) -> dict:
    """Reference polar from xfoil.exe via the existing batch runner."""
    import sys
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import xfoil_runner
    _, coords = airfoils.repaneled(spec, 80)
    p = xfoil_runner.run_polar(coords, re, tuple(alphas), ncrit=ncrit,
                               timeout=timeout)
    return {k: np.asarray(v, float) for k, v in p.items()}
