"""Export the stack in CAD-ready and analysis-ready formats.

Formats
-------
DXF   R2010 ASCII, millimeters, true scale. One closed contour per element on
      its own layer (E1_MAIN, E2_FLAP1, ...), as SPLINE (fit points, smooth
      for lofting) or LWPOLYLINE (exact point data). Optional GROUND layer.
      Imports directly as a sketch in SolidWorks / Fusion / Onshape / NX.
DAT   Selig coordinate files per element: real-position stack contours in
      main-chord units (meshing / panel workflows) plus the unit-chord
      profiles (XFOIL et al.).
TXT   x  y  z point files per element in millimeters (curve-through-points
      import, e.g. SolidWorks "Curve Through XYZ Points").
CSV   all contour points, one row per point, with element metadata.
SVG   true-scale (mm) drawing of the installed stack with ground line.
JSON  full configuration + analysis snapshot (machine-readable manifest).
ZIP   all of the above bundled, with a README describing each file.

Frames: "installed" (as driven: downforce down, ground at y = 0) or
"design" (upright, main LE at origin). Both are exact rigid transforms of
the same geometry.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import date

import numpy as np

from . import geometry
from .geometry import StackConfig

LAYER_COLORS = [5, 1, 3, 2]  # blue, red, green, yellow (AutoCAD color index)


def _frame_elements(cfg: StackConfig, frame: str) -> list[dict]:
    design = geometry.build_stack(cfg)
    if frame == "design":
        return design
    return geometry.install_stack(design, cfg.ride_height_c)


def _mm(e: dict, cfg: StackConfig) -> np.ndarray:
    return np.asarray(e["coords"], float) * cfg.chord_mm


def _layer_name(i: int, role: str) -> str:
    return f"E{i+1}_{role.upper()}"


def dxf_bytes(cfg: StackConfig, frame: str = "installed",
              entity: str = "spline", include_ground: bool = True,
              include_hitbox: bool = False) -> bytes:
    import ezdxf
    from ezdxf import units

    doc = ezdxf.new("R2010", setup=False)
    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    msp = doc.modelspace()

    elements = _frame_elements(cfg, frame)
    for i, e in enumerate(elements):
        layer = _layer_name(i, e["role"])
        doc.layers.add(layer, color=LAYER_COLORS[i % len(LAYER_COLORS)])
        pts = _mm(e, cfg)
        if entity == "polyline":
            msp.add_lwpolyline(pts.tolist(), close=True,
                               dxfattribs={"layer": layer})
        elif np.hypot(*(pts[0] - pts[-1])) > 0.05:
            # blunt trailing edge (manufacturing prep or an open .dat): keep
            # the base flat and its corners sharp — smooth open spline along
            # the surface plus a straight base line, instead of a closed
            # spline that would round the corners off
            msp.add_spline(fit_points=[(x, y, 0.0) for x, y in pts],
                           dxfattribs={"layer": layer})
            msp.add_line(tuple(pts[-1]), tuple(pts[0]),
                         dxfattribs={"layer": layer})
        else:
            closed = np.vstack([pts, pts[:1]])
            msp.add_spline(fit_points=[(x, y, 0.0) for x, y in closed],
                           dxfattribs={"layer": layer})

    if include_ground and frame == "installed":
        doc.layers.add("GROUND", color=8)
        allpts = np.vstack([_mm(e, cfg) for e in elements])
        x0, x1 = allpts[:, 0].min(), allpts[:, 0].max()
        pad = 0.25 * (x1 - x0)
        msp.add_line((x0 - pad, 0.0), (x1 + pad, 0.0),
                     dxfattribs={"layer": "GROUND"})

    if include_hitbox:
        # collective bounding box as 4 separate LINEs on their own layer:
        # each line touches the stack's extreme point on its side, so CAD
        # users get instant overall dimensions and can delete the whole
        # box by killing the HITBOX layer
        doc.layers.add("HITBOX", color=6)   # magenta — clearly not geometry
        allpts = np.vstack([_mm(e, cfg) for e in elements])
        x0, x1 = allpts[:, 0].min(), allpts[:, 0].max()
        y0, y1 = allpts[:, 1].min(), allpts[:, 1].max()
        for a, b in (((x0, y0), (x1, y0)),   # bottom: touches lowest point
                     ((x0, y1), (x1, y1)),   # top: touches highest point
                     ((x0, y0), (x0, y1)),   # left: touches leading point
                     ((x1, y0), (x1, y1))):  # right: touches trailing point
            msp.add_line(a, b, dxfattribs={"layer": "HITBOX"})

    buf = io.StringIO()
    doc.write(buf, fmt="asc")
    return buf.getvalue().encode("utf-8")


def polished_dxf_bytes(polys_m: list, include_ground: bool = True) -> bytes:
    """True-scale (mm) DXF of a POLISHED free-form stack: explicit
    installed-frame polylines in meters (the adjoint polish's
    deliverable), one closed LWPOLYLINE per element on its own layer
    (E1_MAIN_POLISHED, E2_FLAP1_POLISHED, ...). Always exact polylines,
    never fit-point splines — a spline would re-smooth the very
    millimeter-scale shaping the polish added."""
    import ezdxf
    from ezdxf import units

    doc = ezdxf.new("R2010", setup=False)
    doc.units = units.MM
    doc.header["$MEASUREMENT"] = 1
    msp = doc.modelspace()
    roles = ["MAIN"] + [f"FLAP{i}" for i in range(1, len(polys_m))]
    pts_mm = [np.asarray(p, float) * 1000.0 for p in polys_m]
    for i, (pts, role) in enumerate(zip(pts_mm, roles)):
        layer = f"E{i + 1}_{role}_POLISHED"
        doc.layers.add(layer, color=LAYER_COLORS[i % len(LAYER_COLORS)])
        msp.add_lwpolyline(pts.tolist(), close=True,
                           dxfattribs={"layer": layer})
    if include_ground:
        doc.layers.add("GROUND", color=8)
        allpts = np.vstack(pts_mm)
        x0, x1 = allpts[:, 0].min(), allpts[:, 0].max()
        pad = 0.25 * (x1 - x0)
        msp.add_line((x0 - pad, 0.0), (x1 + pad, 0.0),
                     dxfattribs={"layer": "GROUND"})
    buf = io.StringIO()
    doc.write(buf, fmt="asc")
    return buf.getvalue().encode("utf-8")


def dat_text(coords: np.ndarray, name: str) -> str:
    # the name is the payload's first line: an uploaded display name with an
    # embedded newline would inject lines that Selig parsers read as real
    # contour points
    clean = re.sub(r"[\x00-\x1f\x7f]", " ", str(name))
    clean = re.sub(r"\s+", " ", clean).strip()[:80].rstrip() or "airfoil"
    lines = [clean]
    lines += [f" {x:.6f} {y:.6f}" for x, y in np.asarray(coords, float)]
    return "\n".join(lines) + "\n"


def txt_points(coords_mm: np.ndarray) -> str:
    return "\n".join(f"{x:.4f}\t{y:.4f}\t0.0000" for x, y in coords_mm) + "\n"


def csv_text(cfg: StackConfig, frame: str) -> str:
    import csv as _csv
    buf = io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")   # quotes names with commas
    w.writerow(["element", "role", "airfoil", "point", "x_mm", "y_mm"])
    for i, e in enumerate(_frame_elements(cfg, frame)):
        for j, (x, y) in enumerate(_mm(e, cfg)):
            w.writerow([i + 1, e["role"], e["airfoil_name"], j,
                        f"{x:.4f}", f"{y:.4f}"])
    return buf.getvalue()


def svg_bytes(cfg: StackConfig, frame: str = "installed") -> bytes:
    elements = _frame_elements(cfg, frame)
    pts_all = np.vstack([_mm(e, cfg) for e in elements])
    x0, x1 = pts_all[:, 0].min(), pts_all[:, 0].max()
    y0, y1 = pts_all[:, 1].min(), pts_all[:, 1].max()
    if frame == "installed":
        y0 = min(y0, 0.0)
    pad = 0.12 * max(x1 - x0, y1 - y0)
    w, h = (x1 - x0) + 2 * pad, (y1 - y0) + 2 * pad
    colors = ["#1f4e8c", "#b03a2e", "#1e8449", "#b7950b"]

    def Y(y):  # SVG y is down; keep mm scale
        return (y1 + pad) - y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" '
        f'height="{h:.1f}mm" viewBox="{x0 - pad:.2f} 0 {w:.2f} {h:.2f}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    if frame == "installed":
        gy = Y(0.0)
        parts.append(f'<line x1="{x0 - pad:.1f}" y1="{gy:.2f}" '
                     f'x2="{x1 + pad:.1f}" y2="{gy:.2f}" stroke="#333" '
                     f'stroke-width="1.2"/>')
        for gx in np.arange(x0 - pad, x1 + pad, 18.0):
            parts.append(f'<line x1="{gx:.1f}" y1="{gy:.2f}" '
                         f'x2="{gx - 7:.1f}" y2="{gy + 7:.2f}" stroke="#777" '
                         f'stroke-width="0.5"/>')
    for i, e in enumerate(elements):
        pts = _mm(e, cfg)
        d = "M " + " L ".join(f"{x:.3f} {Y(y):.3f}" for x, y in pts) + " Z"
        parts.append(f'<path d="{d}" fill="{colors[i % 4]}22" '
                     f'stroke="{colors[i % 4]}" stroke-width="0.6"/>')
    label = (f"{len(elements)}-element section — chord {cfg.chord_mm:.0f} mm — "
             f"ride height {cfg.ride_height_mm:.0f} mm — scale 1:1 (mm)")
    parts.append(f'<text x="{x0 - pad + 4:.1f}" y="12" font-family="monospace" '
                 f'font-size="6" fill="#333">{label}</text>')
    parts.append("</svg>")
    return "\n".join(parts).encode("utf-8")


def manifest_dict(cfg: StackConfig, analysis_result: dict | None) -> dict:
    design = geometry.build_stack(cfg)
    mfg_warnings = geometry.manufacturing_report(cfg, design)
    ext = geometry.stack_extents(design)
    man = {
        "generated": date.today().isoformat(),
        "configuration": {
            "chord_mm": cfg.chord_mm, "span_mm": cfg.span_mm,
            "ride_height_mm": cfg.ride_height_mm,
            "stack_aoa_deg": cfg.stack_aoa_deg,
            "speed_ms": cfg.speed_ms, "rho": cfg.rho, "nu": cfg.nu,
            "ncrit": cfg.ncrit,
            "viscous_efficiency": cfg.viscous_efficiency,
            "efficiency_3d": cfg.efficiency_3d,
            "elements": [{
                "role": e["role"], "airfoil": e["airfoil"],
                "chord_ratio": e["chord_ratio"],
                "chord_mm": e["chord_ratio"] * cfg.chord_mm,
                "deflection_deg": e["deflection_deg"],
                **({"slot_gap_pct_c": round(e["slot_gap"] * 100, 2),
                    "slot_overlap_pct_c": round(e["slot_overlap"] * 100, 2)}
                   if "slot_gap" in e else {}),
                **({"as_built": e["mfg"]} if "mfg" in e else {}),
            } for e in design],
            "system_chord_mm": round(ext["system_chord_c"] * cfg.chord_mm, 2),
        },
    }
    if cfg.manufacturing is not None:
        m = cfg.manufacturing
        man["configuration"]["manufacturing"] = {
            "te_thickness_mm": m.te_gap_mm,
            "te_treatment": m.te_mode,
            "min_thickness_mm": m.min_thickness_mm,
            "warnings": mfg_warnings,
            "note": "all exported contours are the as-built (treated) "
                    "profiles; trailing edges are opened to the thickness "
                    "above and closed by a flat base",
        }
    if analysis_result:
        man["analysis"] = {k: analysis_result[k] for k in
                           ("coefficients", "forces", "warnings")
                           if k in analysis_result}
    return man


_ZIP_README = """MULTI-ELEMENT WING SECTION - EXPORT BUNDLE
==========================================

Contents
--------
dxf/section_installed.dxf   True-scale (mm) DXF, installed orientation
                            (downforce down, ground line at y = 0).
dxf/section_design.dxf      Same geometry, upright design orientation
                            (lift up, main-element LE at the origin).
                            One closed contour per element, one layer per
                            element (E1_MAIN, E2_FLAP1, ...). Import directly
                            as a sketch and loft/extrude.
txt/element_*.txt           Per-element x/y/z point lists in millimeters,
                            for curve-through-points import.
dat/stack_*.dat             Per-element contours at their installed positions
                            in MAIN-CHORD UNITS (Selig format) - meshing and
                            panel-method input.
dat/profile_*.dat           Unit-chord airfoil profiles (XFOIL-ready).
section.csv                 Every contour point with element metadata (mm).
section.svg                 True-scale drawing for quick visual checks.
manifest.json               Full configuration + analysis snapshot.

Conventions
-----------
Millimeter files are true scale. The installed frame is as-driven: x
downstream, y up, ground plane at y = 0, the section's lowest point at the
configured ride height. The design frame is the same geometry mirrored
upright with the main-element leading edge at the origin.

The DXF uses fit-point splines through the exact contour points (or
polylines, if that option was chosen at export). For lofted CAD surfaces,
splines are usually preferred; for exact point fidelity, use the polyline
variant or the TXT point files.

When manufacturing prep is enabled in the app, every exported contour is the
as-built profile: trailing edges are opened to the configured minimum
thickness (blended thickening or a straight truncation) and closed by a flat
base. In the spline DXF that base is a separate straight LINE between the
two surface endpoints on the same layer, keeping the corners sharp; the
manifest records the achieved trailing-edge thickness per element.
"""


def zip_bundle(cfg: StackConfig, analysis_result: dict | None = None,
               entity: str = "spline", include_hitbox: bool = False) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _ZIP_README)
        z.writestr("dxf/section_installed.dxf",
                   dxf_bytes(cfg, "installed", entity,
                             include_hitbox=include_hitbox))
        z.writestr("dxf/section_design.dxf",
                   dxf_bytes(cfg, "design", entity, include_ground=False,
                             include_hitbox=include_hitbox))
        installed = _frame_elements(cfg, "installed")
        for i, e in enumerate(installed):
            tag = f"{i+1}_{e['role']}"
            z.writestr(f"txt/element_{tag}.txt", txt_points(_mm(e, cfg)))
            z.writestr(f"dat/stack_{tag}.dat",
                       dat_text(e["coords"], f"{e['role']} {e['airfoil_name']} "
                                             f"(installed, main-chord units)"))
        import re as _re
        for i in range(len(cfg.elements)):
            from . import airfoils
            spec_eff = geometry.effective_spec(cfg, i)
            name, uc = airfoils.repaneled(spec_eff, cfg.n_panels_per_side)
            safe = _re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "airfoil"
            z.writestr(f"dat/profile_{i+1}_{safe}.dat", dat_text(uc, name))
        z.writestr("section.csv", csv_text(cfg, "installed"))
        z.writestr("section.svg", svg_bytes(cfg, "installed"))
        z.writestr("manifest.json",
                   json.dumps(manifest_dict(cfg, analysis_result), indent=2))
    return buf.getvalue()
