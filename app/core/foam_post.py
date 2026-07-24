"""Flow-field extraction and rendering from a solved OpenFOAM case.

The RANS cases are 2D (one cell thick), so the internal field IS the
cross-section: no ParaView needed. run.sh finishes with
`postProcess -func writeCellCentres`, which writes the cell-centre
coordinates as a volVectorField `C` beside `U` and `p` in the final time
directory; this module parses those ASCII fields, interpolates them onto a
regular grid, and renders the section view the studio already uses —
as driven, ground at y = 0, flow left to right — with velocity-magnitude
or Cp contours plus streamlines, the element silhouettes, and the ground.

Note on pressure: incompressible OpenFOAM solves kinematic pressure
(p/rho, m^2/s^2), so Cp = p / (0.5 U_inf^2) with no density factor.
"""

from __future__ import annotations

import io
import re
import threading
from pathlib import Path

import numpy as np

from . import geometry
from .geometry import StackConfig

GRID_NX = 640          # interpolation grid; plenty for a screen-size figure
PNG_DPI = 115

# rendering uses the object-oriented matplotlib API (no pyplot, no global
# figure registry), but serialize anyway: two concurrent renders would
# double the peak memory of the griddata grids for no wall-clock gain
_render_lock = threading.Lock()


class PostError(RuntimeError):
    """The case does not carry a readable flow field."""


# ---------- OpenFOAM ASCII field parsing ----------

def _parse_foam_list(text: str, kind: str, n: int, start: int) -> np.ndarray:
    """Numbers of a List<scalar|vector> body starting right after '('.

    The closing paren is found at nesting depth 0 — OpenFOAM writes both
    one-entry-per-line and single-line lists, and vector entries carry
    their own parens."""
    depth, i = 1, start
    while depth and i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        i += 1
    if depth:
        raise PostError("unterminated internalField list")
    block = text[start:i - 1]
    if kind == "vector":
        toks = block.replace("(", " ").replace(")", " ").split()
        vals = np.array(toks, dtype=float)
        if vals.size != 3 * n:
            raise PostError(f"vector field: expected {3 * n} numbers, "
                            f"got {vals.size}")
        return vals.reshape(n, 3)
    vals = np.array(block.split(), dtype=float)
    if vals.size != n:
        raise PostError(f"scalar field: expected {n} numbers, got {vals.size}")
    return vals


def parse_internal_field(text: str) -> np.ndarray:
    """The internalField of an ASCII vol field as an array.

    nonuniform List<scalar>  ->  (N,)
    nonuniform List<vector>  ->  (N, 3)
    Handles the one-per-line, single-line and compact N{value} encodings
    (OpenFOAM emits the last whenever every value is identical).
    Uniform fields are rejected — for the solved fields this module reads,
    a uniform internalField means the case never actually ran."""
    m = re.search(r"internalField\s+nonuniform\s+List<(scalar|vector)>\s*"
                  r"(\d+)\s*([({])", text)
    if m is None:
        if re.search(r"internalField\s+uniform", text):
            raise PostError("field is uniform — the case has no solved flow")
        raise PostError("no parsable internalField in the file")
    kind, n = m.group(1), int(m.group(2))
    if m.group(3) == "{":
        end = text.find("}", m.end())
        if end < 0:
            raise PostError("unterminated internalField list")
        block = text[m.end():end]
        if kind == "vector":
            one = np.array(block.replace("(", " ").replace(")", " ").split(),
                           dtype=float)
            return np.tile(one, (n, 1))
        return np.full(n, float(block))
    return _parse_foam_list(text, kind, n, m.end())


def _patch_boundary_values(text: str, patch: str) -> np.ndarray | None:
    """boundaryField values of one patch as an (N, 3) array, or None.

    Handles the nonuniform list and the uniform single-vector forms."""
    i = text.find("boundaryField")
    if i < 0:
        return None
    bf = text[i:]
    m = re.search(rf"\b{re.escape(patch)}\s*\{{", bf)
    if m is None:
        return None
    seg = bf[m.end():]
    vm = re.search(r"value\s+nonuniform\s+List<vector>\s*(\d+)\s*\(", seg)
    if vm is not None:
        return _parse_foam_list(seg, "vector", int(vm.group(1)), vm.end())
    um = re.search(r"value\s+uniform\s*\(([^)]+)\)", seg)
    if um is not None:
        return np.array([float(x) for x in um.group(1).split()],
                        dtype=float).reshape(1, 3)
    return None


def wall_report(case_dir: Path) -> dict | None:
    """Measured wall state from the latest written diagnostics, or None
    when the case predates them (runs generated before the yPlus1 /
    wallShearStress1 function objects existed).

    Per wing patch: reversed-flow fraction from the wallShearStress field
    (OpenFOAM's wall shear vector points along the near-wall flow with the
    sign of the patch-normal contraction — attached left-to-right flow
    reads tau_x < 0, so tau_x > 0 marks reversed flow), and y+ min/max/avg
    from the yPlus1 file output."""
    case_dir = Path(case_dir)
    # newest time dir carrying the shear field (write times + stop writes)
    tdirs = []
    for d in case_dir.iterdir() if case_dir.is_dir() else []:
        try:
            t = float(d.name)
        except ValueError:
            continue
        if (d / "wallShearStress").is_file():
            tdirs.append((t, d))
    yplus: dict[str, dict] = {}
    for dat in sorted(case_dir.glob("postProcessing/yPlus1/*/yPlus.dat")):
        try:
            for ln in dat.read_text(errors="replace").splitlines():
                if ln.startswith("#") or not ln.strip():
                    continue
                parts = ln.split()
                if len(parts) >= 5:
                    yplus[parts[1]] = {"min": round(float(parts[2]), 2),
                                       "max": round(float(parts[3]), 1),
                                       "avg": round(float(parts[4]), 2)}
        except (OSError, ValueError):
            continue
    separation: dict[str, dict] = {}
    if tdirs:
        _, tdir = max(tdirs)
        try:
            text = (tdir / "wallShearStress").read_text(errors="replace")
            for m in re.finditer(r"\b(wing_e\d+)\s*\{", text):
                patch = m.group(1)
                tau = _patch_boundary_values(text, patch)
                if tau is None or not len(tau):
                    continue
                separation[patch] = {
                    "reversed_frac": round(float((tau[:, 0] > 0).mean()), 3),
                    "n_faces": int(len(tau)),
                }
        except OSError:
            pass
    if not yplus and not separation:
        return None
    return {"yplus": {k: v for k, v in yplus.items()} or None,
            "separation": separation or None}


def latest_time_dir(case_dir: Path) -> Path:
    """The newest solved time directory that carries U and cell centres."""
    best, best_t = None, -1.0
    for d in Path(case_dir).iterdir():
        if not d.is_dir():
            continue
        try:
            t = float(d.name)
        except ValueError:
            continue
        if t > best_t and (d / "U").is_file() and (d / "C").is_file():
            best, best_t = d, t
    if best is None:
        raise PostError("no time directory with both U and cell centres "
                        "(C) — did run.sh's postProcess step run?")
    return best


# ---------- rendering ----------

def _installed_polys_m(cfg: StackConfig) -> list[np.ndarray]:
    design = geometry.build_stack(cfg)
    installed = geometry.install_stack(design, cfg.ride_height_c)
    return [np.asarray(e["coords"], float) * cfg.chord_m for e in installed]


def flow_png(case_dir: Path, cfg: StackConfig, field: str = "umag") -> bytes:
    """Render the solved section flow as a PNG (bytes).

    field: "umag" (velocity magnitude + streamlines) or "cp"."""
    if field not in ("umag", "cp"):
        raise PostError("field must be 'umag' or 'cp'")
    with _render_lock:
        return _flow_png_locked(case_dir, cfg, field)


def _flow_png_locked(case_dir: Path, cfg: StackConfig, field: str) -> bytes:
    from matplotlib.figure import Figure
    from matplotlib.path import Path as MplPath
    from scipy.interpolate import LinearNDInterpolator

    tdir = latest_time_dir(case_dir)
    cxy = parse_internal_field((tdir / "C").read_text(errors="replace"))[:, :2]
    u = parse_internal_field((tdir / "U").read_text(errors="replace"))[:, :2]
    if field == "cp":
        p = parse_internal_field((tdir / "p").read_text(errors="replace"))
        scalar = p / (0.5 * cfg.speed_ms ** 2)
    else:
        scalar = np.hypot(u[:, 0], u[:, 1])

    # a spiky or hand-stopped solve can carry non-finite cells; a few are
    # droppable, a field full of them is a diverged case, not a picture
    finite = (np.isfinite(scalar) & np.isfinite(u).all(axis=1)
              & np.isfinite(cxy).all(axis=1))
    n_bad = int((~finite).sum())
    if n_bad:
        if n_bad > 0.005 * finite.size:
            raise PostError(
                f"{n_bad} of {finite.size} cells are non-finite — the solve "
                f"diverged; there is no flow field to draw")
        cxy, u, scalar = cxy[finite], u[finite], scalar[finite]

    polys = _installed_polys_m(cfg)
    all_pts = np.vstack(polys)
    c = cfg.chord_m
    x0 = all_pts[:, 0].min() - 0.45 * c
    x1 = all_pts[:, 0].max() + 1.10 * c
    y1 = max(all_pts[:, 1].max() + 0.55 * c, 0.85 * c)

    nx = GRID_NX
    ny = max(int(nx * y1 / (x1 - x0)), 160)
    gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(0.0, y1, ny))
    # one triangulation + one simplex search for all three fields — three
    # separate griddata calls each rebuilt the Delaunay of ~100k centres
    # and tripled the render time under the lock
    interp = LinearNDInterpolator(
        cxy, np.column_stack([scalar, u[:, 0], u[:, 1]]))
    stacked = interp(np.column_stack([gx.ravel(), gy.ravel()]))
    gs = stacked[:, 0].reshape(gx.shape)
    gu = stacked[:, 1].reshape(gx.shape)
    gv = stacked[:, 2].reshape(gx.shape)

    # blank the element interiors — griddata happily interpolates across them
    flat = np.column_stack([gx.ravel(), gy.ravel()])
    inside = np.zeros(flat.shape[0], dtype=bool)
    for poly in polys:
        inside |= MplPath(poly).contains_points(flat)
    inside = inside.reshape(gx.shape)
    for g in (gs, gu, gv):
        g[inside] = np.nan

    bg, panel_fill, panel_edge = "#0c111c", "#232c40", "#93a3c2"
    fig = Figure(figsize=(11.6, 11.6 * y1 / (x1 - x0) + 0.55), dpi=PNG_DPI)
    ax = fig.add_subplot()
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    if field == "cp":
        lim = float(np.nanpercentile(np.abs(gs), 98))
        lim = max(lim, 0.5)
        cf = ax.contourf(gx, gy, gs, levels=np.linspace(-lim, lim, 41),
                         cmap="RdBu_r", extend="both")
        cb_label = "Cp"
    else:
        vmax = float(np.nanpercentile(gs, 99))
        cf = ax.contourf(gx, gy, gs, levels=np.linspace(0.0, vmax, 41),
                         cmap="magma", extend="max")
        cb_label = "|U|  [m/s]"
    # white lines carry on the dark magma field; the diverging Cp map is
    # mostly light, where white would vanish
    stream_color = "#ffffff" if field == "umag" else "#41506e"
    ax.streamplot(gx, gy, gu, gv, density=(2.4, 1.5), color=stream_color,
                  linewidth=0.55, arrowsize=0.7)
    # streamplot paints arrows over the blanked interiors too — the
    # silhouettes go on last so the elements always read as solid
    for poly in polys:
        ax.fill(poly[:, 0], poly[:, 1], facecolor=panel_fill,
                edgecolor=panel_edge, linewidth=1.0, zorder=5)
    ax.axhline(0.0, color="#93a3c2", linewidth=1.6, zorder=6)

    ax.set_xlim(x0, x1)
    ax.set_ylim(0.0, y1)
    ax.set_aspect("equal")
    ax.tick_params(colors="#7d8aa5", labelsize=8)
    for s in ax.spines.values():
        s.set_color("#2a3550")
    ax.set_xlabel("x  [m]", color="#7d8aa5", fontsize=9)
    ax.set_ylabel("y  [m]", color="#7d8aa5", fontsize=9)
    ax.set_title(f"RANS section flow — iteration {tdir.name}, "
                 f"as driven (flow left to right)",
                 color="#c6d0e2", fontsize=10)
    cb = fig.colorbar(cf, ax=ax, fraction=0.035, pad=0.015)
    cb.set_label(cb_label, color="#c6d0e2", fontsize=9)
    cb.ax.tick_params(colors="#7d8aa5", labelsize=8)
    cb.outline.set_edgecolor("#2a3550")

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg, bbox_inches="tight")
    return buf.getvalue()
