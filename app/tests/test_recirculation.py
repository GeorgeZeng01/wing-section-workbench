"""Reversed-flow magnitude and structure from the solved field.

Pins foam_post.recirculation_report: the sensor that finally gives the ANSYS
Fluent 2D engine a separation measurement (it exports no wall shear, so
wall_report is hard-None there), and that adds to the OpenFOAM engine the
structure a single reversed-face scalar throws away.

THE CONTRACT THIS SUITE EXISTS TO PROTECT — closure and severity are
orthogonal, and neither may be derived from the other. Measured on the
retained record and pinned below: the collapsed element reads 39% reversed
stations and DOES close (an ~80%-of-arc bubble that reattaches ahead of the
trailing edge), while a healthy element reads 2% reversed and does NOT
cleanly close (its thin reversal runs onto the trailing edge). Any future
change that lets a closure boolean carry severity meaning breaks here.

Also pinned: nothing is graded (verdict is always None), a zero reading is
reported as a measured zero with its own detection floor and never as the
word "attached", and the only None return is a case with no readable field.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_recirculation.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import foam_post  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def notice(label):
    print(f"NOTE  {label}")


CFG_D = {
    "stack_aoa_deg": -3.88, "ride_height_mm": 40, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 20, "rho": 1.225, "nu": 1.5e-05,
    "ncrit": 7, "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
    "span_efficiency": 0.9, "n_panels_per_side": 70,
    "manufacturing": {"te_gap_mm": 2, "te_mode": "thicken",
                      "min_thickness_mm": 2},
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.28, "deflection_deg": 8.9,
         "slot_gap_pct": 1.3, "slot_overlap_pct": 1.2},
        {"airfoil": "s1223", "chord_ratio": 0.2, "deflection_deg": 27,
         "slot_gap_pct": 1.3, "slot_overlap_pct": 1.3},
    ],
}
CFG = StackConfig.from_dict(CFG_D)


def write_case(root, u_fn, n=60, write_cfg=True, cfg_d=None):
    """A synthetic solved case foam_post can read: scattered cell centres
    spanning the installed section, velocity from u_fn(x, y) -> (u, v)."""
    tdir = root / "300"
    tdir.mkdir(parents=True)
    gx, gy = np.meshgrid(np.linspace(-0.25, 0.75, n),
                         np.linspace(0.0, 0.40, n))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    uv = np.array([u_fn(x, y) for x, y in pts], float)
    (tdir / "C").write_text(
        "internalField nonuniform List<vector> %d(%s);\n"
        % (len(pts), " ".join(f"({x:.5f} {y:.5f} 0)" for x, y in pts)))
    (tdir / "U").write_text(
        "internalField nonuniform List<vector> %d(%s);\n"
        % (len(uv), " ".join(f"({u:.5f} {v:.5f} 0)" for u, v in uv)))
    (tdir / "p").write_text(
        "internalField nonuniform List<scalar> %d(%s);\n"
        % (len(pts), " ".join("0" for _ in pts)))
    if write_cfg:
        (root / "config.json").write_text(
            json.dumps(cfg_d if cfg_d is not None else CFG_D))
    return root


def main() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="wss-recirc-"))
    try:
        # ---- the only None path: no readable field --------------------
        empty = tmp / "empty"
        (empty / "notatime").mkdir(parents=True)
        check("a case with no time dir carrying U and C returns None",
              foam_post.recirculation_report(empty, CFG) is None)

        # ---- uniform freestream: a measured zero, never 'attached' ----
        free = write_case(tmp / "free", lambda x, y: (20.0, 0.0))
        r = foam_post.recirculation_report(free, CFG, converged=True)
        check("uniform freestream is measured, not unknown",
              r and r["status"] == "measured", f"({r and r['status']})")
        check("uniform freestream finds no reversed cell and no region",
              r["n_reversed_cells"] == 0 and r["regions"] == []
              and r["n_regions"] == 0)
        e0 = r["elements"][0]
        check("a zero reading is a measured zero carrying its own floor",
              e0["near_wall_reversed_frac"] == 0.0
              and e0["below_detection_floor"] is True
              and e0["detection_floor_frac"] > 0.0
              and "below this sensor" in (e0["reversed_frac_note"] or ""),
              f"({e0['detection_floor_frac']})")
        blob = json.dumps(r).lower()
        check("the payload never calls anything 'attached' as a grade",
              '"attached"' not in blob and "'attached'" not in blob)
        check("nothing is graded in v1",
              r["verdict"] is None
              and all(e["field_grade"] is None for e in r["elements"]))
        check("the resolution note always rides with the numbers",
              "below this sensor" in r["resolution_note"]
              and "shape statement" in r["resolution_note"]
              and len(r["caveats"]) >= 5)

        # ---- bound semantics ------------------------------------------
        check("a converged, un-stopped run is not a bound",
              r["bound"] is False and r["bound_reason"] is None)
        rb = foam_post.recirculation_report(free, CFG, converged=False)
        check("an unconverged run is a bound, and says why",
              rb["bound"] is True and "bound" in (rb["bound_reason"] or ""))
        rs = foam_post.recirculation_report(free, CFG, converged=True,
                                            user_stopped=True)
        check("a hand-stopped run is a bound even when converged reads True",
              rs["bound"] is True and "hand" in (rs["bound_reason"] or ""))
        ru = foam_post.recirculation_report(free, CFG)
        check("convergence not supplied defaults to a bound",
              ru["bound"] is True)

        # ---- reversed flow is found, and it is structure --------------
        def bubble(x, y):
            # a lens of reversed flow riding over the aft upper section
            if 0.10 < x < 0.30 and 0.16 < y < 0.24:
                return (-6.0, 0.0)
            return (20.0, 0.0)

        rb2 = foam_post.recirculation_report(
            write_case(tmp / "bub", bubble), CFG, converged=True)
        check("a planted reversed lens is detected",
              rb2["n_reversed_cells"] > 0 and rb2["n_regions"] >= 1,
              f"({rb2['n_reversed_cells']} cells, "
              f"{rb2['n_regions']} regions)")
        check("region extents are reported in metres and chords",
              all(g["x_start_m"] is not None and g["length_c"] is not None
                  and g["area_c2"] is not None for g in rb2["regions"]))
        check("regions carry a kind and a reattachment basis",
              all(g["kind"] in ("surface", "wake")
                  and g["reattach_basis"] in (
                      "no_wall_contact", "no_listed_run",
                      "reattaches_before_te", "reversed_at_trailing_edge",
                      "unmeasured_or_knife_edge") for g in rb2["regions"]))
        check("the unfiltered scalar is not gated by the region list floor",
              rb2["reversed_area_frac"] > 0.0)

        # ---- engine without wall shear --------------------------------
        check("a case with no wallShearStress reports the absence, and "
              "still measures",
              rb2["engine_has_wall_shear"] is False
              and rb2["wall_agreement"] is None
              and all(e["wall_reversed_frac"] is None
                      for e in rb2["elements"])
              and all(e["agrees"] is None for e in rb2["elements"])
              and rb2["status"] == "measured")

        # ---- geometry provenance --------------------------------------
        alt = json.loads(json.dumps(CFG_D))
        alt["elements"][2]["deflection_deg"] = 12.0
        rm = foam_post.recirculation_report(
            write_case(tmp / "mismatch", lambda x, y: (20.0, 0.0),
                       cfg_d=alt), CFG, converged=True)
        check("a config describing a different wing is unknown, not None "
              "and not a silent measurement",
              rm is not None and rm["status"] == "unknown"
              and rm["geometry_verified"] is False
              and rm["regions"] is None, f"({rm and rm['status']})")
        pan = json.loads(json.dumps(CFG_D))
        pan["n_panels_per_side"] = 120
        rp = foam_post.recirculation_report(
            write_case(tmp / "paneling", lambda x, y: (20.0, 0.0),
                       cfg_d=pan), CFG, converged=True)
        check("a different paneling density is the SAME wing, not a mismatch",
              rp["status"] == "measured" and rp["geometry_verified"] is True,
              f"({rp['status']}, {rp['geometry_verified']})")
        rn = foam_post.recirculation_report(
            write_case(tmp / "nocfg", lambda x, y: (20.0, 0.0),
                       write_cfg=False), CFG, converged=True)
        check("no config beside the case is a declared hole, not a mismatch",
              rn["status"] == "measured" and rn["geometry_verified"] is None)

        # ---- determinism ----------------------------------------------
        a = foam_post.recirculation_report(free, CFG, converged=True)
        b = foam_post.recirculation_report(free, CFG, converged=True)
        check("two runs on the same case serialise byte-identically",
              json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True))
        check("the grid is a pure function of the config, not of a default",
              a["grid"]["nx"] == rb["grid"]["nx"])

        # ---- ORDERING: the field must exist before anything reads it --
        # This shipped broken. On the Fluent paths _finalize assembles the
        # result BEFORE _export_flow_fields writes C/U/p, so a recirculation
        # report taken at result assembly found no time directory and
        # returned None — silently, every run, on the very engine the report
        # exists to serve. A converged verification solve is what caught it.
        f2 = (ROOT / "app" / "core" / "fluent2d_run.py").read_text(
            encoding="utf-8")
        i_exp = f2.find("self._export_flow_fields(fm")
        i_rec = f2.find("foam_post.recirculation_report(")
        i_harv = f2.find("cfd_run.append_harvest(")
        check("fluent2d reads the recirculation field AFTER exporting it",
              i_exp > 0 and i_rec > i_exp, f"(export@{i_exp}, read@{i_rec})")
        check("fluent2d harvests AFTER the export, so the row carries the "
              "field measurement instead of a None",
              i_harv > i_exp, f"(export@{i_exp}, harvest@{i_harv})")
        fr = (ROOT / "app" / "core" / "fluent_run.py").read_text(
            encoding="utf-8")
        i_exp3 = fr.find("self._export_flow_fields(fm")
        i_harv3 = fr.find("cfd_run.append_harvest(")
        check("the 3D Fluent path harvests after its export too",
              i_exp3 > 0 and i_harv3 > i_exp3,
              f"(export@{i_exp3}, harvest@{i_harv3})")

        # ---- the retained record: the orthogonality pin ---------------
        # resolution independence is checked HERE, not on the synthetic
        # case: the planted lens sits off the surface, so the probe reads
        # zero at every cell size and comparing 0.0 to 0.0 proves nothing.
        # It has to be exercised where there is real near-wall signal.
        gold = ROOT / "app_data" / "rans" / "09c6de53265a"
        if not (gold / "config.json").is_file():
            notice("retained case 09c6de53265a absent — the closure/severity "
                   "orthogonality pin did NOT run in this environment")
        else:
            gcfg = StackConfig.from_dict(
                json.loads((gold / "config.json").read_text()))
            gr = foam_post.recirculation_report(gold, gcfg, converged=False)
            fr = {e["patch"]: e["near_wall_reversed_frac"]
                  for e in gr["elements"]}
            check("retained collapse case: e2 reads heavily reversed, "
                  "e1 and e3 do not",
                  abs(fr["wing_e2"] - 0.39) < 0.02
                  and abs(fr["wing_e1"] - 0.02) < 0.02
                  and abs(fr["wing_e3"] - 0.00) < 0.02, f"({fr})")
            lad = gr["elements"][1]["near_wall_reversed_frac_ladder"]
            check("the probe ladder decays monotonically away from the wall",
                  lad["1"] >= lad["2"] >= lad["3"] >= lad["4"], f"({lad})")
            e2_runs = gr["elements"][1]["sides"]["upper"]["runs"]
            e1_low = gr["elements"][0]["sides"]["lower"]["runs"]
            check("THE ORTHOGONALITY PIN: the 39%-reversed element CLOSES "
                  "(a large bubble that reattaches before the TE)",
                  len(e2_runs) == 1 and e2_runs[0]["closes"] is True
                  and e2_runs[0]["basis"] == "reattaches_before_te"
                  and e2_runs[0]["arc_frac"] > 0.7,
                  f"({[(r['closes'], r['arc_frac']) for r in e2_runs]})")
            check("...while the 2%-reversed element does NOT cleanly close "
                  "— so closure can never be read as severity",
                  len(e1_low) == 1 and e1_low[0]["closes"] is not True,
                  f"({[(r['closes'], r['basis']) for r in e1_low]})")
            check("the collapse sits in the wake-confluence corridor",
                  any(g["in_confluence"] and g["n_cells"] > 100
                      for g in gr["regions"]))
            check("grid geometry on the retained case is as measured",
                  abs(gr["grid"]["dx_mm"] - 1.75) < 0.05
                  and abs(gr["grid"]["first_finite_y_mm"] - 3.5) < 0.4,
                  f"({gr['grid']['dx_mm']} mm, "
                  f"{gr['grid']['first_finite_y_mm']} mm)")
            check("the ANSYS engine gets a measurement despite having no "
                  "wall shear at all",
                  gr["engine_has_wall_shear"] is False
                  and gr["status"] == "measured"
                  and gr["elements"][1]["near_wall_reversed_frac"] > 0.3)
            fine = foam_post.recirculation_report(gold, gcfg, converged=False,
                                                  cell_c=0.0035)
            coarse = foam_post.recirculation_report(gold, gcfg, converged=False,
                                                    cell_c=0.007)
            ff = [e["near_wall_reversed_frac"] for e in fine["elements"]]
            fc = [e["near_wall_reversed_frac"] for e in coarse["elements"]]
            check("halving and doubling the analysis cell changes precision, "
                  "not classification, where there IS near-wall signal",
                  fine["grid"]["nx"] != coarse["grid"]["nx"]
                  and all(x is not None and y is not None
                          and abs(x - y) < 0.06 for x, y in zip(ff, fc)),
                  f"(nx {fine['grid']['nx']}/{coarse['grid']['nx']}: "
                  f"{ff} vs {fc})")
            check("the collapse survives both cell sizes as the loud element",
                  ff[1] > 0.3 and fc[1] > 0.3, f"({ff[1]}, {fc[1]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{sum(results)}/{len(results)} recirculation checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
