"""Separation screens measured against the RANS wall-shear record — the
executable evidence behind what shipped (and what did not).

Ground truth: docs/calibration/wall_truth.json — per-element reversed
wall-shear fractions from the retained RANS cases (attached <= 0.10,
partial <= 0.20, separated > 0.20), preserved verbatim because run-dir
housekeeping eventually deletes the case directories.

Candidates measured here, in the order they were built:

1. Integral boundary-layer march (Thwaites laminar, Michel transition
   with the laminar-separation short-bubble rule, Head turbulent with
   Ludwieg-Tillman Cf, H >= 2.4 criterion), marched on both sides of
   every element at the booked realized field. Two stopping rules:
   "first" (classical stop at first H-crossing) and "terminal" (march
   through with H capped; separation = the terminal H-run). MEASURED
   NON-DISCRIMINATING: at the booked realization the free-air-dominated
   distributions of dying and healthy elements are nearly identical, so
   "first" flags the validated baseline's flap (nose-spill bubble) while
   "terminal" re-heals the genuinely dead elements. Not wired — this is
   the integral-method companion to the rejected canonical-recovery
   endpoint metrics (recovery_metric_check.py).

2. Wake-shadow screen (app/core/wake_shadow.py, SHIPPED): minimum
   realized upper-side velocity over the 15-92% arc window, elements
   after the first. Separates the wall-truth classes with a clean gap
   (separated <= 0.459, attached >= 0.533), stable under paneling
   45-70/side and realization 0 -> 0.35. This script is the gate: it
   exits nonzero if the shipped thresholds stop separating the record.

Run:
    .venv\\Scripts\\python.exe scripts\\separation_metric_check.py
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import analysis, geometry, panel, wake_shadow  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

CAL = ROOT / "docs" / "calibration"


# ---------------------------------------------------------------------------
# candidate 1: the integral-BL march (kept here, not in app/core — measured
# non-discriminating; see module docstring)
# ---------------------------------------------------------------------------

def _h1_of_h(h):
    if h <= 1.6:
        return 3.3 + 0.8234 * max(h - 1.1, 0.05) ** -1.287
    return 3.3 + 1.5501 * (h - 0.6778) ** -3.064


def _h_of_h1(h1):
    h1 = max(h1, 3.323)
    if h1 >= 5.39292:
        return 1.1 + (0.8234 / (h1 - 3.3)) ** (1.0 / 1.287)
    return 0.6778 + (1.5501 / (h1 - 3.3)) ** (1.0 / 3.064)


def side_march(s, ue, nu, rule="first"):
    """Thwaites -> Michel/short-bubble -> Head. Returns separation arc
    fraction of the side (1.0 = attached through the 2.5% TE-ignore)."""
    s = np.asarray(s, float)
    ue = np.maximum(np.asarray(ue, float), 1e-3)
    s_total = float(s[-1])
    if s_total <= 0 or len(s) < 4:
        return 1.0
    n = int(np.clip(4 * len(s), 160, 480))
    sf = np.linspace(0.0, s_total, n)
    uf = np.maximum(np.interp(sf, s, ue), 1e-3)
    due = np.gradient(uf, sf)
    integrand = uf ** 5
    integral = np.concatenate([[0.0], np.cumsum(
        0.5 * (integrand[1:] + integrand[:-1]) * np.diff(sf))])
    theta2 = 0.45 * nu * integral / uf ** 6
    theta2[0] = theta2[1]
    lam = theta2 / nu * due
    re_th = np.sqrt(theta2) * uf / nu
    re_s = np.maximum(uf * sf / nu, 1.0)
    michel = 1.174 * (1.0 + 22400.0 / re_s) * re_s ** 0.46
    hit_tr = np.nonzero(re_th > michel)[0]
    hit_ls = np.nonzero(lam < -0.09)[0]
    i_mich = int(hit_tr[0]) if len(hit_tr) else None
    i_lsep = int(hit_ls[0]) if len(hit_ls) and hit_ls[0] > 0 else \
        (int(hit_ls[1]) if len(hit_ls) > 1 else None)
    if i_mich is not None and (i_lsep is None or i_mich <= i_lsep):
        i_tr = i_mich
    elif i_lsep is not None:
        i_tr = i_lsep
    else:
        return 1.0
    if i_tr >= n - 2:
        return 1.0
    theta = float(max(np.sqrt(theta2[i_tr]), 1e-8))
    h = 1.40
    h1 = _h1_of_h(h)
    run_start = None
    for i in range(i_tr, n - 1):
        ds = sf[i + 1] - sf[i]
        u, du = uf[i], due[i]
        cf = 0.246 * 10.0 ** (-0.678 * h) * max(u * theta / nu, 1.0) ** -0.268
        dtheta = cf / 2.0 - (h + 2.0) * theta / u * du
        f_ent = 0.0306 * max(h1 - 3.0, 1e-3) ** -0.6169
        y = u * theta * h1 + ds * u * f_ent
        theta = max(theta + ds * dtheta, 1e-9)
        h1 = max(y / (uf[i + 1] * theta), 3.323)
        h = _h_of_h1(h1)
        if rule == "terminal":
            h = min(h, 3.2)
            h1 = _h1_of_h(h)
        if h >= 2.4:
            if rule == "first":
                return float(sf[i + 1] / s_total)
            if run_start is None:
                run_start = i + 1
        elif run_start is not None:
            run_start = None
    return 1.0 if run_start is None else float(sf[run_start] / s_total)


def march_lost(cfg_dict, rule):
    """Worst-side lost-arc per element under the given stopping rule."""
    cfg = StackConfig.from_dict(cfg_dict)
    inst = geometry.install_stack(geometry.build_stack(cfg),
                                  cfg.ride_height_c)
    free, ground = panel.solve_pair([e["coords"] for e in inst], 0.0)
    _, _, r = analysis.realized_gain(-free.Cl, -ground.Cl, cfg)
    vt_eff = free.vt + r * (ground.vt - free.vt)
    nu = cfg.nu / (cfg.speed_ms * cfg.chord_m)
    out = []
    for k in range(int(free.element_index.max()) + 1):
        sel = free.element_index == k
        sides = wake_shadow.element_sides(
            free.midpoints[sel], vt_eff[sel], free.panel_lengths[sel])
        if sides is None:
            out.append(0.0)
            continue
        worst = 0.0
        for name in ("lower", "upper"):
            f = side_march(sides[name]["s"], sides[name]["ue"], nu, rule)
            if f < 0.975:
                worst = max(worst, 1.0 - f)
        out.append(worst)
    return out


# ---------------------------------------------------------------------------
# candidate 2 (shipped): wake-shadow screen
# ---------------------------------------------------------------------------

def shadow_mins(cfg_dict):
    cfg = StackConfig.from_dict(cfg_dict)
    inst = geometry.install_stack(geometry.build_stack(cfg),
                                  cfg.ride_height_c)
    free, ground = panel.solve_pair([e["coords"] for e in inst], 0.0)
    _, _, r = analysis.realized_gain(-free.Cl, -ground.Cl, cfg)
    return wake_shadow.stack_shadow(free, ground, r)


def baseline(ride_mm=30.0, aoa=0.0, defl=12.0, gap=1.5, ovl=3.0):
    return {
        "elements": [
            {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": defl,
             "slot_gap_pct": gap, "slot_overlap_pct": ovl}],
        "stack_aoa_deg": aoa, "ride_height_mm": ride_mm, "chord_mm": 350,
        "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 70}


def truth_class(frac):
    return ("attached" if frac <= 0.10
            else "partial" if frac <= 0.20 else "separated")


def main() -> int:
    truth = json.loads((CAL / "wall_truth.json").read_text(encoding="utf-8"))
    by_id = {c["case_id"]: c for c in truth["cases"]}
    for c in truth["cases"]:
        if isinstance(c["config"], str) and c["config"].startswith("same_as:"):
            c["config"] = by_id[c["config"].split(":", 1)[1]]["config"]

    print("=== wall-truth cases: march (both rules) vs wake-shadow ===")
    gate_ok = True
    sep_shadow, att_shadow = [], []
    m_first = {"sep": [], "att": []}
    m_term = {"sep": [], "att": []}
    for c in truth["cases"]:
        lost_f = march_lost(c["config"], "first")
        lost_t = march_lost(c["config"], "terminal")
        sh = shadow_mins(c["config"])
        print(f"\n{c['case_id']} [{c['mesh']}, {c['n_iters']} it, "
              f"gate={c['gate']}]  {c['label']}")
        for i in range(len(sh)):
            patch = f"wing_e{i + 1}"
            frac = c["reversed_frac"].get(patch)
            klass = truth_class(frac) if frac is not None else "?"
            sv = sh[i]["shadow_min"]
            sv_s = f"{sv:.3f} {sh[i]['status']}" if sv is not None \
                else "exempt (first element)"
            mark = ""
            if c["gate"] and i > 0 and sv is not None:
                if klass == "separated":
                    sep_shadow.append(sv)
                    m_first["sep"].append(lost_f[i])
                    m_term["sep"].append(lost_t[i])
                    if sv >= wake_shadow.SHADOW_SEP:
                        mark, gate_ok = "  << MISS", False
                elif klass == "attached":
                    att_shadow.append(sv)
                    m_first["att"].append(lost_f[i])
                    m_term["att"].append(lost_t[i])
                    if sv < wake_shadow.SHADOW_SEP:
                        mark, gate_ok = "  << FALSE FLAG", False
            print(f"  e{i + 1}: RANS {klass:9} ({frac:.3f})  "
                  f"march first/term lost {lost_f[i]:.3f}/{lost_t[i]:.3f}  "
                  f"shadow {sv_s}{mark}")

    print("\n=== recorded-class configs (secondary, not gated) ===")
    cases2 = [
        ("baseline 30mm (validated ~0%)", baseline()),
        ("baseline 40mm (validated -1%)", baseline(ride_mm=40.0)),
        ("gapleg g0p8 (-1.3% fine)", json.loads(
            (CAL / "gapleg_g0p8.json").read_text())),
        ("gapleg g1p3", json.loads((CAL / "gapleg_g1p3.json").read_text())),
        ("gapleg g2", json.loads((CAL / "gapleg_g2.json").read_text())),
        ("stage2_clean (-14.2%)", json.loads(
            (CAL / "stage2_clean.json").read_text())),
        ("stage2_flagged (-41.7%)", json.loads(
            (CAL / "stage2_flagged.json").read_text())),
        ("stage2b_flagged (-36.5%)", json.loads(
            (CAL / "stage2b_flagged.json").read_text())),
        ("stage3_maxdf (-19.2%)", json.loads(
            (CAL / "stage3_maxdf_winner.json").read_text())),
        ("stage3b_knee (+78.7%)", json.loads(
            (CAL / "stage3b_pareto_knee.json").read_text())),
        ("aoa +2 (warned -23.1%)", baseline(aoa=2.0)),
        ("flap 20 (warned -30.0%)", baseline(defl=20.0)),
    ]
    worst_clean = None
    for label, d in cases2:
        try:
            sh = shadow_mins(d)
        except Exception as e:
            print(f"{label}: EVAL FAILED {e}")
            continue
        vals = [f"e{i + 1} {s['shadow_min']:.3f} {s['status']}"
                for i, s in enumerate(sh) if s["shadow_min"] is not None]
        print(f"{label:34} " + "  ".join(vals))
        if "validated" in label or "gapleg" in label:
            for s in sh:
                if s["shadow_min"] is not None:
                    worst_clean = (s["shadow_min"] if worst_clean is None
                                   else min(worst_clean, s["shadow_min"]))

    print("\n=== verdict ===")
    # the march's attached pool must include the VALIDATED baseline: its
    # flap carries a nose-spill bubble the first-crossing rule reads as
    # total separation — the false flag that rejected that rule
    for lbl, d in (("baseline 30mm", baseline()),
                   ("gapleg g0p8", json.loads(
                       (CAL / "gapleg_g0p8.json").read_text()))):
        m_first["att"].extend(march_lost(d, "first")[1:])
        m_term["att"].extend(march_lost(d, "terminal")[1:])
    print(f"march first-crossing:  sep-truth lost "
          f"{min(m_first['sep']):.3f}..{max(m_first['sep']):.3f}  "
          f"att-class lost {min(m_first['att']):.3f}.."
          f"{max(m_first['att']):.3f}  -> "
          + ("separates" if min(m_first["sep"]) > max(m_first["att"])
             else "OVERLAPS (rejected: flags the validated baseline)"))
    print(f"march terminal-run:    sep-truth lost "
          f"{min(m_term['sep']):.3f}..{max(m_term['sep']):.3f}  "
          f"att-class lost {min(m_term['att']):.3f}.."
          f"{max(m_term['att']):.3f}  -> "
          + ("separates" if min(m_term["sep"]) > max(m_term["att"])
             else "OVERLAPS (rejected: re-heals the dead elements)"))
    gap = min(att_shadow) - max(sep_shadow)
    print(f"wake-shadow:           sep-truth {min(sep_shadow):.3f}.."
          f"{max(sep_shadow):.3f}  att-truth {min(att_shadow):.3f}.."
          f"{max(att_shadow):.3f}  gap {gap:+.3f}")
    if worst_clean is not None:
        print(f"validated/gapleg configs, worst shadow_min: "
              f"{worst_clean:.3f} (must stay >= SHADOW_WARN "
              f"{wake_shadow.SHADOW_WARN})")
    ok_margin = (gap > 0
                 and max(sep_shadow) < wake_shadow.SHADOW_SEP
                 and min(att_shadow) >= wake_shadow.SHADOW_SEP
                 and (worst_clean is None
                      or worst_clean >= wake_shadow.SHADOW_WARN))
    if gate_ok and ok_margin:
        print("PASS: wake-shadow separates the wall-shear record with the "
              "shipped thresholds; the march variants remain rejected.")
        return 0
    print("FAIL: the shipped thresholds no longer separate the record — "
          "re-measure before trusting the screen.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
