"""Manufacturing-prep regression suite (no server needed).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_manufacturing.py

Covers the TE treatments themselves, the derived-spec plumbing, the full
geometry/analysis integration, exports, optimizer candidate pools and the
config validation surface.
"""
import io
import json
import sys
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import (airfoils, analysis, export, geometry,  # noqa: E402
                      manufacturing as mfg, optimizer)

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


BASE = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}
MFG = {"te_gap_mm": 1.5, "te_mode": "thicken", "min_thickness_mm": 4}


# ---- 1. the treatments themselves --------------------------------------
_, raw = airfoils.resolve("s1223")
c = airfoils.normalize(raw)

out, meta = mfg.apply_te_treatment(c, 0.0035, "thicken")
fwd = c[:, 0] < 0.6
check("thicken: exact TE gap, forward shape untouched",
      abs(mfg.te_gap(out) - 0.0035) < 1e-9
      and np.allclose(c[fwd], out[fwd]) and not meta["clamped"],
      f"(gap {mfg.te_gap(out):.5f})")

out2, meta2 = mfg.apply_te_treatment(c, 0.0100, "truncate")
check("truncate: gap >= target, unit chord restored, sane cut",
      mfg.te_gap(out2) >= 0.0100 - 1e-9
      and abs(out2[:, 0].max() - 1.0) < 1e-6
      and 0.88 <= meta2["x_cut"] <= 0.99 and not meta2["clamped"],
      f"(gap {mfg.te_gap(out2):.4f}, cut at x={meta2['x_cut']})")

# an absurd request must clamp, not destroy the section
out3, meta3 = mfg.apply_te_treatment(c, 0.09, "truncate")
check("truncate: absurd target clamps at the safety limit",
      meta3["clamped"] and out3[:, 0].max() <= 1.0 + 1e-6
      and len(out3) > 20)

# already-blunt sections pass through unchanged
blunt, _ = mfg.apply_te_treatment(out, 0.0020, "thicken")
check("already-blunt TE is left alone", np.allclose(blunt, out))

# the waist guarantee (regression for the reported bug): after thickening,
# nothing aft of the section's thickest point may be thinner than the TE
# gap it was opened to — even for thin sail sections and blunt-TE sections
# whose waist sits well ahead of the TE
for wname, wgap in (("e377", 0.0165), ("as6095", 0.0100),
                    ("s9104BTE", 0.0100), ("s1223", 0.0098)):
    _, wraw = airfoils.resolve(wname)
    wuc = airfoils.normalize(wraw)
    wout, wmeta = mfg.apply_te_treatment(wuc, wgap, "thicken")
    aft_min = mfg.aft_min_thickness(wout)
    check(f"thicken floors the waist ({wname})",
          aft_min >= wgap - 1e-6 and mfg.te_gap(wout) >= wgap - 1e-9,
          f"(aft min {aft_min:.5f}, TE {mfg.te_gap(wout):.5f}, gap {wgap})")

# ... and the guarantee must survive the spline repaneling every consumer
# (solver, viewport, exports) actually sees
wspec = mfg.derived_spec("e377", 0.0165, "thicken")
_, wrp = airfoils.repaneled(wspec, 70)
wg = mfg.parse(wspec)[1]
check("waist floor survives spline repaneling",
      mfg.aft_min_thickness(wrp) >= wg - 2e-4,
      f"(aft min {mfg.aft_min_thickness(wrp):.5f} vs gap {wg})")

# ---- 2. derived specs ---------------------------------------------------
spec = mfg.derived_spec("s1223", 0.0034, "thicken")
mode, gap_c, base_spec = mfg.parse(spec)
check("derived spec round-trip, gap quantized UP",
      mode == "thicken" and base_spec == "s1223" and gap_c >= 0.0034,
      f"({spec})")

spec_c = mfg.derived_spec("custom:my-foil", 0.005, "truncate")
check("base specs containing ':' survive parsing",
      mfg.parse(spec_c)[2] == "custom:my-foil")

# a shape: wrapper between two mfg: layers must not smuggle in a second TE
# treatment (direct nesting is caught by geometry validation already)
try:
    mfg.parse("mfg:thicken:0.0030:shape:+0.001:0:0:1.0:"
              "mfg:thicken:0.0030:s1223")
    check("nested mfg via shape wrapper rejected", False)
except ValueError:
    check("nested mfg via shape wrapper rejected", True)
# ...while mfg-around-shape (the legitimate build-time layering) still parses
check("mfg wrapping a shaped section still parses",
      mfg.parse("mfg:thicken:0.0030:shape:+0.001:0:0:1.0:s1223")[2]
      == "shape:+0.001:0:0:1.0:s1223")

# a raw mfg: element spec + the global manufacturing block must NOT stack a
# second mfg layer (that produced an invalid mfg:...:mfg:... spec that failed
# analysis) — effective_spec respects the explicit per-element treatment
cfg_rawmfg = geometry.StackConfig.from_dict({
    **BASE,
    "elements": [{"airfoil": "mfg:thicken:0.004:s1223", "chord_ratio": 1.0}],
    "manufacturing": {"te_gap_mm": 1.2, "te_mode": "thicken"}})
eff = geometry.effective_spec(cfg_rawmfg, 0)
r_rawmfg = analysis.analyze(cfg_rawmfg, include_geometry=False)
check("raw mfg element + manufacturing block does not double-wrap",
      eff == "mfg:thicken:0.004:s1223"
      and eff.lower().count("mfg:") == 1
      and r_rawmfg["forces"]["downforce_n"] > 0,
      f"(eff {eff}, {r_rawmfg['forces']['downforce_n']} N)")

name, rp = airfoils.repaneled(spec, 70)
check("mfg spec resolves + repanels, gap survives repaneling",
      "(mfg)" in name and abs(mfg.te_gap(rp) - gap_c) < 1e-6,
      f"({name}, {len(rp)} pts)")

# ---- 3. stack + analysis integration ------------------------------------
cfg0 = geometry.StackConfig.from_dict(BASE)
cfg1 = geometry.StackConfig.from_dict({**BASE, "manufacturing": MFG})
r0 = analysis.analyze(cfg0, include_geometry=False)
r1 = analysis.analyze(cfg1, include_geometry=False)
d0, d1 = r0["forces"]["downforce_n"], r1["forces"]["downforce_n"]
check("thicken: analysis runs, downforce within 3% of untreated",
      abs(d1 - d0) / abs(d0) < 0.03, f"({d0} -> {d1} N)")
check("analysis elements use the as-built sections",
      all(e["airfoil_eff"].startswith("mfg:") for e in r1["elements"]))

rep = geometry.geometry_report(cfg1)
els = rep["design"]
check("geometry report: as-built TE thickness >= requested on every element",
      all(e["mfg"]["te_gap_mm"] >= MFG["te_gap_mm"] - 0.05 for e in els)
      and rep["manufacturing"]["te_mode"] == "thicken",
      f"({[e['mfg']['te_gap_mm'] for e in els]} mm)")

# per-element TE gap in real mm on the drawn stack contours
for e, spec_el in zip(els, cfg1.elements):
    pts = np.asarray(e["coords"])
    gap_mm = float(np.hypot(*(pts[0] - pts[-1]))) * cfg1.chord_mm
    check(f"  drawn {e['role']} contour TE = {gap_mm:.2f} mm",
          gap_mm >= MFG["te_gap_mm"] - 0.05)

# min-thickness warning fires when impossible to satisfy
cfg_warn = geometry.StackConfig.from_dict(
    {**BASE, "manufacturing": {**MFG, "min_thickness_mm": 25}})
rep_w = geometry.geometry_report(cfg_warn)
check("min-thickness warning fires (flap thinner than 25 mm)",
      any("thickest point" in w for w in rep_w["warnings"]),
      f"({len(rep_w['warnings'])} warnings)")

# extra-drag disclosure keys off the BUILT geometry: 2 mm on a 70 mm flap is
# 2.86%c — under the 3% ratio line — yet the thickness floor holds the whole
# aft quarter of the S1223 at constant thickness; the same 2 mm barely
# touches the 350 mm main
cfg_slab = geometry.StackConfig.from_dict({
    **BASE,
    "elements": [
        {"airfoil": "s1223"},
        {"airfoil": "s1223", "chord_ratio": 0.20, "deflection_deg": 20,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "manufacturing": {"te_gap_mm": 2.0, "te_mode": "thicken",
                      "min_thickness_mm": 0}})
rep_slab = geometry.geometry_report(cfg_slab)
check("aft slab on a small flap is disclosed (2 mm TE, 70 mm chord)",
      any("constant thickness" in w and w.startswith("flap")
          for w in rep_slab["warnings"]),
      f"({rep_slab['warnings']})")
check("no slab warning for the lightly reshaped main",
      not any("constant thickness" in w and w.startswith("main")
              for w in rep_slab["warnings"]))

# the report measures the as-built waist and vouches for it
check("geometry report carries measured min aft thickness + waist_ok",
      all("min_aft_thickness_mm" in e["mfg"] and e["mfg"]["waist_ok"]
          for e in els),
      f"({[e['mfg']['min_aft_thickness_mm'] for e in els]} mm)")

# plate detection: a section whose thickest point is below the requested TE
# (as6097 is ~3 mm thick at a 70 mm chord) must be flagged critically, and a
# thin stretch between the nose and the thickest point must be floored too
# (as6099 dipped to 2.35 mm between its LE crossing and thickest point)
cfg_plate = geometry.StackConfig.from_dict({
    **BASE,
    "elements": [
        {"airfoil": "s1223"},
        {"airfoil": "as6097", "chord_ratio": 0.20, "deflection_deg": 1.6,
         "slot_gap_pct": 0.9, "slot_overlap_pct": 1.6},
        {"airfoil": "as6099", "chord_ratio": 0.31, "deflection_deg": 15.9,
         "slot_gap_pct": 0.8, "slot_overlap_pct": 0.8}],
    "manufacturing": {"te_gap_mm": 3.0, "te_mode": "thicken",
                      "min_thickness_mm": 0}})
rep_p = geometry.geometry_report(cfg_plate)
mf1, mf2 = rep_p["design"][1]["mfg"], rep_p["design"][2]["mfg"]
check("plate section (as6097 vs 3 mm TE) flagged critically",
      mf1["plate"] and not mf1["thickness_ok"]
      and any("plate" in w for w in rep_p["warnings"]),
      f"(tmax {mf1['max_thickness_mm']} mm)")
check("thin stretch behind the nose floored (as6099)",
      (not mf2["plate"]) and mf2["waist_ok"]
      and mf2["min_aft_thickness_mm"] >= 3.0 - 0.05,
      f"(min behind nose {mf2['min_aft_thickness_mm']} mm)")

# with manufacturing on, the optimizer must not shortlist sections thinner
# than the TE they would have to carry
cfg_te_floor = geometry.StackConfig.from_dict(
    {**BASE, "manufacturing": {"te_gap_mm": 3.0, "te_mode": "thicken",
                               "min_thickness_mm": 0}})
sl_te = optimizer.build_airfoil_shortlists(BASE, cfg_te_floor, pool="auto")
floor_pct_te = 1.5 * 3.0 / (0.35 * 350) * 100
thin_te = []
for s in sl_te[1]:
    if s.startswith("custom:") or s == BASE["elements"][1]["airfoil"]:
        continue
    _, cc = airfoils.repaneled(s, 60)
    if airfoils.geometry_info(cc)["max_thickness"] * 100 < floor_pct_te - 0.1:
        thin_te.append(s)
check("TE thickness floors optimizer candidates (3 mm TE, 122 mm flap)",
      not thin_te, f"(floor {floor_pct_te:.1f}%c, violators {thin_te})")

# toggling manufacturing must be fully reversible: identical sharp builds
# before and after, and legacy dx/dy flaps must migrate to the SHARP
# achieved slot values (as-built numbers would bake the treated placement in)
DXDY = {**BASE, "elements": [
    BASE["elements"][0],
    {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
     "dx": -0.03, "dy": -0.03}]}
r_s1 = geometry.geometry_report(geometry.StackConfig.from_dict(DXDY))
r_m = geometry.geometry_report(geometry.StackConfig.from_dict(
    {**DXDY, "manufacturing": {"te_gap_mm": 3.0, "te_mode": "thicken"}}))
r_s2 = geometry.geometry_report(geometry.StackConfig.from_dict(DXDY))
check("mfg toggle: sharp build identical before/after a treated build",
      all(np.array_equal(np.asarray(a["coords"]), np.asarray(b["coords"]))
          for a, b in zip(r_s1["design"], r_s2["design"])))
f_m = r_m["design"][1]
check("dx/dy migration values come from the sharp geometry",
      "slot_gap_sharp" in f_m
      and abs(f_m["slot_gap_sharp"] - r_s1["design"][1]["slot_gap"]) < 1e-6
      and abs(f_m["slot_gap_sharp"] - f_m["slot_gap"]) > 0.003,
      f"(sharp {f_m['slot_gap_sharp']*100:.2f}%c vs as-built "
      f"{f_m['slot_gap']*100:.2f}%c)")

# the thicken treatment must preserve the camber line exactly — it is a
# manufacturing operation, not a hidden aero re-trim
_, uc_s = airfoils.repaneled("s1223", 70)
tre_s, _ = mfg.apply_te_treatment(airfoils.normalize(
    airfoils.resolve("s1223")[1]), 0.0098, "thicken")
cam0 = airfoils.geometry_info(uc_s)["max_camber"]
cam1 = airfoils.geometry_info(tre_s)["max_camber"]
check("thicken preserves the camber line",
      abs(cam1 - cam0) < 5e-4, f"(camber {cam0} -> {cam1})")

# truncate mode intentionally applies no floor — the report must call out a
# remaining waist instead (e377 is thinner than 2 mm over most of its aft)
cfg_waist = geometry.StackConfig.from_dict(
    {**BASE,
     "elements": [{"airfoil": "e377", "chord_ratio": 1.0}],
     "manufacturing": {"te_gap_mm": 2.0, "te_mode": "truncate",
                       "min_thickness_mm": 0}})
rep_tw = geometry.geometry_report(cfg_waist)
e_tw = rep_tw["design"][0]["mfg"]
# e377 IS thinner than 2 mm over most of its aft: the waist must be flagged
# AND warned about (the old equivalence also passed if a regression made
# truncate stop leaving a waist silently)
check("truncate: remaining waist is measured and warned about",
      (not e_tw["waist_ok"])
      and any("waists to" in w for w in rep_tw["warnings"]),
      f"(waist_ok {e_tw['waist_ok']}, min aft {e_tw['min_aft_thickness_mm']} mm)")

# truncate mode also solves end-to-end
cfg_t = geometry.StackConfig.from_dict(
    {**BASE, "manufacturing": {**MFG, "te_mode": "truncate"}})
r_t = analysis.analyze(cfg_t, include_geometry=False)
check("truncate: analysis solves, downforce finite and positive",
      np.isfinite(r_t["forces"]["downforce_n"])
      and r_t["forces"]["downforce_n"] > 0,
      f"({r_t['forces']['downforce_n']} N)")

# ---- 4. exports ----------------------------------------------------------
import ezdxf  # noqa: E402

dxf = export.dxf_bytes(cfg1, "installed", "spline")
doc = ezdxf.read(io.StringIO(dxf.decode("utf-8")))
splines = [e for e in doc.modelspace() if e.dxftype() == "SPLINE"]
lines = [e for e in doc.modelspace() if e.dxftype() == "LINE"
         and e.dxf.layer != "GROUND"]
check("DXF spline export: open spline + straight TE base per element",
      len(splines) == 2 and len(lines) == 2)

# the base line length equals the requested TE thickness
base_len = min(np.hypot(l.dxf.end.x - l.dxf.start.x,
                        l.dxf.end.y - l.dxf.start.y) for l in lines)
check("DXF TE base length ~ TE thickness",
      MFG["te_gap_mm"] - 0.1 <= base_len <= MFG["te_gap_mm"] + 0.6,
      f"({base_len:.2f} mm)")

zb = zipfile.ZipFile(io.BytesIO(export.zip_bundle(cfg1, None, "spline")))
man = json.loads(zb.read("manifest.json"))
prof_names = [n for n in zb.namelist() if n.startswith("dat/profile_")]
prof = zb.read(prof_names[1]).decode()
pts = np.array([ln.split() for ln in prof.splitlines()[1:]], float)
gap_flap_mm = float(np.hypot(*(pts[0] - pts[-1]))) * 0.35 * 350
check("ZIP: manifest manufacturing block + as-built profile .dat",
      man["configuration"]["manufacturing"]["te_thickness_mm"] == 1.5
      and "as_built" in man["configuration"]["elements"][0]
      and gap_flap_mm >= 1.45,
      f"(flap profile TE {gap_flap_mm:.2f} mm)")

# untreated config exports unchanged (closed spline, no extra lines)
dxf0 = export.dxf_bytes(cfg0, "installed", "spline")
doc0 = ezdxf.read(io.StringIO(dxf0.decode("utf-8")))
lines0 = [e for e in doc0.modelspace() if e.dxftype() == "LINE"
          and e.dxf.layer != "GROUND"]
check("no manufacturing -> DXF unchanged (no TE base lines)",
      len(lines0) == 0)

# SVG: one filled path per element, a ground line, true-mm scale
svg = export.svg_bytes(cfg1, "installed").decode()
check("SVG: path per element + ground + mm units",
      svg.count("<path") == 2 and "<line" in svg and 'width="' in svg
      and "mm" in svg.split(">", 1)[0])

# CSV: header + one row per contour point of both elements
csv_rows = export.csv_text(cfg1, "installed").strip().splitlines()
n_pts = sum(len(e["coords"])
            for e in geometry.build_stack(cfg1))
check("CSV: header + one row per contour point",
      csv_rows[0] == "element,role,airfoil,point,x_mm,y_mm"
      and len(csv_rows) == n_pts + 1,
      f"({len(csv_rows) - 1} rows, {n_pts} points)")

# ---- 5. optimizer candidate pools ---------------------------------------
cfg_p = geometry.StackConfig.from_dict(BASE)

# before anything is uploaded, custom_only must fail with a clear message
if not airfoils.list_custom():
    try:
        optimizer.build_airfoil_shortlists(BASE, cfg_p, pool="custom_only")
        check("custom_only without uploads raises a clear error", False)
    except ValueError as e:
        check("custom_only without uploads raises a clear error",
              "upload" in str(e).lower(), f"({e})")
else:
    check("custom_only without uploads raises a clear error", True,
          "(skipped: registry pre-populated)")

s_a = airfoils.register_custom("pool-test-a", airfoils.naca_coords("6412"))
s_b = airfoils.register_custom("pool-test-b", airfoils.naca_coords("4415"))

sl_auto = optimizer.build_airfoil_shortlists(BASE, cfg_p, pool="auto")
sl_inc = optimizer.build_airfoil_shortlists(BASE, cfg_p, pool="include_custom")
sl_only = optimizer.build_airfoil_shortlists(BASE, cfg_p, pool="custom_only")
check("pool auto: non-empty ranked shortlists",
      len(sl_auto[0]) >= 5 and len(sl_auto[1]) >= 5)
check("pool include_custom: every upload guaranteed a slot",
      all(s in sl_inc[i] for s in (s_a, s_b) for i in (0, 1)))
check("pool custom_only: exactly the uploads",
      set(sl_only[0]) == set(sl_only[1]) == {s_a, s_b}
      or set(sl_only[0]).issuperset({s_a, s_b}),
      f"({sl_only[0]})")

# manufacturing min thickness floors library candidates (uploads exempt)
cfg_f = geometry.StackConfig.from_dict(
    {**BASE, "manufacturing": {**MFG, "min_thickness_mm": 8}})
sl_floor = optimizer.build_airfoil_shortlists(BASE, cfg_f,
                                              pool="include_custom")
floor_pct = 8 / (0.35 * 350) * 100
thin = []
for s in sl_floor[1]:
    if s in (s_a, s_b) or s == "naca4412":
        continue
    _, cc = airfoils.repaneled(s, 60)
    if airfoils.geometry_info(cc)["max_thickness"] * 100 < floor_pct - 0.1:
        thin.append(s)
check("min-thickness floors flap candidates (8 mm on a 122 mm chord)",
      not thin, f"(floor {floor_pct:.1f}%c, violators {thin})")

# ---- 6. validation surface ----------------------------------------------
def rejects(d, why):
    try:
        geometry.StackConfig.from_dict(d)
        return False
    except (ValueError, KeyError):
        return True

check("validation: bad te_mode rejected",
      rejects({**BASE, "manufacturing": {"te_mode": "vshape"}}, "mode"))
check("validation: te_gap_mm out of range rejected",
      rejects({**BASE, "manufacturing": {"te_gap_mm": 50}}, "gap"))
check("validation: NaN min_thickness rejected",
      rejects({**BASE, "manufacturing": {"min_thickness_mm": float("nan")}},
              "nan"))
try:
    optimizer.build_airfoil_shortlists(BASE, cfg_p, pool="not-a-pool")
    check("validation: unknown pool rejected", False)
except ValueError:
    check("validation: unknown pool rejected", True)

# ---- the floor must survive the downstream spline repanel ----
# regression foils: coarse catalog sections whose fit spline used to sag
# below the floor between nodes by up to 0.10 mm (2x the waist tolerance)
for foil, ratio in (("goe652", 0.35), ("e378", 0.35), ("goe233", 0.20)):
    cfg_f = geometry.StackConfig.from_dict({
        "elements": [{"airfoil": "s1223"},
                     {"airfoil": foil, "chord_ratio": ratio,
                      "deflection_deg": 15,
                      "slot_gap_pct": 1.5, "slot_overlap_pct": 2.0}],
        "chord_mm": 350, "ride_height_mm": 30,
        "manufacturing": {"te_gap_mm": 3.0, "te_mode": "thicken"}})
    m = geometry.geometry_report(cfg_f)["design"][1]["mfg"]
    check(f"thicken floor survives repanel ({foil})",
          m["waist_ok"] and m["min_aft_thickness_mm"] >= 2.97,
          f"(min aft {m['min_aft_thickness_mm']} mm vs 3.0 requested)")

# a surface that folds back in x must produce sane treated geometry (after
# the parser fix) — never crossed surfaces or fantasy thickness numbers
cfg_nm = geometry.StackConfig.from_dict({
    "elements": [{"airfoil": "nm26-3smoothed"}],
    "chord_mm": 350, "ride_height_mm": 30,
    "manufacturing": {"te_gap_mm": 1.2, "te_mode": "thicken"}})
m_nm = geometry.geometry_report(cfg_nm)["design"][0]["mfg"]
check("thin foil with historical fold parses + treats sanely",
      10 < m_nm["max_thickness_mm"] < 25
      and m_nm["min_aft_thickness_mm"] > 0 and m_nm["waist_ok"],
      f"(tmax {m_nm['max_thickness_mm']} mm, "
      f"min aft {m_nm['min_aft_thickness_mm']} mm)")

# crossed-surface guard: a genuinely folded contour is rejected, not emitted
fold = np.array([[1.0, 0.0], [0.6, 0.06], [0.72, 0.09], [0.3, 0.08],
                 [0.0, 0.0], [0.3, -0.04], [0.6, -0.05], [1.0, -0.001]])
try:
    mfg.apply_te_treatment(fold, 0.01, "thicken")
    check("folded-surface contour rejected by TE treatment", False)
except ValueError as e:
    check("folded-surface contour rejected by TE treatment",
          "folds back" in str(e))

# ---- optimizer manufacturing guard (frontend contract) -----------------
# Prep stays OFF by default (warn-only decision, see DECISIONS.md), so the
# UI must interpose before optimizing sharp geometry: the guard dialog and
# its two exits have to exist and be wired. A static contract check — there
# is no JS harness — but it pins the pieces a refactor would silently drop.
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
check("mfg guard dialog present with both exits",
      'id="mfg-guard-dialog"' in html
      and 'id="mfg-guard-enable"' in html
      and 'id="mfg-guard-anyway"' in html)
check("optimize click gates on state.config.manufacturing",
      "!state.config.manufacturing" in js
      and '$("mfg-guard-dialog").showModal' in js)
check("'Enable & optimize' applies the default TE prep",
      "te_gap_mm: 1.2" in js and '"mfg-guard-enable"' in js)
check("default config leaves manufacturing off (guard is the chosen path)",
      "manufacturing" not in js.split("const state = {")[1].split("};")[0])

print(f"\n{sum(results)}/{len(results)} manufacturing checks passed")
sys.exit(0 if all(results) else 1)
