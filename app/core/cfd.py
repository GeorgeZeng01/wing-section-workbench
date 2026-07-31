"""OpenFOAM 2D RANS case generation — the roadmap's mesh + case items.

Turns the current stack into a ready-to-run simpleFoam case: a gmsh mesh of
the installed section in METERS (ground at y = 0, downforce down), k-omega
SST, a ground plane translating at the freestream speed, and a forceCoeffs
function object referenced to the main chord with liftDir (0 -1 0) — the
reported Cl is downforce-positive.

Mesh: unstructured triangles graded by a Distance+Threshold field from the
wing contours (0.2–0.7 %c at the wall to 30–60 %c far, by preset), plus quad
boundary layers grown from the airfoil walls (gmsh BoundaryLayer field, fan
elements at the trailing-edge corners). The wing BL curves are closed loops,
so the layer stack never terminates mid-line. The ground gets NO layer
stack: gmsh can only end a boundary layer on a collinear continuation by
staircasing each layer one surface cell further along the wall, which
produces sliver quads (aspect ratio in the thousands, skewness > 10,
non-orthogonality ~ 90 deg) that blow up simpleFoam regardless of scheme
limiting. Under a loaded wing the gap flow runs well past belt speed, so
the moving ground DOES carry real shear there (measured y+ up to ~250 at
fine on the aggressive 3-element case) — that is wall-function range, and
the y+-adaptive treatment below is exactly what handles it on the graded
isotropic cells under the wing.
The first-layer height targets y+ ~ 1 via the flat-plate correlation
Cf = 0.058 Re^-0.2, u_tau = U sqrt(Cf/2), h1 = 2 y+ nu / u_tau (the first
cell CENTER sits at h1/2). The layer stack is capped at a fraction of the
tightest clearance in the geometry (slot gaps, ride height) so opposing
layers cannot collide across a slot or the ground gap. The BoundaryLayer
field is gmsh's fragile corner: if it fails on a geometry, the mesh is
regenerated without it (pure refinement) and the case remains correct —
the 0/* files use y+-adaptive wall treatment (omegaWallFunction blends,
kLowReWallFunction and nutUSpaldingWallFunction are continuous in y+), so
resolved-wall and refinement-fallback meshes run the same case unchanged.

OpenFOAM needs a 3D mesh one cell thick: the meshed surface is extruded by
0.1 chord (one recombined layer -> prisms/hexes) and both z-planes land in
the frontAndBack physical group, which run.sh switches to 'empty' after
gmshToFoam (MSH2 output — gmshToFoam reads nothing newer).

gmsh is a global-state C library and is not thread-safe: build_case
serializes on a module lock and runs initialize/finalize per call.
"""

from __future__ import annotations

import dataclasses
import math
import sys
import threading
from pathlib import Path

import numpy as np

from . import geometry
from .geometry import StackConfig

Y_PLUS_TARGET = 1.0
DZ_C = 0.1                  # extrusion depth, chords (2D slab thickness)
# 16 chords of headroom: an A/B on the aggressive 3-element case (Cl ~ 7.5)
# measured the 8-chord slip ceiling inflating Cl ~5% and Cd far more —
# tunnel confinement, not physics. Doubling the height costs only ~6% more
# cells (the far field is coarse), so the taller box is close to free.
X_UP_C, X_DOWN_C, Y_TOP_C = 6.0, 12.0, 16.0   # domain extents, chords
BL_CLEAR_FRAC = 0.4         # layer stack <= this share of ITS OWN clearance
SLOT_GAP_CELLS = 8          # minimum cells across each slot jet
MESH_SURFACE_MIN_N = 140    # wall polyline nodes/side for meshing (see
                            # build_case: decoupled from the panel count)
TURB_INTENSITY = 0.01       # inlet turbulence intensity (on-track air)
TURB_VISC_RATIO = 10.0      # inlet nut/nu, sets omega
# high-lift stacks converge on force history around 8-11k iterations; the
# old 3000 default truncated exported runs mid-transient (the in-app runs
# stop on force drift instead, so the cap rarely binds there)
N_ITERS = 8000

# wall/far sizes in chords; targets ~15k/40k/90k 2D cells on the default
# two-element stack (calibrated)
MESH_PRESETS = {
    "coarse": {"wall": 0.007, "far": 0.60, "bl_ratio": 1.40, "bl_thick": 0.020},
    "medium": {"wall": 0.0035, "far": 0.40, "bl_ratio": 1.25, "bl_thick": 0.025},
    "fine": {"wall": 0.002, "far": 0.30, "bl_ratio": 1.20, "bl_thick": 0.030},
}

_gmsh_lock = threading.Lock()


# gmsh's C runtime REPLACES the real Win32 process PATH during
# initialize/finalize (measured: 1822 -> 357 chars) without touching
# Python's os.environ snapshot — every subprocess launched afterwards
# (docker for the in-app RANS runs, explorer for "Show in folder") then
# fails to resolve executables. The real environment is snapshotted before
# gmsh runs and restored after.

def _real_env_path() -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetEnvironmentVariableW("PATH", buf, 32768)
    return buf.value if n else None


def _restore_env_path(path: str | None) -> None:
    if path is None or sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.kernel32.SetEnvironmentVariableW("PATH", path)


class MeshError(RuntimeError):
    """gmsh could not produce a usable mesh for this geometry."""


def section_geometry(cfg: StackConfig, mesh_size: str = "medium") -> dict:
    """The exact geometry build_case meshes, minus the meshing: the
    densified installed-section polylines in meters (ground at y = 0),
    the fixed domain box, per-element boundary-layer caps and slot
    refinement boxes — ONE source for every mesher. The gmsh route
    (build_case) consumes it directly; the Fluent-native route
    (fluent_run / fluent_mcp) builds its STL slab and sizing controls
    from the same numbers, so the two meshers argue about cells, never
    about geometry. Also carries the preset's sizes mapped for a
    native mesher: wall/far sizes in meters, BL growth, an explicit
    layer count derived from the preset's thickness cap through the
    geometric series, and the slab depth."""
    if mesh_size not in MESH_PRESETS:
        raise ValueError(f"mesh_size must be one of {sorted(MESH_PRESETS)}")
    preset = MESH_PRESETS[mesh_size]
    c = cfg.chord_m
    # the wall polyline is meshed as straight facets, so its node count is
    # the surface resolution for EVERY preset — decoupled from the panel
    # count (the solver's 70/side leaves ~8 mm facets on the main element;
    # the flow solution deserves better even when the panel method doesn't
    # need it). Same underlying section, denser sampling. The densified
    # rebuild re-solves the slot placement on its own discretisation, so
    # the intersection guard runs AFTER it: the geometry that is checked
    # has to be the geometry that is meshed, or a stack that reads clear
    # at the user's panel count reaches the mesher self-intersecting and
    # fails as an opaque mesh error.
    mesh_nodes = max(cfg.n_panels_per_side, MESH_SURFACE_MIN_N)
    design = geometry.build_stack(
        cfg if mesh_nodes == cfg.n_panels_per_side
        else dataclasses.replace(cfg, n_panels_per_side=mesh_nodes))
    if any(e["intersects"] for e in design):
        raise ValueError("elements intersect — open the slots before meshing")
    installed = geometry.install_stack(design, cfg.ride_height_c)
    polys = [_closed_poly_m(e["coords"], cfg.chord_m) for e in installed]
    # the domain is a fixed box (Y_TOP_C chords tall): a validated config can
    # still push the section out of it (e.g. a huge ride height on a small
    # chord), which would produce a broken or impossible mesh — refuse with a
    # clear message instead
    y_top_c = max(e["coords"][:, 1].max() for e in installed)
    x_lo_c = min(e["coords"][:, 0].min() for e in installed)
    x_hi_c = max(e["coords"][:, 0].max() for e in installed)
    if y_top_c > 0.75 * Y_TOP_C or x_lo_c < -0.5 * X_UP_C \
            or x_hi_c > 0.5 * X_DOWN_C:
        raise ValueError(
            f"the installed section (top at {y_top_c:.2f} chords above the "
            f"ground) does not fit the CFD domain ({Y_TOP_C:g} chords tall) "
            f"with clearance — reduce the ride height relative to the chord")
    # each element's boundary-layer stack is capped by the clearances IT
    # actually faces: its own ground clearance and the slot gaps on either
    # side of it (both sides of a gap carry that gap, so opposing stacks
    # cannot collide across it)
    gaps = [e.get("slot_gap") for e in installed]
    bl_caps_c = []
    for i, e in enumerate(installed):
        own = [float(e["coords"][:, 1].min())]
        if gaps[i] is not None:
            own.append(gaps[i])
        if i + 1 < len(installed) and gaps[i + 1] is not None:
            own.append(gaps[i + 1])
        bl_caps_c.append(max(min(own), 1e-4))
    # refinement box over each slot throat (centred on the flap LE) so the
    # jet is carried by >= SLOT_GAP_CELLS cells on every preset
    slot_boxes = []
    for i, e in enumerate(installed):
        if gaps[i] is None:
            continue
        g_m = gaps[i] * cfg.chord_m
        le = e["coords"][int(np.argmin(e["coords"][:, 0]))] * cfg.chord_m
        r = 3.0 * g_m
        slot_boxes.append((le[0] - r, le[0] + r, max(le[1] - r, 0.0),
                           le[1] + r, g_m / SLOT_GAP_CELLS))

    h1, u_tau = first_layer(cfg)
    growth = preset["bl_ratio"]
    bl_thick_m = min(preset["bl_thick"] * c,
                     BL_CLEAR_FRAC * min(bl_caps_c) * c)
    n_bl = max(4, min(30, int(math.log(
        1.0 + bl_thick_m * (growth - 1.0) / h1) / math.log(growth))))
    return {
        "installed": installed,
        "polys": polys,
        "bl_caps_c": bl_caps_c,
        "slot_boxes": slot_boxes,
        "surface_nodes_per_side": mesh_nodes,
        "wings": [f"wing_e{i+1}" for i in range(len(installed))],
        "domain_m": (-X_UP_C * c, 0.0, X_DOWN_C * c, Y_TOP_C * c),
        "dz_m": DZ_C * c,
        "wall_size_m": preset["wall"] * c,
        "far_size_m": preset["far"] * c,
        "first_layer_m": h1,
        "u_tau": u_tau,
        "bl_growth": growth,
        "bl_thickness_m": bl_thick_m,
        "n_bl_layers": n_bl,
        "slot_gap_cells": SLOT_GAP_CELLS,
    }


def first_layer(cfg: StackConfig) -> tuple[float, float]:
    """(first-layer height m, u_tau m/s) for y+ ~ 1 at the main-chord Re."""
    re = cfg.speed_ms * cfg.chord_m / cfg.nu
    u_tau = cfg.speed_ms * math.sqrt(0.5 * 0.058 * re ** -0.2)
    return 2.0 * Y_PLUS_TARGET * cfg.nu / u_tau, u_tau


def _closed_poly_m(coords_c: np.ndarray, chord_m: float) -> tuple[np.ndarray, bool]:
    """Element contour in meters as closed-polygon vertices (the implicit
    closing segment is the straight TE base when the TE is blunt).
    Returns (points, blunt); coincident sharp-TE endpoints are merged."""
    p = np.asarray(coords_c, float) * chord_m
    p = p[np.r_[True, np.hypot(*np.diff(p, axis=0).T) > 1e-7]]
    blunt = bool(np.hypot(*(p[0] - p[-1])) > 1e-6)
    return (p if blunt else p[:-1]), blunt


def _build_mesh(cfg: StackConfig, polys: list[tuple[np.ndarray, bool]],
                msh_path: Path, preset: dict, bl_caps_c: list[float],
                slot_boxes: list[tuple[float, float, float, float, float]],
                saved_path: str | None = None) -> dict:
    import gmsh
    c = cfg.chord_m
    h1, _ = first_layer(cfg)
    # interruptible would install a SIGINT handler — illegal outside the
    # main thread, and the server calls this from a worker pool
    gmsh.initialize(interruptible=False)
    # the PATH clobber happens inside initialize(): restore immediately so
    # the broken-PATH window is milliseconds, not the whole meshing run —
    # concurrent threads launch subprocesses (docker probes, Show in folder)
    # and must not see the gutted PATH. finalize() clobbers again; the
    # caller's finally restores after that too.
    _restore_env_path(saved_path)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("wing_section")
        occ = gmsh.model.occ

        x0, x1, y1 = -X_UP_C * c, X_DOWN_C * c, Y_TOP_C * c
        corners = [(x0, 0.0), (x1, 0.0), (x1, y1), (x0, y1)]
        cp = [occ.addPoint(x, y, 0.0) for x, y in corners]
        edges = [occ.addLine(cp[i], cp[(i + 1) % 4]) for i in range(4)]
        ground_edge, outlet_edge, top_edge, inlet_edge = edges

        wing_lines, loops, fan_pts = [], [], []
        for p, blunt in polys:
            pts = [occ.addPoint(x, y, 0.0) for x, y in p]
            lines = [occ.addLine(pts[i], pts[(i + 1) % len(pts)])
                     for i in range(len(pts))]
            wing_lines.append(lines)
            loops.append(occ.addCurveLoop(lines))
            # fans keep the BL valid around the convex TE corner(s)
            fan_pts.append([pts[0], pts[-1]] if blunt else [pts[0]])
        surf = occ.addPlaneSurface([occ.addCurveLoop(edges)] + loops)
        ext = occ.extrude([(2, surf)], 0.0, 0.0, DZ_C * c,
                          numElements=[1], heights=[1.0], recombine=True)
        occ.synchronize()
        back = ext[0][1]
        vol = next(t for d, t in ext if d == 3)

        def lateral(line: int) -> int:
            # each z=0 curve bounds exactly two surfaces: the section plane
            # and its extruded lateral face — the patch we want
            up, _ = gmsh.model.getAdjacencies(1, line)
            return next(s for s in up if s != surf)

        grp = gmsh.model.addPhysicalGroup
        grp(2, [lateral(inlet_edge)], name="inlet")
        grp(2, [lateral(outlet_edge)], name="outlet")
        grp(2, [lateral(top_edge)], name="top")
        grp(2, [lateral(ground_edge)], name="ground")
        for i, lines in enumerate(wing_lines):
            grp(2, [lateral(li) for li in lines], name=f"wing_e{i+1}")
        grp(2, [surf, back], name="frontAndBack")
        grp(3, [vol], name="internal")

        f = gmsh.model.mesh.field
        flat = [li for lines in wing_lines for li in lines]
        dist = f.add("Distance")
        f.setNumbers(dist, "CurvesList", flat)
        f.setNumber(dist, "Sampling", 20)
        thr = f.add("Threshold")
        f.setNumber(thr, "InField", dist)
        f.setNumber(thr, "SizeMin", preset["wall"] * c)
        f.setNumber(thr, "SizeMax", preset["far"] * c)
        f.setNumber(thr, "DistMin", 0.02 * c)
        f.setNumber(thr, "DistMax", 3.0 * c)
        # slot jets need cells to exist in: the wall-graded background alone
        # left ~2 cells across a 1.3%c gap on the coarse preset, which cannot
        # carry the jet that keeps a deflected flap attached — a measured
        # driver of the coarse mesh reading low on slotted stacks
        size_fields = [thr]
        for bx0, bx1, by0, by1, size_m in slot_boxes:
            box = f.add("Box")
            f.setNumber(box, "XMin", bx0)
            f.setNumber(box, "XMax", bx1)
            f.setNumber(box, "YMin", by0)
            f.setNumber(box, "YMax", by1)
            f.setNumber(box, "ZMin", -c)
            f.setNumber(box, "ZMax", c)
            f.setNumber(box, "VIn", size_m)
            f.setNumber(box, "VOut", preset["far"] * c)
            f.setNumber(box, "Thickness", 4.0 * size_m)
            size_fields.append(box)
        bg = f.add("Min")
        f.setNumbers(bg, "FieldsList", size_fields)
        f.setAsBackgroundMesh(bg)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)

        # wing curves ONLY: they are closed loops, so the layer stack has no
        # open end. A ground stack would have to terminate mid-line, which
        # gmsh staircases into near-degenerate slivers (see module docstring)
        def add_bl(lines, fans, thickness_c):
            bl = f.add("BoundaryLayer")
            f.setNumbers(bl, "CurvesList", lines)
            f.setNumber(bl, "Size", h1)
            f.setNumber(bl, "Ratio", preset["bl_ratio"])
            f.setNumber(bl, "Thickness", thickness_c * c)
            f.setNumber(bl, "Quads", 1)
            f.setNumbers(bl, "FanPointsList", fans)
            f.setAsBoundaryLayer(bl)
            return bl

        # one stack per element, each capped by ITS OWN nearest clearance:
        # a global min-clearance cap starved every element's stack to the
        # tightest slot gap (0.4 x 1.3%c covers ~19% of the main element's
        # physical BL). Opposing stacks across a slot still cannot collide —
        # both sides of a gap carry that gap in their own clearance set.
        per_elem = [min(preset["bl_thick"], BL_CLEAR_FRAC * cap)
                    for cap in bl_caps_c]
        bl_tags = [add_bl(lines, fans, t) for lines, fans, t
                   in zip(wing_lines, fan_pts, per_elem)]
        bl_mode = "per-element"
        try:
            gmsh.model.mesh.generate(3)
        except Exception:
            # gmsh's BoundaryLayer is its most fragile feature: retry with
            # the old single global stack, then with pure refinement — the
            # y+-adaptive wall treatment keeps all three cases correct
            gmsh.model.mesh.clear()
            for t in bl_tags:
                f.remove(t)
            bl_mode = "global"
            bl = add_bl(flat, [p for fans in fan_pts for p in fans],
                        min(per_elem))
            try:
                gmsh.model.mesh.generate(3)
            except Exception:
                gmsh.model.mesh.clear()
                f.remove(bl)
                bl_mode = None
                gmsh.model.mesh.generate(3)
        bl_used = bl_mode is not None

        types2, tags2, _ = gmsh.model.mesh.getElements(2, surf)
        n_quads = sum(len(t) for ty, t in zip(types2, tags2) if ty == 3)
        n_tris = sum(len(t) for ty, t in zip(types2, tags2) if ty == 2)
        _, tags3, _ = gmsh.model.mesh.getElements(3)
        n_cells = sum(len(t) for t in tags3)
        if n_cells == 0:
            raise MeshError("gmsh produced no volume cells")

        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.write(str(msh_path))
        return {"n_cells": int(n_cells), "n_quads": int(n_quads),
                "n_tris": int(n_tris), "bl_used": bl_used,
                "bl_mode": bl_mode}
    finally:
        gmsh.finalize()


# ---------- OpenFOAM case files (v2xxx dialect, written verbatim) ----------

def _foam(cls_: str, location: str, obj: str, body: str) -> str:
    return ("FoamFile\n{\n"
            "    version     2.0;\n"
            "    format      ascii;\n"
            f"    class       {cls_};\n"
            f"    location    \"{location}\";\n"
            f"    object      {obj};\n"
            "}\n\n" + body)


def _controldict(cfg: StackConfig, dz: float, n_iters: int = N_ITERS) -> str:
    body = f"""\
application     simpleFoam;
startFrom       latestTime;
startTime       0;
stopAt          endTime;
endTime         {n_iters};
deltaT          1;
writeControl    timeStep;
writeInterval   500;
purgeWrite      2;
writeFormat     ascii;
writePrecision  8;
timeFormat      general;
timePrecision   6;
runTimeModifiable true;

functions
{{
    forceCoeffs1
    {{
        type            forceCoeffs;
        libs            (forces);
        writeControl    timeStep;
        writeInterval   1;
        log             no;
        patches         ("wing_.*");
        rho             rhoInf;
        rhoInf          {cfg.rho:g};
        liftDir         (0 -1 0);   // installed frame: Cl downforce-positive
        dragDir         (1 0 0);
        CofR            (0 0 0);    // ground point below the main LE (x=0);
                                    // Cl/Cd are CofR-independent
        pitchAxis       (0 0 1);
        magUInf         {cfg.speed_ms:g};
        lRef            {cfg.chord_m:g};        // main chord
        Aref            {cfg.chord_m * dz:g};   // main chord x slab depth
    }}

    // wall diagnostics beside every written field set: measured y+ (the
    // wall-treatment claim is checkable, not assumed) and wall shear
    // stress (reversed tau_x on a suction side = separation — the studio
    // reads attachment state from it, which a bare Cl cannot show)
    yPlus1
    {{
        type            yPlus;
        libs            (fieldFunctionObjects);
        writeControl    writeTime;
        log             no;
    }}
    wallShearStress1
    {{
        type            wallShearStress;
        libs            (fieldFunctionObjects);
        writeControl    writeTime;
        log             no;
    }}
}}
"""
    return _foam("dictionary", "system", "controlDict", body)


_FVSCHEMES = """\
ddtSchemes
{
    default         steadyState;
}

gradSchemes
{
    default         cellLimited Gauss linear 1;
}

divSchemes
{
    default         none;
    div(phi,U)      bounded Gauss linearUpwind grad(U);
    div(phi,k)      bounded Gauss upwind;
    div(phi,omega)  bounded Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}

// generated meshes can carry a handful of near-degenerate faces where the
// boundary-layer quads meet the tri region (non-orthogonality approaching
// 90 deg): limited 0.33 caps the explicit non-orthogonal correction there,
// trading a little accuracy on bad faces for unconditional stability
laplacianSchemes
{
    default         Gauss linear limited corrected 0.33;
}

interpolationSchemes
{
    default         linear;
}

snGradSchemes
{
    default         limited corrected 0.33;
}

wallDist
{
    method          meshWave;
}
"""

_FVSOLUTION = """\
solvers
{
    p
    {
        solver          GAMG;
        smoother        GaussSeidel;
        tolerance       1e-7;
        relTol          0.05;
        // a diverging pressure solve must bail out, not grind through 1000
        // sweeps amplifying garbage until sigFpe
        maxIter         200;
    }

    Phi        // potentialFoam initialization (run.sh)
    {
        $p;
    }

    "(U|k|omega)"
    {
        solver          smoothSolver;
        smoother        symGaussSeidel;
        tolerance       1e-8;
        relTol          0.1;
        maxIter         100;
    }
}

// plain SIMPLE with heavy p under-relaxation: SIMPLEC (consistent) is
// faster on clean meshes but diverges on the near-degenerate faces a
// generated mesh can contain
SIMPLE
{
    nNonOrthogonalCorrectors 2;

    residualControl
    {
        p               5e-5;
        U               1e-5;
        "(k|omega)"     1e-5;
    }
}

potentialFlow
{
    nNonOrthogonalCorrectors 10;
}

relaxationFactors
{
    fields
    {
        p               0.3;
    }
    equations
    {
        U               0.7;
        "(k|omega)"     0.7;
    }
}
"""

_TURBULENCE = """\
simulationType  RAS;

RAS
{
    RASModel        kOmegaSST;
    turbulence      on;
    printCoeffs     on;
}
"""


def _transport(cfg: StackConfig) -> str:
    return _foam("dictionary", "constant", "transportProperties",
                 "transportModel  Newtonian;\n\n"
                 f"nu              [0 2 -1 0 0 0 0] {cfg.nu:g};\n")


def _field_u(cfg: StackConfig) -> str:
    u = f"({cfg.speed_ms:g} 0 0)"
    body = f"""\
dimensions      [0 1 -1 0 0 0 0];

internalField   uniform {u};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {u};
    }}
    outlet
    {{
        type            inletOutlet;
        inletValue      uniform (0 0 0);
        value           uniform {u};
    }}
    top
    {{
        type            slip;
    }}
    // the road translates with the air in the wing-fixed frame
    ground
    {{
        type            fixedValue;
        value           uniform {u};
    }}
    "wing_.*"
    {{
        type            noSlip;
    }}
    frontAndBack
    {{
        type            empty;
    }}
}}
"""
    return _foam("volVectorField", "0", "U", body)


_FIELD_P = _foam("volScalarField", "0", "p", """\
dimensions      [0 2 -2 0 0 0 0];

internalField   uniform 0;

boundaryField
{
    inlet
    {
        type            zeroGradient;
    }
    outlet
    {
        type            fixedValue;
        value           uniform 0;
    }
    top
    {
        type            zeroGradient;
    }
    "(ground|wing_.*)"
    {
        type            zeroGradient;
    }
    frontAndBack
    {
        type            empty;
    }
}
""")


def _turb_inlet(cfg: StackConfig) -> tuple[float, float]:
    k = 1.5 * (TURB_INTENSITY * cfg.speed_ms) ** 2
    return k, k / (cfg.nu * TURB_VISC_RATIO)


def _field_k(cfg: StackConfig) -> str:
    k, _ = _turb_inlet(cfg)
    body = f"""\
dimensions      [0 2 -2 0 0 0 0];

internalField   uniform {k:.6g};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {k:.6g};
    }}
    outlet
    {{
        type            inletOutlet;
        inletValue      uniform {k:.6g};
        value           uniform {k:.6g};
    }}
    top
    {{
        type            slip;
    }}
    // y+-adaptive: correct on the resolved-wall mesh (y+ ~ 1) and on the
    // pure-refinement fallback
    "(ground|wing_.*)"
    {{
        type            kLowReWallFunction;
        value           uniform {k:.6g};
    }}
    frontAndBack
    {{
        type            empty;
    }}
}}
"""
    return _foam("volScalarField", "0", "k", body)


def _field_omega(cfg: StackConfig) -> str:
    _, om = _turb_inlet(cfg)
    body = f"""\
dimensions      [0 0 -1 0 0 0 0];

internalField   uniform {om:.6g};

boundaryField
{{
    inlet
    {{
        type            fixedValue;
        value           uniform {om:.6g};
    }}
    outlet
    {{
        type            inletOutlet;
        inletValue      uniform {om:.6g};
        value           uniform {om:.6g};
    }}
    top
    {{
        type            slip;
    }}
    // blends viscous-sublayer and log-law omega with y+
    "(ground|wing_.*)"
    {{
        type            omegaWallFunction;
        value           uniform {om:.6g};
    }}
    frontAndBack
    {{
        type            empty;
    }}
}}
"""
    return _foam("volScalarField", "0", "omega", body)


_FIELD_NUT = _foam("volScalarField", "0", "nut", """\
dimensions      [0 2 -1 0 0 0 0];

internalField   uniform 0;

boundaryField
{
    inlet
    {
        type            calculated;
        value           uniform 0;
    }
    outlet
    {
        type            calculated;
        value           uniform 0;
    }
    top
    {
        type            calculated;
        value           uniform 0;
    }
    // Spalding's law: continuous from y+ ~ 1 through the log layer
    "(ground|wing_.*)"
    {
        type            nutUSpaldingWallFunction;
        value           uniform 0;
    }
    frontAndBack
    {
        type            empty;
    }
}
""")


def _decomposepardict(n_ranks: int) -> str:
    # scotch: a graph partitioner with no geometry assumptions — right for
    # unstructured tri+quad meshes, and deterministic for a fixed mesh and
    # rank count, so the same case at the same n_ranks reproduces exactly.
    # (ACROSS rank counts the partition, and with it the solver's iteration
    # path, legitimately differs — see the knife-edge note in cfd_run.)
    return _foam("dictionary", "system", "decomposeParDict",
                 f"numberOfSubdomains {n_ranks};\n\n"
                 "method          scotch;\n")


def _run_sh(wings: list[str], n_ranks: int = 1) -> str:
    fixes = "\n".join(
        f"foamDictionary constant/polyMesh/boundary "
        f"-entry entry0/{w}/type -set wall" for w in ["ground", *wings])
    if n_ranks > 1:
        # potentialFoam stays serial: it initializes 0/U before decomposePar
        # distributes the fields, so serial and parallel runs start from the
        # IDENTICAL initial state. --allow-run-as-root: the container runs
        # as root. --oversubscribe: OpenMPI's slot detection inside a
        # container can undercount; the core budget is enforced by the
        # caller, not by mpirun.
        solve = f"""\
echo "== decomposePar"
decomposePar -force > log.decomposePar 2>&1

echo "== simpleFoam"
mpirun --allow-run-as-root --oversubscribe -np {n_ranks} \\
    simpleFoam -parallel > log.simpleFoam 2>&1

echo "== reconstructPar"
reconstructPar -latestTime > log.reconstructPar 2>&1
# the decomposed copies are dead disk weight once the merged fields exist;
# postProcessing/ (forces, y+) was written by the master rank all along
rm -rf processor*"""
    else:
        solve = """\
echo "== simpleFoam"
simpleFoam > log.simpleFoam 2>&1"""
    return f"""\
#!/usr/bin/env bash
# Run this 2D RANS case in WSL:   wsl -d Ubuntu -- bash run.sh
# OpenFOAM's etc/bashrc cannot be sourced under set -e/-u (see the repo
# README) - strict mode is enabled only after sourcing.
cd "$(dirname "$0")"
BASHRC=$(ls -d /usr/lib/openfoam/openfoam*/etc/bashrc 2>/dev/null | sort -V | tail -1)
if [ -z "$BASHRC" ]; then
    echo "ERROR: no OpenFOAM install found under /usr/lib/openfoam" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$BASHRC"
set -e

# Rerun safety: a second run on a finished (or crashed) case would otherwise
# continue from latestTime — potentialFoam would overwrite the solved fields
# and simpleFoam would exit immediately at endTime, regenerating results.txt
# from stale data. Reset to a clean start instead: every run of this script
# is a full, reproducible solve.
if ls -d [0-9]*.[0-9]* [1-9]* postProcessing 2>/dev/null | grep -q .; then
    echo "== previous run detected - resetting the case to a clean start"
    # flow_*.png / results.txt describe the PREVIOUS solve - stale beside
    # a fresh one
    rm -rf postProcessing processor* constant/polyMesh \
        results.txt flow_*.png flow_field_*.json
    find . -maxdepth 1 -regextype posix-extended -type d \
        -regex '\./[0-9]+(\.[0-9]+)?' ! -name 0 -exec rm -rf {{}} +
fi

echo "== gmshToFoam"
gmshToFoam mesh.msh > log.gmshToFoam 2>&1
# gmshToFoam types every patch as a generic 'patch': the z-planes must be
# 'empty' (that is what makes the case 2D) and the no-slip patches 'wall'
# (wall functions check the patch type)
foamDictionary constant/polyMesh/boundary -entry entry0/frontAndBack/type -set empty
{fixes}

echo "== checkMesh"
checkMesh > log.checkMesh 2>&1 || echo "checkMesh reported problems - see log.checkMesh"

# potential-flow initial field: simpleFoam starts from a divergence-free
# velocity instead of a uniform stream slamming into the wing
echo "== potentialFoam"
potentialFoam -writephi > log.potentialFoam 2>&1

{solve}

# cell-centre coordinates beside the final U/p fields: the app's flow-field
# view (and any external plotting) reads the 2D section straight from them
echo "== postProcess (cell centres for the flow view)"
postProcess -func writeCellCentres -latestTime > log.postProcess 2>&1 || \
    echo "writeCellCentres failed - flow view unavailable (see log.postProcess)"

COEF=$(ls -1 postProcessing/forceCoeffs1/*/coefficient.dat 2>/dev/null | tail -1)
if [ -z "$COEF" ]; then
    echo "ERROR: simpleFoam wrote no force coefficients - see log.simpleFoam" >&2
    exit 1
fi
{{
    echo "# Cl is downforce-positive (liftDir (0 -1 0)); coefficients are"
    echo "# referenced to the main chord and the case's extrusion depth."
    grep '^#' "$COEF" | tail -1
    tail -1 "$COEF"
    # steady RANS on a high-lift section often settles into a bounded limit
    # cycle: the tail mean is the number to trust — but ONLY once the
    # history is flat. A drifting tail mean is a transient snapshot, so the
    # drift between the last two half-windows is printed with the mean and
    # a still-trending run is flagged instead of presented as a result.
    grep -v '^#' "$COEF" | tail -1000 | awk \\
        '{{cl[NR]=$5; cd[NR]=$2; n=NR}}
         END {{
            if (!n) exit
            h = int(n / 2); if (h < 1) exit
            m1 = 0; for (i = 1; i <= h; i++) m1 += cl[i]; m1 /= h
            m2 = 0; cdm = 0
            for (i = h + 1; i <= n; i++) {{ m2 += cl[i]; cdm += cd[i] }}
            m2 /= (n - h); cdm /= (n - h)
            printf "# mean of last %d iterations:  Cd = %.5g   Cl = %.5g\\n", n - h, cdm, m2
            ref = m2; if (ref < 0) ref = -ref; if (ref < 0.05) ref = 0.05
            d = (m2 - m1) / ref
            printf "# Cl drift over the trailing %d iterations: %+.2f%%\\n", n, d * 100
            if (d < 0) d = -d
            if (d > 0.003) {{
                printf "# WARNING: NOT CONVERGED - the force history is still trending, so\\n"
                printf "# the mean above is a transient snapshot (biased low on a rising\\n"
                printf "# history). Raise endTime in system/controlDict and rerun.\\n"
            }}
         }}'
}} > results.txt
echo "== results.txt"
cat results.txt
"""


def _case_readme(cfg: StackConfig, summary: dict, wings: list[str],
                 n_iters: int = N_ITERS, n_ranks: int = 1) -> str:
    re_c = summary["re_main_chord"]
    bl = "yes" if summary["boundary_layer"] else "NO - refinement fallback"
    par = "" if n_ranks == 1 else f"""

PARALLEL
--------
This case is configured for {n_ranks} MPI ranks: run.sh decomposes the
mesh (scotch), solves with mpirun -np {n_ranks} simpleFoam -parallel and
reconstructs the latest time before post-processing, so results.txt and
the flow fields come out exactly where the serial case puts them. The
same script runs in WSL and in the app's Docker container. A fixed rank
count reproduces exactly; DIFFERENT rank counts follow slightly different
iteration paths (domain decomposition changes the linear algebra), which
matters only within the knife-edge band around the attachment verdict
lines — see the studio docs. Regenerate with ranks = 1 for the serial
reference case.\
"""
    return f"""\
OPENFOAM 2D RANS CASE - generated by Wing Section Studio
========================================================

Section     {len(wings)} element(s), main chord {cfg.chord_mm:.0f} mm,
            ride height {cfg.ride_height_mm:.0f} mm (installed frame,
            ground at y = 0, downforce down)
Freestream  {cfg.speed_ms:g} m/s, nu = {cfg.nu:g} m^2/s
            (Re = {re_c:.3g} on the main chord)
Mesh        {summary['mesh_size']}: {summary['n_cells']} cells, first wall
            layer {summary['first_layer_mm']:.3f} mm
            (y+ ~ {summary['y_plus_est']:g}), boundary-layer quads: {bl}

RUN
---
From Windows (the case must sit on a drive WSL can see):

    wsl -d Ubuntu -- bash run.sh

The script sources the newest OpenFOAM under /usr/lib/openfoam, converts
the mesh (gmshToFoam), fixes patch types (frontAndBack -> empty, ground
and wing patches -> wall), runs checkMesh, initializes with potentialFoam,
then simpleFoam (steady, k-omega SST, up to {n_iters} iterations) and
writes results.txt. The residualControl thresholds are a backstop that
does not fire on a loaded high-lift case — expect the run to use the full
iteration budget, and read the drift verdict in results.txt. Rerunning
the script resets the case to a clean start first (previous time
directories, postProcessing and stale result artifacts are removed), so
every run is a full, reproducible solve.{par}

RESULTS
-------
results.txt carries the final row of postProcessing/forceCoeffs1/.../
coefficient.dat under its column header, the mean of the last 500
iterations, and the Cl drift across the trailing 1000 — a NOT CONVERGED
banner means the mean is a transient snapshot (biased low on a rising
history): raise endTime and rerun. liftDir is (0 -1 0), so Cl POSITIVE
= DOWNFORCE. Coefficients are referenced to the main chord
({cfg.chord_m:g} m), the slab depth ({DZ_C * cfg.chord_m:g} m;
Aref = chord x depth) and the freestream dynamic pressure. Sectional
downforce per unit span: L' = Cl * 0.5 * rho * U^2 * c. Steady RANS on
a heavily loaded section often ends in a bounded oscillation rather
than a point value — check the convergence history in coefficient.dat
and prefer the tail mean once the drift verdict is clean.
Compare against the studio's estimate and recalibrate its k_g /
viscous-efficiency knobs with the result.

CASE NOTES
----------
- The ground BC is a wall MOVING at the freestream ({cfg.speed_ms:g} m/s):
  in the wing-fixed frame road and air translate together (0/U ground is
  fixedValue, not noSlip).
- Wall treatment is y+-adaptive (omegaWallFunction, kLowReWallFunction,
  nutUSpaldingWallFunction) — valid on the resolved boundary-layer mesh
  and on the pure-refinement fallback alike. The yPlus1 function object
  writes the measured y+ beside each field set — check it rather than
  trusting the flat-plate target on the mesh summary.
- Turbulence is FULLY-TURBULENT k-omega SST: no transition modeling. An
  A/B on the aggressive 3-element case showed the gamma-ReThetat
  transition model reading ~26% HIGHER Cl (long laminar runs thin the
  boundary layers), so the fully-turbulent setup is the conservative
  choice at these Reynolds numbers, not an optimistic one.
- wallShearStress1 writes the wall shear field beside each field set:
  reversed tau_x along a suction side marks separation — the sanity
  check a bare Cl number cannot give you.
- Wing patches: {', '.join(wings)}. Per-element forces: duplicate the
  forceCoeffs block in system/controlDict with a single patch.
- 2D: the mesh is one cell thick in z; frontAndBack is 'empty'.
- Open case.foam in ParaView to inspect the mesh and fields.
- Regenerate at another resolution from the app's Export tab
  (coarse / medium / fine).
"""


def build_case(cfg: StackConfig, out_dir: Path, mesh_size: str = "medium",
               n_iters: int = N_ITERS, n_ranks: int = 1) -> dict:
    """Write a complete, ready-to-run OpenFOAM case into out_dir.

    n_iters caps the simpleFoam iteration count. The residualControl
    thresholds in fvSolution never fire on a loaded high-lift case (initial
    residuals plateau above them), so an exported run goes the full
    n_iters; results.txt reports the tail drift so a still-trending run is
    flagged. The in-app Docker runs stop on force drift instead.

    n_ranks=1 (the default) generates the same serial case this module has
    always generated, byte for byte. n_ranks>1 additionally writes
    system/decomposeParDict and swaps run.sh's solver step for
    decomposePar / mpirun -np N simpleFoam -parallel / reconstructPar —
    everything upstream (mesh, fields, schemes) and downstream
    (postProcessing layout, results.txt, flow view) is identical, so the
    choice is an explicit opt-in per run, never a format change.
    Returns a summary: cell count, y+ estimate, patch and file lists."""
    if mesh_size not in MESH_PRESETS:
        raise ValueError(f"mesh_size must be one of {sorted(MESH_PRESETS)}")
    n_iters = int(n_iters)
    if not (100 <= n_iters <= 20_000):
        raise ValueError("n_iters must be between 100 and 20000")
    n_ranks = int(n_ranks)
    if not (1 <= n_ranks <= 32):
        raise ValueError("n_ranks must be between 1 and 32")
    preset = MESH_PRESETS[mesh_size]
    # geometry (densified polylines, domain fit, BL caps, slot boxes)
    # comes from the shared source — see section_geometry
    g = section_geometry(cfg, mesh_size)
    installed = g["installed"]
    polys = g["polys"]
    bl_caps_c = g["bl_caps_c"]
    slot_boxes = g["slot_boxes"]
    mesh_nodes = g["surface_nodes_per_side"]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _gmsh_lock:
        saved_path = _real_env_path()
        try:
            stats = _build_mesh(cfg, polys, out_dir / "mesh.msh", preset,
                                bl_caps_c, slot_boxes, saved_path)
        except MeshError:
            raise
        except Exception as e:
            raise MeshError(str(e) or type(e).__name__)
        finally:
            _restore_env_path(saved_path)

    h1, u_tau = first_layer(cfg)
    # fallback meshes put the first cell center at half the isotropic wall
    # size. The estimate is a flat-plate TARGET at the chord Re — suction
    # peaks run several times higher; the solved yPlus field (yPlus1
    # function object) is the measured value.
    y_plus = Y_PLUS_TARGET if stats["bl_used"] \
        else 0.5 * preset["wall"] * cfg.chord_m * u_tau / cfg.nu
    wings = [f"wing_e{i+1}" for i in range(len(installed))]
    summary = {
        "mesh_size": mesh_size,
        "n_iters": n_iters,
        "n_ranks": n_ranks,
        "n_cells": stats["n_cells"],
        "n_bl_quads": stats["n_quads"],
        "boundary_layer": stats["bl_used"],
        "bl_mode": stats["bl_mode"],
        "first_layer_mm": round(h1 * 1e3, 4),
        "y_plus_est": round(y_plus, 2),
        # the wall sampling actually meshed — not cfg.n_panels_per_side,
        # which is the panel solver's number
        "surface_nodes_per_side": mesh_nodes,
        "re_main_chord": int(round(cfg.speed_ms * cfg.chord_m / cfg.nu)),
        "patches": ["inlet", "outlet", "top", "ground", *wings,
                    "frontAndBack"],
    }
    files = {
        "system/controlDict": _controldict(cfg, DZ_C * cfg.chord_m, n_iters),
        "system/fvSchemes": _foam("dictionary", "system", "fvSchemes",
                                  _FVSCHEMES),
        "system/fvSolution": _foam("dictionary", "system", "fvSolution",
                                   _FVSOLUTION),
        "constant/turbulenceProperties": _foam("dictionary", "constant",
                                               "turbulenceProperties",
                                               _TURBULENCE),
        "constant/transportProperties": _transport(cfg),
        "0/U": _field_u(cfg),
        "0/p": _FIELD_P,
        "0/k": _field_k(cfg),
        "0/omega": _field_omega(cfg),
        "0/nut": _FIELD_NUT,
        "run.sh": _run_sh(wings, n_ranks),
        "README.txt": _case_readme(cfg, summary, wings, n_iters, n_ranks),
        "case.foam": "",   # ParaView opens the case through this stub
    }
    if n_ranks > 1:
        files["system/decomposeParDict"] = _decomposepardict(n_ranks)
    for rel, text in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    summary["files"] = ["mesh.msh"] + sorted(files)
    return summary
