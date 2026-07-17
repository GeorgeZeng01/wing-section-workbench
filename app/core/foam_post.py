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

def parse_internal_field(text: str) -> np.ndarray:
    """The internalField of an ASCII vol field as an array.

    nonuniform List<scalar>  ->  (N,)
    nonuniform List<vector>  ->  (N, 3)
    Uniform fields are rejected — for the solved fields this module reads,
    a uniform internalField means the case never actually ran."""
    m = re.search(r"internalField\s+nonuniform\s+List<(scalar|vector)>\s*"
                  r"(\d+)\s*\(", text)
    if m is None:
        if re.search(r"internalField\s+uniform", text):
            raise PostError("field is uniform — the case has no solved flow")
        raise PostError("no parsable internalField in the file")
    kind, n = m.group(1), int(m.group(2))
    start = m.end()
    end = text.find("\n)", start)
    if end < 0:
        raise PostError("unterminated internalField list")
    block = text[start:end]
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
    from scipy.interpolate import griddata

    tdir = latest_time_dir(case_dir)
    cxy = parse_internal_field((tdir / "C").read_text(errors="replace"))[:, :2]
    u = parse_internal_field((tdir / "U").read_text(errors="replace"))[:, :2]
    if field == "cp":
        p = parse_internal_field((tdir / "p").read_text(errors="replace"))
        scalar = p / (0.5 * cfg.speed_ms ** 2)
    else:
        scalar = np.hypot(u[:, 0], u[:, 1])

    polys = _installed_polys_m(cfg)
    all_pts = np.vstack(polys)
    c = cfg.chord_m
    x0 = all_pts[:, 0].min() - 0.45 * c
    x1 = all_pts[:, 0].max() + 1.10 * c
    y1 = max(all_pts[:, 1].max() + 0.55 * c, 0.85 * c)

    nx = GRID_NX
    ny = max(int(nx * y1 / (x1 - x0)), 160)
    gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(0.0, y1, ny))
    gs = griddata(cxy, scalar, (gx, gy), method="linear")
    gu = griddata(cxy, u[:, 0], (gx, gy), method="linear")
    gv = griddata(cxy, u[:, 1], (gx, gy), method="linear")

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
