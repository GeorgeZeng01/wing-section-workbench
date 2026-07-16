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

All coordinates are returned unit-chord, Selig ordering (TE -> upper -> LE ->
lower -> TE), shape (N, 2) float arrays.
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


def read_dat(path_or_text, name_fallback="airfoil"):
    """Parse a Selig .dat (name line optional). Returns (name, (N,2) array)."""
    if isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str) and "\n" not in path_or_text
        and Path(path_or_text).suffix.lower() == ".dat"
    ):
        text = Path(path_or_text).read_text(errors="replace")
        name_fallback = Path(path_or_text).stem
    else:
        text = str(path_or_text)
    lines = text.splitlines()
    name = name_fallback
    pts = []
    for i, line in enumerate(lines):
        parts = line.split()
        if len(parts) >= 2:
            try:
                x, y = float(parts[0]), float(parts[1])
            except ValueError:
                if i == 0:
                    name = line.strip() or name_fallback
                continue
            pts.append((x, y))
        elif i == 0 and line.strip():
            name = line.strip()
    coords = np.asarray(pts, dtype=float)
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


def custom_snapshot() -> dict[str, list]:
    """Serializable snapshot of the custom registry (for project save)."""
    with _custom_lock:
        return {k: v["coords"].tolist() for k, v in _custom.items()}


def restore_custom(snapshot: dict[str, list]) -> None:
    with _custom_lock:
        for k, pts in snapshot.items():
            _custom[k] = {"name": k, "coords": np.asarray(pts, float)}


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

    dat = database_dir() / f"{low}.dat"
    if dat.exists():
        _, coords = read_dat(dat)
        return low, coords

    raise KeyError(f"airfoil {spec!r} not found (not custom/naca/.dat/UIUC name)")


def normalize(coords: np.ndarray) -> np.ndarray:
    """Unit chord, LE at the origin, chord along +x. Camber stays intact;
    a contour drawn with baked-in incidence is de-rotated — incidence
    belongs to the stack (stack angle / deflection), not the profile, and
    every consumer (thickness measures, deflection semantics, TE
    treatments) assumes a chord-aligned section."""
    c = np.asarray(coords, float)
    te = 0.5 * (c[0] + c[-1])
    d = c - te
    i_le = int(np.argmax(np.einsum("ij,ij->i", d, d)))  # farthest from TE
    le = c[i_le]
    chord_vec = te - le
    chord = float(np.hypot(*chord_vec))
    if chord < 1e-9:
        raise ValueError("degenerate airfoil: zero chord")
    ang = float(np.arctan2(chord_vec[1], chord_vec[0]))
    ca, sa = np.cos(ang), np.sin(ang)
    # rotate row vectors by -ang so the LE->TE line lands on +x exactly
    return (c - le) @ np.array([[ca, -sa], [sa, ca]]) / chord


@lru_cache(maxsize=2048)   # shape sweeps generate many distinct specs
def _repanel_cached(spec: str, n_per_side: int) -> tuple:
    name, raw = resolve(spec)
    import aerosandbox as asb
    af = asb.Airfoil(name="af", coordinates=normalize(raw))
    rp = np.asarray(af.repanel(n_points_per_side=n_per_side).coordinates, float)
    return name, rp

_repanel_lock = threading.Lock()


def repaneled(spec: str, n_per_side: int = 80) -> tuple[str, np.ndarray]:
    """(name, coords) with cosine-spaced spline repaneling. Cached per spec."""
    with _repanel_lock:  # asb spline code isn't guaranteed thread-safe
        name, rp = _repanel_cached(str(spec), int(n_per_side))
    return name, rp.copy()


def geometry_info(coords: np.ndarray) -> dict:
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
    return {
        "max_thickness": round(float(thick[j]), 4),
        "x_max_thickness": round(float(xs[j]), 3),
        "max_camber": round(float(camber[k]), 4),
        "x_max_camber": round(float(xs[k]), 3),
        "te_gap": round(float(np.hypot(*(c[0] - c[-1]))), 5),
        "n_points": int(len(coords)),
    }
