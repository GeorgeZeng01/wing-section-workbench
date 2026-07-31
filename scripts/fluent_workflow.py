"""2D external-aero Fluent workflow: profile in, coefficients out.

Automates the documented manual ANSYS chain (CAD -> DXF -> Workbench ->
Discovery -> Mechanical mesh -> Fluent) into callable stages with every
knob exposed.
The DXF reader accepts exactly what the walkthrough exports — profile
splines/polylines, optionally with the domain rectangle drawn around them
(the rectangle is detected and used as the domain when present, so a
file prepared for the manual workflow runs unmodified). Meshing runs the
same gmsh machinery the studio's OpenFOAM cases use, in two modes:

- mesh_mode="walkthrough": the manual workflow's meshing intent —
  uniform profile edge sizing (default 1 mm), inflation grown from a
  fixed first-layer thickness (default 1 mm, 10 layers): wall-function
  y+ territory.
- mesh_mode="resolved" (default): the studio's doctrine — first layer at
  y+ ~ 1 from the flat-plate correlation, growth stack capped by the
  profile's own clearances. Strictly finer wall physics than the manual
  recipe; costs more cells.

Two routes turn that into a Fluent mesh. Default ("fluent"): the
geometry half here — resolve_sizes + write_slab_stl, a watertight
named-solid STL of the fluid slab — feeds ANSYS Fluent Meshing's
watertight workflow (fluent_mcp.mesh_native), which cuts every cell
the solver sees; no Docker anywhere in that chain. Option ("gmsh"):
build_domain_mesh's gmsh mesh goes through the same containerized
gmshToFoam/foamMeshToFluent bridge the studio's RANS cases use — the
identical-mesh footing with the OpenFOAM engine. Either way the
one-cell/thin slab solves in Fluent 3D with symmetry planes —
physically the walkthrough's 2D case. Boundary-condition setup,
conventions ("studio" corrections vs literal "walkthrough" parity) and
solving live in fluent_mcp.

Offline-testable: everything here except the container bridge runs
without Fluent, Docker or a license (the STL writer's plane
tessellation uses gmsh as a geometry tool, no solver anywhere).
"""
from __future__ import annotations

import dataclasses
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
OPENFOAM_IMAGE = os.environ.get("WSS_OPENFOAM_IMAGE",
                                "opencfd/openfoam-run:2406")
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# DXF $INSUNITS -> meters. Unitless files default to millimeters (the
# workflow's CAD exports are mm); pass scale= to override.
_INSUNITS_TO_M = {0: 1e-3, 1: 0.0254, 2: 0.3048, 4: 1e-3, 5: 1e-2,
                  6: 1.0, 14: 1e-1}


# ---------- DXF profiles ----------

def _entity_points(e, tol: float) -> np.ndarray | None:
    """One DXF entity as an (N, 2) polyline in drawing units."""
    t = e.dxftype()
    if t == "LWPOLYLINE":
        pts = np.array([(p[0], p[1]) for p in e.get_points()], float)
        if e.closed and len(pts) > 2:
            pts = np.vstack([pts, pts[:1]])
        return pts
    if t == "POLYLINE":
        pts = np.array([(v.dxf.location.x, v.dxf.location.y)
                        for v in e.vertices], float)
        if e.is_closed and len(pts) > 2:
            pts = np.vstack([pts, pts[:1]])
        return pts
    if t == "SPLINE":
        return np.array([(p[0], p[1]) for p in e.flattening(tol)], float)
    if t == "LINE":
        return np.array([(e.dxf.start.x, e.dxf.start.y),
                         (e.dxf.end.x, e.dxf.end.y)], float)
    if t in ("ARC", "CIRCLE"):
        a0, a1 = ((math.radians(e.dxf.start_angle),
                   math.radians(e.dxf.end_angle)) if t == "ARC"
                  else (0.0, 2 * math.pi))
        if a1 <= a0:
            a1 += 2 * math.pi
        n = max(int((a1 - a0) / (2 * math.pi) * 96), 8)
        ang = np.linspace(a0, a1, n)
        c, r = e.dxf.center, e.dxf.radius
        return np.column_stack([c.x + r * np.cos(ang),
                                c.y + r * np.sin(ang)])
    return None


def _chain_loops(segments: list[np.ndarray], tol: float
                 ) -> list[np.ndarray]:
    """Connect open segments end-to-end into closed loops (endpoints
    matched within tol; segments are reversed as needed)."""
    loops, open_segs = [], []
    for s in segments:
        if len(s) >= 3 and np.hypot(*(s[0] - s[-1])) <= tol:
            loops.append(s[:-1])
        else:
            open_segs.append(s)
    while open_segs:
        cur = open_segs.pop(0)
        grew = True
        while grew:
            grew = False
            for k, s in enumerate(open_segs):
                if np.hypot(*(cur[-1] - s[0])) <= tol:
                    cur = np.vstack([cur, s[1:]])
                elif np.hypot(*(cur[-1] - s[-1])) <= tol:
                    cur = np.vstack([cur, s[::-1][1:]])
                else:
                    continue
                open_segs.pop(k)
                grew = True
                break
        if len(cur) >= 3 and np.hypot(*(cur[0] - cur[-1])) <= tol:
            loops.append(cur[:-1])
        # an unclosable fragment is dropped — construction lines and
        # dimension leaders are normal DXF furniture
    return loops


def _poly_area(p: np.ndarray) -> float:
    x, y = p[:, 0], p[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _is_axis_rect(p: np.ndarray, tol: float) -> bool:
    """4-corner axis-aligned rectangle (the walkthrough's domain box)."""
    if len(p) < 4 or len(p) > 8:
        return False
    # collapse collinear runs to corners
    corners = []
    n = len(p)
    for i in range(n):
        a, b, c = p[i - 1], p[i], p[(i + 1) % n]
        v1, v2 = b - a, c - b
        if abs(v1[0] * v2[1] - v1[1] * v2[0]) > tol * (
                np.hypot(*v1) * np.hypot(*v2) + 1e-30):
            corners.append(b)
    if len(corners) != 4:
        return False
    corners = np.array(corners)
    for i in range(4):
        d = corners[(i + 1) % 4] - corners[i]
        if min(abs(d[0]), abs(d[1])) > tol * max(abs(d[0]), abs(d[1])):
            return False
    return True


def read_dxf(path: str | Path, scale: float | None = None) -> dict:
    """Profiles (and the domain box, when drawn) from a walkthrough-style
    DXF. Returns {"profiles": [(N,2) meters, ...], "domain": (x0,y0,x1,y1)
    meters or None, "scale": applied units factor}. Profiles are closed
    loops sorted largest-first; the domain rectangle — largest axis-
    aligned box enclosing every profile — is separated out exactly so a
    file drawn for the manual workflow needs no editing."""
    import ezdxf
    doc = ezdxf.readfile(str(path))
    if scale is None:
        scale = _INSUNITS_TO_M.get(int(doc.header.get("$INSUNITS", 0)),
                                   1e-3)
    segs = []
    ext_min, ext_max = None, None
    # flattening tolerance is max sagitta in DRAWING units — derive it
    # from a 25 um physical target through the units scale. A fixed 1e-3
    # was 1 um on mm files but a full millimetre on a $INSUNITS=6 meters
    # file: the leading edge collapsed to a handful of straight facets,
    # and those facets ARE the wall geometry on both mesher routes.
    flat_tol = max(1e-6, 2.5e-5 / scale)
    for e in doc.modelspace():
        pts = _entity_points(e, tol=flat_tol)
        if pts is None or len(pts) < 2:
            continue
        segs.append(pts)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        ext_min = lo if ext_min is None else np.minimum(ext_min, lo)
        ext_max = hi if ext_max is None else np.maximum(ext_max, hi)
    if not segs:
        raise ValueError(f"{path}: no usable curves found")
    # 1e-4 of the drawing diagonal: forgiving of real CAD endpoint slop
    # (0.11 mm on a metre-wide sheet) while orders of magnitude below any
    # slot gap, so distinct elements can never weld together
    diag = float(np.hypot(*(ext_max - ext_min)))
    loops = _chain_loops(segs, tol=max(1e-4 * diag, 1e-9))
    if not loops:
        raise ValueError(f"{path}: curves do not form closed loops")
    loops.sort(key=_poly_area, reverse=True)
    domain = None
    if len(loops) >= 2 and _is_axis_rect(loops[0], 1e-3):
        inner = np.vstack(loops[1:])
        lo, hi = loops[0].min(axis=0), loops[0].max(axis=0)
        if (inner[:, 0].min() >= lo[0] and inner[:, 0].max() <= hi[0]
                and inner[:, 1].min() >= lo[1]
                and inner[:, 1].max() <= hi[1]):
            domain = (float(lo[0]) * scale, float(lo[1]) * scale,
                      float(hi[0]) * scale, float(hi[1]) * scale)
            loops = loops[1:]
    profiles = [np.asarray(p, float) * scale for p in loops]
    return {"profiles": profiles, "domain": domain, "scale": scale}


# ---------- meshing ----------

@dataclasses.dataclass
class MeshSpec:
    """Every knob of the domain + mesh stage. Defaults follow the mode:
    "resolved" is the studio doctrine (y+ ~ 1 first layer computed from
    the flow), "walkthrough" is the manual workflow's recipe (fixed
    millimetre inflation, wall-function y+). Any explicit value
    overrides its mode default."""
    mode: str = "resolved"              # "resolved" | "walkthrough"
    # domain (used when the DXF carries no rectangle): multiples of the
    # profile stack's length L / height H, per the walkthrough
    front_l: float = 3.0                # inlet distance, x L
    back_l: float = 7.0                 # outlet distance (doc: 5-10), x L
    top_h: float = 3.0                  # ceiling above, x H
    ground_y: float | None = None       # ground plane y (m); None = free
                                        # air (symmetric box, slip floor)
    # sizing
    edge_size_m: float | None = None    # profile wall size; None = mode
                                        # default (walkthrough 1e-3,
                                        # resolved L/300 capped to
                                        # [0.5, 2] mm)
    far_size_m: float | None = None     # far field; None = span/40
    # inflation
    first_layer_m: float | None = None  # None = mode default
                                        # (walkthrough 1e-3, resolved
                                        # y+~1 from U/nu)
    n_layers: int | None = None         # walkthrough default 10;
                                        # resolved: auto
    growth: float = 1.2
    # flow numbers the resolved first layer needs
    speed_ms: float = 15.0
    nu: float = 1.5e-5
    # slab
    dz_frac: float = 0.1                # extrusion depth, x L


def _first_layer_resolved(u: float, nu: float, length: float) -> float:
    re = max(u * length / nu, 1e3)
    u_tau = u * math.sqrt(0.5 * 0.058 * re ** -0.2)
    return 2.0 * nu / u_tau          # first CELL height for y+ ~ 1


def resolve_sizes(profiles: list[np.ndarray], spec: MeshSpec,
                  domain_m: tuple | None = None) -> dict:
    """Every sizing number a mesher needs, resolved from the spec and
    the geometry — ONE code path for the gmsh route and the
    Fluent-native route, so 'walkthrough' and 'resolved' mean the same
    numbers whichever tool cuts the cells. Returns domain box, edge/far
    sizes, first layer, layer count (explicit even in resolved mode —
    native meshing needs a count, not a thickness cap), per-profile
    stack thickness caps, and the slab depth."""
    if spec.mode not in ("resolved", "walkthrough"):
        raise ValueError("mesh mode must be 'resolved' or 'walkthrough'")
    allp = np.vstack(profiles)
    lx = float(allp[:, 0].max() - allp[:, 0].min())
    ly = float(allp[:, 1].max() - allp[:, 1].min())
    if lx <= 0 or ly <= 0:
        raise ValueError("degenerate profile extents")
    if domain_m is not None:
        x0, yb, x1, yt = domain_m
    else:
        x0 = allp[:, 0].min() - spec.front_l * lx
        x1 = allp[:, 0].max() + spec.back_l * lx
        yt = allp[:, 1].max() + spec.top_h * ly
        yb = (spec.ground_y if spec.ground_y is not None
              else allp[:, 1].min() - spec.top_h * ly)
    if yb >= allp[:, 1].min():
        raise ValueError("ground/floor plane sits at or above the profile")
    edge = spec.edge_size_m or (
        1e-3 if spec.mode == "walkthrough"
        else min(max(lx / 300.0, 0.5e-3), 2e-3))
    far = spec.far_size_m or (x1 - x0) / 40.0
    h1 = spec.first_layer_m or (
        1e-3 if spec.mode == "walkthrough"
        else _first_layer_resolved(spec.speed_ms, spec.nu, lx))
    n_layers = spec.n_layers or (10 if spec.mode == "walkthrough" else 0)
    clear = []
    for i, p in enumerate(profiles):
        own = [float(p[:, 1].min() - yb)]
        for j, q in enumerate(profiles):
            if j != i:
                d = np.min(np.hypot(
                    p[:, None, 0] - q[None, ::4, 0],
                    p[:, None, 1] - q[None, ::4, 1]))
                own.append(float(d))
        clear.append(max(min(own), 1e-5))
    if n_layers:
        thick = [h1 * (spec.growth ** n_layers - 1) / (spec.growth - 1)] \
            * len(profiles)
        thick = [min(t, 0.4 * c) for t, c in zip(thick, clear)]
    else:
        thick = [min(0.35 * c, 0.08 * lx) for c in clear]
    # resolved mode caps by thickness rather than count; native meshing
    # needs a count, so convert the tightest cap back through the
    # geometric series (clamped to a sane stack). An explicit
    # (walkthrough) count passes through the same cap: the native route
    # takes a count, not a thickness, so an uncapped stack would grow
    # prisms from both walls of a slot gap the gmsh route's Thickness
    # field protects.
    # (+1e-9 so an uncapped stack round-trips to exactly n_layers)
    t_min = min(thick)
    n_from_cap = max(1, int(math.log(
        1.0 + t_min * (spec.growth - 1.0) / h1) / math.log(spec.growth)
        + 1e-9))
    n_explicit = (min(n_layers, n_from_cap) if n_layers
                  else max(4, min(30, n_from_cap)))
    return {"x0": x0, "yb": yb, "x1": x1, "yt": yt, "lx": lx, "ly": ly,
            "edge": edge, "far": far, "h1": h1, "n_layers": n_layers,
            "n_layers_explicit": n_explicit, "thick": thick,
            "dz": spec.dz_frac * lx}


def clean_profiles(profiles_m: list[np.ndarray]) -> list[np.ndarray]:
    """Deduplicated open loops ready for meshing: coincident consecutive
    vertices (spline-flattening joins, DXF round trips, closed-polyline
    duplicates) removed, explicit closing points dropped — the same
    guard the studio's mesher carries, shared by both mesher routes."""
    profiles = []
    for p in profiles_m:
        p = np.asarray(p, float)
        diag = float(np.hypot(*(p.max(axis=0) - p.min(axis=0)))) or 1.0
        tol = max(1e-9, 1e-7 * diag)
        p = p[np.r_[True, np.hypot(*np.diff(p, axis=0).T) > tol]]
        if len(p) > 2 and np.hypot(*(p[0] - p[-1])) <= tol:
            p = p[:-1]
        if len(p) >= 3:
            profiles.append(p)
    if not profiles:
        raise ValueError("no profiles to mesh")
    return profiles


def _sharp_corners(p: np.ndarray, deg: float = 45.0) -> list[int]:
    """Vertex indices where the contour turns harder than deg — trailing
    edges and slot lips; the boundary-layer fans anchor there."""
    n = len(p)
    out = []
    for i in range(n):
        v1 = p[i] - p[i - 1]
        v2 = p[(i + 1) % n] - p[i]
        c = np.dot(v1, v2) / (np.hypot(*v1) * np.hypot(*v2) + 1e-30)
        if math.degrees(math.acos(max(-1.0, min(1.0, c)))) > deg:
            out.append(i)
    return out or [0]


def build_domain_mesh(profiles_m: list[np.ndarray], out_dir: str | Path,
                      spec: MeshSpec,
                      domain_m: tuple | None = None) -> dict:
    """gmsh the walkthrough's domain around the profiles and write
    mesh.msh (MSH2, one-cell slab) into out_dir. Physical groups follow
    the studio convention the Fluent setup expects: inlet / outlet /
    top / ground / profile_e{i} / frontAndBack. Returns a stats summary
    including every resolved sizing number, so a run's mesh provenance
    is inspectable rather than implicit."""
    import gmsh
    out_dir = Path(out_dir)
    profiles = clean_profiles(profiles_m)
    s = resolve_sizes(profiles, spec, domain_m)
    out_dir.mkdir(parents=True, exist_ok=True)
    x0, yb, x1, yt = s["x0"], s["yb"], s["x1"], s["yt"]
    lx = s["lx"]
    edge, far, h1 = s["edge"], s["far"], s["h1"]
    n_layers, thick = s["n_layers"], s["thick"]

    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("profile2d")
        occ = gmsh.model.occ
        cp = [occ.addPoint(x, y, 0.0) for x, y in
              [(x0, yb), (x1, yb), (x1, yt), (x0, yt)]]
        edges = [occ.addLine(cp[i], cp[(i + 1) % 4]) for i in range(4)]
        ground_e, outlet_e, top_e, inlet_e = edges

        loops_tags, wing_lines, fan_pts = [], [], []
        for p in profiles:
            pts = [occ.addPoint(x, y, 0.0) for x, y in p]
            lines = [occ.addLine(pts[i], pts[(i + 1) % len(pts)])
                     for i in range(len(pts))]
            wing_lines.append(lines)
            loops_tags.append(occ.addCurveLoop(lines))
            fan_pts.append([pts[k] for k in _sharp_corners(p)])
        surf = occ.addPlaneSurface([occ.addCurveLoop(edges)] + loops_tags)
        dz = spec.dz_frac * lx
        ext = occ.extrude([(2, surf)], 0, 0, dz, numElements=[1],
                          heights=[1.0], recombine=True)
        occ.synchronize()
        back = ext[0][1]
        vol = next(t for d, t in ext if d == 3)

        def lateral(line):
            up, _ = gmsh.model.getAdjacencies(1, line)
            return next(s for s in up if s != surf)

        grp = gmsh.model.addPhysicalGroup
        grp(2, [lateral(inlet_e)], name="inlet")
        grp(2, [lateral(outlet_e)], name="outlet")
        grp(2, [lateral(top_e)], name="top")
        grp(2, [lateral(ground_e)], name="ground")
        for i, lines in enumerate(wing_lines):
            grp(2, [lateral(li) for li in lines], name=f"profile_e{i+1}")
        grp(2, [surf, back], name="frontAndBack")
        grp(3, [vol], name="internal")

        f = gmsh.model.mesh.field
        flat = [li for lines in wing_lines for li in lines]
        dist = f.add("Distance")
        f.setNumbers(dist, "CurvesList", flat)
        f.setNumber(dist, "Sampling", 20)
        thr = f.add("Threshold")
        f.setNumber(thr, "InField", dist)
        f.setNumber(thr, "SizeMin", edge)
        f.setNumber(thr, "SizeMax", far)
        f.setNumber(thr, "DistMin", 0.05 * lx)
        f.setNumber(thr, "DistMax", 3.0 * lx)
        f.setAsBackgroundMesh(thr)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)

        def add_bl(lines, fans, t):
            bl = f.add("BoundaryLayer")
            f.setNumbers(bl, "CurvesList", lines)
            f.setNumber(bl, "Size", h1)
            f.setNumber(bl, "Ratio", spec.growth)
            f.setNumber(bl, "Thickness", t)
            f.setNumber(bl, "Quads", 1)
            f.setNumbers(bl, "FanPointsList", fans)
            f.setAsBoundaryLayer(bl)
            return bl

        # the studio's fallback ladder: per-loop stacks -> one global
        # stack -> pure refinement; every rung is a correct case (the
        # solver's wall treatment is y+-adaptive), quality is reported
        bl_tags = [add_bl(lines, fans, t) for lines, fans, t
                   in zip(wing_lines, fan_pts, thick)]
        bl_mode = "per-profile"
        try:
            gmsh.model.mesh.generate(3)
        except Exception:
            gmsh.model.mesh.clear()
            for t in bl_tags:
                f.remove(t)
            bl_mode = "global"
            bl = add_bl(flat, [p for fp in fan_pts for p in fp],
                        min(thick))
            try:
                gmsh.model.mesh.generate(3)
            except Exception:
                gmsh.model.mesh.clear()
                f.remove(bl)
                bl_mode = None
                gmsh.model.mesh.generate(3)

        _, tags3, _ = gmsh.model.mesh.getElements(3)
        n_cells = sum(len(t) for t in tags3)
        if n_cells == 0:
            raise RuntimeError("gmsh produced no volume cells")
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        msh_path = out_dir / "mesh.msh"
        gmsh.write(str(msh_path))
    finally:
        gmsh.finalize()

    return {
        "msh_path": str(msh_path), "n_cells": int(n_cells),
        "bl_mode": bl_mode, "mode": spec.mode,
        "edge_size_m": edge, "far_size_m": far,
        "first_layer_m": h1, "n_layers": n_layers or "auto",
        "growth": spec.growth, "bl_thickness_m": [round(t, 6)
                                                  for t in thick],
        "domain_m": [round(v, 5) for v in (x0, yb, x1, yt)],
        "domain_source": "dxf" if domain_m is not None else "auto",
        "ground": spec.ground_y is not None or domain_m is not None,
        "length_m": lx, "dz_m": dz,
        "profiles": len(profiles),
        "profile_zones": [f"profile_e{i+1}" for i in range(len(profiles))],
    }


# ---------- Fluent-native geometry (STL slab) ----------
#
# The all-ANSYS meshing route: instead of the containerized
# foamMeshToFluent bridge, the domain is written as a watertight
# multi-solid ASCII STL — the fluid volume's boundary, with one named
# solid per boundary zone — and Fluent Meshing's watertight workflow
# cuts every cell the solver sees (see fluent_mcp.mesh_native). Solid
# names drive Fluent's boundary-type inference: inlet -> velocity-inlet,
# outlet -> pressure-outlet, symmetry-* -> symmetry, everything else
# wall. The z-plane faces are tessellated by gmsh as a GEOMETRY step
# (graded, quality-bounded triangles): measured on 2026 R1, a naive
# ear-clip triangulation put aspect-1e5 slivers on the 5.6 m planes and
# TGrid's import culled ~87% of them as degenerate, leaving free faces
# the surface remesher could not recover. The tunnel walls keep the raw
# polyline facets (import kept every one), and the box faces follow the
# plane tessellation's edge subdivision, so the surface is exactly
# conformal — the self-check write_slab_stl runs before returning
# refuses anything else.

def _plane_triangulation(profiles: list[np.ndarray], x0: float,
                         yb: float, x1: float, yt: float,
                         plane_size: float) -> dict:
    """Graded triangulation of the slab's side plane (domain rectangle
    with the profile tunnels cut out) — gmsh as the geometry
    tessellator, NOT the CFD mesher (Fluent remeshes walls and fills
    the volume). Hole boundaries are transfinite 2-node curves, so the
    profile polylines are never subdivided and the tunnel walls built
    from the raw polylines match verbatim; rectangle edges DO subdivide
    toward plane_size, and the returned per-edge point orderings are
    what the box faces must be built from. Caller owns gmsh
    serialization (global C state) and the Win32 PATH restore."""
    import gmsh
    corners = [(x0, yb), (x1, yb), (x1, yt), (x0, yt)]
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("wss_slab_plane")
        occ = gmsh.model.occ
        cp = [occ.addPoint(x, y, 0.0, meshSize=plane_size)
              for x, y in corners]
        rect_lines = [occ.addLine(cp[i], cp[(i + 1) % 4])
                      for i in range(4)]
        loops = [occ.addCurveLoop(rect_lines)]
        hole_lines: list[int] = []
        for p in profiles:
            n = len(p)
            seg = np.hypot(*np.diff(np.vstack([p, p[:1]]), axis=0).T)
            pts = [occ.addPoint(float(x), float(y), 0.0,
                                meshSize=float(max(
                                    min(seg[k - 1], seg[k]), 1e-5)))
                   for k, (x, y) in enumerate(p)]
            lines = [occ.addLine(pts[k], pts[(k + 1) % n])
                     for k in range(n)]
            hole_lines.extend(lines)
            loops.append(occ.addCurveLoop(lines))
        surf = occ.addPlaneSurface(loops)
        occ.synchronize()
        for li in hole_lines:
            gmsh.model.mesh.setTransfiniteCurve(li, 2)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 1)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.model.mesh.generate(2)
        ntags, ncoords, _ = gmsh.model.mesh.getNodes()
        xy = {int(t): (float(ncoords[3 * i]), float(ncoords[3 * i + 1]))
              for i, t in enumerate(ntags)}
        tris: list[tuple] = []
        etypes, _etags, enodes = gmsh.model.mesh.getElements(2, surf)
        for et, nodes in zip(etypes, enodes):
            if int(et) != 2:      # 3-node triangles only
                continue
            for k in range(0, len(nodes), 3):
                tris.append((xy[int(nodes[k])], xy[int(nodes[k + 1])],
                             xy[int(nodes[k + 2])]))
        if not tris:
            raise ValueError("plane tessellation produced no triangles")
        rect_edges: list[list[tuple]] = []
        for i, li in enumerate(rect_lines):
            a = np.array(corners[i], float)
            b = np.array(corners[(i + 1) % 4], float)
            d = b - a
            tags_l, coords_l, _ = gmsh.model.mesh.getNodes(
                1, li, includeBoundary=True)
            pts_l = [(float(coords_l[3 * k]), float(coords_l[3 * k + 1]))
                     for k in range(len(tags_l))]
            # order along the edge by projection — parametric coords of
            # boundary nodes are version-dependent, projections are not
            pts_l.sort(key=lambda q: float(
                np.dot(np.array(q) - a, d)))
            rect_edges.append(pts_l)
        return {"tris": tris, "rect_edges": rect_edges}
    finally:
        gmsh.finalize()


def _facet(fh, a, b, c) -> None:
    n = np.cross(np.asarray(b) - np.asarray(a),
                 np.asarray(c) - np.asarray(a))
    ln = float(np.linalg.norm(n)) or 1.0
    n = n / ln
    fh.write(f" facet normal {n[0]:.9e} {n[1]:.9e} {n[2]:.9e}\n"
             "  outer loop\n")
    for v in (a, b, c):
        fh.write(f"   vertex {v[0]:.9e} {v[1]:.9e} {v[2]:.9e}\n")
    fh.write("  endloop\n endfacet\n")


def write_slab_stl(profiles_m: list[np.ndarray], out_path: str | Path,
                   domain_m: tuple, dz: float,
                   zone_names: list[str] | None = None,
                   plane_size_m: float | None = None) -> dict:
    """The fluid slab's boundary as a watertight multi-solid ASCII STL:
    box faces (inlet/outlet/top/ground), the two z-planes with the
    profile tunnels cut out (symmetry-front/back — the names make
    Fluent's import infer symmetry), and one wall solid per profile
    tunnel. plane_size_m grades the z-plane tessellation's far-field
    triangle size (default: a fortieth of the domain's longer side).
    All normals point out of the fluid. Verifies its own conformality
    (every edge on exactly two facets) and volume before returning.
    Callers on gmsh-sharing threads must serialize (the plane
    tessellation runs gmsh — global C state)."""
    profiles = clean_profiles(profiles_m)
    x0, yb, x1, yt = (float(v) for v in domain_m)
    dz = float(dz)
    if dz <= 0:
        raise ValueError("slab depth must be positive")
    names = zone_names or [f"profile_e{i+1}"
                           for i in range(len(profiles))]
    if len(names) != len(profiles):
        raise ValueError("one zone name per profile required")
    plane_size = float(plane_size_m or max(x1 - x0, yt - yb) / 40.0)

    def ccw(p):
        a2 = sum(p[i][0] * p[(i + 1) % len(p)][1]
                 - p[(i + 1) % len(p)][0] * p[i][1]
                 for i in range(len(p)))
        return p if a2 >= 0 else p[::-1]

    profiles = [ccw(np.asarray(p, float)) for p in profiles]
    plane = _plane_triangulation(profiles, x0, yb, x1, yt, plane_size)

    solids: dict[str, list[tuple]] = {}

    def add(name, a, b, c):
        solids.setdefault(name, []).append((a, b, c))

    # z-planes: back (z=dz) wound CCW seen from +z (outward +z), front
    # (z=0) reversed (outward -z); gmsh triangle orientation is
    # normalized per triangle rather than assumed
    for (a, b, c) in plane["tris"]:
        if ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0])) < 0:
            b, c = c, b
        add("symmetry-back", (a[0], a[1], dz), (b[0], b[1], dz),
            (c[0], c[1], dz))
        add("symmetry-front", (a[0], a[1], 0.0), (c[0], c[1], 0.0),
            (b[0], b[1], 0.0))

    # box faces: quad strips following the plane tessellation's edge
    # subdivision (conformality demands identical boundary points).
    # rect_edges walk the rectangle CCW, so one outward winding pattern
    # serves all four sides — the volume self-check proves it
    for edge_pts, name in zip(plane["rect_edges"],
                              ("ground", "outlet", "top", "inlet")):
        for p, q in zip(edge_pts[:-1], edge_pts[1:]):
            add(name, (p[0], p[1], 0.0), (q[0], q[1], 0.0),
                (q[0], q[1], dz))
            add(name, (p[0], p[1], 0.0), (q[0], q[1], dz),
                (p[0], p[1], dz))

    # tunnel walls: raw polyline facets (TGrid keeps every one —
    # measured), fluid-outward = into the tunnel. With a CCW loop
    # (fluid outside it), the quad (k,z0)-(k,z1)-(k+1,z1)-(k+1,z0)
    # winds so the normal points into the loop's interior
    for p, name in zip(profiles, names):
        for k in range(len(p)):
            a2, b2 = p[k], p[(k + 1) % len(p)]
            add(name, (a2[0], a2[1], 0.0), (a2[0], a2[1], dz),
                (b2[0], b2[1], dz))
            add(name, (a2[0], a2[1], 0.0), (b2[0], b2[1], dz),
                (b2[0], b2[1], 0.0))

    # ---- self-checks: conformal closed surface, correct volume ----
    from collections import Counter
    edges: Counter = Counter()
    vol6 = 0.0
    for tris in solids.values():
        for (a, b, c) in tris:
            for u, v in ((a, b), (b, c), (c, a)):
                edges[frozenset((u, v))] += 1
            vol6 += float(np.dot(a, np.cross(b, c)))
    bad = [e for e, n in edges.items() if n != 2]
    if bad:
        raise ValueError(f"slab surface is not closed: {len(bad)} "
                         f"edge(s) not shared by exactly two facets")
    hole_area = sum(_poly_area(p) for p in profiles)
    want = ((x1 - x0) * (yt - yb) - hole_area) * dz
    got = vol6 / 6.0
    if not math.isclose(got, want, rel_tol=1e-6):
        raise ValueError(f"slab volume check failed: {got:.6g} vs "
                         f"{want:.6g} m^3 (normals or triangulation)")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="ascii", newline="\n") as fh:
        for name, tris in solids.items():
            fh.write(f"solid {name}\n")
            for (a, b, c) in tris:
                _facet(fh, a, b, c)
            fh.write(f"endsolid {name}\n")
    return {"stl_path": str(out_path),
            "n_facets": sum(len(t) for t in solids.values()),
            "zones": list(solids.keys()),
            "profile_zones": names,
            "plane_size_m": plane_size,
            "volume_m3": round(got, 9), "dz_m": dz,
            "domain_m": [round(v, 5) for v in (x0, yb, x1, yt)]}


# ---------- gmsh -> Fluent bridge ----------

def gmsh_to_fluent(msh_path: str | Path, work_dir: str | Path
                   ) -> str:
    """mesh.msh -> Fluent case.msh via gmshToFoam + foamMeshToFluent in
    the studio's OpenFOAM container (Docker Desktop must be running).
    The z-planes are typed symmetry so the slab solves in Fluent 3D as
    the walkthrough's 2D case."""
    msh_path, work = Path(msh_path), Path(work_dir)
    (work / "system").mkdir(parents=True, exist_ok=True)
    if msh_path.resolve() != (work / "mesh.msh").resolve():
        import shutil
        shutil.copy2(msh_path, work / "mesh.msh")
    (work / "system" / "controlDict").write_text(
        "FoamFile\n{\n    version 2.0;\n    format ascii;\n"
        "    class dictionary;\n    object controlDict;\n}\n"
        "deltaT 1;\nwriteInterval 1;\n", encoding="utf-8")
    try:
        sys.path.insert(0, str(REPO))
        from app.core.cfd_run import _docker_exe
        docker = _docker_exe()
    except Exception:
        docker = "docker"
    inner = ("source /usr/lib/openfoam/openfoam*/etc/bashrc; "
             "gmshToFoam mesh.msh > log.gmshToFoam 2>&1 && "
             "foamDictionary constant/polyMesh/boundary "
             "-entry entry0/frontAndBack/type -set symmetry "
             "> /dev/null && "
             "foamMeshToFluent > log.foamMeshToFluent 2>&1 "
             "&& ls fluentInterface/")
    r = subprocess.run(
        [docker, "run", "--rm", "--name",
         f"wss-fluent-mesh-{os.getpid()}-{int(time.time())}",
         "-v", f"{work}:/case", "-w", "/case",
         "--entrypoint", "/bin/bash", OPENFOAM_IMAGE, "-c", inner],
        capture_output=True, text=True, timeout=900,
        creationflags=_CREATE_NO_WINDOW)
    if r.returncode != 0:
        tails = []
        for lg in ("log.gmshToFoam", "log.foamMeshToFluent"):
            p = work / lg
            if p.is_file():
                tails.append(f"{lg}: "
                             + p.read_text(errors='replace')[-400:])
        raise RuntimeError(
            f"mesh conversion failed: "
            f"{(r.stderr or r.stdout).strip()[-300:]}\n" + "\n".join(tails))
    out = sorted((work / "fluentInterface").glob("*.msh"))
    if not out:
        raise RuntimeError("conversion produced no .msh")
    return str(out[0])
