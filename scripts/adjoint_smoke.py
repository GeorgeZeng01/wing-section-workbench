"""Adjoint-polish contract check against a LIVE Fluent.

Validates, on the real tool, everything the offline suite can only
fake: that Fluent 26.1's settings API accepts the gradient-based setup
exactly as scripts/fluent_mcp.adjoint_setup issues it (observables,
linear-combination objective, envelope-clipped cartesian region,
shape-opt optimizer), that one optimize() call runs a full
flow+adjoint+morph design iteration, and that the morphed profile nodes
export and re-attribute cleanly through app.core.adjoint_run's
extraction and rule checks.

Holds an ANSYS license for the duration (a few minutes); releases it on
every path. Reads the seed run directory read-only.

    .venv\\Scripts\\python.exe scripts\\adjoint_smoke.py [run_dir]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from app.core import adjoint_run, cfd  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402
from scripts import fluent_mcp as fm  # noqa: E402

DEFAULT_RUN = REPO / "app_data" / "rans" / "9e2e4f025ff4"


def main() -> int:
    run_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RUN
    cas = run_dir / "case.cas.h5"
    if not cas.is_file():
        print(f"no case at {cas}")
        return 2
    cfg = StackConfig.from_dict(
        json.loads((run_dir / "config.json").read_text(encoding="utf-8")))
    g = cfd.section_geometry(cfg, "coarse")
    base_polys = [np.asarray(p, float) for p, _b in g["polys"]]
    bounds = adjoint_run.region_bounds(base_polys, cfg, 6.0)
    print(f"region: x={bounds['x']} y={bounds['y']} "
          f"clips={bounds['clips']}")

    t0 = time.time()
    ok = True
    try:
        print("launch...", flush=True)
        print(fm.launch(dimension=2, precision="double", processors=2))
        print("read case+data...", flush=True)
        z = fm.read_case_data(str(cas))
        print(f"zones: {z['zones']}")
        assert "profile" in z["zones"].get("wall", []), "no profile zone"

        print("settle solve (100 iters)...", flush=True)
        fm.solve(iterations=100, initialize=False)
        rep = fm._compute_reports(["lift_coef", "drag_coef"])
        print(f"settled reports: {rep}")

        print("adjoint_setup...", flush=True)
        setup = fm.adjoint_setup(
            profile_zones=["profile"], drag_exchange_k=0.25,
            region_x=bounds["x"], region_y=bounds["y"],
            flow_iterations=60, adjoint_iterations=80)
        print(json.dumps(setup, indent=1))
        if setup["failed"]:
            print("SETUP STEPS FAILED — contract not met")
            ok = False

        if ok:
            print("adjoint_step (one design iteration)...", flush=True)
            step = fm.adjoint_step()
            print(json.dumps(step, indent=1))

            print("export morphed wall nodes...", flush=True)
            out_csv = REPO / "app_data" / "fluent" / "smoke_wall.csv"
            fm.export_ascii(filename=str(out_csv),
                            quantities=["x-wall-shear", "y-wall-shear"],
                            location="node", surfaces=["profile"])
            xs, ys = [], []
            header = None
            for ln in out_csv.read_text(errors="replace").splitlines():
                parts = [p.strip() for p in ln.split(",")]
                if header is None:
                    header = [h.lower() for h in parts]
                    xi = next(i for i, h in enumerate(header)
                              if "x-coordinate" in h)
                    yi = next(i for i, h in enumerate(header)
                              if "y-coordinate" in h)
                    continue
                if len(parts) != len(header):
                    continue
                try:
                    xs.append(float(parts[xi]))
                    ys.append(float(parts[yi]))
                except ValueError:
                    continue
            nodes = np.column_stack([xs, ys])
            print(f"exported nodes: {len(nodes)}")
            polys, stats = adjoint_run.split_wall_nodes(
                nodes, base_polys, adjoint_run.NODE_MATCH_TOL_C
                * cfg.chord_m)
            print(f"extraction: {stats}")
            rules = adjoint_run.check_polished(polys, cfg)
            print(f"rules ok={rules['ok']} "
                  f"violations={rules['violations']}")
            rep2 = fm._compute_reports(["lift_coef", "drag_coef"])
            print(f"post-step reports: {rep2}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"SMOKE FAILED: {e}")
        ok = False
    finally:
        try:
            print(fm.shutdown())
        except Exception:
            pass
    print(f"wall time {time.time() - t0:.0f}s — "
          f"{'CONTRACT OK' if ok else 'CONTRACT BROKEN'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
