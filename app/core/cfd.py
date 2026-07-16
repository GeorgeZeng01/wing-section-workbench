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
limiting. The moving-ground boundary layer is weak (road and freestream
translate together) and the y+-adaptive wall functions below handle the
wall-function-range y+ of the graded isotropic cells under the wing.
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

import math
import sys
import threading
from pathlib import Path

import numpy as np

from . import geometry
from .geometry import StackConfig

Y_PLUS_TARGET = 1.0
DZ_C = 0.1                  # extrusion depth, chords (2D slab thickness)
X_UP_C, X_DOWN_C, Y_TOP_C = 6.0, 12.0, 8.0    # domain extents, chords
BL_CLEAR_FRAC = 0.4         # layer stack <= this share of tightest clearance
TURB_INTENSITY = 0.01       # inlet turbulence intensity (on-track air)
TURB_VISC_RATIO = 10.0      # inlet nut/nu, sets omega
N_ITERS = 3000

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
                msh_path: Path, preset: dict, min_clear_c: float) -> dict:
    import gmsh
    c = cfg.chord_m
    h1, _ = first_layer(cfg)
    # interruptible would install a SIGINT handler — illegal outside the
    # main thread, and the server calls this from a worker pool
    gmsh.initialize(interruptible=False)
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
            fan_pts += [pts[0], pts[-1]] if blunt else [pts[0]]
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
        f.setAsBackgroundMesh(thr)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)

        # wing curves ONLY: they are closed loops, so the layer stack has no
        # open end. A ground stack would have to terminate mid-line, which
        # gmsh staircases into near-degenerate slivers (see module docstring)
        bl = f.add("BoundaryLayer")
        f.setNumbers(bl, "CurvesList", flat)
        f.setNumber(bl, "Size", h1)
        f.setNumber(bl, "Ratio", preset["bl_ratio"])
        f.setNumber(bl, "Thickness",
                    min(preset["bl_thick"], BL_CLEAR_FRAC * min_clear_c) * c)
        f.setNumber(bl, "Quads", 1)
        f.setNumbers(bl, "FanPointsList", fan_pts)
        f.setAsBoundaryLayer(bl)

        bl_used = True
        try:
            gmsh.model.mesh.generate(3)
        except Exception:
            gmsh.model.mesh.clear()
            f.remove(bl)
            bl_used = False
            gmsh.model.mesh.generate(3)

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
                "n_tris": int(n_tris), "bl_used": bl_used}
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
        CofR            (0 0 0);    // main-element leading edge
        pitchAxis       (0 0 1);
        magUInf         {cfg.speed_ms:g};
        lRef            {cfg.chord_m:g};        // main chord
        Aref            {cfg.chord_m * dz:g};   // main chord x slab depth
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


def _run_sh(wings: list[str]) -> str:
    fixes = "\n".join(
        f"foamDictionary constant/polyMesh/boundary "
        f"-entry entry0/{w}/type -set wall" for w in ["ground", *wings])
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

echo "== simpleFoam"
simpleFoam > log.simpleFoam 2>&1

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
    # cycle instead of a point value: the tail mean is the number to trust
    grep -v '^#' "$COEF" | tail -500 | awk \\
        '{{cd+=$2; cl+=$5; n++}}
         END {{if (n) printf "# mean of last %d iterations:  Cd = %.5g   Cl = %.5g\\n", n, cd/n, cl/n}}'
}} > results.txt
echo "== results.txt"
cat results.txt
"""


def _case_readme(cfg: StackConfig, summary: dict, wings: list[str],
                 n_iters: int = N_ITERS) -> str:
    re_c = summary["re_main_chord"]
    bl = "yes" if summary["boundary_layer"] else "NO - refinement fallback"
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
then simpleFoam (steady, k-omega SST, up to {n_iters} iterations with
residual stopping) and writes results.txt.

RESULTS
-------
results.txt carries the final row of postProcessing/forceCoeffs1/.../
coefficient.dat under its column header, plus the mean of the last 500
iterations. liftDir is (0 -1 0), so Cl POSITIVE = DOWNFORCE.
Coefficients are referenced to the main chord ({cfg.chord_m:g} m), the
slab depth ({DZ_C * cfg.chord_m:g} m; Aref = chord x depth) and the
freestream dynamic pressure. Sectional downforce per unit span:
L' = Cl * 0.5 * rho * U^2 * c. Steady RANS on a heavily loaded section
often ends in a bounded oscillation rather than a point value - check
the convergence history in coefficient.dat and prefer the tail mean.
Compare against the studio's estimate and recalibrate its k_g /
viscous-efficiency knobs with the result.

CASE NOTES
----------
- The ground BC is a wall MOVING at the freestream ({cfg.speed_ms:g} m/s):
  in the wing-fixed frame road and air translate together (0/U ground is
  fixedValue, not noSlip).
- Wall treatment is y+-adaptive (omegaWallFunction, kLowReWallFunction,
  nutUSpaldingWallFunction) - valid on the resolved boundary-layer mesh
  and on the pure-refinement fallback alike.
- Wing patches: {', '.join(wings)}. Per-element forces: duplicate the
  forceCoeffs block in system/controlDict with a single patch.
- 2D: the mesh is one cell thick in z; frontAndBack is 'empty'.
- Open case.foam in ParaView to inspect the mesh and fields.
- Regenerate at another resolution from the app's Export tab
  (coarse / medium / fine).
"""


def build_case(cfg: StackConfig, out_dir: Path, mesh_size: str = "medium",
               n_iters: int = N_ITERS) -> dict:
    """Write a complete, ready-to-run OpenFOAM case into out_dir.

    n_iters caps the simpleFoam iteration count (residual stopping usually
    finishes earlier) — the in-app Docker verification runs pass it through.
    Returns a summary: cell count, y+ estimate, patch and file lists."""
    if mesh_size not in MESH_PRESETS:
        raise ValueError(f"mesh_size must be one of {sorted(MESH_PRESETS)}")
    n_iters = int(n_iters)
    if not (100 <= n_iters <= 20_000):
        raise ValueError("n_iters must be between 100 and 20000")
    preset = MESH_PRESETS[mesh_size]
    design = geometry.build_stack(cfg)
    if any(e["intersects"] for e in design):
        raise ValueError("elements intersect — open the slots before meshing")
    installed = geometry.install_stack(design, cfg.ride_height_c)
    polys = [_closed_poly_m(e["coords"], cfg.chord_m) for e in installed]
    min_clear_c = min([cfg.ride_height_c]
                      + [e["slot_gap"] for e in installed if "slot_gap" in e])

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with _gmsh_lock:
        saved_path = _real_env_path()
        try:
            stats = _build_mesh(cfg, polys, out_dir / "mesh.msh", preset,
                                min_clear_c)
        except MeshError:
            raise
        except Exception as e:
            raise MeshError(str(e) or type(e).__name__)
        finally:
            _restore_env_path(saved_path)

    h1, u_tau = first_layer(cfg)
    # fallback meshes put the first cell center at half the isotropic wall size
    y_plus = Y_PLUS_TARGET if stats["bl_used"] \
        else 0.5 * preset["wall"] * cfg.chord_m * u_tau / cfg.nu
    wings = [f"wing_e{i+1}" for i in range(len(installed))]
    summary = {
        "mesh_size": mesh_size,
        "n_iters": n_iters,
        "n_cells": stats["n_cells"],
        "n_bl_quads": stats["n_quads"],
        "boundary_layer": stats["bl_used"],
        "first_layer_mm": round(h1 * 1e3, 4),
        "y_plus_est": round(y_plus, 2),
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
        "run.sh": _run_sh(wings),
        "README.txt": _case_readme(cfg, summary, wings, n_iters),
        "case.foam": "",   # ParaView opens the case through this stub
    }
    for rel, text in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    summary["files"] = ["mesh.msh"] + sorted(files)
    return summary
