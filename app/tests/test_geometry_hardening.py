"""Geometry hardening regression suite (no server needed).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_geometry_hardening.py

Covers polygon intersection edge-crossings, repanel cache tokens for chained
derived specs, and non-finite config field rejection.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import airfoils, geometry  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# ---- polygons_intersect ---------------------------------------------------
# two thin bars crossing at the origin: a genuine 1x1 overlap in which
# neither polygon contains any vertex of the other
hbar = np.array([[-5.0, -0.5], [5.0, -0.5], [5.0, 0.5], [-5.0, 0.5]])
vbar = np.array([[-0.5, -5.0], [0.5, -5.0], [0.5, 5.0], [-0.5, 5.0]])
check("edge-crossing overlap detected (no vertex containment)",
      geometry.polygons_intersect(hbar, vbar))
check("edge-crossing overlap detected (argument order flipped)",
      geometry.polygons_intersect(vbar, hbar))

check("disjoint polygons do not intersect",
      not geometry.polygons_intersect(hbar, hbar + np.array([0.0, 3.0])))

inner = np.array([[-0.2, -0.2], [0.2, -0.2], [0.2, 0.2], [-0.2, 0.2]])
check("contained polygon detected (vertex path)",
      geometry.polygons_intersect(hbar, inner))

near = hbar + np.array([0.0, 1.01])   # 0.01 clear of the top edge
check("nearby but clear polygons do not intersect",
      not geometry.polygons_intersect(hbar, near))

# ---- spec_cache_token unwraps chained specs --------------------------------
tmp = Path(tempfile.mkdtemp(prefix="wss_test_tok_"))
dat = tmp / "tok_test.dat"
dat.write_text("tok\n1.0 0.0\n0.5 0.1\n0.0 0.0\n0.5 -0.1\n1.0 0.0\n",
               encoding="utf-8")
mt = dat.stat().st_mtime_ns

specs = {
    "bare .dat": str(dat),
    "mfg wrapper": f"mfg:thicken:0.003:{dat}",
    "shape wrapper": f"shape:+0.001:0:0:1.0:{dat}",
    "mfg outer, shape inner": f"mfg:thicken:0.003:shape:+0.001:0:0:1.0:{dat}",
    "shape outer, mfg inner": f"shape:+0.001:0:0:1.0:mfg:thicken:0.003:{dat}",
}
for lbl, sp in specs.items():
    tok = airfoils.spec_cache_token(sp)
    check(f"cache token stats the base file ({lbl})", tok == mt,
          f"(got {tok}, want {mt})")

os.utime(dat, ns=(mt + 2_000_000_000, mt + 2_000_000_000))
tok2 = airfoils.spec_cache_token(
    f"shape:+0.001:0:0:1.0:mfg:thicken:0.003:{dat}")
check("cache token follows the .dat mtime under the shape-outer chain",
      tok2 == dat.stat().st_mtime_ns and tok2 != mt,
      f"(got {tok2}, was {mt})")

check("non-filesystem specs stay immutable-per-string (token 0)",
      airfoils.spec_cache_token("mfg:thicken:0.003:s1223") == 0
      and airfoils.spec_cache_token("s1223") == 0)

shutil.rmtree(tmp, ignore_errors=True)

# ---- non-finite n_panels_per_side ------------------------------------------
BASE_CFG = {"elements": [{"airfoil": "naca0012"}], "chord_mm": 350,
            "ride_height_mm": 30}


def npanels_rejected(v):
    try:
        geometry.StackConfig.from_dict({**BASE_CFG, "n_panels_per_side": v})
    except ValueError:
        return True
    except Exception:
        return False   # OverflowError etc. would 500 at the API boundary
    return False


for lbl, v in (("inf (JSON 1e999)", json.loads("1e999")),
               ("-inf", json.loads("-1e999")),
               ("NaN", float("nan"))):
    check(f"n_panels_per_side {lbl} -> ValueError", npanels_rejected(v))

cfg_ok = geometry.StackConfig.from_dict({**BASE_CFG, "n_panels_per_side": 64})
check("integer n_panels_per_side still accepted",
      cfg_ok.n_panels_per_side == 64)
cfg_str = geometry.StackConfig.from_dict(
    {**BASE_CFG, "n_panels_per_side": "40"})
check("numeric-string n_panels_per_side still accepted",
      cfg_str.n_panels_per_side == 40)

# ---- validator floor matches the panel solver's minimum --------------------
# repanel(n_points_per_side=N) yields 2N-1 contour points and the panel
# solver refuses anything under 20, so the lowest accepted count must solve
check("n_panels_per_side 10 (19 points — unsolvable) is rejected",
      npanels_rejected(10))
cfg_floor = geometry.StackConfig.from_dict(
    {**BASE_CFG, "n_panels_per_side": 11})
_, pts_floor = airfoils.repaneled("naca0012", cfg_floor.n_panels_per_side)
check("floor value yields enough points for the panel solver",
      len(pts_floor) >= 20, f"({len(pts_floor)} points)")

# ---- mfg: TE bound applies anywhere in the spec chain -----------------------
# the optimizer's opt_shape re-wrap yields "shape:...:mfg:...:base"; a
# wrapped 12%c gap opens the TE to 42 mm exactly like a bare one and must
# be refused by the same physical bound
_WRAP = "shape:+0.0005:+0.0000:+0.0000:1.000:mfg:thicken:{gap}:s1223"


def stack_rejected(spec):
    try:
        geometry.StackConfig.from_dict(
            {**BASE_CFG, "elements": [{"airfoil": spec}]})
    except ValueError:
        return True
    return False


check("bare mfg: 12%c gap is rejected (42 mm TE on a 350 mm chord)",
      stack_rejected("mfg:thicken:0.12:s1223"))
check("shape-wrapped mfg: 12%c gap is rejected the same way",
      stack_rejected(_WRAP.format(gap="0.12")))
check("shape-wrapped mfg: with a buildable gap still validates",
      not stack_rejected(_WRAP.format(gap="0.003")))

print(f"\n{sum(results)}/{len(results)} geometry hardening checks passed")
sys.exit(0 if all(results) else 1)
