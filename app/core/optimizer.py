"""Target-downforce stack optimization.

Variables (each group can be enabled independently):
  - stack angle of attack
  - flap deflections
  - flap slot gap / overlap
  - flap chord ratios
  - each element's airfoil, chosen from a per-element candidate shortlist
    (the screener's strongest sections at that element's Reynolds number,
    with the currently selected airfoil always included). The choice is
    encoded as one continuous [0,1) dimension per element mapped onto the
    shortlist ranked by CL_max, so neighboring values are similar sections
    and the evolutionary search stays meaningful. The candidate pool is
    selectable (options["airfoil_pool"]): the screened library, the library
    plus every uploaded airfoil, or uploaded airfoils only — the latter for
    optimizing over sections the user can actually manufacture. When
    manufacturing prep specifies a buildable minimum thickness, library
    candidates thinner than that (at the element's own chord) are excluded.

Objective: reach the target downforce with minimum profile drag, subject to
soft penalties keeping the design in the healthy high-lift envelope:
  - slot gap inside a 0.8%c .. 3.5%c band
  - slot overlap inside a -1%c .. 5%c band
  - element loading below ~105% of isolated CL_max
  - realized ground-effect loading inside the model's validity allowance
  - viscous-data trust: NeuralFoil confidence below CONF_FLOOR, and loaded
    elements whose polar never stalled in the grid or whose drag lookup was
    clamped at the stall branch (the signals the evaluations already
    compute; without this the search happily rides data the surrogate
    itself distrusts — shape refinement especially, which can push camber
    until both the load and its own self-referential CL_max cap inflate)
  - hard penalty on intersecting geometry / solver failure

Every soft band is widened per-run so the STARTING design's own operating
point is penalty-free (see Job._baseline_allowance): analyze() reports that
design's downforce as the headline number for the stack, so an optimizer
that refuses its loading level can only ever hand back less downforce than
the user already has — which is exactly the failure this guards against.
Where the baseline sits inside a band, the band applies unchanged, so
searches from healthy designs are still barred from wandering into
separated-flow loading. Default search bounds are likewise widened to
include the baseline's values, keeping the starting design representable
(bounds the caller set explicitly stay hard).

Search: differential evolution (global, seeded and reproducible) or
Nelder-Mead refinement around the current design. Runs on a worker thread;
progress is polled through the job registry.
"""

from __future__ import annotations

import copy
import threading
import time
import traceback
import uuid

import numpy as np

from . import analysis
from .geometry import StackConfig

# Slot geometry as percent of chord. The optimizer searches the workable
# bounds below; the tighter "healthy starting" range shown in the UI and guide
# — gap 1.0–2.5 %c, overlap 2.0–4.0 %c — is conservative manual guidance that
# sits inside them. The gap penalty band equals the gap bounds; the overlap
# penalty band deliberately reaches 1 %c below the 0 %c search floor, so a
# fixed, slightly-negative overlap is tolerated rather than penalized (a tuned
# choice, kept as-is). Kept here so bounds and bands stay in one place.
# These are the ABSOLUTE envelopes; Job._baseline_allowance() widens each
# job's working copies so the starting design is never penalized for being
# what it already is.
GAP_WORKABLE_PCT = (0.8, 3.5)
OVERLAP_WORKABLE_PCT = (0.0, 5.0)
GAP_BAND = (GAP_WORKABLE_PCT[0] / 100.0, GAP_WORKABLE_PCT[1] / 100.0)  # == gap bounds
OVERLAP_BAND = (-0.010, 0.050)

DEFAULT_BOUNDS = {
    "stack_aoa_deg": (-4.0, 12.0),
    "deflection_deg": (0.0, 60.0),
    "slot_gap_pct": GAP_WORKABLE_PCT,
    "slot_overlap_pct": OVERLAP_WORKABLE_PCT,
    "chord_ratio": (0.15, 0.50),
}
SLOT_DEFAULTS = {"slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}


class _Cancelled(Exception):
    pass


SHORTLIST_MAX = 16
THICKNESS_MIN_MAIN = 5.0   # %c — thinner sections are structurally impractical
THICKNESS_MIN_FLAP = 3.5
AIRFOIL_POOLS = ("auto", "include_custom", "custom_only")

CAND_MAX = 6               # candidate designs returned per run
CAND_DIVERSITY = 0.12      # min normalized per-variable spread between them
CAND_TARGET_TOL = 0.03     # alternates must land within 3% of the target

# two-phase target search. Phase A ("attain") is the classic tracker; the
# run then resolves D* — the target if the clean pool reached it, else the
# clean feasible floor/ceiling — and phase B ("descend") minimizes drag
# while a deadzoned spring holds the downforce at D*. This is what fixes
# the two low-target pathologies: the early stop no longer ends the run on
# the FIRST design that hit the target (descend always runs, seeded from
# the lowest-drag on-target designs), and an unreachable-from-above target
# no longer rewards shedding downforce by sabotaging slot flow (sabotage
# carries penalty >= PEN_OK, so it can neither define D* nor seed descend).
PEN_OK = 0.5               # "no live constraint penalty" — shared gate for
                           # the early stop, candidate pools and D*
PHASE_B_FRAC = 0.35        # share of the eval budget reserved for descend
DEADZONE_F = 0.008         # descend: |downforce-D*|/D* treated as "on level"
                           # (same width as the early-stop tolerance, so the
                           # simplex can slide ALONG the on-target manifold
                           # trading the last 0.1% of tracking for drag)
DESCEND_DRAG_W = 0.30      # descend drag-weight floor (user's if higher)

# viscous-data trust penalty. Soft by construction: a screened library stack
# (confidence well above CONF_FLOOR, stalling inside the polar grid) pays
# nothing, and even a fully flagged element costs about as much as an ~11%
# target miss — enough to prefer an equally-performing trustworthy design,
# never enough to beat hitting the target (weight 60) outright.
CONF_FLOOR = 0.5           # NeuralFoil confidence below this is penalized
                           # (the screener's own confidence bar)
CONF_WEIGHT = 6.0          # x ((floor - conf) / floor)^2, per element
FLAG_BUMP = 0.75           # per LOADED element at the polar grid edge or on
                           # a clamped drag lookup
FLAG_LOAD_FRAC = analysis.LOAD_WARN
                           # "loaded": at the free-air loading fraction where
                           # the analysis itself starts warning. It must NOT
                           # be lower: in ground effect even the mild seed
                           # design runs its main element's drag lookup at
                           # the 95%-of-CL_max cap, so a looser gate would
                           # flag every realistic design and say nothing


def build_airfoil_shortlists(config: dict, cfg: StackConfig,
                             max_n: int = SHORTLIST_MAX,
                             pool: str = "auto",
                             min_chord_ratio: dict[int, float] | None = None,
                             ) -> dict[int, list[str]]:
    """Per-element candidate airfoils, ranked by CL_max at the element's Re.

    Blends the screener's strongest sections by CL_max, best L/D, and lowest
    drag at the working CL, and always keeps the currently selected airfoil
    in the running (first-class citizen even if it ranks poorly).

    pool selects where candidates come from:
      auto            screener's best library + uploaded sections (on merit)
      include_custom  as auto, but every uploaded airfoil is guaranteed a slot
      custom_only     uploaded airfoils only (the buildable set)

    When manufacturing prep sets a buildable minimum thickness, the library
    floor is raised so no candidate is thinner than that minimum at the
    element's own chord (uploads are exempt — they are the user's explicit
    choice, and analysis warnings will flag them if too thin).
    """
    from . import airfoils, screener
    if pool not in AIRFOIL_POOLS:
        raise ValueError(f"airfoil_pool must be one of {AIRFOIL_POOLS}")
    customs = [c["spec"] for c in airfoils.list_custom()]
    if pool == "custom_only" and not customs:
        raise ValueError("airfoil candidates are set to 'my uploads only' but "
                         "no airfoils have been uploaded — load .dat files "
                         "first or switch the candidate source.")
    mfg = cfg.manufacturing
    lists: dict[int, list[str]] = {}
    for i, spec in enumerate(cfg.elements):
        t_min = THICKNESS_MIN_MAIN if i == 0 else THICKNESS_MIN_FLAP
        ratio = 1.0 if i == 0 else spec.chord_ratio
        # when the chord itself is being optimized it can shrink below the
        # current value — the buildable floor must hold at the smallest
        # chord the search may pick, not just the starting one
        if min_chord_ratio and i in min_chord_ratio:
            ratio = min(ratio, float(min_chord_ratio[i]))
        c_mm = ratio * cfg.chord_mm
        if mfg is not None:
            # a candidate thinner than the TE it must carry is a plate, not
            # an airfoil — floor at the TE thickness as well as the explicit
            # buildable minimum (with headroom so the section is an airfoil,
            # not a plate that barely clears the TE)
            floor_mm = max(mfg.min_thickness_mm, 1.5 * mfg.te_gap_mm)
            if floor_mm > 0:
                t_min = max(t_min, floor_mm / c_mm * 100.0)
        current = config["elements"][i].get("airfoil", "s1223")
        cl_of: dict[str, float] = {}
        picks: list[str] = []

        if pool == "custom_only":
            picks = list(customs)
        else:
            try:
                rows = screener.screen(cfg.element_re(i), cfg.ncrit, cl_ref=1.5,
                                       thickness_pct_min=t_min,
                                       thickness_pct_max=22.0)
            except Exception:
                rows = []
            cl_of = {r["spec"]: r["CL_max"] for r in rows}

            def add(seq, n):
                for r in seq[:n]:
                    if r["spec"] not in picks:
                        picks.append(r["spec"])

            add(sorted(rows, key=lambda r: -r["CL_max"]), 9)
            add(sorted(rows, key=lambda r: -r["LD_max"]), 5)
            add(sorted([r for r in rows if r.get("CD_at_CL_ref")],
                       key=lambda r: r["CD_at_CL_ref"]), 5)
            picks = picks[:max_n]
            if pool == "include_custom":
                picks += [s for s in customs if s not in picks]
        if pool != "custom_only" and current not in picks:
            picks.append(current)
        # rank order gives the [0,1) encoding its meaning; specs the screener
        # filtered out (uploads below its confidence bar, the current pick)
        # get their CL_max measured directly
        for s in picks:
            if s not in cl_of:
                row = screener.metrics_for(s, cfg.element_re(i), cfg.ncrit)
                cl_of[s] = row["CL_max"] if row else -1e9
        picks.sort(key=lambda s: -cl_of.get(s, -1e9))
        lists[i] = picks
    return lists


SHAPE_KEYS = ("shape_b25", "shape_b55", "shape_b80", "shape_ts")


def _clean_bounds(user_bounds) -> dict:
    """Merge user bounds over the defaults, coercing to (lo, hi) float pairs.

    Malformed entries raise ValueError here — at job creation, where the API
    can turn them into a 422 — instead of surfacing as a crashed search
    thread minutes later."""
    out = dict(DEFAULT_BOUNDS)
    if user_bounds is not None and not isinstance(user_bounds, dict):
        raise ValueError("options['bounds'] must be an object of "
                         "{key: (lo, hi)} pairs")
    for k, v in (user_bounds or {}).items():
        if k not in DEFAULT_BOUNDS:
            raise ValueError(f"unknown bounds key {k!r} — expected one of "
                             f"{sorted(DEFAULT_BOUNDS)}")
        try:
            lo, hi = float(v[0]), float(v[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ValueError(f"bounds[{k!r}] must be a (lo, hi) number pair")
        if not (np.isfinite(lo) and np.isfinite(hi) and lo < hi):
            raise ValueError(f"bounds[{k!r}]: need finite lo < hi")
        out[k] = (lo, hi)
    return out


def build_variables(config: dict, opts: dict) -> list[dict]:
    from . import shaping
    n_flaps = len(config["elements"]) - 1
    bounds = _clean_bounds(opts.get("bounds"))
    user_keys = set(opts.get("bounds") or {})

    def span(key: str, current) -> tuple[float, float]:
        # the starting design must be representable, or "optimize" can only
        # lose it: default bounds stretch to include the current value.
        # Bounds the caller set explicitly stay hard limits.
        lo, hi = bounds[key]
        if current is not None and key not in user_keys:
            lo, hi = min(lo, float(current)), max(hi, float(current))
        return lo, hi

    variables = []
    if opts.get("opt_stack_aoa", True):
        lo, hi = span("stack_aoa_deg", config.get("stack_aoa_deg"))
        variables.append({"key": "stack_aoa_deg", "elem": None,
                          "lo": lo, "hi": hi})
    if opts.get("opt_airfoils", False):
        for i in range(0, n_flaps + 1):
            variables.append({"key": "airfoil_idx", "elem": i,
                              "lo": 0.0, "hi": 1.0})
    if opts.get("opt_shape", False):
        from . import airfoils
        mfg_d = config.get("manufacturing") or {}
        floor_mm = max(float(mfg_d.get("min_thickness_mm") or 0.0),
                       1.5 * float(mfg_d.get("te_gap_mm") or 0.0))
        chord_mm = float(config.get("chord_mm", 350.0))
        for i in range(0, n_flaps + 1):
            # re-optimizing an already-shaped design: its current shape
            # params must be representable (initial_vector clips to bounds,
            # which would silently move the start) — same do-no-harm rule
            # as span() above
            seed = config["elements"][i].get("_shape_x0")
            for k_i, k in enumerate(SHAPE_KEYS[:3]):
                lo, hi = shaping.BOUNDS_BUMP
                if seed is not None:
                    lo = min(lo, float(seed[k_i]))
                    hi = max(hi, float(seed[k_i]))
                variables.append({"key": k, "elem": i, "lo": lo, "hi": hi})
            ts_lo = shaping.BOUNDS_TS[0]
            ts_hi = shaping.BOUNDS_TS[1]
            if seed is not None:
                ts_lo = min(ts_lo, float(seed[3]))
                ts_hi = max(ts_hi, float(seed[3]))
            if floor_mm > 0:
                # thinning must not push the section below the buildable
                # minimum (or into plate territory vs the TE gap)
                try:
                    _, cc = airfoils.repaneled(
                        config["elements"][i].get("airfoil", "s1223"), 60)
                    ratio = (1.0 if i == 0
                             else float(config["elements"][i].get(
                                 "chord_ratio", 0.3)))
                    tmax_mm = (airfoils.geometry_info(cc)["max_thickness"]
                               * ratio * chord_mm)
                    if tmax_mm > 0:
                        # the buildable floor is an explicit user constraint
                        # — it outranks seed representability
                        ts_lo = min(max(ts_lo, floor_mm / tmax_mm),
                                    ts_hi - 0.01)
                except Exception:
                    pass
            variables.append({"key": "shape_ts", "elem": i,
                              "lo": ts_lo, "hi": ts_hi})
    for i in range(1, n_flaps + 1):
        e = config["elements"][i]
        if opts.get("opt_deflections", True):
            lo, hi = span("deflection_deg", e.get("deflection_deg"))
            variables.append({"key": "deflection_deg", "elem": i,
                              "lo": lo, "hi": hi})
        if opts.get("opt_positions", True):
            lo, hi = span("slot_gap_pct", e.get("slot_gap_pct"))
            variables.append({"key": "slot_gap_pct", "elem": i,
                              "lo": lo, "hi": hi})
            lo, hi = span("slot_overlap_pct", e.get("slot_overlap_pct"))
            variables.append({"key": "slot_overlap_pct", "elem": i,
                              "lo": lo, "hi": hi})
        if opts.get("opt_chords", False):
            lo, hi = span("chord_ratio", e.get("chord_ratio"))
            variables.append({"key": "chord_ratio", "elem": i,
                              "lo": lo, "hi": hi})
    if not variables:
        raise ValueError("no optimization variables enabled")
    return variables


def decode_airfoil(x: float, shortlist: list[str]) -> str:
    n = len(shortlist)
    return shortlist[min(int(float(x) * n), n - 1)]


def apply_vector(config: dict, variables: list[dict], x: np.ndarray,
                 shortlists: dict[int, list[str]] | None = None) -> dict:
    cfg = copy.deepcopy(config)
    shape: dict[int, dict[str, float]] = {}
    for var, val in zip(variables, x):
        v = float(val)
        if var["key"] == "airfoil_idx":
            lst = (shortlists or {}).get(var["elem"]) or []
            if lst:
                cfg["elements"][var["elem"]]["airfoil"] = decode_airfoil(v, lst)
        elif var["key"] in SHAPE_KEYS:
            shape.setdefault(var["elem"], {})[var["key"]] = v
        elif var["elem"] is None:
            cfg[var["key"]] = v
        else:
            cfg["elements"][var["elem"]][var["key"]] = v
    # shape params wrap whatever base airfoil the element ended up with
    # (including one just decoded from the shortlist)
    if shape:
        from . import shaping
        for i, p in shape.items():
            e = cfg["elements"][i]
            e["airfoil"] = shaping.derived_spec(
                e["airfoil"],
                [p.get("shape_b25", 0.0), p.get("shape_b55", 0.0),
                 p.get("shape_b80", 0.0)],
                p.get("shape_ts", 1.0))
    for e in cfg["elements"]:
        e.pop("_shape_x0", None)
    return cfg


def initial_vector(config: dict, variables: list[dict],
                   shortlists: dict[int, list[str]] | None = None) -> np.ndarray:
    x0 = []
    for var in variables:
        if var["key"] == "airfoil_idx":
            lst = (shortlists or {}).get(var["elem"]) or []
            cur = config["elements"][var["elem"]].get("airfoil")
            pos = lst.index(cur) if cur in lst else 0
            x0.append((pos + 0.5) / max(len(lst), 1))
            continue
        if var["key"] in SHAPE_KEYS:
            seed = config["elements"][var["elem"]].get("_shape_x0")
            idx = SHAPE_KEYS.index(var["key"])
            v = (seed[idx] if seed else (1.0 if var["key"] == "shape_ts"
                                         else 0.0))
            x0.append(np.clip(float(v), var["lo"], var["hi"]))
            continue
        if var["elem"] is None:
            v = config.get(var["key"], 0.0)
        else:
            v = config["elements"][var["elem"]].get(var["key"])
            if v is None:
                v = SLOT_DEFAULTS.get(var["key"], 0.0)
        x0.append(np.clip(float(v), var["lo"], var["hi"]))
    return np.array(x0)


def _band_penalty(value: float, band: tuple[float, float], scale: float) -> float:
    lo, hi = band
    if value < lo:
        return ((lo - value) / scale) ** 2
    if value > hi:
        return ((value - hi) / scale) ** 2
    return 0.0


def _confidence_penalty(ev: dict) -> float:
    """Trust penalty for one evaluation: low NeuralFoil confidence anywhere,
    plus a fixed bump per loaded element whose viscous limit is a grid-edge
    lower bound or whose drag lookup clamped at the stall branch."""
    pen = 0.0
    for conf, frac, edge, capped in zip(
            ev.get("confidences", ()), ev.get("fracs", ()),
            ev.get("at_grid_edge", ()), ev.get("cd_capped", ())):
        if conf < CONF_FLOOR:
            pen += CONF_WEIGHT * ((CONF_FLOOR - conf) / CONF_FLOOR) ** 2
        if (edge or capped) and frac > FLAG_LOAD_FRAC:
            pen += FLAG_BUMP
    return pen


class Job:
    def __init__(self, config: dict, options: dict):
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(config)
        # pristine copy for the baseline-allowance evaluation: shape specs
        # stay wrapped here even when opt_shape unwraps them below, so the
        # reference loading is that of the design as the user has it
        self.baseline_config = copy.deepcopy(config)
        # validate the numeric options eagerly: a bogus budget or target is a
        # client error the API should 422, not a background-thread crash
        import math as _math
        for key, default, lo, hi in (
                ("target_downforce_n", 200.0, -1e6, 1e6),
                ("drag_weight", 0.10, 0.0, 10.0)):
            try:
                v = float(options.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f"options[{key!r}] must be a number")
            # NaN/Infinity survive float() and would poison every objective
            # evaluation into a garbage "done" result
            if not (_math.isfinite(v) and lo <= v <= hi):
                raise ValueError(f"options[{key!r}] must be a finite number "
                                 f"in {lo}..{hi}")
        try:
            budget = int(options.get("budget", 1500))
        except (TypeError, ValueError):
            raise ValueError("options['budget'] must be an integer")
        if not (1 <= budget <= 200_000):
            raise ValueError("options['budget'] must be between 1 and 200000")
        # the UI shipped "refine" as the local mode's value for a while and
        # anything non-"global" used to fall through to the refinement path,
        # so the eager validation below must keep accepting it as an alias
        if options.get("mode") == "refine":
            options = {**options, "mode": "local"}
        if options.get("mode", "global") not in ("global", "local"):
            raise ValueError("options['mode'] must be 'global' or 'local' "
                             "('refine' is accepted as an alias for 'local')")
        # every element spec must resolve NOW — a job whose every evaluation
        # would fail (e.g. a custom airfoil lost to a server restart) must be
        # rejected up front, not finish 'done' with nothing to show
        from . import airfoils
        for i, e in enumerate(config.get("elements", [])):
            spec = e.get("airfoil", "s1223")
            try:
                airfoils.resolve(spec)
            except (KeyError, ValueError) as exc:
                raise ValueError(f"element {i + 1}: airfoil spec {spec!r} "
                                 f"cannot be resolved ({exc})")
        # rule envelope: a start that already breaks the rules refuses
        # eagerly with a plain message. Rules are legality, not preference —
        # unlike the soft bands there is no do-no-harm widening for them,
        # so a violating start could never produce a feasible evaluation.
        if config.get("rule_envelope"):
            from . import geometry as geo_mod
            cfg0 = StackConfig.from_dict(config)
            try:
                installed0 = geo_mod.install_stack(
                    geo_mod.build_stack(cfg0), cfg0.ride_height_c)
            except Exception:
                installed0 = None   # geometry trouble surfaces per-eval
            rules0 = (geo_mod.envelope_check(installed0, cfg0)
                      if installed0 is not None else None)
            if rules0 is not None and not rules0["ok"]:
                v = rules0["violations"][0]
                label = geo_mod._RULE_LABELS.get(v["rule"], v["rule"])
                more = (f" (and {len(rules0['violations']) - 1} more rule"
                        f"{'s' if len(rules0['violations']) > 2 else ''})"
                        if len(rules0["violations"]) > 1 else "")
                raise ValueError(
                    f"the starting design already violates rule '{label}' "
                    f"by {v['by_mm']:.1f} mm{more} — fix the design or "
                    f"relax the envelope before optimizing")
        # re-optimizing an already-shaped design: unwrap the shape spec and
        # seed the shape variables from it, so refinement continues from the
        # current shape instead of stacking a second modification on top
        if options.get("opt_shape"):
            from . import shaping
            for e in self.config.get("elements", []):
                a = str(e.get("airfoil", ""))
                al = a.lower()
                if al.startswith("shape:"):
                    b, ts, base = shaping.parse(a)
                    e["airfoil"] = base
                    e["_shape_x0"] = [*b, ts]
                elif al.startswith("mfg:"):
                    # an explicit mfg: layer can wrap a shape: layer
                    # (mfg:...:shape:...:base); strip the inner shape and
                    # re-seed, keeping the mfg wrapper on the un-shaped base —
                    # else apply_vector would stack a second shape: layer and
                    # the resolver would reject the nested spec
                    from . import manufacturing as mfg_mod
                    try:
                        mode, gap_c, mbase = mfg_mod.parse(a)
                    except ValueError:
                        continue
                    if str(mbase).lower().startswith("shape:"):
                        b, ts, sbase = shaping.parse(mbase)
                        e["airfoil"] = mfg_mod.derived_spec(sbase, gap_c, mode)
                        e["_shape_x0"] = [*b, ts]
        self.options = options
        self.state = "pending"     # pending | running | finalizing | done |
                                   # failed | cancelled
        self.error = None
        self.history: list[dict] = []
        self.n_error = 0           # evaluations that raised
        self.n_infeasible = 0      # evaluations rejected as infeasible
        self.last_error = None     # last exception message from objective()
        self.last_infeasible = None
        self.best = None           # {"x", "J", "downforce_n", "drag_n"}
        self.best_config = None
        self.result = None         # full analysis of the best design
        self.dstar = None          # achievable downforce level, set between
                                   # the attain and descend phases
        self.target_note = None    # None | "unreachable_low" (target below
                                   # the clean floor) | "unreachable_high"
        self._obj_phase = "attain"  # attain | descend (objective() branch)
        self.archive: list[dict] = []   # every feasible evaluation (for
                                        # candidate selection at the end)
        self.candidates = None     # ranked diverse designs, set on finish
        self.n_eval = 0
        self.t_start = None
        self.t_end = None
        self.phase = None          # screening | searching | refining | finalizing
        self.plan_total = None     # planned evaluation count (progress basis)
        self.shortlists: dict[int, list[str]] = {}
        # soft-penalty bands, widened by _baseline_allowance() at run start
        # so the starting design is penalty-free (None/absolute until then)
        n_flaps = max(0, len(self.config.get("elements", [])) - 1)
        self.load_band: list[float] | None = None
        self.ground_band: list[float] | None = None
        self.conf_pen0 = 0.0   # baseline's own trust penalty (see objective)
        self.gap_bands: list[tuple[float, float]] = [GAP_BAND] * n_flaps
        self.overlap_bands: list[tuple[float, float]] = [OVERLAP_BAND] * n_flaps
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.variables = build_variables(self.config, options)
        self.airfoil_pool = str(options.get("airfoil_pool", "auto"))
        if any(v["key"] == "airfoil_idx" for v in self.variables):
            if self.airfoil_pool not in AIRFOIL_POOLS:
                raise ValueError(f"airfoil_pool must be one of {AIRFOIL_POOLS}")
            if self.airfoil_pool == "custom_only":
                from . import airfoils
                if not airfoils.list_custom():
                    raise ValueError(
                        "airfoil candidates are set to 'my uploads only' but "
                        "no airfoils have been uploaded — load .dat files "
                        "first or switch the candidate source.")

    # ---- objective ----

    def objective(self, x: np.ndarray) -> float:
        if self._cancel.is_set():
            raise _Cancelled()
        self.n_eval += 1
        cfg_dict = apply_vector(self.config, self.variables, x, self.shortlists)
        target = float(self.options.get("target_downforce_n", 200.0))
        w_drag = float(self.options.get("drag_weight", 0.10))
        try:
            cfg = StackConfig.from_dict({**cfg_dict,
                                         "n_panels_per_side": self._opt_panels})
            ev = analysis.quick_objective_eval(cfg)
        except Exception as exc:
            self.n_error += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return 60.0
        if not ev["feasible"]:
            self.n_infeasible += 1
            self.last_infeasible = ev.get("reason")
            return 50.0

        f_err = (ev["downforce_n"] - target) / max(abs(target), 1.0)
        # weight 40: at the old 8.0 the search happily settled 30-40% past
        # isolated CL_max from HEALTHY starts — separated-flow fantasy
        # designs. 40.0 makes a 15% loading overshoot cost about as much as
        # missing the target by 10%, so an honest miss beats a fake hit.
        # The band tops come from _baseline_allowance(): each element may
        # carry max(105% CL_max, its own baseline loading + headroom), so a
        # start that already runs hot is penalized only for getting HOTTER,
        # never for matching itself — an absolute cap here made every run
        # from an aggressive design land below its baseline downforce.
        if self.load_band:
            load_excess = sum(max(0.0, f - top) ** 2
                              for f, top in zip(ev["fracs"], self.load_band))
        else:
            load_excess = ev["load_excess"]
        j_pen = 40.0 * load_excess
        # keep the search out of the regime the model itself calls
        # unreliable: realized ground-effect loading past the allowance —
        # baseline-relative for the same do-no-harm reason as above
        if self.ground_band:
            j_pen += 25.0 * sum(
                max(0.0, f / top - 1.0) ** 2
                for f, top in zip(ev["fracs_ground"], self.ground_band))
        else:
            j_pen += 25.0 * ev.get("load_excess_ground", 0.0)
        for g, band in zip(ev["gaps"], self.gap_bands):
            j_pen += 2.0 * _band_penalty(g, band, 0.005)
        for o, band in zip(ev["overlaps"], self.overlap_bands):
            j_pen += 2.0 * _band_penalty(o, band, 0.01)
        # trust penalty, baseline-relative like the loading bands: a start
        # that already sits on low-confidence data (a re-optimized shaped
        # design) is penalized only for leaning HARDER on it, never for
        # matching itself — a standing offset would break _target_reached()
        # and the candidates' penalty gate for the whole run
        j_pen += max(0.0, _confidence_penalty(ev) - self.conf_pen0)
        if self._obj_phase == "descend" and self.dstar is not None:
            # descend: hold the achievable level D* inside a small deadzone
            # and spend the freedom on drag. Outside the deadzone the same
            # 60-weight spring as the tracker takes over, so the simplex
            # cannot buy drag by drifting off the level.
            scale = max(abs(self.dstar), 1.0)
            f_dev = max(0.0, abs(ev["downforce_n"] - self.dstar) / scale
                        - DEADZONE_F)
            j = (60.0 * f_dev ** 2
                 + max(w_drag, DESCEND_DRAG_W) * ev["drag_n"] / scale * 10.0
                 + j_pen)
        else:
            # the target term must dominate realistic drag savings inside
            # ~1% of target, or induced drag (which falls with downforce^2)
            # would pull the optimum a few percent under the requested load
            j = (60.0 * f_err ** 2
                 + w_drag * ev["drag_n"] / max(abs(target), 1.0) * 10.0
                 + j_pen)

        with self._lock:
            self.archive.append({"x": [round(float(v), 5) for v in x],
                                 "J": float(j), "penalty": float(j_pen),
                                 "downforce_n": round(ev["downforce_n"], 1),
                                 "drag_n": round(ev["drag_n"], 2),
                                 "frac_max": round(max(ev["fracs"]), 3)
                                 if ev.get("fracs") else None,
                                 "phase": self._obj_phase,
                                 "panels": self._opt_panels})
            if self.best is None or j < self.best["J"]:
                self.best = {"x": [round(float(v), 5) for v in x],
                             "J": float(j), "penalty": round(float(j_pen), 3),
                             "downforce_n": round(ev["downforce_n"], 1),
                             "drag_n": round(ev["drag_n"], 2)}
                if self.shortlists:
                    self.best["airfoils"] = [e.get("airfoil")
                                             for e in cfg_dict["elements"]]
                self.history.append({"eval": self.n_eval, "J": float(j),
                                     "downforce_n": round(ev["downforce_n"], 1),
                                     "drag_n": round(ev["drag_n"], 2),
                                     "t": round(time.time() - self.t_start, 1)})
        return j

    # ---- candidate selection ----

    def _distance(self, a: dict, b: dict, spans: list[float]) -> float:
        """Design distance between two archive entries in [0, 1].

        Airfoil choices compare by decoded spec (the continuous encoding is
        an artifact) — a different section is maximally different; everything
        else by worst normalized variable distance."""
        dmax = 0.0
        for v, xa, xb, s in zip(self.variables, a["x"], b["x"], spans):
            if v["key"] == "airfoil_idx":
                lst = self.shortlists.get(v["elem"]) or []
                if lst and decode_airfoil(xa, lst) != decode_airfoil(xb, lst):
                    return 1.0
            else:
                dmax = max(dmax, abs(float(xa) - float(xb)) / s)
        return dmax

    def _rescore(self, a: dict) -> float:
        """An archive entry's value under the FINAL (descend) objective.

        The archive mixes attain-phase and descend-phase J values, which
        are not comparable; selection therefore re-scores every entry from
        its stored downforce/drag/penalty — no re-evaluation needed."""
        dstar = (self.dstar if self.dstar is not None
                 else float(self.options.get("target_downforce_n", 200.0)))
        w_drag = float(self.options.get("drag_weight", 0.10))
        scale = max(abs(dstar), 1.0)
        f_dev = max(0.0, abs(a["downforce_n"] - dstar) / scale - DEADZONE_F)
        return (60.0 * f_dev ** 2
                + max(w_drag, DESCEND_DRAG_W) * a["drag_n"] / scale * 10.0
                + a["penalty"])

    def _resolve_dstar(self) -> None:
        """The downforce level the descend phase holds, set between phases.

        Reachable targets keep D* = target. A target below what the CLEAN
        pool can shed to (loading variables pinned, nothing legitimate left
        to give) resolves D* to that clean floor; the mirror case resolves
        to the clean ceiling. Sabotaged designs cannot define the level:
        the pool is gated on penalty < PEN_OK, and slot-flow abuse carries
        band/trust penalties. D* is measured at the attain phase's paneling
        — the descend deadzone absorbs the coarse-vs-full bias, and the
        candidates re-analyze at full fidelity anyway."""
        target = float(self.options.get("target_downforce_n", 200.0))
        with self._lock:
            pool = [a for a in self.archive if a["penalty"] < PEN_OK]
        self.dstar = target
        self.target_note = None
        if not pool:
            return
        tol = max(CAND_TARGET_TOL * abs(target), 1.0)
        dns = [a["downforce_n"] for a in pool]
        if any(abs(d - target) <= tol for d in dns):
            return
        if min(dns) > target + tol:
            self.dstar = float(min(dns))
            self.target_note = "unreachable_low"
        elif max(dns) < target - tol:
            self.dstar = float(max(dns))
            self.target_note = "unreachable_high"

    def _descend_starts(self, n: int, x_fallback) -> list:
        """Descend seeds: the lowest-drag clean archive entries already on
        D*, diversified so multi-start explores different basins of the
        on-level manifold instead of polishing one point three times."""
        with self._lock:
            pool = [a for a in self.archive if a["penalty"] < PEN_OK]
        tol = max(CAND_TARGET_TOL * abs(self.dstar or 1.0), 1.0)
        on = sorted((a for a in pool
                     if abs(a["downforce_n"] - (self.dstar or 0.0)) <= tol),
                    key=lambda a: a["drag_n"])
        if not on:
            if self.best is not None:
                return [np.array(self.best["x"])]
            return [np.array(x_fallback)]
        spans = [max(v["hi"] - v["lo"], 1e-9) for v in self.variables]
        picked = [on[0]]
        for a in on[1:]:
            if len(picked) >= n:
                break
            if min(self._distance(a, p, spans) for p in picked) \
                    > CAND_DIVERSITY:
                picked.append(a)
        for a in on[1:]:   # top up in plain drag order if diversity ran dry
            if len(picked) >= n:
                break
            if a not in picked:
                picked.append(a)
        return [np.array(a["x"]) for a in picked]

    def _select_candidates(self) -> list[dict]:
        """The best design plus up to CAND_MAX-1 alternates from everything
        the search evaluated: on the achievable level D*, nearly as good
        under the final objective, and chosen farthest-point-first so they
        are genuinely different designs rather than jitter around the
        optimum."""
        with self._lock:
            archive = list(self.archive)
        if not archive:
            return []
        dstar = (self.dstar if self.dstar is not None
                 else float(self.options.get("target_downforce_n", 200.0)))
        spans = [max(v["hi"] - v["lo"], 1e-9) for v in self.variables]
        by_j = sorted(archive, key=self._rescore)
        # the winner must come from full-fidelity evaluations when any exist
        # — the coarse search paneling carries a bias larger than the drag
        # differences between near-optimal designs
        full = [a for a in by_j if a.get("panels") == self._full_panels]
        best = full[0] if full else by_j[0]
        tol = max(CAND_TARGET_TOL * abs(dstar), 1.0)
        good = [a for a in by_j
                if abs(a["downforce_n"] - dstar) <= tol
                and a["penalty"] < PEN_OK]
        best_j = self._rescore(best)
        j_slack = best_j + max(0.15 * abs(best_j), 0.02)
        pool = ([a for a in good if self._rescore(a) <= j_slack]
                or good[:100] or by_j[:100])
        picked = [best]
        while len(picked) < CAND_MAX:
            cand, cd = None, CAND_DIVERSITY
            for a in pool:
                d = min(self._distance(a, p, spans) for p in picked)
                if d > cd:
                    cand, cd = a, d
            if cand is None:
                break
            picked.append(cand)
        return picked

    def _finalize_candidates(self) -> list[dict]:
        target = float(self.options.get("target_downforce_n", 200.0))
        out = []
        for rank, a in enumerate(self._select_candidates(), 1):
            cfg_c = apply_vector(self.config, self.variables,
                                 np.array(a["x"]), self.shortlists)
            # J is the FINAL-objective re-score: the archive mixes phase
            # objectives, and "candidate #1 has the lowest J" must stay true
            entry = {"rank": rank, "J": round(self._rescore(a), 4),
                     "downforce_n": a["downforce_n"], "drag_n": a["drag_n"],
                     "x": a["x"], "config": cfg_c}
            if self.shortlists:
                entry["airfoils"] = [e.get("airfoil")
                                     for e in cfg_c["elements"]]
            try:
                r = analysis.analyze(StackConfig.from_dict(cfg_c),
                                     include_geometry=False)
                els = r["elements"]
                conf_min = min(e["nf_confidence"] for e in els)
                entry["summary"] = {
                    "downforce_n": r["forces"]["downforce_n"],
                    "drag_total_n": r["forces"]["drag_total_n"],
                    "efficiency_ld": r["forces"]["efficiency_ld"],
                    "warnings": len(r["warnings"]),
                    # trust flags, same gates as the objective's penalty —
                    # the UI badges that say which winners to distrust
                    # before RANS
                    "confidence_min": round(conf_min, 3),
                    "low_confidence": bool(conf_min < CONF_FLOOR),
                    "near_stall": any(
                        (e["cd_lookup_capped"] or e["cl_max_at_grid_edge"])
                        and e["loading_fraction"] > FLAG_LOAD_FRAC
                        for e in els),
                }
            except Exception:
                entry["summary"] = None
            # judge "on target" by the full-resolution re-analysis — the
            # search itself runs at coarser paneling whose bias can exceed
            # the target tolerance on big stacks
            ref_dn = (entry["summary"]["downforce_n"] if entry["summary"]
                      else a["downforce_n"])
            entry["on_target"] = bool(
                abs(ref_dn - target) <= max(CAND_TARGET_TOL * abs(target), 1.0))
            if self.dstar is not None:
                entry["at_dstar"] = bool(
                    abs(ref_dn - self.dstar)
                    <= max(CAND_TARGET_TOL * abs(self.dstar), 1.0))
            out.append(entry)
        return out

    # ---- runner ----

    def _baseline_allowance(self) -> None:
        """Widen the soft-penalty bands to admit the starting design.

        analyze() reports the current design's downforce as the number for
        this stack (with warnings at most), so the optimizer must never
        refuse the baseline's own operating point — an absolute envelope
        made every run from an aggressive design land below what the user
        already had. Elements and slots the baseline runs inside the
        absolute bands keep those bands; anything it runs beyond gets its
        own value plus a little headroom (paneling bias between the search
        and full-fidelity phases) as the allowance. Falls back to the
        absolute bands when the baseline itself cannot be evaluated."""
        try:
            ev0 = analysis.quick_objective_eval(StackConfig.from_dict(
                {**self.baseline_config,
                 "n_panels_per_side": self._opt_panels}))
        except Exception:
            return
        if not ev0.get("feasible"):
            return
        self.load_band = [max(1.05, f + 0.03) for f in ev0["fracs"]]
        self.ground_band = [max(analysis.GROUND_CL_ALLOWANCE, f + 0.05)
                            for f in ev0["fracs_ground"]]
        self.conf_pen0 = _confidence_penalty(ev0)
        self.gap_bands = [GAP_BAND if g is None else
                          (min(GAP_BAND[0], g - 5e-4),
                           max(GAP_BAND[1], g + 5e-4))
                          for g in ev0["gaps"]]
        self.overlap_bands = [OVERLAP_BAND if o is None else
                              (min(OVERLAP_BAND[0], o - 5e-4),
                               max(OVERLAP_BAND[1], o + 5e-4))
                              for o in ev0["overlaps"]]

    def _floor_shape_ts_bounds(self):
        """With airfoil selection active, the thickness-scale lower bound
        must hold for EVERY shortlist candidate, not just the currently
        selected airfoil — otherwise thinning a thinner decoded candidate
        can drop below the buildable minimum. Recomputed from the thinnest
        candidate once the shortlists exist."""
        from . import airfoils, shaping
        mfg_d = self.config.get("manufacturing") or {}
        floor_mm = max(float(mfg_d.get("min_thickness_mm") or 0.0),
                       1.5 * float(mfg_d.get("te_gap_mm") or 0.0))
        if floor_mm <= 0 or not self.shortlists:
            return
        chord_mm = float(self.config.get("chord_mm", 350.0))
        lo_ratio = {v["elem"]: v["lo"] for v in self.variables
                    if v["key"] == "chord_ratio"}
        for v in self.variables:
            if v["key"] != "shape_ts" or v["elem"] not in self.shortlists:
                continue
            i = v["elem"]
            ratio = 1.0 if i == 0 else float(lo_ratio.get(
                i, self.config["elements"][i].get("chord_ratio", 0.3)))
            t_min = None
            for spec in self.shortlists[i]:
                try:
                    _, cc = airfoils.repaneled(spec, 60)
                    t = airfoils.geometry_info(cc)["max_thickness"]
                    t_min = t if t_min is None else min(t_min, t)
                except Exception:
                    continue
            if not t_min:
                continue
            t_min_mm = t_min * ratio * chord_mm
            if t_min_mm > 0:
                v["lo"] = min(max(v["lo"], floor_mm / t_min_mm),
                              shaping.BOUNDS_TS[1] - 0.01)

    def _target_reached(self) -> bool:
        b = self.best
        if b is None:
            return False
        target = float(self.options.get("target_downforce_n", 200.0))
        # downforce on target and no live constraint penalties (the drag term
        # is a real force trade-off, not a penalty, so it is not gated on)
        return (abs(b["downforce_n"] - target) <= max(0.008 * abs(target), 0.5)
                and b.get("penalty", 99.0) < PEN_OK)

    def run(self):
        from scipy.optimize import differential_evolution, minimize
        self.state = "running"
        self.t_start = time.time()
        # search resolution: coarser for bigger stacks so wall time stays
        # flat; the refinement phase and final analysis always run at the
        # configured full resolution
        n_el = max(1, len(self.config.get("elements", [])))
        base_panels = 60 if n_el <= 2 else (50 if n_el == 3 else 45)
        self._full_panels = int(self.config.get("n_panels_per_side") or 70)
        self._opt_panels = min(self._full_panels, base_panels)
        cancelled = False
        coarse_best = None   # survives a cancel raised inside the refinement
        try:
            self._baseline_allowance()
            if any(v["key"] == "airfoil_idx" for v in self.variables):
                # candidate lists come from the screener; cached per operating
                # point, so this is seconds cold and instant warm
                self.phase = "screening candidates"
                cfg0 = StackConfig.from_dict(self.config)
                min_cr = {v["elem"]: v["lo"] for v in self.variables
                          if v["key"] == "chord_ratio"}
                self.shortlists = build_airfoil_shortlists(
                    self.config, cfg0, pool=self.airfoil_pool,
                    min_chord_ratio=min_cr or None)
                self._floor_shape_ts_bounds()
                if self._cancel.is_set():
                    raise _Cancelled()

            mode = self.options.get("mode", "global")
            budget = int(self.options.get("budget", 1500))
            dim = len(self.variables)
            bounds = [(v["lo"], v["hi"]) for v in self.variables]
            x0 = initial_vector(self.config, self.variables, self.shortlists)
            # thorough runs trade time for reproducibility: the global phase
            # runs to completion (no early stop — hitting the target in the
            # first basin found is not the same as finding the lowest-drag
            # basin) with a wider population, then polishes multi-start
            thorough = mode == "global" and budget >= 3000

            # descend gets a fixed, predictable share of the budget; an
            # early-stopped attain phase simply ends the run sooner rather
            # than shrinking the drag-descend work (the descend is what
            # turns "first design that hit the target" into "lowest-drag
            # design at the achievable level")
            budget_b = max(150, int(budget * PHASE_B_FRAC))
            if mode == "global":
                popsize = (16 if thorough
                           else max(6, min(12, budget // (25 * dim))))
                maxiter = max(8, budget // (popsize * dim))
                self.plan_total = popsize * dim * (maxiter + 1) + budget_b
            else:
                self.plan_total = max(100, budget - budget_b) + budget_b

            def _de_callback(*args, **kwargs):
                # stop the global phase early once the target is nailed —
                # except in thorough mode, where exploration IS the point
                return self._cancel.is_set() or (not thorough
                                                 and self._target_reached())

            if mode == "global":
                self.phase = "searching"
                differential_evolution(
                    self.objective, bounds, x0=x0, popsize=popsize,
                    maxiter=maxiter, seed=1, tol=1e-4, mutation=(0.4, 0.9),
                    recombination=0.8, polish=False, callback=_de_callback,
                )
            else:
                # local mode: pull the current design toward the target
                # first (at full resolution — there is no coarse phase to
                # bias against), so descend has an on-level start to seed
                # from even when the start was far off target
                self.phase = "refining"
                self._opt_panels = self._full_panels
                attain_fev = max(100, budget - budget_b)
                if self._cancel.is_set():
                    raise _Cancelled()
                minimize(self.objective, x0, method="Nelder-Mead",
                         bounds=bounds,
                         options={"maxfev": attain_fev,
                                  "xatol": 1e-4, "fatol": 1e-5})

            # descend phase (both modes end with it), at FULL panel
            # resolution — the coarse search paneling carries a bias larger
            # than the drag differences that matter — from diverse on-level
            # starts, minimizing drag while the deadzoned spring holds D*
            self.phase = "refining"
            self._resolve_dstar()
            with self._lock:
                coarse_best = self.best
                self.best = None   # the winner must be a full-fidelity eval
            self._obj_phase = "descend"
            self._opt_panels = self._full_panels
            n_starts = 3 if thorough else 2
            starts = self._descend_starts(n_starts, x0)
            fev_each = max(100, budget_b // len(starts))
            with self._lock:
                # re-plan so the progress bar keeps moving after an early
                # stop instead of freezing at the fraction of the never-run
                # global evaluations
                self.plan_total = self.n_eval + fev_each * len(starts)
            for x_s in starts:
                if self._cancel.is_set():
                    raise _Cancelled()
                minimize(self.objective, x_s, method="Nelder-Mead",
                         bounds=bounds,
                         options={"maxfev": fev_each,
                                  "xatol": 1e-4, "fatol": 1e-5})
            if self.best is None:
                with self._lock:
                    self.best = coarse_best
        except _Cancelled:
            cancelled = True
        except Exception as e:
            self.state = "failed"
            self.error = f"{e}\n{traceback.format_exc(limit=3)}"
        finally:
            self.t_end = time.time()

        if self.state == "running":
            try:
                # keep a non-terminal state while re-analyzing the winners:
                # a poller that sees a terminal state must already see the
                # finalized result/candidates beside it
                self.phase = "finalizing"
                self.state = "finalizing"
                if self.best is None and coarse_best is not None:
                    # cancelled mid-refinement, after the full-fidelity phase
                    # cleared the working best: the coarse-search winner is
                    # still a real design — return it instead of nothing
                    with self._lock:
                        self.best = coarse_best
                if self.best is not None:
                    best_cfg = apply_vector(self.config, self.variables,
                                            np.array(self.best["x"]),
                                            self.shortlists)
                    result = analysis.analyze(StackConfig.from_dict(best_cfg))
                    candidates = self._finalize_candidates()
                    with self._lock:
                        self.best_config = best_cfg
                        self.result = result
                        self.candidates = candidates
                        self.state = "cancelled" if cancelled else "done"
                elif cancelled:
                    self.state = "cancelled"
                else:
                    # the search ran but never saw a feasible design — that
                    # is a failure with a diagnosable cause, not a "done"
                    self.state = "failed"
                    parts = [f"no feasible design found in "
                             f"{self.n_eval} evaluations"]
                    if self.n_error:
                        parts.append(f"{self.n_error} evaluations raised "
                                     f"(last: {self.last_error})")
                    if self.n_infeasible:
                        parts.append(f"{self.n_infeasible} were infeasible "
                                     f"(last reason: {self.last_infeasible})")
                    self.error = "; ".join(parts)
            except Exception as e:
                self.state = "failed"
                self.error = str(e)

    def snapshot(self) -> dict:
        with self._lock:
            elapsed = ((self.t_end or time.time())
                       - (self.t_start or time.time()))
            if self.state in ("done", "failed", "cancelled"):
                progress = 1.0
            elif self.plan_total:
                progress = min(0.97, self.n_eval / self.plan_total)
            else:
                progress = 0.0
            eta = (elapsed * (1 - progress) / progress
                   if 0.02 < progress < 1.0 and self.state == "running"
                   else None)
            return {
                "id": self.id, "state": self.state, "error": self.error,
                "n_eval": self.n_eval,
                "n_error": self.n_error,
                "n_infeasible": self.n_infeasible,
                "elapsed_s": round(elapsed, 1),
                "phase": self.phase,
                "progress": round(progress, 3),
                "eta_s": round(eta, 1) if eta is not None else None,
                "history": list(self.history[-400:]),
                "best": self.best,
                "dstar_n": (round(self.dstar, 1)
                            if self.dstar is not None else None),
                "target_note": self.target_note,
                "variables": [{**v} for v in self.variables],
                "best_config": self.best_config,
                "result": self.result if self.state in ("done", "cancelled")
                          else None,
                "candidates": self.candidates
                              if self.state in ("done", "cancelled") else None,
            }

    def cancel(self):
        self._cancel.set()


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def start(config: dict, options: dict) -> str:
    job = Job(config, options)
    with _jobs_lock:
        # keep the registry small
        done_ids = [k for k, j in _jobs.items()
                    if j.state in ("done", "failed", "cancelled")]
        for k in done_ids[:-8]:
            _jobs.pop(k, None)
        _jobs[job.id] = job
    threading.Thread(target=job.run, name=f"opt-{job.id}", daemon=True).start()
    return job.id


def get(job_id: str) -> Job | None:
    with _jobs_lock:
        return _jobs.get(job_id)


def current() -> dict:
    """The active (or, failing that, most recent) job — lets a reloaded UI
    re-attach to a running search instead of orphaning it.

    Optimizer jobs can overlap (a reload orphans one while the user starts
    another), so the MOST RECENT active job is returned — re-attaching to the
    oldest would leave the run the user actually cares about unmonitored."""
    with _jobs_lock:
        jobs = list(_jobs.values())
    active = [j for j in jobs if j.state in ("pending", "running",
                                             "finalizing")]
    if active:
        j = max(active, key=lambda j: j.t_start or 0)
        return {"job_id": j.id, "state": j.state}
    if jobs:
        last = max(jobs, key=lambda j: j.t_start or 0)
        return {"job_id": last.id, "state": last.state}
    return {"job_id": None, "state": None}
