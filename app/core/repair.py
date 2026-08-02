"""Auto-repair: find a nearby design without the wake-shadow collapse.

The optimizer keeps returning stacks that RANS later measures as separated.
Two things follow from that, and this module is the second one. The first is
that the optimizer should stop producing them; the second is that when one
gets through anyway, something should try to fix it rather than just label it.

WHAT THIS IS AND IS NOT. This is the CHEAP half of the repair loop: it
searches the panel model, which costs milliseconds per evaluation, and it
returns a candidate. It does NOT verify. A repaired candidate is a
hypothesis until a solver run measures it, and the module says so in its
verdict rather than implying otherwise.

THE PREMISE BELOW IS MEASURED FALSE ON AT LEAST ONE DESIGN CLASS. READ THIS
BEFORE TRUSTING A REPAIR.

The design argument was: repair needs the screen to be DIRECTIONALLY right
(opening a slot helps, closing it hurts), not accurate. Where the true line
sits is a calibration question; which way to walk is a much weaker
requirement, and it is the only one the search leans on.

A/B against converged, wall-resolved Fluent 2D solves killed that for the
09c6de53265a design:

    seed    screen shadow_min 0.5471 -> measured e2 reversed 0.3848
    repair  screen shadow_min 0.5607 -> measured e2 reversed 0.3895

The screen moved its metric up 2.5% and past its own "ok" threshold with
margin. The measured collapse did not move -- it is marginally worse. The
screen is not merely miscalibrated here, it is NOT DIRECTIONAL: walking
uphill on it does not walk uphill on the flow.

So on this design class a repair verdict of "repaired" means only that the
screen is satisfied, which has been shown to mean nothing. Two things follow.
(1) A repair MUST be solver-verified before it is believed; the verified=False
flag on every verdict is not a formality. (2) The search needs a screen that
tracks the collapse before it can work here at all.

An earlier version of this note proposed delta_cd_pct as that screen,
because it read +442.7% and +438.4% on these two while shadow_min called
both "ok". Negative controls killed it: the validated ATTACHED baselines
read +626.1% and +524.2%, higher than three of the four separated designs.
The drag column does not discriminate attachment at all -- it tracks how
far the capped polar estimate falls short of a loaded stack's real drag,
which is 4-6x whatever the flow is doing.

Untested hypothesis for WHY, worth checking before rebuilding anything: the
wake-shadow screen scopes its own validity to "s1223-class sections", and
this seed is s1223rtl / s1223 / mid53b at chord ratios 1 / 0.5 / 0.26, well
away from the 1 / 0.28 / 0.2 all-s1223 stack it was calibrated on. The screen
may not be wrong everywhere so much as silently out of scope here -- and
nothing currently warns when a design leaves the envelope it was fitted in.

WHAT IT AIMS AT. The attached band with margin, not zero reversal. The
target is SHADOW_WARN + SHADOW_HEADROOM, both shipped constants -- the
caution top plus the headroom already measured to cover paneling drift
(<0.01) and what a legitimate target chase moves the metric (~0.015). No new
threshold is introduced. Aiming AT the line would land designs on it, which
is how the current stack ended up parked 0.004 above the gate.

WHAT IT TRADES. Killing a collapse costs downforce. The trade is judged in
DRAG, because a separated section is already paying an enormous pressure-drag
penalty -- the record has a separated three-element section at a section Cd
around 4x the attached-flow estimate -- so a repair that gives up some
downforce and removes that penalty is frequently net-positive. Downforce is
therefore a BUDGET (a floor the search may not cross), and drag is the
objective among everything that clears the floor.

FAILURE IS A VERDICT. A design that cannot be repaired inside its budget is
not an error: it is a configuration whose collapse is structural, and that is
among the most useful things to know about it -- it maps where no fix exists.
It returns status "infeasible" with the best attempt attached, never an
exception.

Feasibility (element intersection, rule envelope, solver failure) comes from
analysis.quick_objective_eval, the same gate the optimizer uses, so a repair
can never propose a design the optimizer would refuse.
"""

from __future__ import annotations

import copy

import numpy as np

from . import analysis, optimizer, wake_shadow
from .geometry import StackConfig

# aim above the caution top by the already-measured headroom; both shipped
REPAIR_TARGET = wake_shadow.SHADOW_WARN + optimizer.SHADOW_HEADROOM
REPAIR_BUDGET_PCT = 5.0     # default downforce a repair may spend
REPAIR_MAX_EVALS = 900      # total across starts; a repair is not a redesign
REPAIR_STARTS = 3           # seed plus two deterministic perturbations
_MARGIN_SCALE = 0.05        # same Ue scale the shipped wall penalty uses
_W_MARGIN = 100.0           # margin dominates
_W_BUDGET = 100.0           # so does the downforce floor
_INFEASIBLE = 1e6


def _shadow_shortfall(shadow_mins, target: float) -> float:
    """How far the worst elements sit below the target, summed."""
    return sum(max(0.0, target - v) ** 2 for v in (shadow_mins or [])
               if v is not None)


def worst_shadow(shadow_mins) -> float | None:
    """The screen's reading for the stack: its weakest non-exempt element."""
    vals = [v for v in (shadow_mins or []) if v is not None]
    return min(vals) if vals else None


def _measure(cfg_d: dict) -> dict:
    """One panel-model evaluation, in the shape repair reasons about."""
    try:
        ev = analysis.quick_objective_eval(StackConfig.from_dict(cfg_d))
    except (ValueError, KeyError, TypeError) as e:
        return {"feasible": False, "reason": f"{type(e).__name__}: {e}"}
    if not ev.get("feasible"):
        return {"feasible": False, "reason": ev.get("reason", "infeasible")}
    sm = ev.get("shadow_mins") or []
    drag = float(ev["drag_n"])
    down = float(ev["downforce_n"])
    return {
        "feasible": True,
        "downforce_n": round(down, 2),
        "drag_n": round(drag, 3),
        "efficiency_ld": round(down / drag, 3) if drag > 1e-9 else None,
        "shadow_mins": [None if v is None else round(float(v), 4) for v in sm],
        "worst_shadow": (None if worst_shadow(sm) is None
                         else round(worst_shadow(sm), 4)),
    }


def _clears(m: dict, target: float, floor_n: float) -> bool:
    """Does this measurement satisfy BOTH the margin and the budget?"""
    if not m.get("feasible"):
        return False
    ws = m.get("worst_shadow")
    if ws is None:                  # nothing to clear (single element)
        return m["downforce_n"] >= floor_n
    return ws >= target and m["downforce_n"] >= floor_n


def repair(config: dict, *, budget_pct: float = REPAIR_BUDGET_PCT,
           target: float = REPAIR_TARGET,
           max_evals: int = REPAIR_MAX_EVALS,
           n_starts: int = REPAIR_STARTS,
           opts: dict | None = None) -> dict:
    """Search for a nearby design that clears the wake-shadow collapse.

    Returns a verdict dict; never raises for an unrepairable design.

    status:
      "already_clean" the seed already sits at or above the target
      "repaired"      a candidate clears the target inside the budget
      "infeasible"    nothing found inside the budget -- the collapse is
                      structural for this configuration at this cost
      "unmeasurable"  the seed itself cannot be evaluated

    The returned config is a CANDIDATE, not a verified fix. Nothing here has
    been near a solver."""
    from scipy.optimize import minimize

    if not (0.0 <= budget_pct <= 100.0):
        raise ValueError("budget_pct must be between 0 and 100")

    seed = _measure(config)
    if not seed["feasible"]:
        return {"status": "unmeasurable", "reason": seed["reason"],
                "seed": seed, "best": None, "config": None, "n_evals": 1,
                "budget_pct": budget_pct, "target": round(target, 4),
                "verified": False}

    floor_n = seed["downforce_n"] * (1.0 - budget_pct / 100.0)
    ws0 = seed["worst_shadow"]
    if ws0 is None or ws0 >= target:
        return {
            "status": "already_clean",
            "reason": ("no element carries a shadow reading (single-element "
                       "stack)" if ws0 is None else
                       f"weakest element already reads {ws0:.4f}, at or "
                       f"above the {target:.4f} target"),
            "seed": seed, "best": seed, "config": copy.deepcopy(config),
            "n_evals": 1, "budget_pct": budget_pct,
            "target": round(target, 4), "verified": False,
        }

    variables = optimizer.build_variables(config, dict(opts or {}))
    x0 = optimizer.initial_vector(config, variables)
    lo = np.array([v["lo"] for v in variables], float)
    hi = np.array([v["hi"] for v in variables], float)
    span = np.maximum(hi - lo, 1e-12)

    state = {"n": 0, "best": None, "best_cfg": None, "best_drag": None,
             "reach": None, "reach_cfg": None, "floor": floor_n}

    def evaluate(x):
        state["n"] += 1
        xc = np.clip(np.asarray(x, float), lo, hi)
        cfg_d = optimizer.apply_vector(config, variables, xc)
        m = _measure(cfg_d)
        if not m["feasible"]:
            return _INFEASIBLE, cfg_d, m
        short = _shadow_shortfall(m["shadow_mins"], target)
        fl = state["floor"]
        under = (max(0.0, (fl - m["downforce_n"]) / fl) if fl > 1e-9
                 else 0.0)
        # drag normalized to the seed, so it reads as a ratio and stays a
        # tie-break rather than competing with the two hard terms
        j = (_W_MARGIN * short / _MARGIN_SCALE ** 2
             + _W_BUDGET * under ** 2
             + m["drag_n"] / max(seed["drag_n"], 1e-9))
        # the ACCEPTED candidate is tracked separately from the objective:
        # the search may pass through designs that clear nothing, and among
        # those that clear everything the choice is drag, not J
        if _clears(m, target, state["floor"]):
            if state["best_drag"] is None or m["drag_n"] < state["best_drag"]:
                state["best"], state["best_cfg"] = m, cfg_d
                state["best_drag"] = m["drag_n"]
        # separately: the cheapest margin-clearing design at ANY cost. When
        # the budget refuses everything, the useful answer is not "no" but
        # "not at this price", and that needs a price.
        ws = m.get("worst_shadow")
        if ws is not None and ws >= target:
            if (state["reach"] is None
                    or m["downforce_n"] > state["reach"]["downforce_n"]):
                state["reach"], state["reach_cfg"] = m, cfg_d
        return j, cfg_d, m

    def run_starts(cap: int):
        """Deterministic multi-start: the seed, then fixed fractional offsets
        of the bound span. No RNG -- a repair must reproduce exactly, or its
        verdict cannot be pinned by a test."""
        def objective(x):
            if state["n"] >= cap:
                return _INFEASIBLE
            return evaluate(x)[0]

        per = max(60, (cap - state["n"]) // max(n_starts, 1))
        for s in range(max(n_starts, 1)):
            if state["n"] >= cap:
                break
            off = 0.0 if s == 0 else (0.12 * s) * (1 if s % 2 else -1)
            xs = np.clip(x0 + off * span, lo, hi)
            try:
                minimize(objective, xs, method="Nelder-Mead",
                         options={"maxfev": per, "xatol": 1e-3,
                                  "fatol": 1e-4})
            except (ValueError, np.linalg.LinAlgError):
                continue

    run_starts(max_evals)

    if state["best"] is None:
        # PRICE THE REFUSAL. The budget term steers the search away from
        # the region it forbids, so "reach" stays empty unless it is looked
        # for deliberately. A second pass with the floor dropped answers the
        # question the user actually has -- not "can this be fixed" but
        # "what would fixing it cost" -- and an unrepairable-at-any-price
        # design is a genuinely different finding from an expensive one.
        state["floor"] = 0.0
        run_starts(max_evals * 2)
        reach, rcfg = state["reach"], state["reach_cfg"]
        price = (None if reach is None else
                 round((reach["downforce_n"] / seed["downforce_n"] - 1.0)
                       * 100.0, 2))
        if reach is None:
            why = (f"no design reached the {target:.4f} target at any "
                   f"downforce cost in {state['n']} evaluations — on this "
                   f"screen the collapse is structural for this "
                   f"configuration, not a tuning miss")
        else:
            why = (f"no design inside {budget_pct:.1f}% of the seed's "
                   f"downforce reached the {target:.4f} target; the "
                   f"cheapest repair found costs {abs(price):.1f}% "
                   f"downforce, which is outside the budget, not outside "
                   f"the geometry")
        return {
            "status": "infeasible", "reason": why,
            "seed": seed, "best": None, "config": None,
            "cheapest_repair": reach,
            "cheapest_repair_config": rcfg,
            "cheapest_repair_downforce_delta_pct": price,
            "cheapest_repair_drag_delta_pct": (
                None if reach is None else
                round((reach["drag_n"] / seed["drag_n"] - 1.0) * 100.0, 2)),
            "n_evals": state["n"], "budget_pct": budget_pct,
            "target": round(target, 4), "verified": False,
        }

    best = state["best"]
    d_pct = (best["downforce_n"] / seed["downforce_n"] - 1.0) * 100.0
    dr_pct = (best["drag_n"] / seed["drag_n"] - 1.0) * 100.0
    ld = (None if not (best["efficiency_ld"] and seed["efficiency_ld"])
          else (best["efficiency_ld"] / seed["efficiency_ld"] - 1.0) * 100.0)
    return {
        "status": "repaired",
        "reason": (f"weakest element moved {ws0:.4f} -> "
                   f"{best['worst_shadow']:.4f} for {abs(d_pct):.1f}% "
                   f"{'less' if d_pct < 0 else 'more'} downforce"),
        "seed": seed, "best": best, "config": state["best_cfg"],
        "downforce_delta_pct": round(d_pct, 2),
        "drag_delta_pct": round(dr_pct, 2),
        "ld_delta_pct": None if ld is None else round(ld, 2),
        "margin_before": ws0, "margin_after": best["worst_shadow"],
        "n_evals": state["n"], "budget_pct": budget_pct,
        "target": round(target, 4),
        # nothing here has been near a solver: the screen that judged this
        # repair is the same screen that missed the collapse it repaired
        "verified": False,
        "verify_note": ("panel-model candidate only — a solver run is what "
                        "turns this into a result, and a repair the screen "
                        "believes it made but RANS still finds separated is "
                        "the most informative case there is"),
    }
