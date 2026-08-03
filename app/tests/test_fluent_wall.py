"""The Fluent wall-shear channel (spiked and productionized 2026-08-03).

Pins foam_post.fluent_wall_report and its wiring: the accuracy-reference
engine now grades attachment from its OWN wall shear (per-node
x-wall-shear on the profile zone, exported beside the flow field),
attributed to elements by the same cKDTree pattern as the recirculation
report and graded by the same shared cfd_run lines as the OpenFOAM parser.

The sign convention is the load-bearing subtlety: Fluent exports the shear
exerted ON THE WALL (points WITH the near-wall flow, attached = positive
x), OpenFOAM's wallShearStress is traction on the FLUID (attached =
negative x). Opposite signs, same physics; each parser owns its own.

Validated on real solutions before wiring: the retained collapse case
reads e2 0.378 here against its own field probe's 0.385, and the engines
agree on every main element of the pairing record while genuinely
differing on the baseline flap (0.102 here vs 0.437 OpenFOAM) — recorded
as physics, not parser error.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_fluent_wall.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run, foam_post  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


CFG = StackConfig.from_dict({
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12.0,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 45})


def main() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="wss-fws-"))
    try:
        polys = foam_post._installed_polys_m(CFG)

        check("no wall_shear.csv is absence, never a pass",
              foam_post.fluent_wall_report(tmp, CFG) is None)

        # synthetic: main fully attached (tx > 0), flap 40% reversed
        rows = ["nodenumber,x-coordinate,y-coordinate,"
                "x-wall-shear,y-wall-shear"]
        n = 0
        for pt in polys[0][::4]:
            n += 1
            rows.append(f"{n},{pt[0]:.6f},{pt[1]:.6f},5.0,0.1")
        flap = polys[1][::4]
        n_rev = int(0.4 * len(flap)) + 1     # exact count, no truncation slack
        for i, pt in enumerate(flap):
            n += 1
            tx = -3.0 if i < n_rev else 4.0
            rows.append(f"{n},{pt[0]:.6f},{pt[1]:.6f},{tx},0.1")
        expect = round(n_rev / len(flap), 3)
        (tmp / "wall_shear.csv").write_text("\n".join(rows),
                                            encoding="utf-8")
        w = foam_post.fluent_wall_report(tmp, CFG)
        check("Fluent sign convention: reversed = x-shear < 0 (shear ON "
              "the wall points WITH the flow)",
              w is not None
              and w["separation"]["wing_e1"]["reversed_frac"] == 0.0
              and w["separation"]["wing_e2"]["reversed_frac"] == expect,
              f"({w and w['separation']}, expect {expect})")
        check("node counts travel with the fractions",
              w["separation"]["wing_e1"]["n_faces"] == len(polys[0][::4]))
        check("the report names its source so a reader knows the channel",
              w["source"] == "fluent-x-wall-shear")

        v, k = cfd_run.wall_verdict_text(w)
        check("the SHARED grading produces the same verdict prose as the "
              "OpenFOAM engine",
              v is not None and "e1 attached" in v
              and f"e2 separated ({expect * 100:.0f}% reversed" in v,
              f"({v})")
        check("mid-40s is not near either line — no knife flag", k is False)
        check("the shared grading declines an empty report",
              cfd_run.wall_verdict_text(None) == (None, None))

        # non-finite rows are dropped, not propagated
        rows.insert(3, "999,nan,nan,nan,nan")
        (tmp / "wall_shear.csv").write_text("\n".join(rows),
                                            encoding="utf-8")
        w2 = foam_post.fluent_wall_report(tmp, CFG)
        check("non-finite rows are dropped",
              w2 is not None
              and w2["separation"]["wing_e1"]["reversed_frac"] == 0.0)

        (tmp / "wall_shear.csv").write_text("not,a,csv\nx",
                                            encoding="utf-8")
        check("a garbage csv degrades to None, not a crash",
              foam_post.fluent_wall_report(tmp, CFG) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- wiring pins ---------------------------------------------------
    f2 = (ROOT / "app" / "core" / "fluent2d_run.py").read_text(
        encoding="utf-8")
    check("the 2D engine exports wall_shear.csv beside the flow field",
          '"wall_shear.csv"' in f2 and '"x-wall-shear"' in f2)
    check("the export is best-effort — a failure degrades to the field "
          "channel, never a fake wall verdict",
          "except Exception:" in f2[f2.find('"x-wall-shear"') - 400:
                                    f2.find('"x-wall-shear"') + 400])
    check("the 2D engine populates the wall channel from its own solution",
          "fluent_wall_report(" in f2
          and "cfd_run.wall_verdict_text(" in f2)
    mcp = (ROOT / "scripts" / "fluent_mcp.py").read_text(encoding="utf-8")
    check("export_ascii accepts surfaces (default keeps old behaviour)",
          "surfaces: list[str] | None = None" in mcp)
    check("cfd_run keeps ONE grading implementation (no inline duplicate)",
          (ROOT / "app" / "core" / "cfd_run.py").read_text(encoding="utf-8")
          .count("rank-count luck") == 1)

    print(f"\n{sum(results)}/{len(results)} fluent-wall checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
