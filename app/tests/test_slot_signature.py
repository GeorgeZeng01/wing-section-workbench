"""Slot-capture signature advisory: the geometric guardrail for the
recorded over-claim failure mode (gap pinned at the floor + no overlap
tuck — both RANS-flagged winners' geometry). The surface-pressure metric
that was tried first is documented and rejected in
scripts/recovery_metric_check.py; these checks pin the shipped behavior.

Run directly:  python app/tests/test_slot_signature.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import analysis, geometry  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def cfg_d(gap=1.5, ovl=3.0, defl=12.0, aoa=0.0):
    return {
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": defl,
             "slot_gap_pct": gap, "slot_overlap_pct": ovl}],
        "stack_aoa_deg": aoa, "ride_height_mm": 30, "chord_mm": 350,
        "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 60}


def sig(cfg_dict):
    cfg = geometry.StackConfig.from_dict(cfg_dict)
    design = geometry.build_stack(cfg)
    return analysis.slot_signature_warnings(design)


def main():
    flagged = json.loads(
        (ROOT / "docs" / "calibration" / "stage2_flagged.json").read_text())
    w = sig(flagged)
    check("recorded flagged winner triggers the signature advisory",
          len(w) == 1 and "workable floor" in w[0], f"({len(w)})")
    check("advisory cites the record and the fix",
          w and "Fine-mesh truth runs on record" in w[0] and "overlap" in w[0])

    # the record's key subtlety: the CLEAN -14% winners share the same
    # slot corner (gap 0.80, overlap ~0) — the advisory must fire on them
    # too, because no recorded truth point supports that corner at all
    clean = json.loads(
        (ROOT / "docs" / "calibration" / "stage2_clean.json").read_text())
    check("recorded clean winner shares the corner and also triggers",
          len(sig(clean)) == 1)
    check("healthy baseline does not trigger it", sig(cfg_d()) == [])

    # boundary behavior: BOTH halves of the signature must be present
    check("tight gap alone (healthy tuck) does not trigger",
          sig(cfg_d(gap=0.85, ovl=2.5)) == [])
    check("no tuck alone (healthy gap) does not trigger",
          sig(cfg_d(gap=1.8, ovl=0.0)) == [])
    check("tight gap + no tuck triggers",
          len(sig(cfg_d(gap=0.9, ovl=0.2))) == 1)
    check("just past the gap line does not trigger",
          sig(cfg_d(gap=1.2, ovl=0.2)) == [])

    # end-to-end: the flag reaches analyze() and quick_objective_eval
    cfg = geometry.StackConfig.from_dict(cfg_d(gap=0.9, ovl=0.2))
    r = analysis.analyze(cfg, include_geometry=False)
    check("analyze surfaces the advisory and the flag",
          r["slot_signature"] is True
          and any("workable floor" in x for x in r["warnings"]))
    ev = analysis.quick_objective_eval(cfg)
    check("quick eval carries the signature flag",
          ev["feasible"] and ev["slot_signature"] is True)
    ev_ok = analysis.quick_objective_eval(
        geometry.StackConfig.from_dict(cfg_d()))
    check("quick eval flag is false on a healthy slot",
          ev_ok["slot_signature"] is False)

    print(f"\n{sum(results)}/{len(results)} slot-signature checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
