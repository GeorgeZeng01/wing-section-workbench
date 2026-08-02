"""Summarise the accumulated harvest, and optionally extract a reviewable
record into docs/calibration.

The app appends one row per finished solve to the harvest sink under the
data directory (see cfd_run.append_harvest). That file is per-machine
working state: it is not version-controlled, because a tracked target
would dirty the working tree on every run and sweep unreviewed rows into
commits.

This script is the reviewed step between the two. By default it only
REPORTS -- what accumulated, how much of it is usable, and what is still
missing. Pass --write to extract the usable rows into
docs/calibration/harvest.jsonl for inspection and commit.

What "usable" means here is deliberately narrow, and none of it is a
grading decision:
  * converged, and not stopped by hand -- a bound is not a result;
  * carries the cheap screen's prediction (shadow_mins);
  * carries at least one measurement (wall-shear fractions, or the field
    probe's near-wall fractions).
A row failing any of those is still kept in the raw sink; it just cannot
anchor a regression.

Nothing here decides where a threshold falls. That is a regression over
many rows, and it needs rows carrying BOTH channels -- see the blocker
note printed at the end.

Run:  .venv\\Scripts\\python.exe scripts\\promote_harvest.py [--write]
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run  # noqa: E402

OUT = ROOT / "docs" / "calibration" / "harvest.jsonl"


def usable(r):
    if not r.get("converged") or r.get("user_stopped"):
        return False, "bound (unconverged or hand-stopped)"
    if not r.get("shadow_mins"):
        return False, "no screen prediction"
    has_wall = bool(r.get("wall_reversed"))
    has_field = any(v is not None
                    for v in (r.get("recirc_near_wall_frac") or []))
    if not (has_wall or has_field):
        return False, "no measurement"
    return True, "both channels" if (has_wall and has_field) else (
        "wall shear only" if has_wall else "field probe only")


def main() -> int:
    src = cfd_run._harvest_path()
    print(f"harvest sink: {src}")
    if not src.is_file():
        print("  (nothing accumulated yet — the sink is written by a "
              "finished solve)")
        return 0
    rows, bad = [], 0
    for ln in src.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError:
            bad += 1
    print(f"  {len(rows)} rows read"
          + (f", {bad} unparseable lines skipped" if bad else ""))

    # dedupe on the case: a row written twice (a re-measured case, a driver
    # that appended alongside the runner) would double-weight that design in
    # any regression fitted on this file. Last write wins.
    seen, dedup = {}, []
    for r in rows:
        k = (r.get("case_id"), r.get("engine"), r.get("mesh_size"))
        if k[0] is None:
            dedup.append(r)
            continue
        seen[k] = r
    dedup.extend(seen.values())
    if len(dedup) != len(rows):
        print(f"  {len(rows) - len(dedup)} duplicate case rows collapsed "
              f"(last write wins)")
    rows = dedup

    keep, reasons = [], {}
    for r in rows:
        ok, why = usable(r)
        reasons[why] = reasons.get(why, 0) + 1
        if ok:
            keep.append(r)
    print()
    for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {why}")

    both = [r for r in keep if r.get("wall_reversed")
            and any(v is not None
                    for v in (r.get("recirc_near_wall_frac") or []))]
    by_engine = {}
    for r in rows:
        by_engine[r.get("engine")] = by_engine.get(r.get("engine"), 0) + 1
    print()
    print(f"  usable rows           : {len(keep)}")
    print(f"  carrying BOTH channels: {len(both)}")
    print(f"  by engine             : "
          + ", ".join(f"{k}={v}" for k, v in sorted(by_engine.items())))

    if "--write" in sys.argv:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text("".join(json.dumps(r) + "\n" for r in keep),
                       encoding="utf-8")
        print(f"\n  wrote {len(keep)} rows -> {OUT.relative_to(ROOT)}")
    else:
        print("\n  (report only; pass --write to extract into "
              "docs/calibration/harvest.jsonl)")

    print()
    print("BLOCKER, unchanged until paired runs exist: a field-side grading "
          "line needs rows carrying BOTH a wall-shear measurement and a "
          "field probe on the same solve. The ANSYS engine exports no wall "
          "shear, and the OpenFOAM cases that had it are gone, so "
          f"'both channels' currently stands at {len(both)}. Re-solving the "
          "calibration configs on OpenFOAM with fields preserved is what "
          "moves that number.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
