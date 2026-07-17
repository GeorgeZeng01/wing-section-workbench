"""Parametric shape refinement of airfoil sections.

The optimizer's airfoil-selection stage picks the best EXISTING section;
this module lets the search then modify that section — push camber where
loading wants it, thin or thicken the envelope — while staying inside the
manufacturing constraints and the surrogate's comfort zone.

Parameterization: three Hicks–Henne bumps applied to the camber line
(centers at 25 / 55 / 80 %c, the classical front-loading / mid-loading /
aft-loading levers) plus one thickness-envelope scale factor. Four numbers
per element keep the added search dimensions cheap, the shapes stay smooth
and airfoil-like (so NeuralFoil's confidence remains meaningful), and every
constraint downstream keeps working: the manufacturing TE treatment wraps
the shaped spec, the load budget uses the shaped section's own polar, and
exports carry the as-built shaped contour.

Shaped sections are addressed by derived specs

    "shape:<b25>:<b55>:<b80>:<tscale>:<base spec>"

mirroring the "mfg:" pattern: `airfoils.resolve` dispatches back here, which
makes a shaped section a first-class airfoil everywhere. Parameters are
quantized (bumps to 0.05 %c, scale to 0.01) so spec strings — and with them
the repanel / polar caches — stay stable while the optimizer sweeps.

All functions operate on unit-chord Selig contours (TE -> upper -> LE ->
lower -> TE).
"""

from __future__ import annotations

import numpy as np

BUMP_X = (0.25, 0.55, 0.80)   # Hicks-Henne bump centers
BUMP_POW = 3.0                # bump locality exponent
AMP_LIMIT = 0.02              # |bump| validation ceiling (fraction of chord)
TS_LIMIT = (0.70, 1.40)       # thickness-scale validation ceiling
Q_BUMP = 5e-4                 # spec quantization: 0.05 %c
Q_TS = 0.01

# search bounds the optimizer uses (tighter than the validation ceilings)
BOUNDS_BUMP = (-0.012, 0.012)
BOUNDS_TS = (0.85, 1.25)


def _hh(x: np.ndarray, x0: float, p: float = BUMP_POW) -> np.ndarray:
    """Hicks-Henne bump: peak 1.0 at x0, zero at both ends."""
    m = np.log(0.5) / np.log(x0)
    return np.sin(np.pi * np.clip(x, 0.0, 1.0) ** m) ** p


def derived_spec(base_spec: str, bumps, tscale: float) -> str:
    """Spec string for `base_spec` with the given shape modification.

    Quantizes the parameters; an (effectively) identity modification
    returns the base spec unchanged so caches stay hot."""
    b = [float(np.clip(round(float(v) / Q_BUMP) * Q_BUMP,
                       -AMP_LIMIT, AMP_LIMIT)) for v in bumps]
    if len(b) != 3:
        raise ValueError("shape spec needs exactly 3 bump amplitudes")
    ts = float(np.clip(round(float(tscale) / Q_TS) * Q_TS, *TS_LIMIT))
    if all(abs(v) < Q_BUMP / 2 for v in b) and abs(ts - 1.0) < Q_TS / 2:
        return str(base_spec)
    return f"shape:{b[0]:+.4f}:{b[1]:+.4f}:{b[2]:+.4f}:{ts:.3f}:{base_spec}"


def parse(spec: str) -> tuple[list[float], float, str]:
    """"shape:<b25>:<b55>:<b80>:<ts>:<base>" -> ([b25,b55,b80], ts, base)."""
    parts = str(spec).split(":", 5)
    if len(parts) != 6 or parts[0].lower() != "shape":
        raise ValueError(f"not a shape spec: {spec!r}")
    try:
        b = [float(parts[i]) for i in (1, 2, 3)]
        ts = float(parts[4])
    except ValueError:
        raise ValueError(f"malformed shape parameters in {spec!r}")
    if any(not np.isfinite(v) or abs(v) > AMP_LIMIT for v in b):
        raise ValueError(f"shape bump out of ±{AMP_LIMIT} in {spec!r}")
    if not (np.isfinite(ts) and TS_LIMIT[0] <= ts <= TS_LIMIT[1]):
        raise ValueError(f"thickness scale out of {TS_LIMIT} in {spec!r}")
    base = parts[5]
    # anywhere, not just the head: an mfg: wrapper between two shape: layers
    # ("shape:...:mfg:...:shape:...:af") would otherwise smuggle a second,
    # stacked shape modification past this guard
    if "shape:" in base.lower():
        raise ValueError("nested shape: specs are not allowed")
    return b, ts, base


def apply_shape(coords: np.ndarray, bumps, tscale: float) -> np.ndarray:
    """Modify a unit-chord Selig contour: camber bumps + thickness scale.

    The camber line moves by the bump sum; each surface's offset from the
    camber line scales by `tscale`. Endpoints (LE point, TE pair) keep their
    x positions; the TE gap scales with the envelope, which the mfg TE
    treatment reopens as needed."""
    c = np.asarray(coords, float)
    i_le = int(np.argmin(c[:, 0]))
    up, lo = c[: i_le + 1][::-1], c[i_le:]
    x = c[:, 0]
    yu = np.interp(x, up[:, 0], up[:, 1])
    yl = np.interp(x, lo[:, 0], lo[:, 1])
    cam = 0.5 * (yu + yl)
    half = c[:, 1] - cam                      # signed offset from camber
    dcam = np.zeros_like(x)
    for amp, x0 in zip(bumps, BUMP_X):
        if abs(float(amp)) > 0.0:
            dcam += float(amp) * _hh(x, x0)
    out = c.copy()
    out[:, 1] = cam + dcam + half * float(tscale)
    return out


def resolve_shape(spec: str) -> tuple[str, np.ndarray]:
    """Resolve a "shape:" spec -> (display name, shaped unit-chord coords)."""
    from . import airfoils
    b, ts, base = parse(spec)
    name, raw = airfoils.resolve(base)
    out = apply_shape(airfoils.normalize(raw), b, ts)
    return f"{name} (shaped)", out
