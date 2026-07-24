"""Slot-relief surface-pressure metric: measured against the RANS record,
and REJECTED. This script is the executable evidence for that decision
(DECISIONS.md "Rule envelopes and optimizer objectives", LOG.md 2026-07).

Hypothesis tested: Smith-1975 dumping-velocity relief is an inviscid
circulation effect, so a canonical-recovery-style metric on the coupled
panel solution should detect the recorded failure mode (flap not relieving
the main element; flow detaching ahead of the main TE; fine-mesh RANS
deltas -37..-42% vs -14% for clean winners).

Variants measured on the main element, against every configuration whose
fine-mesh RANS delta is on the record (docs/calibration):

  A  CRF on the ground solution, peak -> TE panels (the naive form)
  B  CRF on the free-air solution (ground images removed from the peak)
  C  as B, TE value read 2.5% chord upstream (finite-angle TEs are
     stagnation points in potential flow — the very TE always reads ~1)
  D  aft-only recovery, free air (start at 60% of the suction arc)
  E  dumping-velocity ratio sqrt(1 - cp) at the 2.5% station, free air

Measured outcome (2026-07, this script):
  * A reads 0.977-0.990 for EVERY design — ground-image suction peaks
    (cp_min down to -49) dominate the canonical normalization.
  * B, C, D: clean and warned classes OVERLAP (gaps -0.06..-0.14).
  * E nearly separates the classes (clean <= 1.172, warned >= 1.175) but
    is a loading proxy — and on the gap axis it moves the WRONG way:
    closing the slot from 3.0 to 0.8 %c LOWERS E from 1.24 to 1.12, i.e.
    the inviscid solver reads a tighter slot as healthier. The real
    tight-gap failure is boundary-layer merging, invisible inviscidly.
  * Every variant is monotone the wrong way or flat along the gap sweep.

Conclusion: the recorded over-claim classes are separated by the LOADING
fraction (already priced by the optimizer's bands and trust penalty, and
RANS-validated); the slot axis needs viscous evidence (the RANS gap-axis
leg), not an inviscid surface metric. What ships instead is the geometric
slot-signature advisory (analysis.slot_signature_warnings): gap pinned at
the workable floor + no overlap tuck, the exact geometry both recorded
flagged winners share.

    .venv\\Scripts\\python.exe scripts\\recovery_metric_check.py
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import geometry, panel  # noqa: E402

CAL = ROOT / "docs" / "calibration"


def baseline(ride_mm=30.0, aoa=0.0, defl=12.0, gap=1.5, ovl=3.0):
    return {
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": defl,
             "slot_gap_pct": gap, "slot_overlap_pct": ovl}],
        "stack_aoa_deg": aoa, "ride_height_mm": ride_mm, "chord_mm": 350,
        "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 70}


# (label, class, recorded fine-mesh delta_cl %, config)
CASES = [
    ("baseline 30mm (validated)", "clean", -0.0, baseline()),
    ("baseline 40mm (validated)", "clean", -1.0, baseline(ride_mm=40.0)),
    ("stage2_clean", "clean", -14.2,
     json.loads((CAL / "stage2_clean.json").read_text())),
    ("stage2b_clean", "clean", -14.4,
     json.loads((CAL / "stage2b_clean.json").read_text())),
    ("aoa +2 (loading warn)", "warned", -23.1, baseline(aoa=2.0)),
    ("flap 20 (loading warn)", "warned", -30.0, baseline(defl=20.0)),
    ("stage2_flagged", "warned", -41.7,
     json.loads((CAL / "stage2_flagged.json").read_text())),
    ("stage2b_flagged", "warned", -36.5,
     json.loads((CAL / "stage2b_flagged.json").read_text())),
]


def suction_side(sol, k):
    """(cp, dist-from-TE) along the suction side, TE end first.

    Winding-agnostic: panels run TE -> around -> TE; the far-from-TE panel
    splits the sides, the side holding the global Cp minimum is suction."""
    sel = sol.element_index == k
    cp = np.asarray(sol.cp[sel], float)
    mid = np.asarray(sol.midpoints[sel], float)
    te = 0.5 * (mid[0] + mid[-1])
    d_te = np.hypot(mid[:, 0] - te[0], mid[:, 1] - te[1])
    i_far = int(np.argmax(d_te))
    if int(np.argmin(cp)) <= i_far:
        idx = np.arange(i_far + 1)
    else:
        idx = np.arange(len(cp) - 1, i_far - 1, -1)
    return cp[idx], d_te[idx]


def variants(sol, k):
    cps, dts = suction_side(sol, k)
    cp_min = float(cps.min())
    cp_te = float(np.median(cps[:3]))
    cp_off = float(cps[int(np.argmin(np.abs(dts - 0.025)))])
    cp_aft = float(cps[int(np.argmin(np.abs(dts - 0.6 * dts.max())))])
    crf = (cp_te - cp_min) / (1 - cp_min) if cp_min < 0.5 else 0.0
    crf_off = (cp_off - cp_min) / (1 - cp_min) if cp_min < 0.5 else 0.0
    aft = (cp_off - cp_aft) / (1 - cp_aft) if cp_aft < 0.5 else 0.0
    vte = float(np.sqrt(max(0.0, 1.0 - cp_off)))
    return crf, crf_off, aft, vte


def solve(cfg_dict):
    cfg = geometry.StackConfig.from_dict(cfg_dict)
    inst = geometry.install_stack(geometry.build_stack(cfg),
                                  cfg.ride_height_c)
    return panel.solve_pair([e["coords"] for e in inst], 0.0)


def main() -> int:
    print(f"{'case':28}{'class':8}{'RANS d%':>8}"
          f"{'A grd':>7}{'B free':>8}{'C off':>7}{'D aft':>7}{'E vte':>7}")
    rows = []
    for label, klass, delta, d in CASES:
        free, ground = solve(d)
        a = variants(ground, 0)[0]
        b, c, aft, e = variants(free, 0)
        rows.append((klass, a, b, c, aft, e))
        print(f"{label:28}{klass:8}{delta:8.1f}"
              f"{a:7.3f}{b:8.3f}{c:7.3f}{aft:7.3f}{e:7.3f}")
    print()
    verdict_sep = True
    for col, name, hi_is_bad in ((1, "A", True), (2, "B", True),
                                 (3, "C", True), (4, "D", True),
                                 (5, "E", True)):
        clean = [r[col] for r in rows if r[0] == "clean"]
        warned = [r[col] for r in rows if r[0] == "warned"]
        gap = min(warned) - max(clean)
        sep = gap > 0
        print(f"variant {name}: clean {min(clean):.3f}..{max(clean):.3f}  "
              f"warned {min(warned):.3f}..{max(warned):.3f}  "
              f"gap {gap:+.3f} {'SEPARATES' if sep else 'OVERLAPS'}")
        verdict_sep = verdict_sep and sep

    print("\nslot-gap sweep at healthy loading (the D7 axis):")
    print(f"{'gap %c':>7}{'C off':>8}{'D aft':>7}{'E vte':>7}"
          "   (a viscous-choke metric should WORSEN toward 0.8)")
    for g in (0.8, 1.0, 1.3, 1.6, 2.0, 2.5, 3.0):
        free, _ = solve(baseline(gap=g))
        _, c, aft, e = variants(free, 0)
        print(f"{g:7.1f}{c:8.3f}{aft:7.3f}{e:7.3f}")
    print("\nverdict: " + (
        "classes separate — revisit wiring a surface metric"
        if verdict_sep else
        "no variant separates the classes AND tracks the gap axis — the "
        "inviscid surface metric stays REJECTED; loading fractions carry "
        "the trust story and the slot-signature advisory covers the "
        "recorded geometry."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
