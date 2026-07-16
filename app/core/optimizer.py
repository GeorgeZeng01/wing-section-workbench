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
  - hard penalty on intersecting geometry / solver failure

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

CAND_MAX = 4               # candidate designs returned per run
CAND_DIVERSITY = 0.12      # min normalized per-variable spread between them
CAND_TARGET_TOL = 0.03     # alternates must land within 3% of the target


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
    variables = []
    if opts.get("opt_stack_aoa", True):
        variables.append({"key": "stack_aoa_deg", "elem": None,
                          "lo": bounds["stack_aoa_deg"][0],
                          "hi": bounds["stack_aoa_deg"][1]})
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
            for k in SHAPE_KEYS[:3]:
                variables.append({"key": k, "elem": i,
                                  "lo": shaping.BOUNDS_BUMP[0],
                                  "hi": shaping.BOUNDS_BUMP[1]})
            ts_lo = shaping.BOUNDS_TS[0]
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
                        ts_lo = min(max(ts_lo, floor_mm / tmax_mm),
                                    shaping.BOUNDS_TS[1] - 0.01)
                except Exception:
                    pass
            variables.append({"key": "shape_ts", "elem": i,
                              "lo": ts_lo, "hi": shaping.BOUNDS_TS[1]})
    for i in range(1, n_flaps + 1):
        if opts.get("opt_deflections", True):
            variables.append({"key": "deflection_deg", "elem": i,
                              "lo": bounds["deflection_deg"][0],
                              "hi": bounds["deflection_deg"][1]})
        if opts.get("opt_positions", True):
            variables.append({"key": "slot_gap_pct", "elem": i,
                              "lo": bounds["slot_gap_pct"][0],
                              "hi": bounds["slot_gap_pct"][1]})
            variables.append({"key": "slot_overlap_pct", "elem": i,
                              "lo": bounds["slot_overlap_pct"][0],
                              "hi": bounds["slot_overlap_pct"][1]})
        if opts.get("opt_chords", False):
            variables.append({"key": "chord_ratio", "elem": i,
                              "lo": bounds["chord_ratio"][0],
                              "hi": bounds["chord_ratio"][1]})
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


class Job:
    def __init__(self, config: dict, options: dict):
        self.id = uuid.uuid4().hex[:12]
        self.config = copy.deepcopy(config)
        # validate the numeric options eagerly: a bogus budget or target is a
        # client error the API should 422, not a background-thread crash
        for key, default in (("target_downforce_n", 200.0),
                             ("drag_weight", 0.10)):
            try:
                float(options.get(key, default))
            except (TypeError, ValueError):
                raise ValueError(f"options[{key!r}] must be a number")
        try:
            int(options.get("budget", 1500))
        except (TypeError, ValueError):
            raise ValueError("options['budget'] must be an integer")
        if options.get("mode", "global") not in ("global", "local"):
            raise ValueError("options['mode'] must be 'global' or 'local'")
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
        # re-optimizing an already-shaped design: unwrap the shape spec and
        # seed the shape variables from it, so refinement continues from the
        # current shape instead of stacking a second modification on top
        if options.get("opt_shape"):
            from . import shaping
            for e in self.config.get("elements", []):
                a = str(e.get("airfoil", ""))
                if a.lower().startswith("shape:"):
                    b, ts, base = shaping.parse(a)
                    e["airfoil"] = base
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
        self.archive: list[dict] = []   # every feasible evaluation (for
                                        # candidate selection at the end)
        self.candidates = None     # ranked diverse designs, set on finish
        self.n_eval = 0
        self.t_start = None
        self.t_end = None
        self.phase = None          # screening | searching | refining | finalizing
        self.plan_total = None     # planned evaluation count (progress basis)
        self.shortlists: dict[int, list[str]] = {}
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
        # at 8.0 the search happily settled 30-40% past isolated CL_max —
        # separated-flow fantasy designs. 40.0 makes a 15% overshoot cost
        # about as much as missing the target by 10%, so an honest miss
        # beats a fake hit
        j_pen = 40.0 * ev["load_excess"]
        # keep the search out of the regime the model itself calls
        # unreliable: realized ground-effect loading past the allowance
        j_pen += 25.0 * ev.get("load_excess_ground", 0.0)
        for g in ev["gaps"]:
            j_pen += 2.0 * _band_penalty(g, GAP_BAND, 0.005)
        for o in ev["overlaps"]:
            j_pen += 2.0 * _band_penalty(o, OVERLAP_BAND, 0.01)
        # the target term must dominate realistic drag savings inside ~1%
        # of target, or induced drag (which falls with downforce^2) would
        # pull the optimum a few percent under the requested load
        j = (60.0 * f_err ** 2
             + w_drag * ev["drag_n"] / max(abs(target), 1.0) * 10.0
             + j_pen)

        with self._lock:
            self.archive.append({"x": [round(float(v), 5) for v in x],
                                 "J": float(j), "penalty": float(j_pen),
                                 "downforce_n": round(ev["downforce_n"], 1),
                                 "drag_n": round(ev["drag_n"], 2),
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

    def _select_candidates(self) -> list[dict]:
        """The best design plus up to CAND_MAX-1 alternates from everything
        the search evaluated: on target, nearly as good on the objective,
        and chosen farthest-point-first so they are genuinely different
        designs rather than jitter around the optimum."""
        with self._lock:
            archive = list(self.archive)
        if not archive:
            return []
        target = float(self.options.get("target_downforce_n", 200.0))
        spans = [max(v["hi"] - v["lo"], 1e-9) for v in self.variables]
        by_j = sorted(archive, key=lambda a: a["J"])
        # the winner must come from full-fidelity evaluations when any exist
        # — the coarse search paneling carries a bias larger than the drag
        # differences between near-optimal designs
        full = [a for a in by_j if a.get("panels") == self._full_panels]
        best = full[0] if full else by_j[0]
        tol = max(CAND_TARGET_TOL * abs(target), 1.0)
        good = [a for a in by_j
                if abs(a["downforce_n"] - target) <= tol and a["penalty"] < 0.5]
        j_slack = best["J"] + max(0.15 * abs(best["J"]), 0.02)
        pool = ([a for a in good if a["J"] <= j_slack]
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
            entry = {"rank": rank, "J": round(a["J"], 4),
                     "downforce_n": a["downforce_n"], "drag_n": a["drag_n"],
                     "x": a["x"], "config": cfg_c}
            if self.shortlists:
                entry["airfoils"] = [e.get("airfoil")
                                     for e in cfg_c["elements"]]
            try:
                r = analysis.analyze(StackConfig.from_dict(cfg_c),
                                     include_geometry=False)
                entry["summary"] = {
                    "downforce_n": r["forces"]["downforce_n"],
                    "drag_total_n": r["forces"]["drag_total_n"],
                    "efficiency_ld": r["forces"]["efficiency_ld"],
                    "warnings": len(r["warnings"]),
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
            out.append(entry)
        return out

    # ---- runner ----

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
                and b.get("penalty", 99.0) < 0.5)

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
        try:
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

            if mode == "global":
                popsize = (16 if thorough
                           else max(6, min(12, budget // (25 * dim))))
                maxiter = max(8, budget // (popsize * dim))
                polish_fev = max(150, budget // 5)
                self.plan_total = popsize * dim * (maxiter + 1) + polish_fev
            else:
                polish_fev = max(150, budget)
                self.plan_total = polish_fev

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
            # local refinement (both modes end with it), at FULL panel
            # resolution — the coarse search paneling carries a bias larger
            # than the drag differences that matter — and from several
            # diverse starts on bigger budgets, so repeated runs converge to
            # the same plateau minimum instead of scattering across it
            self.phase = "refining"
            if self._target_reached() and not thorough:
                polish_fev = min(polish_fev, 60 * dim)
            starts = ([np.array(self.best["x"])]
                      if self.best is not None else [x0])
            n_starts = 3 if thorough else 1
            if n_starts > 1:
                for alt in self._select_candidates()[1:n_starts]:
                    starts.append(np.array(alt["x"]))
            with self._lock:
                coarse_best = self.best
                self.best = None   # the winner must be a full-fidelity eval
            self._opt_panels = self._full_panels
            fev_each = max(100, polish_fev // len(starts))
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
