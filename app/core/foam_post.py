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
import math
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
                        "(C) — the case carries no readable flow field "
                        "(for an OpenFOAM case, check that run.sh's "
                        "postProcess step ran)")
    return best


# ---------- rendering ----------

def _installed_polys_m(cfg: StackConfig) -> list[np.ndarray]:
    design = geometry.build_stack(cfg)
    installed = geometry.install_stack(design, cfg.ride_height_c)
    return [np.asarray(e["coords"], float) * cfg.chord_m for e in installed]


# figure chrome + defaults per app theme: the dark set is the studio's
# original look; the light set sits on the drafting theme without a
# heavy dark slab, and defaults to the rainbow map Fluent users read
# natively. The colormap, range and streamlines are per-request
# settings on top (the "viewing settings you'd have manually").
_THEMES = {
    "dark": {"bg": "#0c111c", "panel": "#232c40", "edge": "#93a3c2",
             "ink": "#c6d0e2", "tick": "#7d8aa5", "spine": "#2a3550",
             "stream_umag": "#ffffff", "stream_cp": "#41506e",
             "cmap_umag": "magma", "cmap_cp": "RdBu_r"},
    "light": {"bg": "#ffffff", "panel": "#dfe4ee", "edge": "#46536f",
              "ink": "#242b3a", "tick": "#5a6478", "spine": "#c3cad8",
              "stream_umag": "#1c2434", "stream_cp": "#46536f",
              "cmap_umag": "turbo", "cmap_cp": "coolwarm"},
}
FLOW_CMAPS = ("magma", "viridis", "turbo", "coolwarm", "RdBu_r")


def flow_png(case_dir: Path, cfg: StackConfig, field: str = "umag",
             theme: str = "dark", cmap: str | None = None,
             vmin: float | None = None, vmax: float | None = None,
             streamlines: bool = True, extent: str = "section") -> bytes:
    """Render the solved section flow as a PNG (bytes).

    field: "umag" (velocity magnitude) or "cp". theme selects the figure
    chrome + default colormap ("dark" = the studio's original look);
    cmap/vmin/vmax/streamlines override the defaults the way the manual
    GUI's contour dialog would."""
    if field not in ("umag", "cp"):
        raise PostError("field must be 'umag' or 'cp'")
    if theme not in _THEMES:
        raise PostError("theme must be 'dark' or 'light'")
    if cmap is not None and cmap not in FLOW_CMAPS:
        raise PostError(f"cmap must be one of {FLOW_CMAPS}")
    if (vmin is not None and vmax is not None and not vmax > vmin):
        raise PostError("vmax must exceed vmin")
    if extent not in ("section", "domain"):
        raise PostError("extent must be 'section' or 'domain'")
    with _render_lock:
        return _flow_png_locked(case_dir, cfg, field, theme, cmap,
                                vmin, vmax, streamlines, extent)


def _field_grid(case_dir: Path, cfg: StackConfig, field: str,
                nx: int, extent: str = "section",
                window: tuple | None = None,
                with_cp: bool = False) -> dict:
    """The solved field interpolated onto a regular grid with element
    interiors masked — ONE code path shared by the PNG renderer and the
    animated-flow JSON endpoint, so the two views can never disagree
    about the flow they show. extent="section" windows in around the
    installed section (the working view); "domain" spans the full solve
    domain. window=(x0, y0, x1, y1) overrides both with an arbitrary
    box (clamped to the solved cloud) — the animated view's
    level-of-detail refetch grids just the zoomed-in region at full
    resolution, so zooming never runs out of pixels. with_cp adds the
    Cp field on the same grid as a fourth interpolation column, so the
    animated view's pressure backdrop never pays a second file parse
    or Delaunay rebuild."""
    from matplotlib.path import Path as MplPath
    from scipy.interpolate import LinearNDInterpolator

    tdir = latest_time_dir(case_dir)
    cxy = parse_internal_field((tdir / "C").read_text(errors="replace"))[:, :2]
    u = parse_internal_field((tdir / "U").read_text(errors="replace"))[:, :2]
    cp = None
    if field == "cp" or with_cp:
        p = parse_internal_field((tdir / "p").read_text(errors="replace"))
        cp = p / (0.5 * cfg.speed_ms ** 2)
    scalar = cp if field == "cp" else np.hypot(u[:, 0], u[:, 1])

    # a spiky or hand-stopped solve can carry non-finite cells; a few are
    # droppable, a field full of them is a diverged case, not a picture
    finite = (np.isfinite(scalar) & np.isfinite(u).all(axis=1)
              & np.isfinite(cxy).all(axis=1))
    if cp is not None:
        finite &= np.isfinite(cp)
    n_bad = int((~finite).sum())
    if n_bad:
        if n_bad > 0.005 * finite.size:
            raise PostError(
                f"{n_bad} of {finite.size} cells are non-finite — the solve "
                f"diverged; there is no flow field to draw")
        cxy, u, scalar = cxy[finite], u[finite], scalar[finite]
        if cp is not None:
            cp = cp[finite]

    polys = _installed_polys_m(cfg)
    all_pts = np.vstack(polys)
    c = cfg.chord_m
    if window is not None:
        wx0, wy0, wx1, wy1 = (float(v) for v in window)
        cx0, cx1 = float(cxy[:, 0].min()), float(cxy[:, 0].max())
        cy0, cy1 = float(cxy[:, 1].min()), float(cxy[:, 1].max())
        x0, x1 = max(wx0, cx0), min(wx1, cx1)
        y0, y1 = max(wy0, cy0), min(wy1, cy1)
        if not (x1 - x0 > 1e-6 and y1 - y0 > 1e-6):
            raise PostError("window lies outside the solved domain")
    elif extent == "domain":
        x0 = float(cxy[:, 0].min())
        x1 = float(cxy[:, 0].max())
        y0 = max(float(cxy[:, 1].min()), 0.0) \
            if cxy[:, 1].min() > -1e-6 else float(cxy[:, 1].min())
        y1 = float(cxy[:, 1].max())
    else:
        x0 = all_pts[:, 0].min() - 0.45 * c
        x1 = all_pts[:, 0].max() + 1.10 * c
        y0 = 0.0
        y1 = max(all_pts[:, 1].max() + 0.55 * c, 0.85 * c)

    # ny tracks the window aspect but the total pixel budget stays fixed:
    # a sliver window after the cloud clamp (1 mm wide by the full domain
    # height) must never allocate a multi-GB grid under the render lock
    ny = min(max(int(nx * (y1 - y0) / (x1 - x0)), 160 * nx // GRID_NX),
             4 * GRID_NX)
    gx, gy = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y0, y1, ny))
    # one triangulation + one simplex search for all three fields — three
    # separate griddata calls each rebuilt the Delaunay of ~100k centres
    # and tripled the render time under the lock
    cols = [scalar, u[:, 0], u[:, 1]]
    if with_cp:
        cols.append(cp)
    interp = LinearNDInterpolator(cxy, np.column_stack(cols))
    stacked = interp(np.column_stack([gx.ravel(), gy.ravel()]))
    gs = stacked[:, 0].reshape(gx.shape)
    gu = stacked[:, 1].reshape(gx.shape)
    gv = stacked[:, 2].reshape(gx.shape)
    gcp = stacked[:, 3].reshape(gx.shape) if with_cp else None

    # blank the element interiors — griddata happily interpolates across them
    flat = np.column_stack([gx.ravel(), gy.ravel()])
    inside = np.zeros(flat.shape[0], dtype=bool)
    for poly in polys:
        inside |= MplPath(poly).contains_points(flat)
    inside = inside.reshape(gx.shape)
    for g in ((gs, gu, gv) if gcp is None else (gs, gu, gv, gcp)):
        g[inside] = np.nan

    return {"tdir": tdir, "gx": gx, "gy": gy, "gs": gs, "gu": gu,
            "gv": gv, "gcp": gcp, "polys": polys, "x0": x0, "x1": x1,
            "y0": y0, "y1": y1}


def flow_field_json(case_dir: Path, cfg: StackConfig,
                    nx: int = 320, extent: str = "section",
                    window: tuple | None = None,
                    include_cp: bool = False) -> dict:
    """The solved velocity field on a regular grid, JSON-ready — the
    studio's animated flow view advects particles through it client-side
    at the case's actual velocities. Same parsing, gridding and interior
    masking as the PNG renderer; masked or out-of-hull cells are null.
    Arrays are row-major flat lists, row 0 at the BOTTOM (y ascending).
    extent: "section" (working window) or "domain" (the whole box).
    include_cp adds the Cp field on the identical grid (the animated
    view's pressure backdrop) plus cp_p98, its symmetric auto-scale."""
    if extent not in ("section", "domain"):
        raise PostError("extent must be 'section' or 'domain'")
    nx = max(120, min(int(nx), GRID_NX))
    with _render_lock:
        g = _field_grid(case_dir, cfg, "umag", nx, extent, window,
                        with_cp=include_cp)
    # a window sitting entirely inside an element silhouette grids to all
    # NaN — nanpercentile would hand json.dumps(allow_nan=False) a NaN
    if not np.isfinite(g["gs"]).any():
        raise PostError("window contains no flow cells — it lies entirely "
                        "inside an element silhouette")

    def flat(a):
        return [None if not math.isfinite(x) else round(float(x), 3)
                for x in np.asarray(a, float).ravel()]

    ny, nx_actual = g["gs"].shape
    out = {
        "nx": int(nx_actual), "ny": int(ny),
        "x0": round(float(g["x0"]), 5), "x1": round(float(g["x1"]), 5),
        "y0": round(float(g["y0"]), 5), "y1": round(float(g["y1"]), 5),
        "iter": g["tdir"].name,
        "speed_ms": cfg.speed_ms, "chord_m": cfg.chord_m,
        "umag_p99": round(float(np.nanpercentile(g["gs"], 99)), 3),
        "u": flat(g["gu"]), "v": flat(g["gv"]), "umag": flat(g["gs"]),
        "polys": [np.round(p, 5).tolist() for p in g["polys"]],
    }
    if g["gcp"] is not None:
        out["cp"] = flat(g["gcp"])
        # same floor as the static Cp render's auto-range
        out["cp_p98"] = round(max(float(np.nanpercentile(
            np.abs(g["gcp"]), 98)), 0.5), 3)
    return out


def _flow_png_locked(case_dir: Path, cfg: StackConfig, field: str,
                     theme: str = "dark", cmap: str | None = None,
                     vmin: float | None = None, vmax: float | None = None,
                     streamlines: bool = True,
                     extent: str = "section") -> bytes:
    from matplotlib.figure import Figure

    g = _field_grid(case_dir, cfg, field, GRID_NX, extent)
    tdir, polys = g["tdir"], g["polys"]
    gx, gy, gs, gu, gv = g["gx"], g["gy"], g["gs"], g["gu"], g["gv"]
    x0, x1, y0, y1 = g["x0"], g["x1"], g["y0"], g["y1"]

    pal = _THEMES[theme]
    bg, panel_fill, panel_edge = pal["bg"], pal["panel"], pal["edge"]
    fig = Figure(figsize=(11.6, 11.6 * (y1 - y0) / (x1 - x0) + 0.55),
                 dpi=PNG_DPI)
    ax = fig.add_subplot()
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)

    use_cmap = cmap or pal["cmap_cp" if field == "cp" else "cmap_umag"]
    # each bound overrides its default independently, both fields alike
    if field == "cp":
        lim = max(float(np.nanpercentile(np.abs(gs), 98)), 0.5)
        lo = vmin if vmin is not None else -lim
        hi = vmax if vmax is not None else lim
        cb_label = "Cp"
    else:
        hi = vmax if vmax is not None else \
            float(np.nanpercentile(gs, 99))
        lo = vmin if vmin is not None else 0.0
        cb_label = "|U|  [m/s]"
    # a lone bound can invert the resolved range (vmin above the auto
    # ceiling) — refuse with a parameter message, not a matplotlib error
    if not hi > lo:
        raise PostError("vmax must exceed vmin after defaults "
                        f"(resolved range {lo:g}..{hi:g})")
    cf = ax.contourf(gx, gy, gs, levels=np.linspace(lo, hi, 41),
                     cmap=use_cmap, extend="both")
    if streamlines:
        # per-theme stream ink: white carries on the dark magma field,
        # dark slate on the light chrome and the mostly-light Cp maps
        stream_color = pal["stream_umag" if field == "umag"
                           else "stream_cp"]
        ax.streamplot(gx, gy, gu, gv, density=(2.4, 1.5),
                      color=stream_color, linewidth=0.55, arrowsize=0.7)
    # streamplot paints arrows over the blanked interiors too — the
    # silhouettes go on last so the elements always read as solid
    for poly in polys:
        ax.fill(poly[:, 0], poly[:, 1], facecolor=panel_fill,
                edgecolor=panel_edge, linewidth=1.0, zorder=5)
    ax.axhline(0.0, color=panel_edge, linewidth=1.6, zorder=6)

    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal")
    ax.tick_params(colors=pal["tick"], labelsize=8)
    for s in ax.spines.values():
        s.set_color(pal["spine"])
    ax.set_xlabel("x  [m]", color=pal["tick"], fontsize=9)
    ax.set_ylabel("y  [m]", color=pal["tick"], fontsize=9)
    ax.set_title(f"RANS section flow — iteration {tdir.name}, "
                 f"as driven (flow left to right)",
                 color=pal["ink"], fontsize=10)
    cb = fig.colorbar(cf, ax=ax, fraction=0.035, pad=0.015)
    cb.set_label(cb_label, color=pal["ink"], fontsize=9)
    cb.ax.tick_params(colors=pal["tick"], labelsize=8)
    cb.outline.set_edgecolor(pal["spine"])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg, bbox_inches="tight")
    return buf.getvalue()
