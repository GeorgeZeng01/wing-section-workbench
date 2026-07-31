"""True-2D ANSYS meshing chain: DXF in, Fluent 2D mesh out.

Replicates the documented manual ANSYS Workbench workflow headlessly,
with every cell cut by ANSYS tools: a walkthrough-style DXF (closed
profile polylines + the domain rectangle) -> SpaceClaim fills the fluid
face and groups its edges -> a Workbench "Fluid Flow (Fluent)" system
imports it pinned to 2D -> Mechanical meshes it (profile edge sizing +
first-layer inflation, named selections inlet/outlet/upper_bound/ground/
profile/fluid) -> the Fluent transfer mesh (FFF.msh) is validated and
handed back. The solver half (Fluent 2D setup and the solve) lives in
app/core/fluent2d_run; this module owns geometry and meshing only.

That workflow's recipe is the default everywhere: rectangle 3L ahead /
7L behind / 3H above with the ground at y = 0, profile edge sizing
0.1 mm, inflation first layer 1 mm x 10 layers. The studio's
resolved-wall sizing ("studio-yplus1", y+ ~ 1 first layer from the
app's correlation) is an option, never a default. The domain
proportions are knobs (write_dxf_2d front_l/back_l/top_h); their
defaults ARE the documented rectangle.

The batch drivers encode the v261 quirks the de-risk spike paid for;
each guard cites its quirk number where it lives. The one that shapes
the whole module: RunWB2 and SpaceClaim exit 0 even when their script
failed (quirk 13), so every stage writes its own result JSON and the
chain trusts nothing else — the final mesh is validated by cell count,
zone names and the 2D header, never by file existence.

Offline-testable: script generation, the DXF writer, sizing resolution
and mesh parsing run without ANSYS. Stages launch through the
module-level _popen seam (tests fake it; never patch subprocess.Popen).
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# stage wall-clock budgets (spike: SpaceClaim ~40 s, Workbench ~5 min on
# a 120k-cell mesh; the budgets leave room for finer sizing, not a hang)
SC_BUDGET_S = 300.0
WB_BUDGET_S = 900.0
_POLL_S = 2.0             # stage poll cadence (cancel/timeout checks)

# indirection so tests can fake the stage processes without touching the
# global subprocess module (same seam pattern as cfd_run._popen)
_popen = subprocess.Popen


class Fluent2DError(RuntimeError):
    """A chain stage failed. stage names which ('availability',
    'spaceclaim', 'workbench'); detail says why, carrying the stage
    log's tail when one exists. cancelled marks a user cancel."""

    def __init__(self, stage: str, detail: str):
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail
        self.cancelled = str(detail).startswith("cancelled")


# ---------- installation discovery ----------

def _find_install(version: str = "") -> tuple[str, str, str, str]:
    """(awp_root, version, spaceclaim_exe, runwb2_exe) for the chain.
    Prefers AWP_ROOT261 (the spike-proven release), else the newest
    root that carries BOTH executables. Raises Fluent2DError with a
    user-facing detail when nothing qualifies."""
    roots = {k[len("AWP_ROOT"):]: v for k, v in os.environ.items()
             if k.startswith("AWP_ROOT") and k[len("AWP_ROOT"):].isdigit()
             and v}
    if version:
        roots = {k: v for k, v in roots.items() if k == str(version)}
        if not roots:
            raise Fluent2DError(
                "availability",
                f"ANSYS version {version} was requested but no matching "
                f"installation was found on this machine")
    if not roots:
        raise Fluent2DError(
            "availability",
            "needs a local ANSYS Workbench installation (no AWP_ROOT "
            "environment variable found — install ANSYS 2024 R2 or "
            "newer with SpaceClaim/Discovery and Workbench)")
    missing = []
    for ver in sorted(roots, key=lambda k: (k != "261", -int(k))):
        root = roots[ver]
        sc = os.path.join(root, "scdm", "SpaceClaim.exe")
        wb = os.path.join(root, "Framework", "bin", "Win64", "RunWB2.exe")
        if os.path.isfile(sc) and os.path.isfile(wb):
            return root, ver, sc, wb
        missing.append(
            f"v{ver} is missing "
            + ("SpaceClaim (geometry)" if not os.path.isfile(sc)
               else "Workbench (meshing)"))
    raise Fluent2DError(
        "availability",
        "no ANSYS installation carries both SpaceClaim and Workbench: "
        + "; ".join(missing))


def availability() -> dict:
    """Is the ANSYS 2D meshing chain reachable? Cheap checks only
    (environment roots + the two executables); the definitive test is
    running a stage."""
    try:
        root, ver, _sc, _wb = _find_install()
        return {"available": True, "detail": "", "awp_root": root,
                "version": ver}
    except Fluent2DError as e:
        return {"available": False, "detail": e.detail,
                "awp_root": "", "version": ""}


# ---------- sizing ----------

def mesh_sizing(mode: str, cfg) -> dict:
    """The chain's four meshing knobs for a sizing mode.

    "default" is the manual walkthrough's exact numbers — never
    derived, never scaled. "studio-yplus1" is the studio option: first
    layer at y+ ~ 1 from the app's flat-plate correlation, profile edge
    size at the fine preset's wall fraction (0.002 chords),
    layers/growth from the fine preset (1.2, cap 30). cfg is the app's
    config dict (or a StackConfig); the default mode ignores it."""
    if mode == "default":
        return {"edge_size_mm": 0.1, "first_layer_mm": 1.0,
                "n_layers": 10, "growth": 1.2}
    if mode != "studio-yplus1":
        raise ValueError("sizing mode must be 'default' or "
                         "'studio-yplus1'")
    # scripts/ must stay importable without the app: a missing app
    # environment is a ValueError at call time, not an ImportError at
    # module load
    try:
        import sys
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from app.core import cfd as app_cfd
        from app.core.geometry import StackConfig
    except Exception as e:
        raise ValueError(
            "studio-yplus1 sizing needs the studio app environment "
            f"(default sizing works standalone): {e}")
    cfg_obj = StackConfig.from_dict(cfg) if isinstance(cfg, dict) else cfg
    h1, _u_tau = app_cfd.first_layer(cfg_obj)
    return {"edge_size_mm": 0.002 * cfg_obj.chord_m * 1000.0,
            "first_layer_mm": h1 * 1000.0,
            "n_layers": 30,
            "growth": app_cfd.MESH_PRESETS["fine"]["bl_ratio"]}


# ---------- DXF writer ----------

def write_dxf_2d(profiles_m: list, out_path: str | Path, *,
                 ground: bool = True, front_l: float = 3.0,
                 back_l: float = 7.0, top_h: float = 3.0) -> dict:
    """Profiles + the documented domain rectangle as an R2000 DXF in mm.

    profiles_m: closed Nx2 loops in meters, installed frame (ground at
    y = 0), written as-driven — one closed LWPOLYLINE each on layer
    PROFILE. The rectangle (layer DOMAIN) follows the documented manual
    ANSYS workflow: front front_l ahead of min-x, back back_l behind
    max-x (both x L = streamwise extent of the union bounding box).
    Ground mode puts the floor at y = 0 and the ceiling top_h stack
    heights above it (H = profile top above the ground, ride height
    included — the spike's walkthrough reading); free air mirrors
    top_h x the bbox height above and below. The 3 / 7 / 3 defaults
    ARE the documented rectangle — changing them moves the domain, not
    the recipe label. $INSUNITS=4 so SpaceClaim reads millimetres
    (spike-verified)."""
    if not (0.5 <= front_l <= 20 and math.isfinite(front_l)):
        raise ValueError("front_l must be between 0.5 and 20 chords")
    if not (0.5 <= back_l <= 40 and math.isfinite(back_l)):
        raise ValueError("back_l must be between 0.5 and 40 chords")
    if not (0.5 <= top_h <= 20 and math.isfinite(top_h)):
        raise ValueError("top_h must be between 0.5 and 20 heights")
    profs = []
    for p in profiles_m:
        p = np.asarray(p, float)
        if p.ndim != 2 or p.shape[1] != 2 or not np.isfinite(p).all():
            raise ValueError("each profile must be a finite Nx2 array")
        diag = float(np.hypot(*(p.max(axis=0) - p.min(axis=0)))) or 1.0
        # an explicit closing duplicate would put a zero-length segment
        # in the LWPOLYLINE; close=True supplies the closure
        if len(p) > 3 and np.hypot(*(p[0] - p[-1])) <= 1e-7 * diag:
            p = p[:-1]
        if len(p) < 3:
            raise ValueError("each profile needs at least 3 points")
        profs.append(p)
    if not profs:
        raise ValueError("at least one profile required")
    allp = np.vstack(profs)
    x_min, y_min = (float(v) for v in allp.min(axis=0))
    x_max, y_max = (float(v) for v in allp.max(axis=0))
    lx, ly = x_max - x_min, y_max - y_min
    if lx <= 0 or ly <= 0:
        raise ValueError("degenerate profile extents")
    if ground and y_min <= 0:
        raise ValueError("the ground workflow needs every profile above "
                         "y = 0 (installed frame, ground at the origin)")
    x0 = x_min - front_l * lx
    x1 = x_max + back_l * lx
    # ground mode: the ceiling clears top_h STACK HEIGHTS above the
    # ground plane (H = profile top above y = 0, ride height included)
    # — the spike's walkthrough replication pins this reading of the
    # documented "3x the height of your profile", and the bbox-only
    # reading tightens vertical confinement by 3x the ride height on
    # exactly the parity path this mode exists for. Free air keeps the
    # bbox mirror above and below.
    y1 = y_max + top_h * (y_max if ground else ly)
    y0 = 0.0 if ground else y_min - top_h * ly

    import ezdxf
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4          # millimetres (quirk 4)
    for name, color in (("PROFILE", 1), ("DOMAIN", 3)):
        if name not in doc.layers:
            doc.layers.add(name, color=color)
    msp = doc.modelspace()
    for p in profs:
        msp.add_lwpolyline([(x * 1e3, y * 1e3) for x, y in p],
                           close=True, dxfattribs={"layer": "PROFILE"})
    rect = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    msp.add_lwpolyline([(x * 1e3, y * 1e3) for x, y in rect],
                       close=True, dxfattribs={"layer": "DOMAIN"})
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(out_path)
    return {"dxf_path": str(out_path),
            "domain_m": (x0, y0, x1, y1),
            "proportions": {"front_l": float(front_l),
                            "back_l": float(back_l),
                            "top_h": float(top_h)},
            "n_profiles": len(profs),
            "n_points": sum(len(p) for p in profs)}


# ---------- generated stage scripts ----------
#
# The three batch scripts are the spike's, parameterized and trimmed of
# probe chatter. Substitution is token-based (@@NAME@@) because the
# scripts are full of literal braces; path tokens are injected as
# json.dumps strings so backslashes survive IronPython 2.7 verbatim.

_SC_TEMPLATE = '''\
# SpaceClaim batch script (generated). IronPython 2.7.
# DXF (profiles + domain rectangle) -> one planar fluid face (rectangle
# minus the profile islands) + named edge groups -> fluid.scdocx.
import os
import json
import traceback

DXF = @@DXF@@
LOG = @@LOG@@
RESULT = @@RESULT@@
OUT_SCDOC = @@SCDOC@@

state = {"status": "started", "groups": {}}


def L(msg):
    try:
        f = open(LOG, "a")
        f.write(str(msg) + "\\n")
        f.close()
    except:
        pass


def dump():
    try:
        f = open(RESULT, "w")
        f.write(json.dumps(state))
        f.close()
    except:
        pass


def sample_pts(shape):
    pts = []
    try:
        pts.append((shape.StartPoint.X, shape.StartPoint.Y))
        pts.append((shape.EndPoint.X, shape.EndPoint.Y))
    except:
        pass
    try:
        b = shape.Bounds
        try:
            s = float(b.Start)
            e = float(b.End)
        except:
            s = float(b.Start.Value)
            e = float(b.End.Value)
        g = shape.Geometry
        for t in (0.25, 0.5, 0.75):
            try:
                p = g.Evaluate(s + (e - s) * t).Point
                pts.append((p.X, p.Y))
            except:
                pass
    except:
        pass
    return pts


try:
    L("=== SC script start ===")
    doc = DocumentOpen.Execute(DXF)
    root = GetRootPart()
    curves = list(root.Curves)
    for comp in root.Components:
        try:
            curves.extend(list(comp.Content.Curves))
        except:
            pass
    # quirk 4: headless DXF import parks every entity as DesignCurves on
    # an imported DatumPlane (root.Curves stays empty); LWPOLYLINEs
    # arrive exploded into individual segments
    for dp in root.DatumPlanes:
        try:
            curves.extend(list(dp.Curves))
        except:
            pass
    L("curves found: %d" % len(curves))
    if not curves:
        state["status"] = "error: the DXF import yielded no curves"
        dump()
        raise Exception(state["status"])

    # quirk 5: Fill over ALL curves makes one body carrying the annular
    # fluid face plus one filled island face per profile
    Fill.Execute(Selection.Create(curves))

    def faces():
        out = []
        for b in GetRootPart().Bodies:
            for fc in b.Faces:
                out.append((b, fc))
        return out

    def biggest(fs):
        best = fs[0]
        for bf in fs:
            if bf[1].Area > best[1].Area:
                best = bf
        return best

    fs = faces()
    if not fs:
        state["status"] = "error: Fill produced no faces"
        dump()
        raise Exception(state["status"])
    dom = biggest(fs)
    if len(dom[1].Edges) == 4 and len(fs) >= 2:
        # rectangle filled solid over the islands: subtract each island
        # body so the survivor becomes the annulus (spike route)
        for bf in fs:
            if bf[1].Area < dom[1].Area * 0.999 and bf[0] is not dom[0]:
                try:
                    Combine.Subtract(Selection.Create(dom[0]),
                                     Selection.Create(bf[0]))
                except:
                    L("subtract failed:\\n%s" % traceback.format_exc())
        fs = faces()
        dom = biggest(fs)
    # quirk 5 (cont.): only the max-area face survives -- every filled
    # island face is deleted, whatever their count (multi-element DXFs)
    extras = [bf[1] for bf in fs if bf[1].Area < dom[1].Area * 0.999]
    if extras:
        Delete.Execute(Selection.Create(extras))
        L("deleted %d island face(s)" % len(extras))
        fs = faces()
        dom = biggest(fs)
    face = dom[1]
    if len(face.Edges) <= 4:
        state["status"] = ("error: the fluid face has no profile cutout "
                           "(fill/subtract failed)")
        dump()
        raise Exception(state["status"])
    # tidy: the scdocx carries only the fluid surface body
    try:
        left = [c for c in curves if not c.IsDeleted]
        if left:
            Delete.Execute(Selection.Create(left))
    except:
        L("source-curve cleanup failed (non-fatal):\\n%s"
          % traceback.format_exc())

    # classify edges against the face bbox: rectangle sides become the
    # flow boundaries, everything not on the rectangle is profile
    edges = list(face.Edges)
    edge_pts = [sample_pts(e.Shape) for e in edges]
    allx = []
    ally = []
    for pts in edge_pts:
        for p in pts:
            allx.append(p[0])
            ally.append(p[1])
    xmin, xmax = min(allx), max(allx)
    ymin, ymax = min(ally), max(ally)
    tol = 1e-6 * max(xmax - xmin, ymax - ymin)

    def on_line(pts, idx, target):
        if not pts:
            return False
        for p in pts:
            if abs(p[idx] - target) > tol:
                return False
        return True

    cls = {"inlet": [], "outlet": [], "ground": [], "upper_bound": [],
           "profile": []}
    for e, pts in zip(edges, edge_pts):
        if on_line(pts, 0, xmin):
            cls["inlet"].append(e)
        elif on_line(pts, 0, xmax):
            cls["outlet"].append(e)
        elif on_line(pts, 1, ymin):
            cls["ground"].append(e)
        elif on_line(pts, 1, ymax):
            cls["upper_bound"].append(e)
        else:
            cls["profile"].append(e)
    for k in cls:
        L("edge class %s: %d" % (k, len(cls[k])))
        state["groups"][k] = len(cls[k])

    def make_group(name, items):
        # quirk 6: NamedSelection.Create always names the new group
        # "Group1" (the auto-namer reuses freed numbers) -- rename
        # immediately and CHECK the boolean (Rename returns False
        # instead of raising)
        try:
            s = Selection.Create(items)
            try:
                sec = Selection.Empty()
            except:
                sec = Selection.Create([])
            NamedSelection.Create(s, sec)
            renamed = False
            for gi in range(1, 12):
                ok = NamedSelection.Rename("Group%d" % gi, name)
                if ok:
                    renamed = True
                    break
            if not renamed:
                L("WARN: could not rename a group to %s" % name)
        except:
            L("group %s failed:\\n%s" % (name, traceback.format_exc()))

    for name in ("inlet", "outlet", "ground", "upper_bound", "profile"):
        if cls[name]:
            make_group(name, cls[name])
    make_group("fluid", [face])

    try:
        if os.path.exists(OUT_SCDOC):
            os.remove(OUT_SCDOC)
    except:
        pass
    # quirk 7: DocumentSave writes .scdocx whatever extension is asked
    # for -- the target name is .scdocx up front so the path is honest
    DocumentSave.Execute(OUT_SCDOC)
    state["status"] = "ok"
    state["scdoc"] = OUT_SCDOC
    dump()
    L("=== SC script end (ok) ===")
except:
    L("FATAL:\\n%s" % traceback.format_exc())
    if state["status"] == "started":
        state["status"] = "error: SpaceClaim geometry stage failed"
    dump()
'''

_MECH_TEMPLATE = '''\
# Mechanical meshing script (generated). Delivered into the Mesh cell
# via SendCommand(Language="Python", ...). IronPython 2.7.
import os
import json
import traceback

LOG = @@LOG@@
RESULT = @@RESULT@@

res = {"status": "started", "ns": {}, "mesh": {}}


def L(msg):
    try:
        f = open(LOG, "a")
        f.write(str(msg) + "\\n")
        f.close()
    except:
        pass


def dump():
    try:
        f = open(RESULT, "w")
        f.write(json.dumps(res))
        f.close()
    except:
        pass


try:
    L("=== Mechanical script start ===")
    # quirk 9: v261 drops ExtAPI.Application.Version in this context --
    # never touch it
    model = ExtAPI.DataModel.Project.Model
    geo = ExtAPI.DataModel.GeoData
    bodies = []
    for asm in geo.Assemblies:
        for part in asm.Parts:
            for b in part.Bodies:
                bodies.append(b)
    body = bodies[0]
    faces = list(body.Faces)
    edges = list(body.Edges)
    res["n_faces"] = len(faces)
    res["n_edges"] = len(edges)

    # quirk 11: GeoData vertex coordinates are METERS; classify each
    # edge against the measured bbox. A closed-spline edge has zero
    # vertices -- that is a profile by construction (the rectangle is
    # four straight lines)
    xs = []
    ys = []
    for e in edges:
        for v in e.Vertices:
            xs.append(v.X)
            ys.append(v.Y)
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    tol = 1e-6 * max(xmax - xmin, ymax - ymin)

    def classify(e):
        vx = [v.X for v in e.Vertices]
        vy = [v.Y for v in e.Vertices]
        if not vx:
            return "profile"
        if max([abs(v - xmin) for v in vx]) <= tol:
            return "inlet"
        if max([abs(v - xmax) for v in vx]) <= tol:
            return "outlet"
        if max([abs(v - ymin) for v in vy]) <= tol:
            return "ground"
        if max([abs(v - ymax) for v in vy]) <= tol:
            return "upper_bound"
        return "profile"

    cls = {"inlet": [], "outlet": [], "ground": [], "upper_bound": [],
           "profile": []}
    for e in edges:
        cls[classify(e)].append(e.Id)
    for k in cls:
        L("class %s: %d edges" % (k, len(cls[k])))
        res["ns"][k] = len(cls[k])

    # the CAD import's named selections are purged and rebuilt from the
    # measured geometry, so the zone names are deterministic
    try:
        if model.NamedSelections is not None:
            for ns in list(model.NamedSelections.Children):
                ns.Delete()
    except:
        L("NS purge issue:\\n%s" % traceback.format_exc())

    SelType = Ansys.ACT.Interfaces.Common.SelectionTypeEnum.GeometryEntities

    def make_ns(name, ids):
        sel = ExtAPI.SelectionManager.CreateSelectionInfo(SelType)
        sel.Ids = ids
        ns = model.AddNamedSelection()
        ns.Name = name
        ns.Location = sel
        L("NS %s: %d ids" % (name, len(ids)))

    for name in ("inlet", "outlet", "ground", "upper_bound", "profile"):
        if cls[name]:
            make_ns(name, cls[name])
    make_ns("fluid", [faces[0].Id])

    mesh = model.Mesh
    # (v261's Mesh object exposes no DefeatureSize/DefeatureTolerance
    # in this batch context -- measured AttributeError -- so the
    # defeaturing warnings on the profile's short segments cannot be
    # tuned away; the inflation degrade-retry in run_chain is the
    # working mitigation for the mesher crash they accompany)
    sizing = mesh.AddSizing()
    selP = ExtAPI.SelectionManager.CreateSelectionInfo(SelType)
    selP.Ids = cls["profile"]
    sizing.Location = selP
    try:
        sizing.ElementSize = Quantity("@@EDGE_MM@@ [mm]")
    except:
        from Ansys.Core.Units import Quantity as Q2
        sizing.ElementSize = Q2("@@EDGE_MM@@ [mm]")

    if @@N_LAYERS@@ > 0:
        inf = mesh.AddInflation()
        selF = ExtAPI.SelectionManager.CreateSelectionInfo(SelType)
        selF.Ids = [faces[0].Id]
        inf.Location = selF
        selB = ExtAPI.SelectionManager.CreateSelectionInfo(SelType)
        selB.Ids = cls["profile"]
        inf.BoundaryLocation = selB
        # quirk 10: plain int 2 = First Layer Thickness (the property's
        # enum type is not introspectable through System.Enum in this
        # context); FirstLayerHeight is only assignable once the option
        # is switched
        inf.InflationOption = 2
        try:
            inf.FirstLayerHeight = Quantity("@@H1_MM@@ [mm]")
        except:
            from Ansys.Core.Units import Quantity as Q3
            inf.FirstLayerHeight = Q3("@@H1_MM@@ [mm]")
        inf.MaximumLayers = @@N_LAYERS@@
        try:
            inf.GrowthRate = @@GROWTH@@
        except:
            L("GrowthRate not settable (Mechanical default stands)")
    else:
        L("inflation disabled (n_layers=0)")

    L("generating mesh...")
    mesh.GenerateMesh()
    res["mesh"]["nodes"] = int(str(mesh.Nodes))
    res["mesh"]["elements"] = int(str(mesh.Elements))
    L("mesh done: nodes=%s elements=%s" % (mesh.Nodes, mesh.Elements))
    # the generation's own diagnostics -- the only visibility into WHY
    # a mesh came back empty (best-effort: the Messages API is not
    # guaranteed in every batch context)
    try:
        for m in ExtAPI.Application.Messages:
            try:
                L("MSG [%s] %s" % (m.Severity, m.DisplayString))
            except:
                L("MSG %s" % str(m))
    except:
        L("(mesh messages unavailable in this context)")
    if res["mesh"]["elements"] <= 0:
        # GenerateMesh can "complete" with an empty mesh (seen live when
        # an infeasible inflation collapses the whole generation) -- an
        # empty mesh is a failure, whatever Mechanical's return said
        res["status"] = ("error: mesh generated 0 elements (the sizing "
                         "or inflation does not fit this geometry)")
    else:
        res["status"] = "ok"
    dump()
    L("=== Mechanical script end (%s) ===" % res["status"])
except:
    L("MECH FATAL:\\n%s" % traceback.format_exc())
    res["status"] = "error: Mechanical meshing failed"
    dump()
'''

_WBJN_TEMPLATE = '''\
# encoding: utf-8
# Workbench journal (generated). Run: RunWB2.exe -B -R <this file>.
# Fluid Flow (Fluent) system, geometry pinned 2D, Mechanical mesh via
# SendCommand, Fluent transfer mesh located and reported.
import os
import json
import traceback

LOG = @@LOG@@
RESULT = @@RESULT@@
SCDOC = @@SCDOC@@
MECHPY = @@MECHPY@@
PROJDIR = @@PROJDIR@@

state = {"status": "started"}


def L(msg):
    try:
        f = open(LOG, "a")
        f.write(str(msg) + "\\n")
        f.close()
    except:
        pass


def dump():
    try:
        f = open(RESULT, "w")
        f.write(json.dumps(state))
        f.close()
    except:
        pass


try:
    L("=== WB journal start ===")
    if not os.path.exists(PROJDIR):
        os.makedirs(PROJDIR)
    Save(FilePath=os.path.join(PROJDIR, "proj.wbpj"), Overwrite=True)

    # quirk 1: the Fluent FFF template's journal name is plain
    # "Fluid Flow" -- v261 has no solver argument for it (the CFX and
    # POLYFLOW templates carry suffixed names, the Fluent one does not)
    template1 = GetTemplate(TemplateName="Fluid Flow")
    system1 = template1.CreateSystem()
    L("system created: %s" % system1.DisplayText)

    geometry1 = system1.GetContainer(ComponentName="Geometry")
    geomComp = system1.GetComponent(Name="Geometry")
    props = geometry1.GetGeometryProperties()
    # quirk 2: the 2D pin must land BEFORE the geometry file is
    # attached, or the import runs 3D
    props.GeometryImportAnalysisType = "AnalysisType_2D"
    # quirk 3: carry the SpaceClaim groups through the import
    props.GeometryImportNamedSelections = True
    geometry1.SetFile(FilePath=SCDOC)
    geomComp.Update(AllDependencies=True)
    L("geometry updated (2D)")

    meshComp = system1.GetComponent(Name="Mesh")
    mesh1 = system1.GetContainer(ComponentName="Mesh")
    meshComp.Refresh()
    # quirk 8: batch Mechanical = Edit(Interactive=False) + SendCommand
    # (IronPython 2.7 inside)
    mesh1.Edit(Interactive=False)
    cmd = open(MECHPY).read()
    mesh1.SendCommand(Language="Python", Command=cmd)
    mesh1.Exit()
    meshComp.Update()
    L("mesh cell updated")

    # Refresh (NOT Update -- Update would launch Fluent) makes Workbench
    # write the Fluent input mesh transfer file
    system1.GetComponent(Name="Setup").Refresh()
    Save(Overwrite=True)

    # quirk 12: the transfer mesh lands at <proj>_files/dp0/FFF/MECH/
    # FFF.msh -- there is no explicit export API, so it is located by
    # walking the project (largest wins if several data points exist)
    msh = None
    for r, d, fns in os.walk(PROJDIR):
        for fn in fns:
            if fn.lower() == "fff.msh":
                p = os.path.join(r, fn)
                if msh is None or os.path.getsize(p) > os.path.getsize(msh):
                    msh = p
    if msh is None:
        state["status"] = "error: Workbench produced no Fluent mesh"
    else:
        state["status"] = "ok"
        state["msh"] = msh
        state["msh_bytes"] = os.path.getsize(msh)
        L("mesh file: %s (%d bytes)" % (msh, os.path.getsize(msh)))
except:
    L("WB FATAL:\\n%s" % traceback.format_exc())
    if state["status"] == "started":
        state["status"] = "error: Workbench meshing stage failed"
dump()
L("=== WB journal end (%s) ===" % state["status"])
'''


def _fill(template: str, **tokens) -> str:
    for k, v in tokens.items():
        template = template.replace(f"@@{k}@@", v)
    # IronPython 2.7 (SpaceClaim AND Mechanical) treats any non-ASCII
    # byte in a script without a coding line as a COMPILE error, and
    # SendCommand swallows it silently: the run then fails minutes later
    # on the default-mesh guard with no visible cause. Refuse at
    # generation time instead. (Found live: an em dash in a template
    # comment cost a full meshing chain.)
    try:
        template.encode("ascii")
    except UnicodeEncodeError as e:
        raise Fluent2DError(
            "generate", f"generated script is not pure ASCII at "
            f"offset {e.start}: {template[e.start:e.start + 20]!r}")
    return template


def _pyq(path: Path | str) -> str:
    # json string == valid IronPython string literal, backslashes escaped
    return json.dumps(str(path))


def _cap_inflation(first_layer_mm: float, n_layers: int, growth: float,
                   slot_gap_mm: float | None,
                   ground_clear_mm: float | None):
    """(first_layer_mm, n_layers, note|None) with the layer stack capped
    to the clearances it actually faces.

    A slot gap hosts TWO colliding inflation fronts (one per side), so
    each front gets 0.4 x gap — the same share the studio's own meshes
    use (cfd.BL_CLEAR_FRAC; mirrored literally, scripts/ stays
    standalone). Ground clearance hosts ONE front (the ground edge
    carries no inflation): 0.9 x clearance only guards outright overlap
    — the proven single-element walkthrough case (26 mm of layers in a
    30 mm ride height) must stay uncapped, per default-recipe parity.
    Found live: 1 mm x 10 uncapped in a 5.25 mm slot collapses
    Mechanical's whole generation to 0 elements."""
    budgets = []
    if slot_gap_mm is not None and slot_gap_mm > 0:
        budgets.append(0.4 * slot_gap_mm)
    if ground_clear_mm is not None and ground_clear_mm > 0:
        budgets.append(0.9 * ground_clear_mm)
    if not budgets:
        return first_layer_mm, n_layers, None
    budget = min(budgets)
    total = first_layer_mm * (growth ** n_layers - 1.0) / (growth - 1.0)
    if total <= budget:
        return first_layer_mm, n_layers, None
    n_fit = int(math.log(1.0 + budget * (growth - 1.0) / first_layer_mm)
                / math.log(growth) + 1e-9)
    if n_fit >= 1:
        note = (f"inflation capped to the tightest clearance (layer "
                f"budget {budget:.3g} mm): {n_layers} layers -> {n_fit}")
        return first_layer_mm, n_fit, note
    fl = max(0.05, budget)
    note = (f"inflation capped to fit the tightest clearance: first "
            f"layer {first_layer_mm:g} mm -> {fl:.3g} mm, single layer")
    return fl, 1, note


# ---------- stage runner ----------

def _tail(*paths, n: int = 600) -> str:
    """The first existing, non-empty log's tail — the detail a failed
    stage hands to the user."""
    for p in paths:
        try:
            if p and Path(p).is_file():
                txt = Path(p).read_text(errors="replace").strip()
                if txt:
                    return "; log tail:\n" + txt[-n:]
        except OSError:
            pass
    return ""


def _kill_tree(proc) -> None:
    """Kill a stage and exactly its own children. taskkill /T sweeps
    the tree from the launched pid (RunWB2 fans out to AnsysFW/AnsysWBU/
    ansyscl; SpaceClaim runs standalone) — no psutil, no name-matching
    that could hit another session's ANSYS processes."""
    try:
        k = _popen(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   creationflags=_CREATE_NO_WINDOW)
        k.wait(timeout=30)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def _run_stage(stage: str, cmd: list, work: Path, budget_s: float,
               result_path: Path, script_log: Path,
               cancel_evt) -> tuple[dict, float]:
    """One batch stage: launch, poll (cancel/timeout every ~2 s), then
    trust ONLY the stage's own result JSON (quirk 13: RunWB2 and
    SpaceClaim exit 0 even when their script failed)."""
    try:
        result_path.unlink()          # a stale verdict must never pass
    except FileNotFoundError:
        pass
    except OSError:
        if result_path.exists():
            raise Fluent2DError(
                stage,
                f"could not clear the previous run's {result_path.name} "
                "(locked) — close any orphaned ANSYS process or use a "
                "fresh work directory")
    console = work / f"{stage}_console.txt"
    t0 = time.time()
    with open(console, "wb") as fh:
        proc = _popen(cmd, cwd=str(work), stdout=fh,
                      stderr=subprocess.STDOUT,
                      creationflags=_CREATE_NO_WINDOW)
        while True:
            if cancel_evt is not None and cancel_evt.is_set():
                _kill_tree(proc)
                raise Fluent2DError(stage, "cancelled")
            if proc.poll() is not None:
                break
            if time.time() - t0 > budget_s:
                _kill_tree(proc)
                raise Fluent2DError(
                    stage, f"timed out after {budget_s:.0f} s"
                    + _tail(script_log, console))
            time.sleep(_POLL_S)
    elapsed = time.time() - t0
    if not result_path.is_file():
        raise Fluent2DError(
            stage, "the stage finished without reporting a result"
            + _tail(script_log, console))
    try:
        data = json.loads(result_path.read_text(encoding="utf-8-sig",
                                                errors="replace"))
    except ValueError:
        raise Fluent2DError(
            stage, "the stage's result file is unreadable"
            + _tail(script_log, console))
    if data.get("status") != "ok":
        raise Fluent2DError(
            stage, str(data.get("status") or "no status reported")
            + _tail(script_log, console))
    return data, elapsed


# ---------- mesh validation ----------

def parse_msh_zones(msh_path: str | Path) -> dict:
    """Dimension, cell count and zone names from a Fluent legacy .msh
    (mixed ASCII/binary; the headers this reads are ASCII). Cell count
    comes from the zone-0 declaration — indices are HEX."""
    data = Path(msh_path).read_bytes()
    import re
    m = re.search(rb"\(2\s+(\d)\)", data[:4000])
    dimension = int(m.group(1)) if m else 0
    n_cells = 0
    m = re.search(rb"\(12\s*\(\s*0\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)",
                  data)
    if m:
        n_cells = int(m.group(2), 16) - int(m.group(1), 16) + 1
    zones = []
    for m in re.finditer(
            rb"\((?:45|39)\s*\(\s*(\d+)\s+([\w:-]+)\s+([\w:.-]+)\s*\)\s*\(",
            data):
        zones.append(m.group(3).decode())
    return {"dimension": dimension, "n_cells": n_cells, "zones": zones}


# ---------- the chain ----------

# the six zone names the solver side codes against; the interior zone
# ("interior-fluid") is named by Fluent itself
REQUIRED_ZONES = ("fluid", "inlet", "outlet", "ground", "upper_bound",
                  "profile")


def run_chain(dxf_path: str | Path, work_dir: str | Path, *,
              edge_size_mm: float = 0.1, first_layer_mm: float = 1.0,
              n_layers: int = 10, growth: float = 1.2,
              slot_gap_mm: float | None = None,
              ground_clear_mm: float | None = None,
              sc_budget_s: float = SC_BUDGET_S,
              wb_budget_s: float = WB_BUDGET_S,
              version: str = "", cancel_evt=None,
              progress_cb=None) -> dict:
    """DXF -> validated Fluent 2D mesh, entirely through ANSYS tools.

    Generates the three stage scripts into work_dir (sc_build.py /
    mech_mesh.py / mesh.wbjn, results in sc_result.json /
    mech_result.json / wb_result.json, geometry in fluid.scdocx, the
    Workbench project under work_dir/wb), runs SpaceClaim then RunWB2
    through the _popen seam, and validates each stage's own result.
    The returned mesh is proven, not assumed: 2D header, all six zone
    names, and a cell count that matches what Mechanical generated
    (quirk 13: a failed Mechanical script still yields a default mesh,
    so file existence proves nothing).

    cancel_evt (a threading.Event) aborts mid-stage within ~2 s by
    killing the stage's process tree; progress_cb(str), when given, is
    called with a phase line at each stage start."""
    if not (edge_size_mm > 0 and math.isfinite(edge_size_mm)):
        raise ValueError("edge_size_mm must be positive")
    if not (first_layer_mm > 0 and math.isfinite(first_layer_mm)):
        raise ValueError("first_layer_mm must be positive")
    n_layers = int(n_layers)
    if not (0 <= n_layers <= 100):
        raise ValueError("n_layers must be between 0 (no inflation) "
                         "and 100")
    if not (1.0 < growth <= 3.0):
        raise ValueError("growth must be in (1, 3]")
    dxf_path = Path(dxf_path)
    if not dxf_path.is_file():
        raise ValueError(f"DXF not found: {dxf_path}")
    first_layer_mm, n_layers, cap_note = _cap_inflation(
        first_layer_mm, n_layers, growth, slot_gap_mm, ground_clear_mm)
    _root, _ver, sc_exe, wb_exe = _find_install(version)

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    sc_py = work / "sc_build.py"
    mech_py = work / "mech_mesh.py"
    wbjn = work / "mesh.wbjn"
    sc_result = work / "sc_result.json"
    mech_result = work / "mech_result.json"
    wb_result = work / "wb_result.json"
    sc_log = work / "sc_log.txt"
    wb_log = work / "wb_log.txt"
    mech_log = work / "mech_log.txt"
    scdocx = work / "fluid.scdocx"
    projdir = work / "wb"
    # a rerun in the same work_dir must never validate last run's mesh
    # OR read last run's log tail into a new failure: results, logs and
    # the project all go. A file that will not delete (an orphaned
    # ANSYS process still holds it) is a loud failure — silently
    # keeping it is exactly the stale-verdict hole this sweep closes.
    for stale in (sc_result, mech_result, wb_result, scdocx,
                  sc_log, wb_log, mech_log):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            if stale.exists():
                raise Fluent2DError(
                    "availability",
                    f"could not clear the previous run's {stale.name} "
                    "(locked) — close any orphaned ANSYS process or "
                    "use a fresh work directory")
    if projdir.exists():
        import shutil
        shutil.rmtree(projdir, ignore_errors=True)
    if projdir.exists() and any(projdir.rglob("FFF.msh")):
        # the largest-FFF.msh walk in the journal would validate last
        # run's mesh as this run's on a soft transfer failure
        raise Fluent2DError(
            "workbench",
            "the previous Workbench project could not be removed (a "
            "file is locked, likely by an orphaned ANSYS process) — "
            "close it or use a fresh work directory")

    def _progress(msg: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(msg)
            except Exception:
                pass

    sc_py.write_text(_fill(_SC_TEMPLATE, DXF=_pyq(dxf_path),
                           LOG=_pyq(sc_log), RESULT=_pyq(sc_result),
                           SCDOC=_pyq(scdocx)),
                     encoding="utf-8", newline="\n")
    wbjn.write_text(_fill(_WBJN_TEMPLATE, LOG=_pyq(wb_log),
                          RESULT=_pyq(wb_result), SCDOC=_pyq(scdocx),
                          MECHPY=_pyq(mech_py), PROJDIR=_pyq(projdir)),
                    encoding="utf-8", newline="\n")

    if cancel_evt is not None and cancel_evt.is_set():
        raise Fluent2DError("spaceclaim", "cancelled")
    _progress("SpaceClaim: filling the fluid face from the DXF")
    sc_data, sc_s = _run_stage(
        "spaceclaim",
        [sc_exe, f"/RunScript={sc_py}", "/Headless=True",
         "/Splash=False", "/Welcome=False", "/ExitAfterScript=True"],
        work, sc_budget_s, sc_result, sc_log, cancel_evt)
    groups = sc_data.get("groups") or {}
    missing = [k for k in ("inlet", "outlet", "ground", "upper_bound")
               if not groups.get(k)]
    if missing or int(groups.get("profile") or 0) < 3:
        raise Fluent2DError(
            "spaceclaim",
            "the fluid face's edges did not classify into the expected "
            f"boundaries (missing: {', '.join(missing) or 'profile'})"
            + _tail(sc_log))
    if not scdocx.is_file():
        raise Fluent2DError(
            "spaceclaim", "the geometry document was not saved"
            + _tail(sc_log))

    def _wb_attempt(nl: int) -> tuple:
        """One Workbench meshing pass at nl inflation layers; returns
        (wb_data, wb_s, elements, parsed) or raises Fluent2DError."""
        mech_py.write_text(_fill(_MECH_TEMPLATE, LOG=_pyq(mech_log),
                                 RESULT=_pyq(mech_result),
                                 EDGE_MM=f"{edge_size_mm:g}",
                                 H1_MM=f"{first_layer_mm:g}",
                                 N_LAYERS=str(nl),
                                 GROWTH=f"{growth:g}"),
                           encoding="utf-8", newline="\n")
        # a retry must never validate the failed attempt's leftovers
        for stale in (mech_result, wb_result):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        if projdir.exists():
            import shutil
            shutil.rmtree(projdir, ignore_errors=True)
        if projdir.exists() and any(projdir.rglob("FFF.msh")):
            raise Fluent2DError(
                "workbench",
                "the previous Workbench project could not be removed "
                "(a file is locked) — close any orphaned ANSYS process "
                "or use a fresh work directory")
        if cancel_evt is not None and cancel_evt.is_set():
            raise Fluent2DError("workbench", "cancelled")
        _progress("Workbench: 2D import + Mechanical mesh (edge sizing "
                  f"{edge_size_mm:g} mm, "
                  + (f"inflation {first_layer_mm:g} mm x {nl})" if nl
                     else "no inflation)"))
        wb_data, wb_s = _run_stage(
            "workbench", [wb_exe, "-B", "-R", str(wbjn)],
            work, wb_budget_s, wb_result, wb_log, cancel_evt)

        # the Mechanical script reports through its own JSON — a
        # "default mesh" run (quirk 13) fails HERE, not in Fluent
        if not mech_result.is_file():
            raise Fluent2DError(
                "workbench", "Mechanical never reported a meshing "
                "result" + _tail(mech_log, wb_log))
        mech_data = json.loads(
            mech_result.read_text(encoding="utf-8-sig",
                                  errors="replace"))
        if mech_data.get("status") != "ok":
            raise Fluent2DError(
                "workbench", str(mech_data.get("status"))
                + _tail(mech_log, wb_log))
        elements = int((mech_data.get("mesh") or {}).get("elements")
                       or 0)
        if elements <= 0:
            raise Fluent2DError(
                "workbench", "Mechanical reported an empty mesh"
                + _tail(mech_log))

        msh_path = wb_data.get("msh") or ""
        parsed = parse_msh_zones(msh_path) if os.path.isfile(msh_path) \
            else {"dimension": 0, "n_cells": 0, "zones": []}
        if parsed["dimension"] != 2:
            raise Fluent2DError(
                "workbench", "the transfer mesh is not 2D (dimension "
                f"{parsed['dimension'] or 'unknown'})" + _tail(wb_log))
        missing_z = [z for z in REQUIRED_ZONES
                     if z not in parsed["zones"]]
        if missing_z:
            raise Fluent2DError(
                "workbench", "the transfer mesh is missing zone(s): "
                + ", ".join(missing_z)
                + " — the named selections did not survive the transfer"
                + _tail(mech_log, wb_log))
        # cell-count cross-check: the mesh Fluent gets must be the mesh
        # Mechanical cut (spike: 119371 vs 119410 — interface
        # tolerance, not a different mesh)
        if not parsed["n_cells"] or \
                abs(parsed["n_cells"] - elements) > 0.2 * elements:
            raise Fluent2DError(
                "workbench",
                f"the transfer mesh's cell count ({parsed['n_cells']}) "
                f"does not match the generated mesh ({elements} "
                "elements) — the transfer picked up a stale or default "
                "mesh" + _tail(wb_log))
        return wb_data, wb_s, elements, parsed

    # inflation that cannot be laid on this geometry fails the WHOLE
    # generation inside Mechanical (measured: 0 elements). The clearance
    # cap prevents the predictable case; anything that still empties the
    # mesh gets ONE retry without inflation, recorded as a degradation —
    # a usable mesh with an honest caveat beats an opaque failure
    bl_note = cap_note
    nl_final = n_layers
    try:
        wb_data, wb_s, elements, parsed = _wb_attempt(n_layers)
    except Fluent2DError as e:
        empty = ("empty mesh" in e.detail
                 or "never reported a meshing result" in e.detail
                 or "0 elements" in e.detail)
        if not empty and mech_result.is_file():
            # the journal's own Update() failure outranks the mech
            # verdict in the raise order — read the verdict directly:
            # an empty-mesh report underneath means the same inflation
            # collapse (measured: the 2D mesher crashes internally on
            # infeasible inflation and Update fails at journal level)
            try:
                _md = json.loads(mech_result.read_text(
                    encoding="utf-8-sig", errors="replace"))
                empty = "0 elements" in str(_md.get("status", ""))
            except Exception:
                pass
        if n_layers <= 0 or e.cancelled or not empty:
            raise
        nl_final = 0
        bl_note = (((cap_note + "; ") if cap_note else "")
                   + "the inflation layers failed on this geometry — "
                   "meshed WITHOUT boundary layers (wall resolution "
                   "degraded; treat forces as screening values)")
        wb_data, wb_s, elements, parsed = _wb_attempt(0)

    return {"msh_path": wb_data.get("msh") or "",
            "n_cells": parsed["n_cells"],
            "zones": parsed["zones"],
            "stage_s": {"spaceclaim": round(sc_s, 1),
                        "workbench": round(wb_s, 1)},
            "project_dir": str(projdir),
            "inflation": {"first_layer_mm": first_layer_mm,
                          "n_layers": nl_final,
                          "capped": cap_note is not None,
                          "degraded": nl_final == 0 and n_layers > 0,
                          "note": bl_note}}
