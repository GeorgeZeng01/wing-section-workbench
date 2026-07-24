"""Export content validation: the geometry inside DXF/CSV/SVG/ZIP exports
must be the geometry the app analyzed — right frame, right scale, right
y-sense — not merely a file that opens.

Run directly:  python app/tests/test_export_content.py
"""
import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import airfoils, export, geometry  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = geometry.StackConfig.from_dict({
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "n_panels_per_side": 60,
    "manufacturing": {"te_gap_mm": 1.5, "te_mode": "thicken"},
})


def main():
    design = geometry.build_stack(CFG)
    installed = geometry.install_stack(design, CFG.ride_height_c)
    inst_mm = [np.asarray(e["coords"], float) * CFG.chord_mm
               for e in installed]
    design_mm = [np.asarray(e["coords"], float) * CFG.chord_mm
                 for e in design]

    # ---- CSV: every point matches the installed-frame geometry ----
    text = export.csv_text(CFG, "installed")
    rows = list(csv.reader(io.StringIO(text)))
    check("csv header",
          rows[0] == ["element", "role", "airfoil", "point", "x_mm", "y_mm"])
    by_el = {}
    for row in rows[1:]:
        if row:
            by_el.setdefault(int(row[0]), []).append(
                (float(row[4]), float(row[5])))
    ok = len(by_el) == 2
    for i, pts in by_el.items():
        got = np.asarray(pts)
        ok = ok and got.shape == inst_mm[i - 1].shape \
            and np.allclose(got, inst_mm[i - 1], atol=5e-4)
    check("csv points == installed-frame mm geometry", ok,
          f"({len(rows)-1} rows)")
    # frame parameter respected
    text_d = export.csv_text(CFG, "design")
    rows_d = list(csv.reader(io.StringIO(text_d)))
    got_d = np.asarray([(float(r[4]), float(r[5]))
                        for r in rows_d[1:] if r and r[0] == "1"])
    check("csv design frame differs and matches design geometry",
          np.allclose(got_d, design_mm[0], atol=5e-4)
          and not np.allclose(got_d, inst_mm[0], atol=1.0))

    # ---- DXF (polyline): exact points, layers, ground line ----
    import ezdxf
    data = export.dxf_bytes(CFG, "installed", "polyline")
    doc = ezdxf.read(io.StringIO(data.decode("utf-8")))
    msp = doc.modelspace()
    layers = {e.dxf.layer for e in msp}
    check("dxf layers", {"E1_MAIN", "E2_FLAP1", "GROUND"} <= layers,
          f"({sorted(layers)})")
    polys = {e.dxf.layer: e for e in msp.query("LWPOLYLINE")}
    ok = True
    for i, layer in ((0, "E1_MAIN"), (1, "E2_FLAP1")):
        pts = np.asarray([(p[0], p[1]) for p in polys[layer].get_points()])
        ok = ok and pts.shape == inst_mm[i].shape \
            and np.allclose(pts, inst_mm[i], atol=1e-3) \
            and polys[layer].closed
    check("dxf polyline points == installed mm geometry (closed)", ok)
    glines = [e for e in msp.query("LINE") if e.dxf.layer == "GROUND"]
    check("dxf ground line at y = 0",
          len(glines) == 1 and abs(glines[0].dxf.start[1]) < 1e-9
          and abs(glines[0].dxf.end[1]) < 1e-9)
    # blunt TE (manufacturing prep): spline export = open spline + base LINE
    data_s = export.dxf_bytes(CFG, "installed", "spline")
    doc_s = ezdxf.read(io.StringIO(data_s.decode("utf-8")))
    msp_s = doc_s.modelspace()
    n_splines = len(msp_s.query("SPLINE"))
    base_lines = [e for e in msp_s.query("LINE")
                  if e.dxf.layer in ("E1_MAIN", "E2_FLAP1")]
    check("dxf spline export keeps blunt TE base as straight LINE",
          n_splines == 2 and len(base_lines) == 2,
          f"({n_splines} splines, {len(base_lines)} base lines)")
    for e in base_lines:
        i = 0 if e.dxf.layer == "E1_MAIN" else 1
        s0 = np.asarray(e.dxf.start)[:2]
        e0 = np.asarray(e.dxf.end)[:2]
        want = {tuple(np.round(inst_mm[i][0], 3)),
                tuple(np.round(inst_mm[i][-1], 3))}
        got = {tuple(np.round(s0, 3)), tuple(np.round(e0, 3))}
        check(f"dxf TE base line spans the contour endpoints ({e.dxf.layer})",
              got == want)

    # ---- DXF hitbox: 4 lines on their own layer at the collective extremes
    check("dxf hitbox absent by default", "HITBOX" not in layers)
    data_h = export.dxf_bytes(CFG, "installed", "polyline",
                              include_hitbox=True)
    doc_h = ezdxf.read(io.StringIO(data_h.decode("utf-8")))
    hlines = [e for e in doc_h.modelspace().query("LINE")
              if e.dxf.layer == "HITBOX"]
    allpts_h = np.vstack(inst_mm)
    hx0, hx1 = allpts_h[:, 0].min(), allpts_h[:, 0].max()
    hy0, hy1 = allpts_h[:, 1].min(), allpts_h[:, 1].max()
    segs = {tuple(np.round(sorted([tuple(np.asarray(e.dxf.start)[:2]),
                                   tuple(np.asarray(e.dxf.end)[:2])])
                           , 3).flatten()) for e in hlines}
    want_h = {tuple(np.round(sorted([a, b]), 3).flatten()) for a, b in (
        ((hx0, hy0), (hx1, hy0)), ((hx0, hy1), (hx1, hy1)),
        ((hx0, hy0), (hx0, hy1)), ((hx1, hy0), (hx1, hy1)))}
    check("dxf hitbox = 4 lines touching the collective extremes",
          len(hlines) == 4 and segs == want_h,
          f"({len(hlines)} lines)")
    # the design frame has no ground line but the box is still meaningful
    data_hd = export.dxf_bytes(CFG, "design", "polyline",
                               include_hitbox=True)
    doc_hd = ezdxf.read(io.StringIO(data_hd.decode("utf-8")))
    hd = [e for e in doc_hd.modelspace().query("LINE")
          if e.dxf.layer == "HITBOX"]
    allpts_d = np.vstack(design_mm)
    check("dxf hitbox in the design frame spans the design extents",
          len(hd) == 4 and abs(min(min(e.dxf.start[0], e.dxf.end[0])
                                   for e in hd)
                               - allpts_d[:, 0].min()) < 1e-3)

    # ---- SVG: true scale, ground at y = 0, y-flip consistent ----
    svg = export.svg_bytes(CFG, "installed").decode()
    vb = re.search(r'viewBox="([-\d.]+) 0 ([\d.]+) ([\d.]+)"', svg)
    allpts = np.vstack(inst_mm)
    x0, x1 = allpts[:, 0].min(), allpts[:, 0].max()
    y0, y1 = min(allpts[:, 1].min(), 0.0), allpts[:, 1].max()
    pad = 0.12 * max(x1 - x0, y1 - y0)
    check("svg viewBox is true mm scale",
          vb and abs(float(vb.group(2)) - ((x1 - x0) + 2 * pad)) < 0.1
          and abs(float(vb.group(3)) - ((y1 - y0) + 2 * pad)) < 0.1)
    gy = re.search(r'<line x1="[-\d.]+" y1="([\d.]+)"', svg)
    check("svg ground line maps y=0 through the flip",
          gy and abs(float(gy.group(1)) - (y1 + pad)) < 0.05)
    first_pt = re.search(r'd="M ([-\d.]+) ([\d.]+)', svg)
    exp = inst_mm[0][0]
    check("svg first path point matches geometry (y flipped)",
          first_pt and abs(float(first_pt.group(1)) - exp[0]) < 0.01
          and abs(float(first_pt.group(2)) - ((y1 + pad) - exp[1])) < 0.01)

    # ---- ZIP bundle: dat round-trip + manifest ----
    blob = export.zip_bundle(CFG, None, "spline")
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = set(z.namelist())
    need = {"README.txt", "dxf/section_installed.dxf",
            "dxf/section_design.dxf", "section.csv", "section.svg",
            "manifest.json"}
    check("zip bundle contents", need <= names, f"({sorted(names)[:4]}...)")
    ok = True
    for i, e in enumerate(installed):
        tag = f"{i+1}_{e['role']}"
        _, back = airfoils.read_dat(
            z.read(f"dat/stack_{tag}.dat").decode(), "rt")
        ok = ok and np.allclose(back, installed[i]["coords"], atol=1e-5)
    check("zip stack .dat files round-trip the installed geometry", ok)
    man = json.loads(z.read("manifest.json"))
    els = man["configuration"]["elements"]
    check("manifest carries slot metrics and as-built data",
          len(els) == 2 and "slot_gap_pct_c" in els[1]
          and "as_built" in els[1]
          and els[1]["as_built"]["te_gap_mm"] >= 1.45,
          f"(flap as_built {els[1].get('as_built')})")

    print(f"\n{sum(results)}/{len(results)} export-content checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
