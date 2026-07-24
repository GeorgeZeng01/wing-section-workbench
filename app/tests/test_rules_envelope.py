"""Rule-envelope validation: the user-entered geometric rules must be
checked in one place and enforced identically by the report, the analysis
feasibility gate, and the optimizer's eager start refusal.

Run directly:  python app/tests/test_rules_envelope.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app.core import analysis, geometry, optimizer  # noqa: E402

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


def cfg_with(env):
    return geometry.StackConfig.from_dict({**BASE, "rule_envelope": env})


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

    print(f"\n{sum(results)}/{len(results)} rule-envelope checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
