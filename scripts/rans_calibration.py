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
          "stack_aoa_deg", "defl2_deg", "speed_ms",
          "n_iters_cap", "state", "converged", "stop_reason", "n_iters_run",
          "elapsed_s", "cl_rans", "cl_rans_std", "cd_rans", "c_est_panel",
          "c_free", "c_ground", "k_g_used", "delta_cl_pct", "suggested_k_g",
          "error"]


def baseline_config(ride_height_mm: float, aoa: float = 0.0,
                    defl: float = 12.0, speed: float = 15.0) -> dict:
    """The two-element baseline the UI seeds (sharp TEs, no mfg prep).

    aoa / flap deflection / speed are overridable so the campaign can test
    whether a fitted curve SHAPE generalizes beyond one operating point —
    a constant fitted to a single config would be curve-fitting one
    design, not calibrating the model."""
    return {
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35,
             "deflection_deg": float(defl),
             "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
        "stack_aoa_deg": float(aoa), "ride_height_mm": float(ride_height_mm),
        "chord_mm": 350, "span_mm": 1400, "speed_ms": float(speed),
        "rho": 1.225, "nu": 1.5e-5, "ncrit": 7,
        "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
        "span_efficiency": 0.9, "n_panels_per_side": 70,
    }


def run_one(ride: float, mesh: str, iters: int, label: str,
            aoa: float = 0.0, defl: float = 12.0,
            speed: float = 15.0, config: dict | None = None) -> dict:
    """One RANS run. `config` (e.g. an optimizer winner under Stage 2
    badge validation) overrides the built baseline entirely; the logged
    ride/aoa/defl/speed columns are then read back out of it."""
    cfg = config if config is not None else baseline_config(
        ride, aoa, defl, speed)
    if config is not None:
        ride = float(cfg.get("ride_height_mm", ride))
        aoa = float(cfg.get("stack_aoa_deg", 0.0))
        defl = float(cfg["elements"][1].get("deflection_deg", 0.0)) \
            if len(cfg.get("elements", [])) > 1 else 0.0
        speed = float(cfg.get("speed_ms", 15.0))
    # the calibration abscissa must use the chord of the stack actually run
    # — a --config stack is not guaranteed to be the 350 mm baseline
    chord_mm = float(cfg.get("chord_mm", 350.0))
    t0 = time.time()
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(
               timespec="seconds"),
           "label": label, "ride_height_mm": ride,
           "h_over_c": round(ride / chord_mm, 4), "mesh": mesh,
           "stack_aoa_deg": aoa, "defl2_deg": defl, "speed_ms": speed,
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
    ap.add_argument("--rides", nargs="+", type=float, default=None,
                    help="ride heights in mm, one run each "
                         "(required unless --config is given)")
    ap.add_argument("--mesh", default="coarse",
                    choices=["coarse", "medium", "fine"])
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--label", default="cal")
    ap.add_argument("--aoa", type=float, default=0.0)
    ap.add_argument("--defl", type=float, default=12.0,
                    help="flap deflection in degrees")
    ap.add_argument("--speed", type=float, default=15.0)
    ap.add_argument("--config", default=None,
                    help="JSON config file: run this exact stack instead of "
                         "the built baseline (one run; --rides ignored)")
    args = ap.parse_args()

    avail = cfd_run.availability()
    if not avail.get("available"):
        print(f"RANS unavailable: {avail.get('detail')}")
        return 1

    if args.config:
        import json
        cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        label = f"{args.label}-{Path(args.config).stem}-{args.mesh}"
        row = run_one(0.0, args.mesh, args.iters, label, config=cfg)
        append_row(row)
        if row["state"] == "done":
            print(f"RESULT {label}: cl {row.get('cl_rans')} vs C_est "
                  f"{row.get('c_est_panel')} (Δ {row.get('delta_cl_pct')}%), "
                  f"{row.get('stop_reason')}", flush=True)
            return 0
        print(f"FAILED {label}: {row.get('state')} — {row.get('error')}",
              flush=True)
        return 2
    if not args.rides:
        ap.error("--rides is required unless --config is given")

    failures = 0
    for ride in args.rides:
        label = f"{args.label}-h{ride:g}-{args.mesh}"
        if (args.aoa, args.defl, args.speed) != (0.0, 12.0, 15.0):
            label += f"-a{args.aoa:g}-d{args.defl:g}-v{args.speed:g}"
        row = run_one(ride, args.mesh, args.iters, label,
                      args.aoa, args.defl, args.speed)
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
