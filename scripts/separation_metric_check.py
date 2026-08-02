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

3. Integral deficits (NOT SHIPPED, NOT ADJUDICATED). The hypothesis was
   that shadow_min is the wrong SHAPE of statistic: it is one number
   from one station, so two flaps that both dip to 0.48 read alike even
   if one recovers at once and the other holds it for 40% of the arc,
   and separation ought to depend on how long the layer stays slow.
   Six reductions of the same windowed curve were measured against the
   record - a 10th percentile, the arc mean, arc fraction below 0.50
   and 0.55, and mean deficit below 0.50 and 0.55.
   MEASURED: the shipped minimum holds the widest normalized class gap
   (+0.514) and every integral reduction scores lower (+0.116 to
   +0.307). So the hypothesis is not supported here.
   BUT THAT IS NOT A RESULT EITHER: the gate pools are 4 separated and
   1 attached element, and a class gap measured against a single
   attached point is arithmetic, not evidence. It can neither rank the
   candidates nor confirm the shipped one. The comparison is kept and
   re-run on every invocation so it sharpens as labelled elements
   accumulate (see cfd_run.append_harvest, which now writes one row per
   finished solve for exactly this reason).

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


# ---------------------------------------------------------------------------
# candidate 3: integral deficits — is a POINT statistic the right shape?
#
# shadow_min is the minimum realized upper-side Ue over the arc window: one
# number from one station. Two flaps can both dip to 0.48 with one recovering
# immediately and the other holding it for 40% of the arc, and the minimum
# cannot tell them apart — but separation depends on how LONG the layer stays
# slow, not on how slow it briefly gets. These candidates keep everything else
# about the shipped screen (same solve, same realized field, same arc window,
# same upper side, first element exempt) and change only the reduction.
#
# Every score below is oriented so HIGHER = WORSE, so the classes can be
# compared on one convention. Threshold-referenced candidates carry the level
# in their name; the level is a REFERENCE for an integral, not a decision
# line — where any of these would cut is a separate calibration.
# ---------------------------------------------------------------------------

def shadow_curves(cfg_dict):
    """Per-element windowed (arc fraction, Ue/V_inf) on the upper side —
    the array stack_shadow reduces to a single minimum. None for the first
    element (exempt) and for degenerate solutions, matching the shipped
    screen's own exemptions exactly."""
    cfg = StackConfig.from_dict(cfg_dict)
    inst = geometry.install_stack(geometry.build_stack(cfg),
                                  cfg.ride_height_c)
    free, ground = panel.solve_pair([e["coords"] for e in inst], 0.0)
    _, _, r = analysis.realized_gain(-free.Cl, -ground.Cl, cfg)
    r = float(np.clip(r, 0.0, 1.0))
    vt_eff = free.vt + r * (ground.vt - free.vt)
    n = int(free.element_index.max()) + 1 if len(free.element_index) else 0
    out = []
    for k in range(n):
        if k == 0:
            out.append(None)
            continue
        sel = free.element_index == k
        sides = wake_shadow.element_sides(free.midpoints[sel], vt_eff[sel],
                                          free.panel_lengths[sel])
        if sides is None:
            out.append(None)
            continue
        up = sides["upper"]
        s, ue = np.asarray(up["s"], float), np.asarray(up["ue"], float)
        st = float(s[-1])
        m = (s >= wake_shadow.ARC_LO * st) & (s <= wake_shadow.ARC_HI * st)
        out.append((s[m] / st, ue[m]) if m.any() else None)
    return out


def integral_scores(curve):
    """Badness scores for one element's windowed curve. Higher = worse."""
    s, ue = curve
    if len(s) < 2:
        return None
    span = float(s[-1] - s[0])
    if span <= 0:
        return None

    def arc_mean(y):
        return float(np.trapezoid(y, s) / span)

    return {
        "min (SHIPPED)": -float(ue.min()),
        "p10": -float(np.percentile(ue, 10)),
        "mean_ue": -arc_mean(ue),
        "frac_below_0.50": arc_mean((ue < 0.50).astype(float)),
        "frac_below_0.55": arc_mean((ue < 0.55).astype(float)),
        "deficit_0.50": arc_mean(np.maximum(0.0, 0.50 - ue)),
        "deficit_0.55": arc_mean(np.maximum(0.0, 0.55 - ue)),
    }


def report_candidates(truth) -> None:
    """Measure every candidate against the wall-shear record and report the
    class gap. A candidate earns nothing by looking clever: it has to put
    the separated-measured elements strictly worse than the attached ones,
    and the margin is reported normalized so metrics on different scales
    can be compared at all."""
    pools = {}
    for c in truth["cases"]:
        if not c["gate"]:
            continue
        curves = shadow_curves(c["config"])
        for i, cur in enumerate(curves):
            if i == 0 or cur is None:
                continue
            frac = c["reversed_frac"].get(f"wing_e{i + 1}")
            if frac is None:
                continue
            klass = truth_class(frac)
            if klass == "partial":
                continue          # the band the record cannot adjudicate
            sc = integral_scores(cur)
            if sc is None:
                continue
            for name, v in sc.items():
                pools.setdefault(name, {"sep": [], "att": []})[
                    "sep" if klass == "separated" else "att"].append(v)

    print("\n=== candidate 3: integral deficits vs the shipped minimum ===")
    print("    (higher = worse for every score; gap = worst attached to "
          "best separated,")
    print("     normalized by the full spread so different scales compare)")
    n_sep = len(next(iter(pools.values()))["sep"]) if pools else 0
    n_att = len(next(iter(pools.values()))["att"]) if pools else 0
    print(f"    pools: {n_sep} separated-measured, {n_att} "
          f"attached-measured elements\n")
    print(f"    {'candidate':18s} {'separated':>18s} {'attached':>18s} "
          f"{'gap':>9s} {'norm':>8s}")
    ranked = []
    for name, p in pools.items():
        if not p["sep"] or not p["att"]:
            continue
        gap = min(p["sep"]) - max(p["att"])
        spread = max(p["sep"] + p["att"]) - min(p["sep"] + p["att"])
        norm = gap / spread if spread > 1e-12 else 0.0
        ranked.append((norm, gap, name, p))
        print(f"    {name:18s} "
              f"{min(p['sep']):8.4f}..{max(p['sep']):<8.4f} "
              f"{min(p['att']):8.4f}..{max(p['att']):<8.4f} "
              f"{gap:+9.4f} {norm:+8.3f}"
              + ("" if gap > 0 else "   OVERLAPS"))
    ranked.sort(reverse=True)
    if ranked:
        best = ranked[0]
        shipped = next((r for r in ranked if r[2] == "min (SHIPPED)"), None)
        print()
        if shipped and best[2] != "min (SHIPPED)" and best[0] > shipped[0]:
            print(f"    Best margin: {best[2]} (norm {best[0]:+.3f}) beats "
                  f"the shipped minimum (norm {shipped[0]:+.3f}).")
        elif shipped:
            print(f"    Ordering on this record: the shipped minimum holds "
                  f"the best margin (norm {shipped[0]:+.3f}); every integral "
                  f"reduction scores lower.")
        # data sufficiency comes AFTER the ordering and outranks it: a gap
        # measured against one element is arithmetic, not evidence
        if min(n_sep, n_att) < 3:
            print()
            print(f"    *** NOT ADJUDICATED: {n_sep} separated and {n_att} "
                  f"attached elements. A class")
            print("    gap computed against a pool this small is arithmetic, "
                  "not evidence — it")
            print("    cannot rank these candidates and it cannot confirm "
                  "the shipped one either.")
            print("    The ordering above is recorded so it can be re-run "
                  "as rows accumulate;")
            print("    it is not a result. What this needs is labelled "
                  "elements, which is")
            print("    exactly what the harvest sink exists to produce.")


# ---------------------------------------------------------------------------
# candidate 4: the two-route compound — measured on the EXTENDED record
#
# The harvest grew the record past wall_truth.json: docs/calibration/
# field_truth.json holds three field-labeled separated elements (the probe
# reads LOW, so its high readings are strong separation evidence), including
# the false-negative family the shipped minimum misses. On that extended
# pool the shipped minimum OVERLAPS (attached min 0.533 vs separated max
# 0.547) — the record outgrew the threshold, which is exactly what the
# harvest exists to reveal.
#
# Measured profile shapes show TWO collapse routes on the labeled record:
#   route 1  enter the window fast, decelerate through it (every labeled
#            e2: entry 0.77-0.99, decel 0.38-0.47)
#   route 2  enter already buried in wake and stay there (df1865's e3:
#            entry 0.475, decel 0.060 — no room to decelerate)
# No single statistic catches both: the minimum misses route 1's high-entry
# family (their minima end up above the line), and any deceleration measure
# misses route 2 (it never decelerates). The compound flags an element when
# EITHER its windowed deceleration exceeds D_STAR or its minimum sits under
# M_STAR.
# ---------------------------------------------------------------------------

CAND4_DECEL = 0.35    # entry-minus-min above this -> route 1
CAND4_MIN = 0.47      # windowed min below this -> route 2


def curve_stats(cfg_dict):
    """Per-element (min, entry, decel) of the windowed upper-side curve;
    None for exempt/degenerate elements."""
    out = []
    for cur in shadow_curves(cfg_dict):
        if cur is None:
            out.append(None)
            continue
        s, ue = cur
        if len(s) < 2:
            out.append(None)
            continue
        out.append({"min": float(ue.min()), "entry": float(ue[0]),
                    "decel": float(ue[0] - ue.min())})
    return out


def report_two_route(truth) -> None:
    ft_path = CAL / "field_truth.json"
    field = json.loads(ft_path.read_text(encoding="utf-8")) \
        if ft_path.is_file() else {"cases": []}
    labeled = []   # (name, truth, stats)
    for c in truth["cases"]:
        if not c["gate"]:
            continue
        st = curve_stats(c["config"])
        for i, s in enumerate(st):
            if s is None:
                continue
            frac = c["reversed_frac"].get(f"wing_e{i + 1}")
            if frac is None:
                continue
            k = truth_class(frac)
            if k == "partial":
                continue
            labeled.append((f"{c['case_id'][:8]} e{i + 1}",
                            "SEP" if k == "separated" else "ATT", s))
    for c in field["cases"]:
        st = curve_stats(c["config"])
        for patch, frac in (c.get("field_reversed") or {}).items():
            i = int(patch.replace("wing_e", "")) - 1
            if st[i] is not None and frac > 0.28:
                labeled.append((f"{c['case']} e{i + 1}", "SEP", st[i]))
    for lbl, d in (("baseline30", baseline()),
                   ("baseline40", baseline(ride_mm=40.0))):
        st = curve_stats(d)
        if st[1] is not None:
            labeled.append((f"{lbl} e2", "ATT", st[1]))

    print("\n=== candidate 4: two-route compound, EXTENDED record ===")
    n_sep = sum(1 for _, t, _ in labeled if t == "SEP")
    n_att = sum(1 for _, t, _ in labeled if t == "ATT")
    print(f"    pools: {n_sep} separated, {n_att} attached "
          f"(wall_truth + field_truth + validated baselines)")
    ship_sep = [s["min"] for _, t, s in labeled if t == "SEP"]
    ship_att = [s["min"] for _, t, s in labeled if t == "ATT"]
    print(f"    shipped min:  SEP {min(ship_sep):.3f}..{max(ship_sep):.3f}"
          f"  ATT {min(ship_att):.3f}..{max(ship_att):.3f}"
          f"  gap {min(ship_att) - max(ship_sep):+.3f}"
          + ("" if min(ship_att) > max(ship_sep) else "   OVERLAPS"))
    miss = fa = 0
    for name, t, s in labeled:
        flag = s["decel"] > CAND4_DECEL or s["min"] < CAND4_MIN
        if t == "SEP" and not flag:
            miss += 1
            print(f"    MISS  {name}: decel {s['decel']:.3f}, "
                  f"min {s['min']:.3f}")
        if t == "ATT" and flag:
            fa += 1
            print(f"    FALSE ALARM  {name}: decel {s['decel']:.3f}, "
                  f"min {s['min']:.3f}")
    print(f"    compound (decel>{CAND4_DECEL} OR min<{CAND4_MIN}): "
          f"{n_sep - miss}/{n_sep} separated caught, "
          f"{n_att - fa}/{n_att} attached clean")
    print("    NOT ADJUDICATED and NOT WIRED: two fitted constants on "
          f"{len(labeled)} points, an attached pool of {n_att}, and the "
          "field labels are a weaker channel than wall shear. This is a "
          "hypothesis the growing harvest can test, recorded so it is "
          "re-measured on every run.")


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

    report_candidates(truth)
    report_two_route(truth)

    # the shipped envelope must still bound the record it was derived from
    print("\n=== validated envelope vs the record ===")
    hc, secs, flaps, ns = [], set(), [], set()
    cfgs = [c["config"] for c in truth["cases"]]
    for p in sorted(CAL.glob("*.json")):
        if p.name == "wall_truth.json":
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(d, dict) and "elements" in d:
            cfgs.append(d)
    for d in cfgs:
        hc.append(d["ride_height_mm"] / d["chord_mm"])
        ns.add(len(d["elements"]))
        for i, e in enumerate(d["elements"]):
            secs.add(wake_shadow.base_section(e.get("airfoil")))
            if i:
                flaps.append(float(e.get("chord_ratio", 1.0)))
    rows = [
        ("h/c", (min(hc), max(hc)), wake_shadow.SCOPE_HC,
         min(hc) >= wake_shadow.SCOPE_HC[0] - 1e-6
         and max(hc) <= wake_shadow.SCOPE_HC[1] + 1e-6),
        ("flap chord ratio", (min(flaps), max(flaps)),
         wake_shadow.SCOPE_CHORD_RATIO_FLAP,
         min(flaps) >= wake_shadow.SCOPE_CHORD_RATIO_FLAP[0] - 1e-6
         and max(flaps) <= wake_shadow.SCOPE_CHORD_RATIO_FLAP[1] + 1e-6),
        ("element count", (min(ns), max(ns)), wake_shadow.SCOPE_N_ELEMENTS,
         min(ns) >= wake_shadow.SCOPE_N_ELEMENTS[0]
         and max(ns) <= wake_shadow.SCOPE_N_ELEMENTS[1]),
    ]
    env_ok = True
    for label, obs, env, ok in rows:
        env_ok = env_ok and ok
        print(f"  {label:18s} record {obs[0]:.4f}..{obs[1]:.4f}  "
              f"envelope {env[0]}..{env[1]}  "
              + ("bounds it" if ok else "<< ENVELOPE NO LONGER BOUNDS THE "
                                        "RECORD"))
    missing = sorted(secs - set(wake_shadow.SCOPE_SECTIONS))
    env_ok = env_ok and not missing
    print(f"  {'sections':18s} record {sorted(secs)}")
    print(f"  {'':18s} envelope {list(wake_shadow.SCOPE_SECTIONS)}  "
          + ("covers it" if not missing
             else f"<< MISSING {missing}"))
    if not env_ok:
        print("  The record has grown past the shipped envelope. Widen "
              "wake_shadow.SCOPE_* to match, or the scope warning will "
              "fire on designs the screen now HAS been validated against.")

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
