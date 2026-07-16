"""Shape-refinement regression (no server needed).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_shaping.py

Covers the shape: derived-spec plumbing, the geometry of the modification,
constraint interplay with manufacturing prep, and the optimizer integration.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import (airfoils, geometry, manufacturing as mfg,  # noqa: E402
                      optimizer, shaping)

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# ---- 1. spec plumbing ----------------------------------------------------
sp = shaping.derived_spec("s1223", [0.004, -0.002, 0.009], 1.10)
b, ts, base = shaping.parse(sp)
check("spec round-trip", b == [0.004, -0.002, 0.009] and ts == 1.10
      and base == "s1223", f"({sp})")

check("identity modification collapses to the base spec",
      shaping.derived_spec("s1223", [1e-5, 0, 0], 1.001) == "s1223")

check("base specs containing ':' survive parsing",
      shaping.parse(shaping.derived_spec("custom:my-foil", [0.001, 0, 0],
                                         1.0))[2] == "custom:my-foil")

for bad in ("shape:9:0:0:1.0:s1223",          # bump out of range
            "shape:+0.001:+0.001:+0.001:5.0:s1223",   # ts out of range
            "shape:+0.001:0:0:1.0:shape:+0.001:0:0:1.0:s1223"):  # nested
    try:
        shaping.parse(bad)
        check(f"hostile spec rejected ({bad[:28]}…)", False)
    except ValueError:
        check(f"hostile spec rejected ({bad[:28]}…)", True)

# ---- 2. the modification itself ------------------------------------------
uc = airfoils.normalize(airfoils.resolve("s1223")[1])
info0 = airfoils.geometry_info(uc)
_, shaped = airfoils.resolve(sp)
info1 = airfoils.geometry_info(shaped)
check("thickness scales by the requested factor",
      abs(info1["max_thickness"] - info0["max_thickness"] * 1.10) < 0.004,
      f"({info0['max_thickness']} -> {info1['max_thickness']})")
check("aft camber bump raises camber",
      info1["max_camber"] > info0["max_camber"])
le0 = uc[int(np.argmin(uc[:, 0]))]
le1 = shaped[int(np.argmin(shaped[:, 0]))]
check("LE and TE x-positions preserved",
      abs(le1[0] - le0[0]) < 1e-9
      and abs(shaped[:, 0].max() - uc[:, 0].max()) < 1e-9)

# ---- 3. constraint interplay ----------------------------------------------
wrapped = mfg.derived_spec(sp, 0.0035, "thicken")
name_w, cw = airfoils.repaneled(wrapped, 70)
check("manufacturing prep wraps a shaped section",
      "(shaped)" in name_w and abs(mfg.te_gap(cw) - 0.0035) < 1e-6,
      f"({name_w})")

cfg_s = geometry.StackConfig.from_dict({
    "elements": [
        {"airfoil": sp, "chord_ratio": 1.0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 20,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "chord_mm": 350, "ride_height_mm": 30,
    "manufacturing": {"te_gap_mm": 1.5, "te_mode": "thicken"}})
rep_s = geometry.geometry_report(cfg_s)
check("geometry report runs on a shaped + treated stack",
      rep_s["design"][0]["mfg"]["waist_ok"]
      and "shaped" in rep_s["design"][0]["airfoil_name"])

# min-thickness floors the thickness-scale search bound
cfg_d = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}
vars_free = optimizer.build_variables(cfg_d, {"opt_shape": True})
vars_mfg = optimizer.build_variables(
    {**cfg_d, "manufacturing": {"te_gap_mm": 1.5, "te_mode": "thicken",
                                "min_thickness_mm": 13}},
    {"opt_shape": True})
ts_free = [v for v in vars_free if v["key"] == "shape_ts"]
ts_mfg = [v for v in vars_mfg if v["key"] == "shape_ts"]
# flap: 13 mm minimum on a 122.5 mm chord, s1223 tmax 12.14% -> 14.9 mm;
# the scale may not thin it below 13/14.9 = 0.874, above the default 0.85
check("min buildable thickness floors the thickness-scale bound",
      ts_mfg[1]["lo"] > ts_free[1]["lo"] + 0.01
      and abs(ts_mfg[1]["lo"] - 13.0 / 14.87) < 0.02,
      f"(flap ts_lo {ts_free[1]['lo']} -> {round(ts_mfg[1]['lo'], 3)})")

# ---- 4. optimizer integration ---------------------------------------------
job = optimizer.Job(cfg_d, {"target_downforce_n": 250, "budget": 300,
                            "mode": "global", "opt_shape": True})
job.run()
snap = job.snapshot()
shape_keys = [v["key"] for v in snap["variables"]]
check("optimizer runs with shape variables",
      snap["state"] == "done"
      and all(k in shape_keys for k in optimizer.SHAPE_KEYS),
      f"({snap['state']}, {snap['n_eval']} evals)")
bc = snap["best_config"]
check("best config carries applicable (possibly shaped) airfoils",
      all(isinstance(e["airfoil"], str) and
          geometry.StackConfig.from_dict(
              {**cfg_d, "elements": [dict(x) for x in bc["elements"]]})
          for e in bc["elements"]))

# re-optimizing a shaped design seeds from (and does not nest) the shape
cfg_re = {**cfg_d, "elements": [
    {**cfg_d["elements"][0], "airfoil": sp}, cfg_d["elements"][1]]}
job2 = optimizer.Job(cfg_re, {"target_downforce_n": 250, "budget": 300,
                              "mode": "global", "opt_shape": True})
x0 = optimizer.initial_vector(job2.config, job2.variables, {})
i_b80 = [i for i, v in enumerate(job2.variables)
         if v["key"] == "shape_b80" and v["elem"] == 0][0]
check("re-optimizing a shaped design seeds from its parameters",
      job2.config["elements"][0]["airfoil"] == "s1223"
      and abs(x0[i_b80] - 0.009) < 1e-9,
      f"(x0[b80] = {x0[i_b80]})")

print(f"\n{sum(results)}/{len(results)} shaping checks passed")
sys.exit(0 if all(results) else 1)
