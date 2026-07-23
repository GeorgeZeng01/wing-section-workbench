"""RANS calibration campaign runner: panel C_est vs OpenFOAM, logged.

Runs the two-element baseline section through the in-app RANS pipeline
(app.core.cfd_run, Docker/OpenFOAM) at one or more ride heights, waits for
each run's convergence verdict, and appends one row per run to the
campaign log at docs/calibration/runs.csv — timestamped, so the order and
provenance of every calibration constant that later cites these runs is
on the record.

    .venv\\Scripts\\python.exe scripts\\rans_calibration.py --rides 30
    .venv\\Scripts\\python.exe scripts\\rans_calibration.py \
        --rides 15 25 40 60 90 --mesh coarse --iters 10000

Sequential by construction (cfd_run enforces a one-job guard); a failed
run is logged and the campaign continues. The geometry is the sharp
(no manufacturing prep) baseline, matching the historical verified
comparison point at 30 mm.
"""
import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import cfd_run  # noqa: E402

LOG_CSV = ROOT / "docs" / "calibration" / "runs.csv"

FIELDS = ["timestamp_utc", "label", "ride_height_mm", "h_over_c", "mesh",
          "n_iters_cap", "state", "converged", "stop_reason", "n_iters_run",
          "elapsed_s", "cl_rans", "cl_rans_std", "cd_rans", "c_est_panel",
          "c_free", "c_ground", "k_g_used", "delta_cl_pct", "suggested_k_g",
          "error"]


def baseline_config(ride_height_mm: float) -> dict:
    """The two-element baseline the UI seeds (sharp TEs, no mfg prep)."""
    return {
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
             "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
        "stack_aoa_deg": 0.0, "ride_height_mm": float(ride_height_mm),
        "chord_mm": 350, "span_mm": 1400, "speed_ms": 15,
        "rho": 1.225, "nu": 1.5e-5, "ncrit": 7,
        "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        "span_efficiency": 0.9, "n_panels_per_side": 70,
    }


def run_one(ride: float, mesh: str, iters: int, label: str) -> dict:
    cfg = baseline_config(ride)
    t0 = time.time()
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(
               timespec="seconds"),
           "label": label, "ride_height_mm": ride,
           "h_over_c": round(ride / 350.0, 4), "mesh": mesh,
           "n_iters_cap": iters}
    try:
        job_id = cfd_run.start(cfg, mesh_size=mesh, n_iters=iters)
    except Exception as e:
        row.update({"state": "start_failed", "error": str(e)})
        return row
    print(f"[{label}] started job {job_id} (ride {ride} mm, {mesh})",
          flush=True)
    last_note = 0.0
    while True:
        time.sleep(5)
        job = cfd_run.get(job_id)
        if job is None:
            row.update({"state": "lost", "error": "job vanished"})
            return row
        s = job.snapshot()
        if s["state"] in ("done", "failed", "cancelled"):
            break
        if time.time() - last_note > 60:
            latest = s.get("latest") or {}
            print(f"[{label}] {s['state']} {s.get('phase') or ''} "
                  f"iter {s['iteration']} cl {latest.get('cl')} "
                  f"({s['elapsed_s']:.0f}s)", flush=True)
            last_note = time.time()
    row.update({"state": s["state"], "elapsed_s": round(time.time() - t0, 1),
                "error": s.get("error")})
    r = s.get("result") or (job.result if s["state"] == "done" else None)
    if r:
        p = r.get("panel") or {}
        row.update({
            "converged": r["converged"], "stop_reason": r["stop_reason"],
            "n_iters_run": r["n_iters_run"], "cl_rans": r["cl_rans"],
            "cl_rans_std": r["cl_rans_std"], "cd_rans": r["cd_rans"],
            "c_est_panel": p.get("c_est"), "c_free": p.get("c_free"),
            "c_ground": p.get("c_ground"), "k_g_used": p.get("k_g_used"),
            "delta_cl_pct": r["delta_cl_pct"],
            "suggested_k_g": r["suggested_k_g"],
        })
    return row


def append_row(row: dict) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rides", nargs="+", type=float, required=True,
                    help="ride heights in mm, one run each")
    ap.add_argument("--mesh", default="coarse",
                    choices=["coarse", "medium", "fine"])
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--label", default="cal")
    args = ap.parse_args()

    avail = cfd_run.availability()
    if not avail.get("available"):
        print(f"RANS unavailable: {avail.get('detail')}")
        return 1

    failures = 0
    for ride in args.rides:
        label = f"{args.label}-h{ride:g}-{args.mesh}"
        row = run_one(ride, args.mesh, args.iters, label)
        append_row(row)
        if row["state"] == "done":
            print(f"RESULT {label}: cl {row.get('cl_rans')} "
                  f"± {row.get('cl_rans_std')} vs C_est "
                  f"{row.get('c_est_panel')} (Δ {row.get('delta_cl_pct')}%), "
                  f"suggested k_g {row.get('suggested_k_g')}, "
                  f"{row.get('stop_reason')}, {row.get('n_iters_run')} iters, "
                  f"{row.get('elapsed_s'):.0f}s", flush=True)
        else:
            failures += 1
            print(f"FAILED {label}: {row.get('state')} — {row.get('error')}",
                  flush=True)
    print(f"campaign leg complete: {len(args.rides) - failures}/"
          f"{len(args.rides)} runs ok, log at {LOG_CSV}", flush=True)
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
