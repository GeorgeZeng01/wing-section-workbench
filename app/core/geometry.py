"""Multi-element stack geometry.

Frames:
  design  — upright (lift = +y), main LE at (0,0), main chord = 1
  install — y-flipped (downforce), ground plane at y = 0, stack translated so
            its lowest point sits at the ride height

All lengths are fractions of the main chord unless suffixed _mm / _m.

Element placement follows the convention of the original stack_builder:
each flap is scaled about its LE, rotated TE-down by its deflection, then its
LE is placed at (previous element TE + (dx, dy)).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import airfoils

NU_AIR_DEFAULT = 1.5e-5  # m^2/s
RHO_AIR_DEFAULT = 1.225  # kg/m^3


@dataclass
class ElementSpec:
    airfoil: str = "s1223"
    chord_ratio: float = 1.0      # fraction of main chord (main element: 1.0)
    deflection_deg: float = 0.0   # TE-down positive in the design frame
    dx: float = -0.03             # flap LE relative to previous TE, x
    dy: float = -0.03             # flap LE relative to previous TE, y
    # preferred slot parameterization: when set, placement is solved so the
    # achieved slot gap / overlap equal these values (percent of main chord),
    # overriding dx/dy. This is how the UI drives flaps; dx/dy remain for
    # legacy configs and scripting.
    slot_gap_pct: float | None = None
    slot_overlap_pct: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "ElementSpec":
        if not isinstance(d, dict):
            raise ValueError(f"element entry must be an object, got {type(d).__name__}")
        def _opt(key):
            v = d.get(key)
            return None if v is None else float(v)
        return cls(
            airfoil=str(d.get("airfoil", "s1223")),
            chord_ratio=float(d.get("chord_ratio", 1.0)),
            deflection_deg=float(d.get("deflection_deg", 0.0)),
            dx=float(d.get("dx", -0.03)),
            dy=float(d.get("dy", -0.03)),
            slot_gap_pct=_opt("slot_gap_pct"),
            slot_overlap_pct=_opt("slot_overlap_pct"),
        )


@dataclass
class ManufacturingSpec:
    """Optional manufacturing prep, applied to every element's profile.

    te_gap_mm         minimum trailing-edge thickness; each element's TE is
                      opened to at least this (knife edges cannot be built)
    te_mode           "thicken" (add blended thickness, keep chord — default)
                      or "truncate" (cut the tip off and rescale)
    min_thickness_mm  warn when an element's thickest point is below this
                      buildable minimum; also floors optimizer/screener
                      airfoil selection. 0 disables the check.
    """
    te_gap_mm: float = 1.2
    te_mode: str = "thicken"
    min_thickness_mm: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "ManufacturingSpec":
        if not isinstance(d, dict):
            raise ValueError("manufacturing must be an object")
        return cls(
            te_gap_mm=float(d.get("te_gap_mm", 1.2)),
            te_mode=str(d.get("te_mode", "thicken")),
            min_thickness_mm=float(d.get("min_thickness_mm", 0.0) or 0.0),
        )


@dataclass
class StackConfig:
    elements: list[ElementSpec] = field(default_factory=lambda: [ElementSpec()])
    stack_aoa_deg: float = 0.0
    ride_height_mm: float = 30.0
    chord_mm: float = 350.0
    span_mm: float = 1400.0
    speed_ms: float = 15.0
    rho: float = RHO_AIR_DEFAULT
    nu: float = NU_AIR_DEFAULT
    ncrit: float = 7.0                # on-track turbulence (clean tunnel is 9)
    viscous_efficiency: float = 0.85  # inviscid -> expected real downforce
    efficiency_3d: float = 1.0        # finite-span / endplate knockdown (lift)
    span_efficiency: float = 0.9      # Oswald-style e for induced drag
    # ground-gain model calibration (see analysis module docstring):
    k_g: float | None = None          # None = ride-height curve; number = pinned
    gain_cap_ratio: float = 3.0       # realized-gain ceiling, x |C_free|
    choke_h_c: float = 0.045          # venturi-choke ride height, chords
    n_panels_per_side: int = 70
    manufacturing: ManufacturingSpec | None = None

    @property
    def chord_m(self) -> float:
        return self.chord_mm / 1000.0

    @property
    def ride_height_c(self) -> float:
        return self.ride_height_mm / self.chord_mm

    @property
    def q_pa(self) -> float:
        return 0.5 * self.rho * self.speed_ms**2

    def element_re(self, i: int) -> float:
        return self.speed_ms * self.elements[i].chord_ratio * self.chord_m / self.nu

    @classmethod
    def from_dict(cls, d: dict) -> "StackConfig":
        els = [ElementSpec.from_dict(e) for e in d.get("elements", [{}])]
        if not els:
            raise ValueError("stack needs at least one element")
        els[0].chord_ratio = 1.0  # main chord is the reference by definition
        els[0].dx = els[0].dy = 0.0
        # the main element's incidence IS the stack angle; a deflection on it
        # would double-count, so it is normalized away like chord_ratio
        els[0].deflection_deg = 0.0
        kw = {}
        for f in ("stack_aoa_deg", "ride_height_mm", "chord_mm", "span_mm",
                  "speed_ms", "rho", "nu", "ncrit", "viscous_efficiency",
                  "efficiency_3d", "span_efficiency", "gain_cap_ratio",
                  "choke_h_c"):
            if f in d and d[f] is not None:
                kw[f] = float(d[f])
        if d.get("k_g") is not None:
            kw["k_g"] = float(d["k_g"])
        if "n_panels_per_side" in d and d["n_panels_per_side"]:
            kw["n_panels_per_side"] = int(d["n_panels_per_side"])
        if d.get("manufacturing") is not None:
            kw["manufacturing"] = ManufacturingSpec.from_dict(d["manufacturing"])
        cfg = cls(elements=els, **kw)
        _validate(cfg)
        return cfg


def _validate(cfg: StackConfig) -> None:
    import math
    scalar_bounds = {
        "chord_mm": (10.0, 5000.0),
        "ride_height_mm": (0.0, 5000.0),
        "span_mm": (10.0, 20000.0),
        "speed_ms": (0.1, 200.0),
        "stack_aoa_deg": (-45.0, 45.0),
        "rho": (0.05, 20.0),
        "nu": (1e-7, 1e-3),
        "ncrit": (0.1, 20.0),
        "viscous_efficiency": (0.05, 1.5),
        "efficiency_3d": (0.05, 1.5),
        "span_efficiency": (0.4, 1.5),
        "gain_cap_ratio": (0.5, 10.0),
        "choke_h_c": (0.001, 0.5),
    }
    for name, (lo, hi) in scalar_bounds.items():
        v = getattr(cfg, name)
        if not (math.isfinite(v) and lo <= v <= hi):
            raise ValueError(f"{name} must be a finite value in {lo}..{hi}")
    if cfg.k_g is not None and not (math.isfinite(cfg.k_g)
                                    and 0.0 <= cfg.k_g <= 1.0):
        raise ValueError("k_g must be a finite value in 0..1 (or omitted for "
                         "the automatic ride-height curve)")
    if not (1 <= len(cfg.elements) <= 4):
        raise ValueError("element count must be 1..4")
    if not (10 <= cfg.n_panels_per_side <= 200):
        raise ValueError("n_panels_per_side must be 10..200")
    if cfg.ride_height_mm < 0.005 * cfg.chord_mm:
        raise ValueError("ride height below 0.5% chord — panel method invalid")
    m = cfg.manufacturing
    if m is not None:
        from . import manufacturing as mfg_mod
        if m.te_mode not in mfg_mod.MODES:
            raise ValueError(f"te_mode must be one of {mfg_mod.MODES}")
        if not (math.isfinite(m.te_gap_mm) and 0.1 <= m.te_gap_mm <= 8.0):
            raise ValueError("te_gap_mm must be a finite value in 0.1..8 mm")
        if not (math.isfinite(m.min_thickness_mm)
                and 0.0 <= m.min_thickness_mm <= 50.0):
            raise ValueError("min_thickness_mm must be 0..50 mm")
    for i, e in enumerate(cfg.elements):
        for f, lo, hi in (("chord_ratio", 0.05, 1.0),
                          ("deflection_deg", -90.0, 90.0),
                          ("dx", -1.0, 1.0), ("dy", -1.0, 1.0)):
            v = getattr(e, f)
            if not (math.isfinite(v) and lo <= v <= hi):
                raise ValueError(f"element {i+1}: {f} must be finite, {lo}..{hi}")
        # raw "mfg:" specs arrive straight from API clients — hold them to
        # the same physical bounds as the validated manufacturing block
        spec = str(e.airfoil)
        if spec.lower().startswith("mfg:"):
            from . import manufacturing as mfg_mod
            mode, gap_c, base = mfg_mod.parse(spec)   # raises ValueError
            if base.lower().startswith("mfg:"):
                raise ValueError(f"element {i+1}: nested mfg: specs are not "
                                 f"allowed")
            gap_mm = gap_c * element_chord_mm(cfg, i)
            if not (0.05 <= gap_mm <= 8.0):
                raise ValueError(
                    f"element {i+1}: mfg spec opens the TE to {gap_mm:.2f} mm "
                    f"on a {element_chord_mm(cfg, i):.0f} mm chord — allowed "
                    f"range is 0.05..8 mm")
        for f, lo, hi in (("slot_gap_pct", 0.2, 10.0),
                          ("slot_overlap_pct", -3.0, 10.0)):
            v = getattr(e, f)
            if v is not None and not (math.isfinite(v) and lo <= v <= hi):
                raise ValueError(f"element {i+1}: {f} must be {lo}..{hi} %c")


def rotate(coords: np.ndarray, deg: float, center=(0.0, 0.0)) -> np.ndarray:
    th = np.radians(deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    return (coords - center) @ R.T + center


def te_point(coords: np.ndarray) -> np.ndarray:
    return 0.5 * (coords[0] + coords[-1])


def te_base_points(coords: np.ndarray, n: int = 7) -> np.ndarray:
    """Sample points along the (possibly blunt) TE base segment."""
    t = np.linspace(0.0, 1.0, n)[:, None]
    return coords[-1] + t * (coords[0] - coords[-1])


def slot_gap_dist(prev_coords: np.ndarray, flap_coords: np.ndarray) -> float:
    """Physical slot throat: nearest distance from the previous element's
    TE base (the whole segment, not its midpoint — with manufacturing prep
    the base is 1-2 mm wide and the midpoint overstates the throat by half
    of that) to the flap contour."""
    return min(min_dist_to_polyline(p, flap_coords)
               for p in te_base_points(prev_coords))


def min_dist_to_polyline(pt: np.ndarray, coords: np.ndarray) -> float:
    a, b = coords[:-1], coords[1:]
    ab = b - a
    t = np.clip(np.einsum("ij,ij->i", pt - a, ab) /
                np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-16), 0.0, 1.0)
    proj = a + t[:, None] * ab
    return float(np.min(np.hypot(*(pt - proj).T)))


def polygons_intersect(a: np.ndarray, b: np.ndarray) -> bool:
    from matplotlib.path import Path as MplPath
    return bool(MplPath(a).contains_points(b).any()
                or MplPath(b).contains_points(a).any())


def _solve_dy_for_gap(prev_coords: np.ndarray, flap_local: np.ndarray,
                      dx: float, gap: float) -> float:
    """dy such that the slot throat from the previous element's TE base to
    the placed flap equals `gap`, with the flap fully below the TE's
    clearance circle.

    Closed form (against the TE base midpoint): the flap only translates
    vertically, so each contour point p demands
    dy <= (ty - py) - sqrt(gap^2 - (px - tx)^2) whenever it passes within
    the gap circle horizontally; the most demanding point binds and sits
    exactly on the circle. (Gap vs. drop is NOT monotone — the flap's
    upper-surface crest sweeps past the TE on the way down — so a naive
    bisection has no valid bracket.) A short refinement then converges on
    the real slot_gap_dist measure, which sees the whole TE base — for a
    blunt manufacturing TE the nearest base corner, not the midpoint, sets
    the physical throat.
    """
    prev_te = te_point(prev_coords)
    tx, ty = float(prev_te[0]), float(prev_te[1])
    pts = flap_local + np.array([tx + dx, ty])       # placement at dy = 0
    ddx = pts[:, 0] - tx
    inside = np.abs(ddx) < gap
    if not inside.any():                             # no horizontal overlap
        return -gap
    s = np.sqrt(np.maximum(gap * gap - ddx[inside] ** 2, 0.0))
    dy = float(np.min((ty - pts[inside, 1]) - s))
    for _ in range(4):                               # true-measure touch-up
        achieved = slot_gap_dist(
            prev_coords, flap_local + prev_te + np.array([dx, dy]))
        err = gap - achieved
        if abs(err) < 1e-5:
            break
        dy -= err
    return dy


def element_chord_mm(cfg: StackConfig, i: int) -> float:
    return (1.0 if i == 0 else cfg.elements[i].chord_ratio) * cfg.chord_mm


def effective_spec(cfg: StackConfig, i: int) -> str:
    """The airfoil actually built for element i: the configured spec, with
    manufacturing TE prep folded in when enabled."""
    spec = cfg.elements[i].airfoil
    if cfg.manufacturing is None:
        return spec
    # an element that already carries its own explicit "mfg:" treatment (a
    # scripting/API path) wins over the global manufacturing block — wrapping
    # it in a second mfg: layer would double-open the TE and produce an
    # invalid nested "mfg:...:mfg:..." spec. Check anywhere in the chain, not
    # just the head: the optimizer's opt_shape re-wrap yields
    # "shape:...:mfg:...:base", where the mfg: layer is not the outer one.
    if "mfg:" in str(spec).lower():
        return spec
    from . import manufacturing as mfg_mod
    gap_c = cfg.manufacturing.te_gap_mm / element_chord_mm(cfg, i)
    return mfg_mod.derived_spec(spec, gap_c, cfg.manufacturing.te_mode)


def build_stack(cfg: StackConfig) -> list[dict]:
    """Design-frame elements: [{role, airfoil_name, coords, slot_gap, ...}]."""
    out = []
    for i, spec in enumerate(cfg.elements):
        spec_eff = effective_spec(cfg, i)
        name, uc = airfoils.repaneled(spec_eff, cfg.n_panels_per_side)
        if i == 0:
            coords = uc
            role = "main"
            dx = dy = 0.0
        else:
            c = uc * spec.chord_ratio
            c = rotate(c, -spec.deflection_deg)  # TE-down = clockwise
            prev_te = te_point(out[-1]["coords"])
            # slot overlap fixes dx exactly: overlap = -(dx + min_x(flap))
            if spec.slot_overlap_pct is not None:
                dx = -(spec.slot_overlap_pct / 100.0) - float(c[:, 0].min())
            else:
                dx = spec.dx
            if spec.slot_gap_pct is not None:
                dy = _solve_dy_for_gap(out[-1]["coords"], c, dx,
                                       spec.slot_gap_pct / 100.0)
            else:
                dy = spec.dy
            coords = c + prev_te + np.array([dx, dy])
            role = f"flap{i}"
        out.append({
            "role": role, "airfoil": spec.airfoil, "airfoil_eff": spec_eff,
            "airfoil_name": name,
            "chord_ratio": spec.chord_ratio if i else 1.0,
            "deflection_deg": spec.deflection_deg if i else 0.0,
            "dx": round(float(dx), 5), "dy": round(float(dy), 5),
            "coords": coords,
        })

    # slot metrics are wing-relative: measure BEFORE the stack rotation so
    # overlap (an x-projection) doesn't drift with stack angle
    for prev, cur in zip(out, out[1:]):
        pte = te_point(prev["coords"])
        cur["slot_gap"] = slot_gap_dist(prev["coords"], cur["coords"])
        cur["slot_overlap"] = float(pte[0] - cur["coords"][:, 0].min())
    # intersection must be checked across ALL pairs, not just neighbors — a
    # third element can overlap the main while clearing its own predecessor
    from itertools import combinations
    for e in out:
        e["intersects"] = False
    for i, j in combinations(range(len(out)), 2):
        if polygons_intersect(out[i]["coords"], out[j]["coords"]):
            out[j]["intersects"] = True

    if cfg.stack_aoa_deg:
        # positive stack angle = nose-up in the design frame (more incidence,
        # more downforce once installed) — same sense as flap deflection
        for e in out:
            e["coords"] = rotate(e["coords"], -cfg.stack_aoa_deg)
    return out


def install_stack(design_elements: list[dict], ride_height_c: float) -> list[dict]:
    """Design frame -> install frame (downforce, ground at y = 0)."""
    inst = []
    for e in design_elements:
        c = e["coords"] * np.array([1.0, -1.0])
        inst.append({**e, "coords": c[::-1].copy()})  # keep Selig winding sense
    y_min = min(e["coords"][:, 1].min() for e in inst)
    shift = np.array([0.0, ride_height_c - y_min])
    for e in inst:
        e["coords"] = e["coords"] + shift
    return inst


def stack_extents(elements: list[dict]) -> dict:
    allc = np.vstack([e["coords"] for e in elements])
    return {
        "x_min": float(allc[:, 0].min()), "x_max": float(allc[:, 0].max()),
        "y_min": float(allc[:, 1].min()), "y_max": float(allc[:, 1].max()),
        "system_chord_c": float(allc[:, 0].max() - allc[:, 0].min()),
    }


def manufacturing_report(cfg: StackConfig, design: list[dict]) -> list[str]:
    """Annotate design elements with as-built mm data; returns warnings.

    Adds e["mfg"] = {te_gap_mm, max_thickness_mm, min_aft_thickness_mm,
    te_ok, thickness_ok, waist_ok} to every element when manufacturing prep
    is enabled. min_aft_thickness_mm is MEASURED on the repaneled as-built
    contour (the geometry the solver and exports actually use), so it
    verifies the thickness floor end-to-end rather than trusting it.
    """
    from . import manufacturing as mfg_mod
    m = cfg.manufacturing
    if m is None:
        return []
    warnings = []
    for i, e in enumerate(design):
        c_mm = e["chord_ratio"] * cfg.chord_mm
        _, uc = airfoils.repaneled(e["airfoil_eff"], cfg.n_panels_per_side)
        info = airfoils.geometry_info(uc)
        te_mm = info["te_gap"] * c_mm
        tmax_mm = info["max_thickness"] * c_mm
        ref_c = min(m.te_gap_mm, te_mm) / c_mm
        taftmin_mm = mfg_mod.aft_min_thickness(uc, ref_c) * c_mm
        # a section whose UNTREATED thickest point is not clearly above the
        # requested TE cannot honor the treatment — thicken turns it into a
        # near-constant-thickness plate with a thin nose, which is almost
        # never what the user meant
        _, uc_raw = airfoils.repaneled(e["airfoil"], cfg.n_panels_per_side)
        tmax_raw_mm = airfoils.geometry_info(uc_raw)["max_thickness"] * c_mm
        plate = tmax_raw_mm < m.te_gap_mm * 1.05
        te_ok = te_mm >= m.te_gap_mm - 0.05
        thickness_ok = ((m.min_thickness_mm <= 0
                         or tmax_mm >= m.min_thickness_mm) and not plate)
        waist_ok = taftmin_mm >= min(m.te_gap_mm, te_mm) - 0.05
        e["mfg"] = {"te_gap_mm": round(te_mm, 2),
                    "max_thickness_mm": round(tmax_mm, 1),
                    "min_aft_thickness_mm": round(taftmin_mm, 2),
                    "te_ok": bool(te_ok), "thickness_ok": bool(thickness_ok),
                    "waist_ok": bool(waist_ok), "plate": bool(plate)}
        if not te_ok:
            warnings.append(
                f"{e['role']}: trailing edge reaches only {te_mm:.1f} mm of "
                f"the requested {m.te_gap_mm:.1f} mm — the section is too "
                f"thin for this treatment; lower the TE thickness or pick a "
                f"thicker airfoil.")
        if plate:
            warnings.append(
                f"{e['role']}: {e['airfoil_name'].replace(' (mfg)', '')} is "
                f"only {tmax_raw_mm:.1f} mm thick at its thickest — not "
                f"meaningfully thicker than the {m.te_gap_mm:.1f} mm TE you "
                f"require. The treatment turns it into a plate with a thin "
                f"nose; pick a thicker airfoil or reduce the TE thickness.")
        elif not thickness_ok:
            warnings.append(
                f"{e['role']}: thickest point is {tmax_mm:.1f} mm — below "
                f"your {m.min_thickness_mm:.1f} mm buildable minimum. Use a "
                f"thicker section or a larger chord.")
        if not waist_ok:
            warnings.append(
                f"{e['role']}: the section waists to {taftmin_mm:.2f} mm "
                f"behind its nose — thinner than the {te_mm:.1f} mm trailing "
                f"edge it was opened for. Use the 'thicken' treatment "
                f"(which floors thickness behind the nose) or a thicker "
                f"airfoil.")
        if m.te_gap_mm / c_mm > mfg_mod.GAP_C_MAX:
            warnings.append(
                f"{e['role']}: the requested {m.te_gap_mm:.1f} mm TE is "
                f"{m.te_gap_mm / c_mm * 100:.0f}% of its {c_mm:.0f} mm chord "
                f"— the treatment is capped at {mfg_mod.GAP_C_MAX * 100:.0f}% "
                f"of chord, so the built TE is thinner than requested. "
                f"Use a larger chord or a smaller TE thickness.")
        elif m.te_gap_mm / c_mm > 0.03:
            warnings.append(
                f"{e['role']}: TE thickness is {m.te_gap_mm / c_mm * 100:.1f}% "
                f"of its {c_mm:.0f} mm chord — expect extra drag; consider a "
                f"smaller TE thickness or a larger flap chord.")
    return warnings


def geometry_report(cfg: StackConfig) -> dict:
    """Everything the UI needs to draw and sanity-check the stack."""
    design = build_stack(cfg)
    installed = install_stack(design, cfg.ride_height_c)
    ext = stack_extents(design)
    # legacy dx/dy flaps get migrated to the gap/overlap parameterization by
    # the UI, which adopts the achieved values. Those must come from the
    # SHARP geometry: as-built numbers depend on the manufacturing state, and
    # baking them in would permanently move the flap once mfg is toggled off.
    if cfg.manufacturing is not None and any(
            e.slot_gap_pct is None or e.slot_overlap_pct is None
            for e in cfg.elements[1:]):
        import dataclasses
        sharp = build_stack(dataclasses.replace(cfg, manufacturing=None))
        for e_t, e_s in zip(design[1:], sharp[1:]):
            e_t["slot_gap_sharp"] = float(e_s["slot_gap"])
            e_t["slot_overlap_sharp"] = float(e_s["slot_overlap"])
    warnings = []
    for i, e in enumerate(design):
        if e.get("intersects"):
            warnings.append(f"{e['role']} intersects another element — "
                            f"open the slot or reduce overlap.")
        elif "slot_gap" in e and e["slot_gap"] < 0.005:
            warnings.append(f"{e['role']} slot gap {e['slot_gap']*100:.2f}%c is "
                            f"below 0.5%c — the slot will choke.")
        # a requested gap can be geometrically unreachable (e.g. the flap
        # sits fully behind the TE with a large negative overlap) — say so
        # instead of silently drawing something else
        req = cfg.elements[i].slot_gap_pct if i else None
        if (req is not None and "slot_gap" in e
                and abs(e["slot_gap"] * 100 - req) > 0.15):
            warnings.append(
                f"{e['role']}: requested slot gap {req:.2f}%c could not be "
                f"achieved — the geometry gives {e['slot_gap']*100:.2f}%c. "
                f"Bring the flap closer (less negative overlap) or relax "
                f"the gap.")
    warnings += manufacturing_report(cfg, design)
    te_clear = min(e["coords"][:, 1].min() for e in installed)
    m = cfg.manufacturing
    return {
        "design": [_elem_out(e) for e in design],
        "installed": [_elem_out(e) for e in installed],
        "system_chord_c": ext["system_chord_c"],
        "system_chord_mm": ext["system_chord_c"] * cfg.chord_mm,
        "ride_height_c": cfg.ride_height_c,
        "lowest_point_c": float(te_clear),
        "manufacturing": None if m is None else {
            "te_gap_mm": m.te_gap_mm, "te_mode": m.te_mode,
            "min_thickness_mm": m.min_thickness_mm,
        },
        "warnings": warnings,
    }


def _elem_out(e: dict) -> dict:
    d = {k: v for k, v in e.items() if k != "coords"}
    d["coords"] = np.round(e["coords"], 6).tolist()
    for k in ("slot_gap", "slot_overlap"):
        if k in d:
            d[k] = float(d[k])
    return d
