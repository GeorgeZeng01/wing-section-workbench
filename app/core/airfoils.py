"""Airfoil resolution and library access.

Sources, in resolution order:
  0. "mfg:<mode>:<gap>:<base>"      — any other spec with its trailing edge
                          opened for manufacturing (core/manufacturing.py)
  0b. "shape:<b>:<b>:<b>:<ts>:<base>" — any other spec with camber bumps /
                          thickness scaling applied (core/shaping.py)
  1. "custom:<id>"      — airfoils uploaded through the API (in-memory registry)
  2. "nacaXXXX"         — 4-digit NACA generated analytically (5/6-digit and
                          modified series resolve from the database below)
  3. UIUC database name — the .dat collection bundled with AeroSandbox (~2,170 foils)
  4. filesystem path    — a Selig-format .dat file inside the project folder

All coordinates are returned in Selig ordering (TE -> upper -> LE -> lower ->
TE) as (N, 2) float arrays, in the source file's own scale; call
`normalize()` (or `repaneled()`, which normalizes internally) for unit-chord,
chord-aligned geometry.
"""

from __future__ import annotations

import re
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

_DB_DIR: Path | None = None
_custom_lock = threading.Lock()
_custom: dict[str, dict] = {}  # id -> {"name": str, "coords": ndarray}


def database_dir() -> Path:
    global _DB_DIR
    if _DB_DIR is None:
        import aerosandbox.geometry.airfoil as af_mod
        d = Path(af_mod.__file__).parent / "airfoil_database"
        if not d.is_dir():
            hits = list(Path(af_mod.__file__).parent.rglob("*.dat"))
            if not hits:
                raise FileNotFoundError("AeroSandbox airfoil database not found")
            d = hits[0].parent
        _DB_DIR = d
    return _DB_DIR


@lru_cache(maxsize=1)
def library_names() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in database_dir().glob("*.dat")))


def search_library(query: str = "", limit: int = 50) -> list[str]:
    q = query.strip().lower()
    names = library_names()
    if not q:
        return list(names[:limit])
    starts = [n for n in names if n.lower().startswith(q)]
    contains = [n for n in names if q in n.lower() and not n.lower().startswith(q)]
    return (starts + contains)[:limit]


def read_dat(path_or_text, name_fallback="airfoil", *, allow_path=True):
    """Parse a Selig .dat (name line optional). Returns (name, (N,2) array).

    A coordinate line is exactly two numeric fields (parentheses around a
    field, used by some catalog files for interpolated TE ordinates, are
    ignored). Lines with any other shape are headers or trailing commentary
    and are skipped — this matters in practice: MSES-style files carry a
    four-number plot-window line after the name, and several catalog files
    end with "a -> b" edit notes whose numeric fragments a laxer parser
    would append to the contour as garbage points.

    allow_path: when True (internal callers passing a trusted path), a
    newline-free ".dat"-suffixed string is read from disk. API callers pass
    dat_text that must ALWAYS be treated as literal content — set False so a
    crafted single-line ".dat" path can never make this read an arbitrary
    file off the disk.
    """
    if isinstance(path_or_text, Path) or (
        allow_path
        and isinstance(path_or_text, str) and "\n" not in path_or_text
        and Path(path_or_text).suffix.lower() == ".dat"
    ):
        # utf-8-sig: a BOM read through the locale codec becomes garbage
        # glued to the first token, which silently drops the first
        # coordinate of a headerless file
        text = Path(path_or_text).read_text(encoding="utf-8-sig",
                                            errors="replace")
        name_fallback = Path(path_or_text).stem
    else:
        text = str(path_or_text).lstrip("﻿")
    lines = text.splitlines()
    name = name_fallback
    pts = []
    for i, line in enumerate(lines):
        parts = line.split()
        vals = []
        for tok in parts:
            try:
                vals.append(float(tok.strip("()")))
            except ValueError:
                vals = None
                break
        if vals is not None and len(vals) == 2:
            pts.append((vals[0], vals[1]))
        elif i == 0 and line.strip():
            name = line.strip() or name_fallback
        # anything else (MSES plot-window line, "->" edit notes, separators
        # like "1.0000 ......") is not contour data
    coords = np.asarray(pts, dtype=float)
    # reject non-finite coordinates at the door: "nan"/"inf" tokens parse
    # cleanly via float() but would be stored as a custom airfoil and then
    # poison every downstream solve (and break JSON rendering -> a 500)
    if coords.size and not np.isfinite(coords).all():
        raise ValueError(f"{name}: coordinates contain non-finite "
                         f"values (nan/inf)")
    # Lednicer format: a leading (n_upper, n_lower) count pair, then both
    # surfaces LE->TE. Only re-stitch when the first pair really is a count
    # pair — near-integers >= 2 whose sum matches the remaining rows —
    # otherwise large-x contours (e.g. installed-position stack exports)
    # would be scrambled.
    if len(coords) > 2 and coords[0, 0] > 1.5:
        x0, y0 = coords[0]
        n_up, n_lo = int(round(x0)), int(round(y0))
        counts_like = (abs(x0 - n_up) < 1e-6 and abs(y0 - n_lo) < 1e-6
                       and n_up >= 2 and n_lo >= 2
                       and n_up + n_lo == len(coords) - 1)
        if counts_like:
            body = coords[1:]
            up, lo = body[:n_up], body[n_up:]
            coords = np.vstack([up[::-1], lo[1:]])
    # a name line of exactly two numeric tokens ("63 412") parses as a
    # coordinate; when that first pair sits wildly outside the rest of the
    # contour it is a header artifact, not geometry (genuine first points —
    # unit-chord TE, mm-scale TE, installed-position exports — all sit
    # within the contour's own span)
    if len(coords) > 10:
        rest = coords[1:]
        # span over BOTH axes: a thin section stored near-vertical (a flap
        # deflected ~90 deg, re-imported from an installed-position export)
        # has a tiny x-span but a large y-span, and measuring only x would
        # flag its legitimate first coordinate as a header artifact
        span = float(max(rest[:, 0].max() - rest[:, 0].min(),
                         rest[:, 1].max() - rest[:, 1].min(), 1e-9))
        d0 = max(abs(float(coords[0, 0]) - float(np.median(rest[:, 0]))),
                 abs(float(coords[0, 1]) - float(np.median(rest[:, 1]))))
        if d0 > 10.0 * span:
            coords = rest
    # exactly-duplicated consecutive points (present in some catalog files
    # and uploads) survive upload validation but break the spline repanel
    # much later, deep inside analysis — drop them at the door
    if len(coords) > 1:
        keep = np.ones(len(coords), bool)
        keep[1:] = np.hypot(*np.diff(coords, axis=0).T) > 1e-10
        coords = coords[keep]
    if len(coords) < 10:
        raise ValueError(f"{name}: only {len(coords)} coordinate pairs parsed")
    return name, coords


def naca_coords(digits: str, n_per_side: int = 100) -> np.ndarray:
    """4-digit NACA section, cosine-spaced, Selig ordering."""
    if len(digits) != 4 or not digits.isdigit():
        raise ValueError(f"only 4-digit NACA supported, got naca{digits}")
    m = int(digits[0]) / 100.0
    p = int(digits[1]) / 10.0
    t = int(digits[2:]) / 100.0
    if m > 0 and p == 0:
        raise ValueError(f"naca{digits}: max camber {digits[0]}% at position "
                         f"0 is undefined — use naca0{digits[2:]} for a "
                         f"symmetric section")
    beta = np.linspace(0.0, np.pi, n_per_side)
    x = 0.5 * (1 - np.cos(beta))
    yt = 5 * t * (0.2969 * np.sqrt(x) - 0.1260 * x - 0.3516 * x**2
                  + 0.2843 * x**3 - 0.1036 * x**4)  # closed-TE variant
    if m == 0 or p == 0:
        yc = np.zeros_like(x)
        dyc = np.zeros_like(x)
    else:
        yc = np.where(x < p, m / p**2 * (2 * p * x - x**2),
                      m / (1 - p)**2 * ((1 - 2 * p) + 2 * p * x - x**2))
        dyc = np.where(x < p, 2 * m / p**2 * (p - x),
                       2 * m / (1 - p)**2 * (p - x))
    th = np.arctan(dyc)
    xu, yu = x - yt * np.sin(th), yc + yt * np.cos(th)
    xl, yl = x + yt * np.sin(th), yc - yt * np.cos(th)
    return np.vstack([np.column_stack([xu, yu])[::-1],
                      np.column_stack([xl, yl])[1:]])


def register_custom(name: str, coords: np.ndarray) -> str:
    """Store an uploaded airfoil; returns its spec id ("custom:<slug>")."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower()).strip("-") or "airfoil"
    with _custom_lock:
        base, k = slug, 2
        while slug in _custom and not np.array_equal(_custom[slug]["coords"], coords):
            slug = f"{base}-{k}"
            k += 1
        # a re-slugged upload shares its file name with a different geometry
        # already in the registry — disambiguate the display name too, or the
        # two are indistinguishable everywhere in the UI
        display = name if slug == base else f"{name} ({slug.rsplit('-', 1)[-1]})"
        _custom[slug] = {"name": display, "coords": np.asarray(coords, float)}
    return f"custom:{slug}"


def list_custom() -> list[dict]:
    with _custom_lock:
        return [{"spec": f"custom:{k}", "name": v["name"], "n_points": len(v["coords"])}
                for k, v in _custom.items()]


def _is_unc_path(s: str) -> bool:
    """True for a UNC share path (``\\\\host\\share\\...`` or ``//host/share/...``).
    Probing such a path with os.stat / Path.exists / Path.resolve opens an
    outbound SMB session that authenticates with — and thereby leaks — the
    user's NetNTLM credentials, so a UNC spec must be refused before any
    filesystem operation touches it. Absolute and drive-qualified paths are
    NOT rejected here: a legitimate in-project ".dat" may be given as an
    absolute path, and the project-root / database-dir containment checks in
    resolve() already confine those to safe locations."""
    return s.strip().replace("/", "\\").startswith("\\\\")


def resolve(spec: str) -> tuple[str, np.ndarray]:
    """Any airfoil spec -> (display_name, unit-chord Selig coords)."""
    s = str(spec).strip()
    if s.lower().startswith("mfg:"):
        from . import manufacturing
        return manufacturing.resolve_mfg(s)
    if s.lower().startswith("shape:"):
        from . import shaping
        return shaping.resolve_shape(s)
    if s.lower().startswith("custom:"):
        key = s.split(":", 1)[1]
        with _custom_lock:
            if key not in _custom:
                raise KeyError(f"unknown custom airfoil {key!r}")
            return _custom[key]["name"], _custom[key]["coords"].copy()

    low = s.lower().replace(" ", "")
    if low.startswith("naca") and low[4:].isdigit() and len(low[4:]) == 4:
        # 4-digit sections are generated analytically; 5/6-digit and modified
        # series fall through — dozens of them ship in the UIUC database
        return f"NACA {low[4:]}", naca_coords(low[4:])

    # A UNC spec must never reach a filesystem probe: os.stat/exists on
    # \\host\share opens an outbound SMB session that leaks NetNTLM
    # credentials. Reject it lexically before any .exists()/.stat()/.resolve().
    # (Absolute/drive and "../" traversal specs are contained instead by the
    # relative_to() checks below, which keep legitimate in-project .dat paths
    # working while refusing out-of-tree reads.)
    if _is_unc_path(s):
        raise KeyError(f"airfoil {spec!r} not found (not custom/naca/.dat/"
                       f"UIUC name)")

    p = Path(s)
    if p.suffix.lower() == ".dat" and p.exists():
        # specs come in over the local HTTP API: keep the filesystem branch
        # for project files but refuse to serve arbitrary paths on the disk
        root = Path(__file__).resolve().parents[2]
        try:
            p.resolve().relative_to(root)
        except ValueError:
            raise KeyError(f"airfoil path {spec!r} is outside the project "
                           f"folder — upload the .dat instead")
        return read_dat(p)

    # a UIUC library name is a bare stem — never a path. Refuse any spec that
    # carries a separator or ".." before touching the disk, so the appended
    # ".dat" can't turn "../../secret" (or a drive path) into an out-of-tree
    # read. A purely lexical guard keeps this hot path filesystem-I/O-free
    # (every library-airfoil lookup reaches here); all 2174 bundled names are
    # bare stems, so nothing legitimate is rejected.
    if "/" in low or "\\" in low or ".." in low:
        raise KeyError(f"airfoil {spec!r} not found (not custom/naca/.dat/"
                       f"UIUC name)")
    dat = database_dir() / f"{low}.dat"
    if dat.exists():
        _, coords = read_dat(dat)
        return low, coords

    raise KeyError(f"airfoil {spec!r} not found (not custom/naca/.dat/UIUC name)")


TILT_EPS_DEG = 0.5   # smaller implied tilts are catalog-alignment noise


def normalize(coords: np.ndarray) -> np.ndarray:
    """Unit chord, LE at the origin, chord along +x.

    A contour drawn with genuinely baked-in incidence is de-rotated —
    incidence belongs to the stack (stack angle / deflection), not the
    profile. The tilt is measured from the TE to the contour point farthest
    from it; on a cambered section that vertex sits slightly off the true
    chord line (for a NACA 2412 the implied angle is ~0.16 deg), so tilts
    below TILT_EPS_DEG are treated as zero: a chord-aligned catalog file
    passes through untouched instead of being rotated by the vertex offset,
    which would misreport camber and skew deflection semantics for every
    cambered airfoil in the library."""
    c = np.asarray(coords, float)
    te = 0.5 * (c[0] + c[-1])
    d = c - te
    i_far = int(np.argmax(np.einsum("ij,ij->i", d, d)))  # farthest from TE
    far = c[i_far]
    chord_vec = te - far
    if float(np.hypot(*chord_vec)) < 1e-9:
        raise ValueError("degenerate airfoil: zero chord")
    ang = float(np.arctan2(chord_vec[1], chord_vec[0]))
    if abs(ang) > np.radians(TILT_EPS_DEG):
        ca, sa = np.cos(ang), np.sin(ang)
        c = (c - far) @ np.array([[ca, -sa], [sa, ca]])
    # x-origin at the front-most point; y-origin at the TE midpoint — the
    # stable catalog convention. Referencing y to the LE vertex instead
    # would shift every measurement by that vertex's (thickness-dependent)
    # height off the chord line.
    te = 0.5 * (c[0] + c[-1])
    x0 = float(c[:, 0].min())
    chord = float(te[0] - x0)
    if chord < 1e-9:
        raise ValueError("degenerate airfoil: zero chord")
    return (c - np.array([x0, te[1]])) / chord


def spec_cache_token(spec: str) -> int:
    """Cache-invalidation token for a spec. Filesystem .dat specs change
    when the file changes on disk — key their caches on the mtime so edits
    are picked up; every other spec source is immutable per string."""
    s = str(spec).strip()
    # unwrap until no prefix matches: layers chain in either order (the
    # optimizer's opt_shape re-wrap yields "shape:...:mfg:...:base"), and a
    # single ordered pass would leave the inner layer glued to the filename
    stripped = True
    while stripped:
        stripped = False
        for prefix in ("mfg:", "shape:"):
            if s.lower().startswith(prefix):
                s = s.split(":", {"mfg:": 3, "shape:": 5}[prefix])[-1].strip()
                stripped = True
    # never stat a UNC path — os.stat on \\host\share opens an outbound SMB
    # session (NetNTLM leak). resolve() rejects UNC specs anyway.
    if _is_unc_path(s):
        return 0
    p = Path(s)
    if p.suffix.lower() == ".dat":
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return 0
    return 0


@lru_cache(maxsize=2048)   # shape sweeps generate many distinct specs
def _repanel_cached(spec: str, n_per_side: int, _token: int) -> tuple:
    name, raw = resolve(spec)
    import aerosandbox as asb
    af = asb.Airfoil(name="af", coordinates=normalize(raw))
    rp = np.asarray(af.repanel(n_points_per_side=n_per_side).coordinates, float)
    return name, rp

# RLock: resolving an "mfg:" spec re-enters repaneled() for its base section
_repanel_lock = threading.RLock()


def repaneled(spec: str, n_per_side: int = 80) -> tuple[str, np.ndarray]:
    """(name, coords) with cosine-spaced spline repaneling. Cached per spec."""
    with _repanel_lock:  # asb spline code isn't guaranteed thread-safe
        name, rp = _repanel_cached(str(spec), int(n_per_side),
                                   spec_cache_token(spec))
    return name, rp.copy()


# leading-edge radius fit. LE_WINDOW_C is the nose window as a fraction of
# chord and LE_MIN_NOSE_PTS the number of contour points required on EACH
# surface inside it. Measured, a 70-panel side supplies 7-9 points per
# surface inside the window and a 45-panel side — the optimizer's coarse
# search resolution — 3-6, so the floor starts biting there; by 40 panels a
# side half the common sections are under it and by 35 all of them are, and
# the radius is reported unmeasured rather than fitted to four points.
# LE_FIT_EPS softens the 1/x^2 weighting (below) at the vertex, as a fraction
# of the window width. LE_MAX_R_GEOM is a self-consistency ceiling: a nose of
# radius r spans 2*sqrt(2*r*d) of y over a depth d, so the window's own span
# implies a radius, and a fit far above it is reading the parent contour
# behind a truncated or faceted edge rather than the edge itself.
LE_WINDOW_C = 0.02
LE_MIN_NOSE_PTS = 4
LE_FIT_EPS = 0.15
LE_MAX_R_C = 0.5      # a nose rounder than half a chord is not an airfoil
LE_MAX_R_GEOM = 2.5


def _le_fit(c: np.ndarray) -> tuple[float | None, str | None]:
    """Nose fit on an ALREADY NORMALIZED contour; see le_radius_fit."""
    i_le = int(np.argmin(c[:, 0]))
    # walk outward from the nose so the window stays one contiguous run, and
    # stop at a y reversal: past its own y extremum a surface has left the
    # nose, and x is no longer a function of y there. Trimming rather than
    # discarding keeps undercambered noses — the FSAE front-wing family —
    # measurable, since their lower-surface minimum often falls inside the
    # window while the nose itself is monotone.
    j0 = j1 = i_le
    fold_u = fold_l = False
    while j0 > 0 and c[j0 - 1, 0] - c[i_le, 0] <= LE_WINDOW_C:
        if c[j0 - 1, 1] <= c[j0, 1]:
            fold_u = True
            break
        j0 -= 1
    while j1 < len(c) - 1 and c[j1 + 1, 0] - c[i_le, 0] <= LE_WINDOW_C:
        if c[j1 + 1, 1] >= c[j1, 1]:
            fold_l = True
            break
        j1 += 1
    short_u = (i_le - j0) < LE_MIN_NOSE_PTS
    short_l = (j1 - i_le) < LE_MIN_NOSE_PTS
    if short_u or short_l:
        # a run cut short by a reversal is a shape the contour cannot carry;
        # one cut short by the window edge is only a resolution shortfall
        return None, ("shape" if (short_u and fold_u) or (short_l and fold_l)
                      else "coarse")
    X = c[j0:j1 + 1, 0] - c[i_le, 0]
    Y = c[j0:j1 + 1, 1] - c[i_le, 1]
    A = np.column_stack([np.ones_like(Y), Y, Y**2])
    w = 1.0 / (X + LE_FIT_EPS * max(float(X.max()), 1e-12)) ** 2
    try:
        co = np.linalg.lstsq(A * w[:, None], X * w, rcond=None)[0]
    except np.linalg.LinAlgError:
        return None, "shape"
    if not np.all(np.isfinite(co)) or co[2] <= 0.0:
        return None, "shape"
    # x'(y) = 0 at the nose vertex, which the constant and linear terms place
    # exactly. A vertex outside the window means the data hold no vertex at
    # all, so the fit is describing a flank and must not be reported.
    yv = -co[1] / (2 * co[2])
    if not (Y.min() <= yv <= Y.max()):
        return None, "shape"
    r = float(1.0 / (2 * co[2]))
    # the window's own span implies a radius of span^2/(8*depth); a fit far
    # above it is describing the contour behind a squared-off nose, not the
    # nose. Cross-multiplied so a zero-depth window cannot divide.
    span = float(Y.max() - Y.min())
    if r * 8.0 * float(X.max()) > LE_MAX_R_GEOM * span * span:
        return None, "shape"
    if not (0.0 < r < LE_MAX_R_C):
        return None, "shape"
    return r, None


def le_radius_fit(coords: np.ndarray) -> tuple[float | None, str | None]:
    """(radius of curvature at the leading edge in chord units, reason).

    The reason is None when the radius was measured, "coarse" when the nose
    carries too few points at this resolution, and "shape" when the contour
    itself defeats the fit — a folded, faceted or truncated leading edge has
    no radius to report, and the remedies for the two differ.

    Near the nose the surface follows y^2 = 2*r*x, so the shape is recovered
    by least-squares fitting x against y over the first LE_WINDOW_C of chord,
    using BOTH surfaces: the quadratic coefficient is 1/(2r). Fitting the
    parabola is far steadier than finite-difference curvature, which on a
    repaneled spline differentiates the panel spacing as much as the shape.

    Two refinements on the bare parabola:

      * a constant and a linear term absorb the offset between the frontmost
        contour POINT and the true nose vertex — they differ by up to one
        panel, and on a cambered section the vertex sits off the x axis;
      * points are weighted by 1/(x + eps)^2, so the fit is anchored where
        the parabola is actually valid instead of by the far end of the
        window, which is where paneling artifacts live.

    Cubic terms were tried and REJECTED. A |y|^3 column carries the sqrt nose
    and removes a systematic 4-7% underread against the analytic 4-digit NACA
    radius of 1.1019*t^2, but over a nose window it is near-collinear with
    y^2 (condition numbers of 1e7 to 1e8 under the nose weighting), so the
    curvature coefficient is left unconstrained: across the library the
    recovered radius then swung by 3-6x with panel count alone, flipping a
    rule verdict on a knob the user changes for solver resolution. The
    three-term fit holds panel-count spread under 10% over 45..200 panels a
    side at the cost of that few-percent bias, which is the trade a rule
    check wants. A y^3 term for camber asymmetry swung by up to 300% on
    high-camber sections (s1223, e423); the linear term absorbs that
    asymmetry to first order instead.
    """
    return _le_fit(normalize(coords))


def le_radius(coords: np.ndarray) -> float | None:
    """Radius of curvature at the leading edge, in chord units. None when the
    contour cannot support the fit; le_radius_fit also gives the reason."""
    return _le_fit(normalize(coords))[0]


def geometry_info(coords: np.ndarray, *,
                  with_le_radius: bool = True) -> dict:
    """Section measurements in chord units. with_le_radius=False skips the
    nose fit — the only expensive part — for callers that do not read it."""
    c = normalize(coords)
    x, y = c[:, 0], c[:, 1]
    i_le = int(np.argmin(x))
    up, lo = c[: i_le + 1][::-1], c[i_le:]  # both LE -> TE
    xs = np.linspace(0.005, 0.995, 200)
    yu = np.interp(xs, up[:, 0], up[:, 1])
    yl = np.interp(xs, lo[:, 0], lo[:, 1])
    thick = yu - yl
    camber = 0.5 * (yu + yl)
    j = int(np.argmax(thick))
    k = int(np.argmax(np.abs(camber)))
    r_le, why = _le_fit(c) if with_le_radius else (None, "skipped")
    return {
        "max_thickness": round(float(thick[j]), 4),
        "x_max_thickness": round(float(xs[j]), 3),
        "max_camber": round(float(camber[k]), 4),
        "x_max_camber": round(float(xs[k]), 3),
        "te_gap": round(float(np.hypot(*(c[0] - c[-1]))), 5),
        # None when the nose is too coarse or too far from a rounded edge to
        # measure — a rule check must say "unmeasured", never guess
        "le_radius": None if r_le is None else round(r_le, 6),
        "le_radius_reason": why,
        "n_points": int(len(coords)),
    }
