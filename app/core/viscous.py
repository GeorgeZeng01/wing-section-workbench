"""Viscous airfoil data via NeuralFoil (fast surrogate) and XFOIL (reference).

NeuralFoil evaluates a full polar in milliseconds, which makes it usable
inside optimization loops and for library-wide screening. XFOIL runs are
offered as a slower cross-check through the same interface.

The surrogate cross-check gate (surrogate_check / cl_limit) exists
because the surrogate's error is not always accompanied by low
confidence: measured 2026-08-04, NeuralFoil rated the blunt-TE library
section s9104BTE at CL_max 2.78 with 0.88 confidence at Re 467k while
XFOIL converged 7 of 31 polar points and capped it at 0.95 — a ~3x
CONFIDENT over-rating on a geometry outside the surrogate's family,
which every downstream gate (loading bands, stall budget, trust
penalty, wake screen) inherited because they all read the surrogate's
own numbers. The gate runs the real XFOIL once per base section and
Reynolds bucket, persists the verdict, and caps the usable CL_max /
floors the confidence where the two tools disagree or XFOIL cannot
even converge the section — feeding the existing trust machinery
instead of adding a new one.
"""

from __future__ import annotations

import json
import math
import os
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


# ---------- the surrogate cross-check gate ----------

# Thresholds MEASURED on the 2026-08-04 library sweep at Re 467k /
# ncrit 7 (scripts/surrogate_sweep.py; table in
# app_data/surrogate_sweep.json, clusters in DECISIONS.md). The 37
# agreeing sections converge >= 0.71 of the XFOIL grid at ratio <= 1.11;
# the blind-spot cluster (s9104BTE, fx74cl5140) converges 0.16-0.23 at
# ratio 2.6-2.9. The lines sit in the measured gaps:
XCHECK_CONV_OK = 0.50        # measured gap 0.23 <- 0.50 -> 0.71
XCHECK_RATIO_OK = 1.30       # measured gap 1.11 <- 1.30 -> 2.64
XCHECK_CAP_MARGIN = 1.10     # capped CL_max = xf CL_max x this
# Below this convergence fraction XFOIL has no usable view of the
# section AT ALL and its stray points are numeric noise (measured:
# real blind-spot evidence converges >= 0.16; sections XFOIL merely
# chokes on — including the panel-validated s1223 — converge 0.00-0.03).
# Missing data is not a verdict (the queue's own doctrine): such
# sections are recorded "unverifiable" and NEVER penalized, because
# punishing s1223 for XFOIL's numerics would be a false positive on
# the most-validated section in the library.
XCHECK_EVIDENCE_MIN = 0.10
_XCHECK_ALPHAS = (-6.0, 24.0, 1.0)
_XCHECK_TIMEOUT_S = 240

_xcheck_mem: dict[str, dict] = {}
_xcheck_lock = threading.Lock()   # serializes first-touch XFOIL runs and
                                  # every cache mutation — misses are rare
                                  # (once per base+bucket, persisted)
_xcheck_disk_loaded = False


def _xcheck_enabled() -> bool:
    return os.environ.get("WSS_SURROGATE_CHECK", "on").lower() not in (
        "off", "0", "no", "false")


def _xcheck_path() -> Path:
    root = os.environ.get("WSS_DATA_DIR")
    base = Path(root) if root else \
        Path(__file__).resolve().parents[2] / "app_data"
    return base / "surrogate_checks.json"


def check_base(spec: str) -> str:
    """The base section behind mfg:/shape: wrappers — the geometry family
    the cross-check verdict is a property of. The wrappers perturb, they
    do not change family membership; keying on the base bounds the XFOIL
    cost to once per section instead of once per quantized variant."""
    from . import manufacturing, shaping
    s = str(spec)
    for _ in range(8):
        low = s.lower()
        if low.startswith("mfg:"):
            s = manufacturing.parse(s)[2]
        elif low.startswith("shape:"):
            s = shaping.parse(s)[2]
        else:
            break
    return s


def _xcheck_re_bucket(re: float) -> float:
    """Quarter-decade Reynolds buckets: the blind spot is a geometry
    property, not Re-fine-structure, and the polar cache's 3-significant-
    figure buckets would re-run XFOIL for every element chord the
    optimizer tries."""
    return float(f"{10 ** (round(math.log10(max(re, RE_FLOOR)) * 4) / 4):.3g}")


def _xfoil_for_check(base: str, re_key: float, ncrit: float) -> dict:
    """Seam: the real XFOIL polar for the gate (tests fake this)."""
    return xfoil_polar(base, re_key, ncrit, alphas=_XCHECK_ALPHAS,
                       timeout=_XCHECK_TIMEOUT_S)


def surrogate_check(spec: str, re: float, ncrit: float = 9.0) -> dict:
    """XFOIL cross-check verdict for the section behind `spec`, cached in
    process and on disk (once per base section, Reynolds bucket and
    ncrit — ever, across restarts).

    status:
      "verified"    XFOIL converges the grid and agrees — the surrogate's
                    numbers stand untouched
      "capped"      underconverged or disagreeing — cl_max_usable is
                    XFOIL's converged ceiling x XCHECK_CAP_MARGIN and
                    confidence_cap is the convergence fraction
      "unverified"  XFOIL produced no converged points (or failed) — no
                    measured ceiling exists, so only the confidence is
                    floored (XCHECK_UNVERIFIED_CONF) and the trust
                    machinery does the rest
    """
    global _xcheck_disk_loaded
    base = check_base(spec)
    re_key = _xcheck_re_bucket(re)
    key = (f"{base}|{re_key:g}|{round(float(ncrit), 2):g}"
           f"|{airfoils.spec_cache_token(base)}")
    hit = _xcheck_mem.get(key)
    if hit is not None:
        return _xcheck_verdict(hit)
    with _xcheck_lock:
        if not _xcheck_disk_loaded:
            _xcheck_disk_loaded = True
            try:
                _xcheck_mem.update(
                    json.loads(_xcheck_path().read_text(encoding="utf-8")))
            except Exception:
                pass
        hit = _xcheck_mem.get(key)
        if hit is not None:
            return _xcheck_verdict(hit)
        # the bucket is the DEDUPE key only — the measurement runs at the
        # REQUESTING Reynolds number. Measured 2026-08-04: s9104BTE's
        # XFOIL convergence flips from 7/31 at Re 467k to 30/31 at the
        # bucket's 562k, so measuring at the bucket value would have
        # verified the exact section the gate exists to catch. The first
        # touch's Re is a real operating point; the entry records it.
        re_used = max(float(re), RE_FLOOR)
        p = polar(base, re_used, ncrit, "large")
        m = polar_metrics(p)
        nf_max = m["CL_max"]
        a0, a1, da = _XCHECK_ALPHAS
        n_req = int(round((a1 - a0) / da)) + 1
        try:
            xp = _xfoil_for_check(base, re_used, ncrit)
            cl = np.asarray(xp.get("CL", ()), float)
        except Exception:
            cl = np.asarray((), float)
        conv = float(len(cl)) / n_req
        xf_max = float(cl.max()) if len(cl) else None
        # the cache stores MEASUREMENTS only; the verdict is derived from
        # the current thresholds at read time, so tuning a line never
        # serves a stale status from disk
        entry = {"base": base, "re_key": re_key,
                 "re_used": round(re_used, 1),
                 "cl_max_nf": round(float(nf_max), 3),
                 "cl_max_xfoil": (round(xf_max, 3)
                                  if xf_max is not None else None),
                 "conv_frac": round(conv, 3),
                 "ratio": (round(float(nf_max) / xf_max, 3)
                           if xf_max else None)}
        _xcheck_mem[key] = entry
        try:
            path = _xcheck_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(_xcheck_mem, indent=0),
                           encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            pass   # the verdict stands for this process either way
        return _xcheck_verdict(entry)


def _xcheck_verdict(entry: dict) -> dict:
    """Measurements -> verdict, against the CURRENT thresholds."""
    out = {k: entry.get(k) for k in ("base", "re_key", "re_used",
                                    "cl_max_nf", "cl_max_xfoil",
                                    "conv_frac", "ratio")}
    out["cl_max_usable"] = None
    out["confidence_cap"] = None
    xf_max = out["cl_max_xfoil"]
    if xf_max is None or out["conv_frac"] < XCHECK_EVIDENCE_MIN:
        # XFOIL has no usable view — recorded, never punished (see the
        # threshold block: absence of evidence is not evidence of a
        # blind spot, and s1223 lives on this branch)
        out["status"] = "unverifiable"
    elif out["conv_frac"] < XCHECK_CONV_OK \
            or (out["ratio"] or 0.0) > XCHECK_RATIO_OK:
        out["status"] = "capped"
        out["cl_max_usable"] = round(xf_max * XCHECK_CAP_MARGIN, 3)
        out["confidence_cap"] = round(max(out["conv_frac"], 0.05), 3)
    else:
        out["status"] = "verified"
    return out


def cl_limit(spec: str, re: float, ncrit: float = 9.0,
             model_size: str = "large") -> dict:
    """CL_max of the isolated airfoil (the element-loading sanity bound).

    With the cross-check gate enabled (default; WSS_SURROGATE_CHECK=off
    disables), the surrogate's CL_max and confidence pass through the
    XFOIL verdict for the section's base geometry: a capped section
    reports the MEASURED ceiling (claim kept in CL_max_claimed) and a
    confidence no higher than XFOIL's convergence fraction, so every
    consumer — loading fractions, stall budget, drag caps, the
    optimizer's trust penalty — inherits the correction through the
    numbers it already reads."""
    p = polar(spec, re, ncrit, model_size)
    m = polar_metrics(p)
    out = {"CL_max": m["CL_max"], "alpha_CL_max": m["alpha_CL_max"],
           "at_grid_edge": m["CL_max_at_grid_edge"],
           "confidence": m["confidence_at_CL_max"]}
    if _xcheck_enabled():
        chk = surrogate_check(spec, re, ncrit)
        if chk["status"] != "verified":
            out["surrogate_check"] = chk
            if chk["cl_max_usable"] is not None \
                    and chk["cl_max_usable"] < out["CL_max"]:
                out["CL_max_claimed"] = out["CL_max"]
                out["CL_max"] = chk["cl_max_usable"]
            if chk["confidence_cap"] is not None:
                out["confidence"] = min(out["confidence"],
                                        chk["confidence_cap"])
    return out


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
