"""Rule-envelope validation: the user-entered geometric rules must be
checked in one place and enforced identically by the report, the analysis
feasibility gate, and the optimizer's eager start refusal.

Covers the box limits (length, height, ground clearance) and the per-element
edge limits (leading-edge radius, trailing-edge thickness), which are
measured on the as-built contour and reported per element.

Run directly:  python app/tests/test_rules_envelope.py
"""
import dataclasses
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import airfoils, analysis, geometry, optimizer  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


BASE = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "n_panels_per_side": 60,
}


# two sections with very different noses on very different chords: the main
# is blunt (naca0024, r/c 0.0635 -> 22 mm on 350 mm) and the flap is sharp
# (naca0006, r/c 0.0040 -> 0.5 mm on 122.5 mm), so a scope mistake is
# unmissable rather than a rounding argument
EDGE = {
    "elements": [
        {"airfoil": "naca0024", "chord_ratio": 1.0},
        {"airfoil": "naca0006", "chord_ratio": 0.35, "deflection_deg": 20,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "n_panels_per_side": 60,
}


def cfg_with(env):
    return geometry.StackConfig.from_dict({**BASE, "rule_envelope": env})


def chopped_dat(td, base="naca0012", cut=0.01):
    """A section whose nose has been squared off — the geometry a blunt-edge
    rule exists to outlaw. Written as a .dat so it arrives the way a user's
    own file does."""
    c = airfoils.normalize(airfoils.repaneled(base, 240)[1])
    i_le = int(np.argmin(c[:, 0]))
    up, lo = c[:i_le + 1], c[i_le:]
    yt = float(np.interp(cut, up[::-1, 0], up[::-1, 1]))
    yb = float(np.interp(cut, lo[:, 0], lo[:, 1]))
    face = np.array([[cut, y] for y in np.linspace(yt, yb, 21)])
    pts = np.vstack([up[up[:, 0] > cut], face, lo[lo[:, 0] > cut]])
    p = Path(td) / "chopped.dat"
    p.write_text("chopped\n"
                 + "\n".join(f" {x:.6f} {y:.6f}" for x, y in pts))
    return str(p)


def edge_rules(env, base=None, **over):
    """envelope_check on the EDGE stack (or an override of it)."""
    cfg = geometry.StackConfig.from_dict(
        {**(base or EDGE), **over, "rule_envelope": env})
    installed = geometry.install_stack(geometry.build_stack(cfg),
                                       cfg.ride_height_c)
    return geometry.envelope_check(installed, cfg)


def main():
    # ---- validation ----
    for bad, why in [({"max_length_mm": -5}, "negative length"),
                     ({"max_height_mm": 0}, "zero height"),
                     ({"min_ground_clearance_mm": 5000}, "clearance range"),
                     ({"max_length_mm": float("nan")}, "NaN"),
                     ({"max_height_mm": 50, "min_ground_clearance_mm": 60},
                      "empty box (clearance above height)")]:
        try:
            cfg_with(bad)
            check(f"validation rejects {why}", False)
        except ValueError:
            check(f"validation rejects {why}", True)
    cfg = cfg_with({"max_length_mm": 800, "max_height_mm": 250,
                    "min_ground_clearance_mm": 20, "x_offset_mm": -10,
                    "preset_name": "Test 2026"})
    e = cfg.rule_envelope
    check("valid envelope round-trips",
          e.max_length_mm == 800 and e.max_height_mm == 250
          and e.min_ground_clearance_mm == 20 and e.x_offset_mm == -10
          and e.preset_name == "Test 2026")
    env2 = geometry.validate_rule_envelope({"max_height_mm": 120})
    check("standalone validate_rule_envelope accepts partial envelopes",
          env2.max_height_mm == 120 and env2.max_length_mm is None)

    # ---- envelope_check ----
    plain = geometry.StackConfig.from_dict(BASE)
    installed = geometry.install_stack(geometry.build_stack(plain),
                                       plain.ride_height_c)
    check("no envelope -> no rules verdict",
          geometry.envelope_check(installed, plain) is None)

    r = geometry.geometry_report(plain)
    check("report always carries installed extents",
          all(k in r["installed_extents_c"]
              for k in ("x_min", "x_max", "y_min", "y_max")))
    ext = r["installed_extents_c"]
    mm = plain.chord_mm
    length_mm = (ext["x_max"] - ext["x_min"]) * mm
    top_mm = ext["y_max"] * mm
    bottom_mm = ext["y_min"] * mm

    # limits exactly at the measured extents: compliant (tolerance eats the
    # float jitter — a design tuned onto the rule line must pass)
    exact = cfg_with({"max_length_mm": length_mm, "max_height_mm": top_mm,
                      "min_ground_clearance_mm": bottom_mm})
    rules = geometry.envelope_check(installed, exact)
    check("exact-boundary design passes", rules["ok"],
          f"(violations: {rules['violations']})")

    # limits 1 mm inside the measured extents: three named violations
    tight = cfg_with({"max_length_mm": length_mm - 1.0,
                      "max_height_mm": top_mm - 1.0,
                      "min_ground_clearance_mm": bottom_mm + 1.0})
    rules = geometry.envelope_check(installed, tight)
    check("tight envelope raises three violations",
          not rules["ok"] and len(rules["violations"]) == 3)
    by_rule = {v["rule"]: v for v in rules["violations"]}
    check("violations carry the overshoot in mm",
          all(0.9 <= by_rule[k]["by_mm"] <= 1.1 for k in
              ("max_length_mm", "max_height_mm", "min_ground_clearance_mm")),
          f"({ {k: v['by_mm'] for k, v in by_rule.items()} })")
    check("violations name the box edge",
          by_rule["max_length_mm"]["edge"] == "length"
          and by_rule["max_height_mm"]["edge"] == "top"
          and by_rule["min_ground_clearance_mm"]["edge"] == "bottom")
    check("verdict echoes the envelope limits for the drawing",
          rules["envelope"]["max_length_mm"] == length_mm - 1.0)

    # ---- geometry_report integration ----
    rep = geometry.geometry_report(tight)
    check("report rules block present and violated",
          rep["rules"] is not None and not rep["rules"]["ok"])
    joined = " ".join(rep["warnings"])
    check("report warnings spell out rule violations in mm",
          "Rule 'max length'" in joined and "mm" in joined)
    rep_ok = geometry.geometry_report(
        cfg_with({"max_length_mm": length_mm + 50}))
    check("compliant envelope adds no warnings",
          rep_ok["rules"]["ok"]
          and not any("Rule" in w for w in rep_ok["warnings"]))

    # ---- analysis feasibility gate ----
    ev = analysis.quick_objective_eval(tight)
    check("quick eval marks rule violations infeasible",
          ev["feasible"] is False and str(ev["reason"]).startswith("rule:"),
          f"(reason: {ev.get('reason')})")
    ev_ok = analysis.quick_objective_eval(
        cfg_with({"max_length_mm": length_mm + 50}))
    check("quick eval passes a compliant design", ev_ok["feasible"] is True)

    # ---- optimizer eager refusal ----
    try:
        optimizer.Job({**BASE, "rule_envelope":
                       {"max_length_mm": length_mm - 1.0}},
                      {"target_downforce_n": 200})
        check("optimizer refuses a violating start", False)
    except ValueError as exc:
        msg = str(exc)
        check("optimizer refuses a violating start",
              "violates rule" in msg and "mm" in msg, f"({msg[:90]})")
    job = optimizer.Job({**BASE, "rule_envelope":
                         {"max_length_mm": length_mm + 50}},
                        {"target_downforce_n": 200})
    check("optimizer accepts a compliant start", job.state == "pending")

    # ---- edge-limit validation ----
    for bad, why in [({"min_le_radius_mm": -1}, "negative LE radius"),
                     ({"min_le_radius_mm": 500}, "LE radius out of range"),
                     ({"min_le_radius_mm": float("inf")}, "infinite LE radius"),
                     ({"min_te_thickness_mm": -0.5}, "negative TE floor"),
                     ({"measure_ride_height_mm": -5}, "negative measure height"),
                     ({"measure_ride_height_mm": 2000}, "measure height range"),
                     ({"le_radius_scope": "front"}, "unknown radius scope"),
                     ({"min_le_radius": 5.0}, "misspelled edge field")]:
        try:
            cfg_with(bad)
            check(f"validation rejects {why}", False)
        except ValueError:
            check(f"validation rejects {why}", True)
    full = {"max_length_mm": 700, "max_height_mm": 250,
            "min_ground_clearance_mm": 20, "x_offset_mm": -10,
            "preset_name": "Draft 2027", "min_le_radius_mm": 5.0,
            "le_radius_scope": "all", "min_te_thickness_mm": 3.0,
            "measure_ride_height_mm": 55.0}
    env_full = geometry.validate_rule_envelope(full)
    check("standalone validate_rule_envelope accepts the edge limits",
          env_full.min_le_radius_mm == 5.0 and env_full.le_radius_scope == "all"
          and env_full.min_te_thickness_mm == 3.0
          and env_full.measure_ride_height_mm == 55.0)
    # the preset endpoints persist an envelope as a plain dict and re-parse it
    round_trip = geometry.RuleEnvelopeSpec.from_dict(
        dataclasses.asdict(env_full))
    check("envelope round-trips through asdict/from_dict",
          round_trip == env_full, f"({round_trip})")
    check("edge limits default to off",
          all(getattr(geometry.RuleEnvelopeSpec(), f) is None
              for f in ("min_le_radius_mm", "min_te_thickness_mm",
                        "measure_ride_height_mm"))
          and geometry.RuleEnvelopeSpec().le_radius_scope == "frontmost")
    check("no edge limits -> no per-element verdicts",
          "elements" not in edge_rules({"max_length_mm": 700}))

    # ---- leading-edge radius, scope, and the as-built contour ----
    r_all = edge_rules({"min_le_radius_mm": 5.0, "le_radius_scope": "all"})
    by_el = {e["element"]: e for e in r_all["elements"]}
    an24 = 1.1019 * 0.24**2 * 350.0      # analytic naca0024 nose, mm
    an06 = 1.1019 * 0.06**2 * 122.5
    # the three-term nose fit reads a sqrt nose a few percent low by
    # construction (see airfoils.le_radius_fit); 8% is the measured envelope
    # over naca0006..0024, and the trade bought panel-count stability
    check("per-element LE radius measured in mm on each element's chord",
          abs(by_el["main"]["le_radius_mm"] - an24) < 0.08 * an24
          and abs(by_el["flap1"]["le_radius_mm"] - an06) < 0.08 * an06,
          f"(main {by_el['main']['le_radius_mm']} vs {an24:.2f}, "
          f"flap1 {by_el['flap1']['le_radius_mm']} vs {an06:.2f})")
    check("scope 'all' fails the sharp flap and passes the blunt main",
          by_el["main"]["le_radius_ok"] is True
          and by_el["flap1"]["le_radius_ok"] is False
          and [v["element"] for v in r_all["violations"]] == ["flap1"])
    r_front = edge_rules({"min_le_radius_mm": 5.0})
    by_front = {e["element"]: e for e in r_front["elements"]}
    # membership in elements[] means a check reached that element: an element
    # outside the scope contributes no row at all, never an empty one
    check("scope 'frontmost' checks only the leading element",
          r_front["ok"] and "le_radius_mm" in by_front["main"]
          and "flap1" not in by_front
          and all(len(e) > 1 for e in r_front["elements"]))
    # frontmost is measured, not assumed: this flap is placed a full main
    # chord forward and rotated until its nose leads the stack
    ahead = {**EDGE, "elements": [
        EDGE["elements"][0],
        {"airfoil": "naca0006", "chord_ratio": 0.35, "deflection_deg": -70,
         "dx": -1.0, "dy": 0.25}]}
    cfg_ahead = geometry.StackConfig.from_dict(ahead)
    inst_ahead = geometry.install_stack(geometry.build_stack(cfg_ahead),
                                        cfg_ahead.ride_height_c)
    lead = [float(e["coords"][:, 0].min()) for e in inst_ahead]
    r_ahead = edge_rules({"min_le_radius_mm": 5.0}, base=ahead)
    check("frontmost element is computed, not assumed to be index 0",
          lead[1] < lead[0]
          and [v["element"] for v in r_ahead["violations"]] == ["flap1"],
          f"(leading x {lead[0]:.3f} vs {lead[1]:.3f})")
    # the nose is a rigid-rotation invariant: deflecting the flap must not
    # move its radius, and neither must the stack angle
    defl = [edge_rules({"min_le_radius_mm": 5.0, "le_radius_scope": "all"},
                       elements=[EDGE["elements"][0],
                                 {**EDGE["elements"][1], "deflection_deg": d}],
                       stack_aoa_deg=a)["elements"][1]["le_radius_mm"]
            for d, a in ((0, 0), (12, 0), (30, 0), (30, 6), (45, -4))]
    check("LE radius is invariant to deflection and stack angle",
          max(defl) - min(defl) < 1e-9, f"({defl})")

    # ---- as-built: manufacturing prep is what gets measured ----
    sharp = edge_rules({"min_te_thickness_mm": 1.5})
    built = edge_rules({"min_te_thickness_mm": 1.5},
                       manufacturing={"te_gap_mm": 2.0, "te_mode": "thicken"})
    te_sharp = {e["element"]: e["te_thickness_mm"] for e in sharp["elements"]}
    te_built = {e["element"]: e["te_thickness_mm"] for e in built["elements"]}
    check("TE thickness is measured on the as-built contour",
          not sharp["ok"] and built["ok"]
          and all(te_built[k] >= 1.99 for k in te_built)
          and all(te_sharp[k] < 0.5 for k in te_sharp),
          f"(sharp {te_sharp}, built {te_built})")
    check("TE violation names the element and the shortfall",
          [(v["rule"], v["element"], v["by_mm"] > 1.4)
           for v in sharp["violations"]]
          == [("min_te_thickness", "main", True),
              ("min_te_thickness", "flap1", True)])
    r_le_built = edge_rules({"min_le_radius_mm": 5.0, "le_radius_scope": "all"},
                            manufacturing={"te_gap_mm": 2.0,
                                           "te_mode": "thicken"})
    nose_built = [e["le_radius_mm"] for e in r_le_built["elements"]]
    nose_raw = [e["le_radius_mm"] for e in r_all["elements"]]
    check("trailing-edge prep does not move the measured nose",
          all(abs(b / a - 1) < 0.01 for a, b in zip(nose_raw, nose_built)),
          f"({nose_raw} -> {nose_built})")
    warn_te = geometry.envelope_warnings(sharp)
    check("TE prose says the rules give no number",
          any("no number" in w for w in warn_te), f"({warn_te[:1]})")

    # ---- radius cost flag (advisory, never a violation) ----
    cost = edge_rules({"min_le_radius_mm": 8.0, "le_radius_scope": "all"})
    by_cost = {e["element"]: e for e in cost["elements"]}
    check("cost flag fires only where the radius eats the chord",
          by_cost["flap1"]["radius_cost_flag"] is True      # 8/122.5 = 6.5%
          and by_cost["main"]["radius_cost_flag"] is False,  # 8/350  = 2.3%
          f"(flags {[e['radius_cost_flag'] for e in cost['elements']]})")
    check("cost flag is not a violation",
          all(v["rule"] != "radius_cost_flag" for v in cost["violations"]))
    check("cost flag reaches the warning prose",
          any("more than 5% of this element's chord" in w
              for w in geometry.envelope_warnings(cost)))

    # ---- knife-edge band: the nose fit's own uncertainty is -5.8% bias plus
    # ~9% panel scatter, so a floor inside 10% of the measurement is the fit
    # talking. The label rides BESIDE the verdict, it never flips it.
    r_meas = edge_rules({"min_le_radius_mm": 1.0})["elements"][0]["le_radius_mm"]
    band = geometry.LE_RADIUS_BAND
    for floor, want_knife, want_ok in (
            (round(r_meas * (1 - 0.5 * band), 2), True, True),
            (round(r_meas * (1 + 0.5 * band), 2), True, False),
            (round(r_meas * (1 - 3 * band), 2), False, True),
            (round(r_meas * (1 + 3 * band), 2), False, False)):
        row = edge_rules({"min_le_radius_mm": floor})["elements"][0]
        check(f"knife-edge at floor {floor} mm (measured {r_meas} mm)",
              row["le_radius_knife_edge"] is want_knife
              and row["le_radius_ok"] is want_ok,
              f"(knife {row['le_radius_knife_edge']}, ok {row['le_radius_ok']})")
    knife = edge_rules({"min_le_radius_mm": round(r_meas, 2)})
    check("knife-edge does not by itself create a violation",
          all(v["rule"] != "min_le_radius" for v in knife["violations"])
          and knife["ok"] is True)
    check("knife-edge reaches the warning prose with both numbers",
          any("cannot decide this one" in w
              for w in geometry.envelope_warnings(knife)))
    ok_cost = edge_rules({"min_le_radius_mm": 8.0})
    check("an element outside the radius scope carries no cost flag",
          "flap1" not in {e["element"]: e for e in ok_cost["elements"]})

    # ---- measurement ride height: caps move, the floor does not ----
    run_h = edge_rules({})["extents_mm"]
    top0, bot0 = run_h["top"], run_h["bottom"]
    moved = edge_rules({"max_height_mm": top0 + 15.0,
                        "min_ground_clearance_mm": bot0 - 5.0,
                        "measure_ride_height_mm": 60.0})
    check("height cap is evaluated at the rule ride height",
          moved["measured_at_ride_height_mm"] == 60.0
          and abs(moved["extents_mm"]["top"] - (top0 + 30.0)) < 0.05
          and [v["rule"] for v in moved["violations"]] == ["max_height_mm"],
          f"(top {top0} -> {moved['extents_mm']['top']})")
    floor = edge_rules({"min_ground_clearance_mm": bot0 + 10.0,
                        "measure_ride_height_mm": 60.0})
    check("ground clearance stays on the configured ride height",
          floor["extents_mm"]["bottom"] == bot0
          and abs(floor["violations"][0]["by_mm"] - 10.0) < 0.05,
          f"(bottom {floor['extents_mm']['bottom']})")
    unset = edge_rules({"max_height_mm": top0 + 15.0,
                        "min_ground_clearance_mm": bot0 - 5.0})
    check("unset measurement height leaves the caps alone",
          unset["ok"] and "measured_at_ride_height_mm" not in unset
          and unset["extents_mm"]["top"] == top0)

    # ---- per-element records must not disturb the box-edge drawing ----
    box = edge_rules({"max_length_mm": 10.0, "max_height_mm": top0 - 10.0,
                      "min_ground_clearance_mm": bot0 + 10.0,
                      "min_le_radius_mm": 5.0, "le_radius_scope": "all",
                      "min_te_thickness_mm": 1.5})
    # viewport.js _drawRules keys violations by edge and reads only
    # length / top / bottom, so an "element" record must land off to the side
    by_edge = {}
    for v in box["violations"]:
        by_edge[v["edge"]] = v
    check("box edges still resolve to their own violations",
          by_edge["length"]["rule"] == "max_length_mm"
          and by_edge["top"]["rule"] == "max_height_mm"
          and by_edge["bottom"]["rule"] == "min_ground_clearance_mm")
    check("per-element records carry an edge the drawing ignores",
          all(v["edge"] == "element" for v in box["violations"]
              if v["rule"] in ("min_le_radius", "min_te_thickness"))
          and set(by_edge) == {"length", "top", "bottom", "element"})
    check("box violations come first, so the refusal message quotes one",
          box["violations"][0]["edge"] == "length")
    check("every new violation gets prose ending in a remedy",
          len(geometry.envelope_warnings(box)) >= len(box["violations"]))

    # ---- the report and the optimizer gate inherit the edge rules ----
    rep_edge = geometry.geometry_report(geometry.StackConfig.from_dict(
        {**EDGE, "rule_envelope": {"min_le_radius_mm": 5.0,
                                   "le_radius_scope": "all"}}))
    check("report carries per-element verdicts in both frames",
          rep_edge["design"][1]["rules"]["le_radius_ok"] is False
          and rep_edge["installed"][1]["rules"]["le_radius_ok"] is False)
    ev_edge = analysis.quick_objective_eval(geometry.StackConfig.from_dict(
        {**EDGE, "rule_envelope": {"min_le_radius_mm": 5.0,
                                   "le_radius_scope": "all"}}))
    check("quick eval refuses a design that fails an edge rule",
          ev_edge["feasible"] is False
          and str(ev_edge["reason"]).startswith("rule: min_le_radius"),
          f"(reason: {ev_edge.get('reason')})")
    try:
        optimizer.Job({**EDGE, "rule_envelope": {"min_le_radius_mm": 5.0,
                                                 "le_radius_scope": "all"}},
                      {"target_downforce_n": 200})
        check("optimizer refuses an edge-rule violation eagerly", False)
    except ValueError as exc:
        check("optimizer refuses an edge-rule violation eagerly",
              "min leading-edge radius" in str(exc), f"({str(exc)[:90]})")

    # ---- the verdict is a property of the section, not of the paneling ----
    # the optimizer searches 3- and 4-element stacks at 50 and 45 panels a
    # side; the rule check re-panels to its own resolution so the radius
    # cannot be measured (or missed) differently there
    env_pan = {"min_le_radius_mm": 5.0, "le_radius_scope": "all"}
    pan = {n: edge_rules(env_pan, n_panels_per_side=n)
           for n in (11, 45, 50, 70, 120, 200)}
    radii = {n: [e["le_radius_mm"] for e in r["elements"]]
             for n, r in pan.items()}
    check("LE radius is measured at every solver paneling",
          all(all(v is not None for v in vals) for vals in radii.values()),
          f"({radii})")
    check("the radius verdict does not move with the panel count",
          all(abs(v / radii[70][i] - 1) < 0.02
              for vals in radii.values() for i, v in enumerate(vals)),
          f"({radii})")
    # exactly, not approximately: the measurement re-panels to a FIXED
    # resolution, so identical sections must read bit-identical radii at
    # every solver setting — a max(user, floor) rule would drift above the
    # floor and flip a design sitting on the limit
    check("the measured radius is bit-identical at every solver paneling",
          all(vals == radii[70] for vals in radii.values()), f"({radii})")
    check("the violation set does not move with the panel count",
          all([(v["rule"], v["element"]) for v in r["violations"]]
              == [("min_le_radius", "flap1")] for r in pan.values()),
          f"({ {n: len(r['violations']) for n, r in pan.items()} })")
    ev_coarse = analysis.quick_objective_eval(geometry.StackConfig.from_dict(
        {**EDGE, "n_panels_per_side": 45, "rule_envelope": env_pan}))
    check("the coarse search resolution cannot smuggle a sharp nose past",
          ev_coarse["feasible"] is False
          and str(ev_coarse["reason"]).startswith("rule: min_le_radius"),
          f"(reason: {ev_coarse.get('reason')})")

    # ---- degradation: an unmeasurable nose is unknown, not compliant ----
    with tempfile.TemporaryDirectory(dir=ROOT) as td:
        chop = chopped_dat(td)
        cut = edge_rules({"min_le_radius_mm": 5.0},
                         elements=[{"airfoil": chop, "chord_ratio": 1.0}])
        row = cut["elements"][0]
        check("a squared-off nose is unmeasured, never a large radius",
              row["le_radius_mm"] is None and row["le_radius_ok"] is None
              and not cut["violations"], f"({row})")
        check("an unverifiable rule is published as unresolved, not as a pass",
              cut["ok"] is True
              and cut["unverified"] == [{"rule": "min_le_radius",
                                         "element": "main",
                                         "reason": "shape"}],
              f"({cut.get('unverified')})")
        warn_cut = " ".join(geometry.envelope_warnings(cut))
        check("prose names the shape, and does not blame the panel count",
              "could not be measured" in warn_cut
              and "folds back or is cut off" in warn_cut
              and "panel count" not in warn_cut, f"({warn_cut[:120]})")
    thin = edge_rules({"min_le_radius_mm": 5.0, "le_radius_scope": "all"},
                      n_panels_per_side=11)
    check("no envelope carries an unverified block when every check ran",
          "unverified" not in thin and all(e["le_radius_ok"] is not None
                                           for e in thin["elements"]))

    # ---- an undercambered nose stays measurable ----
    # fx74modsm's lower surface passes its own minimum inside the nose window;
    # trimming the window there must not throw the fit away, or the rule
    # silently stops being enforced on exactly the drooped-nose family
    drooped = [edge_rules({"min_le_radius_mm": 8.0},
                          elements=[{"airfoil": "fx74modsm",
                                     "chord_ratio": 1.0}],
                          n_panels_per_side=n)["elements"][0]
               for n in (45, 60, 70, 80, 90, 100, 160)]
    check("an undercambered nose is measured at every panel count",
          all(e["le_radius_ok"] is False for e in drooped),
          f"({[e['le_radius_mm'] for e in drooped]})")

    # ---- measurement height: the drawing is told what moved ----
    shifted = edge_rules({"max_height_mm": top0 + 100.0,
                          "measure_ride_height_mm": 60.0})
    check("the measurement shift and the running top are both published",
          shifted["measure_shift_mm"] == 30.0
          and abs(shifted["extents_mm"]["top_running"] - top0) < 0.05
          and shifted["extents_mm"]["top"] > shifted["extents_mm"]["top_running"],
          f"({shifted['extents_mm']}, shift {shifted['measure_shift_mm']})")
    note = geometry.envelope_warnings(shifted)
    check("a compliant design still says the caps were measured elsewhere",
          shifted["ok"] and any("Height limits are measured" in w
                                and "on screen" in w for w in note),
          f"({note})")
    try:
        cfg_with({"max_height_mm": 250.0, "measure_ride_height_mm": 300.0})
        check("validation rejects a measurement height above the cap", False)
    except ValueError as exc:
        check("validation rejects a measurement height above the cap",
              "no stack of any height can comply" in str(exc),
              f"({str(exc)[:80]})")

    # ---- 0 means off, and the echo says so ----
    zero = edge_rules({"min_le_radius_mm": 0.0, "min_te_thickness_mm": 0.0})
    check("a zero edge floor disables the check and is echoed as off",
          "elements" not in zero
          and zero["envelope"]["min_le_radius_mm"] is None
          and zero["envelope"]["min_te_thickness_mm"] is None)

    # ---- the TE remedy names a control the user can actually reach ----
    warn_off = " ".join(geometry.envelope_warnings(
        edge_rules({"min_te_thickness_mm": 1.5})))
    warn_on = " ".join(geometry.envelope_warnings(
        edge_rules({"min_te_thickness_mm": 3.0},
                   manufacturing={"te_gap_mm": 2.0, "te_mode": "thicken"})))
    check("with prep off the TE remedy says to turn it on",
          "Manufacturing prep is off" in warn_off
          and "turn it on and set its trailing-edge thickness to at least "
              "1.5 mm" in warn_off, f"({warn_off[:160]})")
    check("with prep on the TE remedy points at its thickness",
          "Manufacturing prep is off" not in warn_on
          and "raise the manufacturing trailing-edge thickness" in warn_on)
    check("the radius remedy keeps the permanently-attached qualifier",
          all("permanently attached leading-edge piece" in w
              for w in geometry.envelope_warnings(r_all)
              if "min leading-edge radius" in w))

    print(f"\n{sum(results)}/{len(results)} rule-envelope checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
