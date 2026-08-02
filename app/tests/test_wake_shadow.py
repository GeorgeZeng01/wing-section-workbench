"""Wake-shadow separation screen: the stagnation-split geometry, the
regression pins against the RANS wall-shear record (the screen must keep
separating the measured classes, or it is lying), the analysis wiring
(element fields, warnings, sweep parity), and the optimizer wiring
(do-no-harm target band, max-mode gate and output filter).

Run directly:  python app/tests/test_wake_shadow.py
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import analysis, geometry, optimizer, panel, wake_shadow  # noqa: E402
from app.core.geometry import StackConfig  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


BASE = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 70,
}

TRUTH = ROOT / "docs" / "calibration" / "wall_truth.json"


def solved(cfg_d, n_panels=None):
    d = dict(cfg_d)
    if n_panels:
        d["n_panels_per_side"] = n_panels
    cfg = StackConfig.from_dict(d)
    inst = geometry.install_stack(geometry.build_stack(cfg),
                                  cfg.ride_height_c)
    free, ground = panel.solve_pair([e["coords"] for e in inst], 0.0)
    _, _, r = analysis.realized_gain(-free.Cl, -ground.Cl, cfg)
    return free, ground, r


def main():
    # ---- stagnation split invariants ----
    free, ground, r_gain = solved(BASE)
    for k in range(2):
        sel = free.element_index == k
        vt_eff = (free.vt + r_gain * (ground.vt - free.vt))[sel]
        sides = wake_shadow.element_sides(free.midpoints[sel], vt_eff,
                                          free.panel_lengths[sel])
        idx_all = sorted(np.concatenate([sides["lower"]["idx"],
                                         sides["upper"]["idx"]]).tolist())
        check(f"e{k + 1}: sides partition every panel exactly once",
              idx_all == list(range(int(sel.sum()))))
        check(f"e{k + 1}: arc coordinates ascend from the stagnation point",
              all(np.all(np.diff(sides[s]["s"]) > 0)
                  for s in ("lower", "upper")))
        check(f"e{k + 1}: both sides start at stagnation (Ue ~ 0)",
              sides["lower"]["ue"][0] == 0.0 and sides["upper"]["ue"][0] == 0.0)
        ylo = np.mean(free.midpoints[sel][sides["lower"]["idx"], 1])
        yup = np.mean(free.midpoints[sel][sides["upper"]["idx"], 1])
        check(f"e{k + 1}: 'lower' side faces the ground", ylo < yup,
              f"(mean y {ylo:.3f} vs {yup:.3f})")

    # ---- stack_shadow structure on the healthy baseline ----
    sh = wake_shadow.stack_shadow(free, ground, r_gain)
    check("first element is exempt (no upstream wake)",
          sh[0]["shadow_min"] is None and sh[0]["status"] is None)
    check("baseline flap reads clear of the caution band",
          sh[1]["shadow_min"] is not None
          and sh[1]["shadow_min"] >= wake_shadow.SHADOW_WARN
          and sh[1]["status"] == "ok",
          f"(shadow {sh[1]['shadow_min']})")
    check("minimum station is inside the evaluated arc window",
          wake_shadow.ARC_LO <= sh[1]["arc_at_min"] <= wake_shadow.ARC_HI)
    check("minimum station carries plot coordinates",
          sh[1]["x_min"] is not None and sh[1]["y_min"] is not None)

    # ---- regression pins against the RANS wall-shear record ----
    # These are the measured classes the screen was validated on
    # (scripts/separation_metric_check.py). If a solver or geometry change
    # moves these across the thresholds, the screen no longer describes
    # the record and must be re-measured, not shipped quietly.
    if not TRUTH.is_file():
        check("wall_truth.json present (validation record)", False,
              f"({TRUTH} missing)")
    else:
        wt = json.loads(TRUTH.read_text(encoding="utf-8"))
        by_id = {c["case_id"]: c for c in wt["cases"]}
        for c in wt["cases"]:
            if isinstance(c["config"], str):
                c["config"] = by_id[c["config"].split(":", 1)[1]]["config"]
        pins = [
            # (case, element index, must_collapse)
            ("1c4bf2d726c3", 1, True),    # e2 measured 21.7% reversed
            ("1c4bf2d726c3", 2, False),   # e3 measured attached (2.7%)
            ("df1865bb82a3", 1, True),    # e2 measured 39.2%
            ("df1865bb82a3", 2, True),    # e3 measured 29.8%
            ("859e79a7486d", 1, True),    # e2 measured 39.7%
            ("859e79a7486d", 2, False),   # e3 partial 11.8% — not flagged
        ]
        shadows = {}
        for cid in {p[0] for p in pins}:
            f2, g2, r2 = solved(by_id[cid]["config"])
            shadows[cid] = wake_shadow.stack_shadow(f2, g2, r2)
        for cid, i, must in pins:
            v = shadows[cid][i]["shadow_min"]
            ok = (v is not None
                  and (v < wake_shadow.SHADOW_SEP) == must)
            check(f"record pin {cid[:5]} e{i + 1}: "
                  f"{'collapse' if must else 'no collapse'}", ok,
                  f"(shadow {v})")
        # paneling stability: the search runs at 45-60 panels/side
        f3, g3, r3 = solved(by_id["1c4bf2d726c3"]["config"], n_panels=50)
        v70 = shadows["1c4bf2d726c3"][1]["shadow_min"]
        v50 = wake_shadow.stack_shadow(f3, g3, r3)[1]["shadow_min"]
        check("metric stable across search/full paneling",
              abs(v70 - v50) < 0.02, f"(70p {v70} vs 50p {v50})")

        # knife-edge skirt: flagged exactly when shadow_min sits inside
        # [SEP - band, WARN + band); exempt/degenerate elements carry None.
        # The flag never alters status — that pair is pinned above.
        lo = wake_shadow.SHADOW_SEP - wake_shadow.SHADOW_KNIFE_BAND
        hi = wake_shadow.SHADOW_WARN + wake_shadow.SHADOW_KNIFE_BAND
        check("knife-edge flag matches the band on every recorded case",
              all((e["knife_edge"] is None) == (e["shadow_min"] is None)
                  and (e["shadow_min"] is None
                       or e["knife_edge"] == (lo <= e["shadow_min"] < hi))
                  for sh_case in shadows.values() for e in sh_case))
        check("healthy baseline flap is not knife-edge",
              sh[1]["knife_edge"] is False and sh[0]["knife_edge"] is None,
              f"(flap shadow {sh[1]['shadow_min']})")

        # ---- analysis wiring ----
        cfg_hot = StackConfig.from_dict(by_id["1c4bf2d726c3"]["config"])
        r_hot = analysis.analyze(cfg_hot, include_geometry=False)
        els = r_hot["elements"]
        check("analyze: elements carry shadow fields",
              els[0]["shadow_min"] is None
              and all(e["shadow_min"] is not None for e in els[1:]))
        check("analyze: collapse element is graded 'collapse'",
              els[1]["shadow_status"] == "collapse",
              f"({els[1]['shadow_min']})")
        n_shadow = sum("wake-shadow" in w for w in r_hot["warnings"])
        check("analyze: collapse warning emitted once", n_shadow == 1)
        sweep = analysis.sweep(cfg_hot, "ride_height_mm",
                               [cfg_hot.ride_height_mm])
        check("sweep: warning-count parity holds with the shadow message",
              sweep["points"][0]["n_warnings"] == len(r_hot["warnings"]),
              f"({sweep['points'][0]['n_warnings']} vs "
              f"{len(r_hot['warnings'])})")
        ev_hot = analysis.quick_objective_eval(cfg_hot)
        check("quick eval: shadow_mins aligned with elements",
              len(ev_hot["shadow_mins"]) == len(cfg_hot.elements)
              and ev_hot["shadow_mins"][0] is None
              and ev_hot["shadow_mins"][1] < wake_shadow.SHADOW_SEP)

        r_base = analysis.analyze(StackConfig.from_dict(BASE),
                                  include_geometry=False)
        check("analyze: healthy baseline emits no shadow warning",
              not any("wake-shadow" in w for w in r_base["warnings"]))

        # ---- optimizer wiring ----
        cfg_d = by_id["1c4bf2d726c3"]["config"]
        job = optimizer.Job(dict(cfg_d), {
            "objective": "target", "target_downforce_n": 300,
            "opt_airfoils": False, "opt_shape": False})
        job._opt_panels = 60
        job._full_panels = 70
        job.t_start = time.time()
        job._baseline_allowance()
        check("target mode: collapsed baseline is noted",
              job.shadow_note == "baseline_below_sep")
        check("target mode: shadow band widened to the baseline",
              job.shadow_band is not None
              and job.shadow_band[1] is not None
              and job.shadow_band[1] < wake_shadow.SHADOW_SEP)
        x0 = optimizer.initial_vector(job.config, job.variables, {})
        job.objective(x0)
        a = job.archive[-1]
        check("target mode: do-no-harm — the start is penalty-free and "
              "inside the clean pool",
              a["penalty"] < 1e-9 and a["pen_gate"] < optimizer.PEN_OK,
              f"(pen {a['penalty']}, gate {a['pen_gate']})")

        job2 = optimizer.Job(dict(cfg_d), {
            "objective": "max_downforce",
            "opt_airfoils": False, "opt_shape": False})
        job2._opt_panels = 60
        job2._full_panels = 70
        job2.t_start = time.time()
        job2._baseline_allowance()
        check("max mode: no do-no-harm widening for the shadow screen",
              job2.shadow_band is None)
        job2.objective(x0)
        a2 = job2.archive[-1]
        check("max mode: a collapsed design is penalized out of the "
              "clean pool",
              a2["penalty"] > 10.0 and a2["pen_gate"] >= optimizer.PEN_OK,
              f"(pen {a2['penalty']:.1f}, gate {a2['pen_gate']:.1f})")

        # output filter: max mode drops collapsed candidates at full
        # fidelity and names the cause
        entry = {"rank": 1, "J": 1.0, "downforce_n": 300.0, "drag_n": 20.0,
                 "x": [0.0], "config": dict(cfg_d),
                 "summary": {"frac_max": 0.5, "confidence_min": 0.9,
                             "efficiency_ld": 8.0, "shadow_collapse": True,
                             "shadow_min": 0.44}}
        clean = {**entry, "J": 1.1,
                 "summary": {**entry["summary"], "shadow_collapse": False,
                             "shadow_min": 0.60}}
        kept = job2._apply_output_filters([dict(entry), dict(clean)])
        check("max mode filter: collapsed candidate dropped, clean "
              "survivor promoted",
              len(kept) == 1 and not kept[0]["summary"]["shadow_collapse"]
              and kept[0]["rank"] == 1)
        check("max mode filter: drop is named in the failure stats",
              job2._drop_stats is not None
              and job2._drop_stats.get("shadow") == 1)
        msg = job2._guarantee_failure_message()
        check("guarantee message names the wake-shadow drop",
              "wake-shadow" in msg)

        job3 = optimizer.Job(dict(BASE), {
            "objective": "target", "target_downforce_n": 200,
            "opt_airfoils": False, "opt_shape": False})
        job3._opt_panels = 60
        job3._full_panels = 70
        job3.t_start = time.time()
        job3._baseline_allowance()
        x0b = optimizer.initial_vector(job3.config, job3.variables, {})
        job3.objective(x0b)
        a3 = job3.archive[-1]
        check("healthy baseline: zero shadow penalty, no note",
              a3["penalty"] < 1e-9 and job3.shadow_note is None)

    # ---- validated envelope -------------------------------------------
    # The module's scope paragraph was prose that nothing enforced, and a
    # design outside it drew a confident "ok". Measured on two converged
    # wall-resolved Fluent solves: shadow_min 0.5471 ("ok", zero penalty)
    # against 0.3848 reversed near-wall stations on element 2, and moving
    # the screen to 0.5607 left the flow at 0.3895 — outside the envelope
    # the metric is not even directional.
    import json as _json
    from app.core.geometry import StackConfig as _SC

    _cal = Path(ROOT) / "docs" / "calibration"
    _truth = _json.loads((_cal / "wall_truth.json").read_text(
        encoding="utf-8"))
    _by = {c["case_id"]: c for c in _truth["cases"]}
    _in = True
    for _c in _truth["cases"]:
        _cf = _c["config"]
        if isinstance(_cf, str) and _cf.startswith("same_as:"):
            _cf = _by[_cf.split(":", 1)[1]]["config"]
        _in = _in and wake_shadow.scope_check(_SC.from_dict(_cf))["in_scope"]
    check("every case the screen was CALIBRATED on is inside the envelope "
          "derived from it — the envelope must bound its own record", _in)

    _seed = _cal.parent.parent / "app_data" / "rans" / "09c6de53265a" \
        / "config.json"
    if _seed.is_file():
        _sc = wake_shadow.scope_check(
            _SC.from_dict(_json.loads(_seed.read_text(encoding="utf-8"))))
        _ax = {o["axis"] for o in _sc["out"]}
        check("the design that read 'ok' while measuring 0.38 reversed is "
              "flagged out of scope, on sections and proportions",
              _sc["in_scope"] is False
              and {"sections", "flap_chord_ratio"} <= _ax, f"({_ax})")
    else:
        print("NOTE  retained seed config absent — the out-of-scope pin did "
              "NOT run here")

    check("wrappers come off before a section is compared",
          wake_shadow.base_section("shape:+0.0055:-0.0115:+0.0075:0.960:s1223")
          == "s1223"
          and wake_shadow.base_section("mfg:thicken:2:s1223") == "s1223"
          and wake_shadow.base_section("S1223") == "s1223")
    check("an in-scope design produces no scope warning, so the note stays "
          "meaningful instead of becoming wallpaper",
          wake_shadow.scope_warning(_SC.from_dict(
              _by[_truth["cases"][0]["case_id"]]["config"]
              if not isinstance(_truth["cases"][0]["config"], str)
              else _by[_truth["cases"][0]["config"].split(":", 1)[1]]
              ["config"])) is None)
    _out_note = wake_shadow.scope_warning(_SC.from_dict({
        "elements": [{"airfoil": "naca2412", "chord_ratio": 1.0,
                      "deflection_deg": 0},
                     {"airfoil": "naca2412", "chord_ratio": 0.9,
                      "deflection_deg": 20, "slot_gap_pct": 1.5,
                      "slot_overlap_pct": 3.0}],
        "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
        "span_mm": 1400, "speed_ms": 15, "n_panels_per_side": 45}))
    check("an out-of-scope design says the reading is not evidence EITHER "
          "way — never that it passed",
          _out_note is not None and "not evidence" in _out_note
          and "pass" not in _out_note.lower())

    # ---- arc pinning: the metric must say when it read its own window --
    _p512 = _cal.parent.parent / "app_data" / "rans" / "512df696f4bd" \
        / "config.json"
    if _p512.is_file():
        import glob as _glob
        from app.core import analysis as _an, geometry as _geo, panel as _pn

        def _shadows(cfg_d):
            _c = _SC.from_dict(cfg_d)
            _i = _geo.install_stack(_geo.build_stack(_c), _c.ride_height_c)
            _f, _g = _pn.solve_pair([e["coords"] for e in _i], 0.0)
            _, _, _r = _an.realized_gain(-_f.Cl, -_g.Cl, _c)
            return wake_shadow.stack_shadow(_f, _g, _r)

        _sh = _shadows(_json.loads(_p512.read_text(encoding="utf-8")))
        check("the invalid-route alarm (deflection-45, min at arc 0.153) "
              "is flagged arc_pinned",
              _sh[1]["arc_pinned"] is True
              and abs(_sh[1]["arc_at_min"] - 0.153) < 0.01,
              f"({_sh[1]['arc_at_min']})")
        _none_pinned = True
        for _c in _truth["cases"]:
            _cf = _c["config"]
            if isinstance(_cf, str) and _cf.startswith("same_as:"):
                _cf = _by[_cf.split(":", 1)[1]]["config"]
            for _s in _shadows(_cf):
                if _s["shadow_min"] is not None and _s["arc_pinned"]:
                    _none_pinned = False
        check("no calibrated element pins at the window's nose edge — the "
              "flag marks out-of-family profiles, not the record",
              _none_pinned)
    else:
        print("NOTE  512df696f4bd absent — the arc-pinning pin did NOT run")

    print(f"\n{sum(results)}/{len(results)} wake-shadow checks passed")
    return all(results)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
