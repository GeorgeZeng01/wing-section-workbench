"""Figures for the Wing Section Studio guide, generated from the real core.

Every diagram is produced from live app.core computations (panel solutions,
TE treatments, shape modifications, optimizer runs) so the guide always
matches the shipping behavior. Run via build_guide.py, or standalone to
refresh the PNGs in the build directory:

    .venv\\Scripts\\python.exe docs\\guide_figures.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Circle

from app.core import (airfoils, analysis, geometry, panel,  # noqa: E402
                      manufacturing as mfg, shaping)
from app.core.panel import _panelize  # noqa: E402

# palette — the application's element series (blue, red, green, gold) + ink
BLUE, RED, GREEN, GOLD = "#1f4e8c", "#b03a2e", "#1e8449", "#b7950b"
INK, INK2, INK3 = "#1b2433", "#55606f", "#9aa4b2"
GRID = "#dfe4ea"
PANEL = "#eef1f5"
SERIES = [BLUE, RED, GREEN, GOLD]

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Segoe UI", "DejaVu Sans", "Arial"],
    "font.size": 9,
    "axes.edgecolor": INK2,
    "axes.labelcolor": INK,
    "axes.titlecolor": INK,
    "xtick.color": INK2,
    "ytick.color": INK2,
    "axes.linewidth": 0.8,
    "figure.dpi": 200,
})

DEMO = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 24,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 1.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15, "ncrit": 7,
    "viscous_efficiency": 0.85, "efficiency_3d": 0.9,
}


def _style(ax, grid=True):
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    if grid:
        ax.grid(True, color=GRID, linewidth=0.7, zorder=0)
        ax.set_axisbelow(True)


def _save(fig, out: Path, name: str):
    fig.savefig(out / f"{name}.png", bbox_inches="tight", pad_inches=0.06,
                facecolor="white")
    plt.close(fig)
    return out / f"{name}.png"


# ---------------------------------------------------------------- figures

def fig_slot_definition(out: Path):
    """Gap and overlap, zoomed into a real slot so both read clearly."""
    # a slightly larger flap and gap keep the callouts legible at zoom
    cfg = geometry.StackConfig.from_dict({**DEMO, "elements": [
        DEMO["elements"][0],
        {"airfoil": "s1223", "chord_ratio": 0.40, "deflection_deg": 30,
         "slot_gap_pct": 2.2, "slot_overlap_pct": 4.0}]})
    design = geometry.build_stack(cfg)
    main, flap = design[0]["coords"], design[1]["coords"]
    pte = geometry.te_point(main)
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.fill(main[:, 0], main[:, 1], color=BLUE, alpha=0.13, zorder=1)
    ax.plot(main[:, 0], main[:, 1], color=BLUE, lw=1.6, zorder=2)
    ax.fill(flap[:, 0], flap[:, 1], color=RED, alpha=0.13, zorder=1)
    ax.plot(flap[:, 0], flap[:, 1], color=RED, lw=1.6, zorder=2)

    # gap: nearest point on the flap to the previous TE
    d = np.hypot(flap[:, 0] - pte[0], flap[:, 1] - pte[1])
    j = int(np.argmin(d))
    ax.plot([pte[0], flap[j, 0]], [pte[1], flap[j, 1]], color=GREEN,
            lw=2.0, zorder=4)
    ax.scatter([pte[0], flap[j, 0]], [pte[1], flap[j, 1]], s=18,
               color=GREEN, zorder=5)
    mid = (0.5 * (pte[0] + flap[j, 0]), 0.5 * (pte[1] + flap[j, 1]))
    ax.annotate("gap", mid, (mid[0] + 0.02, mid[1] + 0.055),
                color=GREEN, fontsize=10, fontweight="bold", ha="left",
                arrowprops=dict(arrowstyle="-", color=GREEN, lw=0.8))

    # overlap: horizontal tuck of the flap nose under the TE
    nose_x = float(flap[:, 0].min())
    y_ov = float(flap[:, 1].min()) - 0.045
    ax.annotate("", (pte[0], y_ov), (nose_x, y_ov),
                arrowprops=dict(arrowstyle="<|-|>", color=GOLD, lw=1.4))
    ax.text(0.5 * (pte[0] + nose_x), y_ov - 0.018, "overlap", color=GOLD,
            fontsize=10, fontweight="bold", ha="center", va="top")
    ax.axvline(pte[0], color=INK3, lw=0.7, ls=":", zorder=0)
    ax.axvline(nose_x, color=INK3, lw=0.7, ls=":", zorder=0)
    ax.text(pte[0], float(main[:, 1].max()) + 0.02, "previous TE",
            color=INK2, fontsize=8, ha="center", va="bottom")

    # zoom into the slot region
    x0 = nose_x - 0.10
    x1 = float(flap[:, 0].max()) + 0.02
    ax.set_xlim(x0, x1)
    ax.set_ylim(y_ov - 0.06, float(main[:, 1].max()) + 0.06)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Slot geometry — gap (nearest clearance) and overlap "
                 "(horizontal tuck)", loc="left", fontsize=9.5,
                 fontweight="bold", color=INK)
    return _save(fig, out, "slot_definition")


def fig_panel_method(out: Path):
    """Panel discretization + normals of a real repaneled section."""
    _, uc = airfoils.repaneled("s1223", 26)
    (P1, d, length, t, nrm, mid, *_rest) = _panelize([uc])
    fig, ax = plt.subplots(figsize=(6.4, 2.5))
    # panels as segments
    P2 = P1 + d
    for a, b in zip(P1, P2):
        ax.plot([a[0], b[0]], [a[1], b[1]], color=BLUE, lw=1.1, zorder=3)
    ax.scatter(P1[:, 0], P1[:, 1], s=6, color=BLUE, zorder=4)
    # a subset of outward normals
    step = 3
    ax.quiver(mid[::step, 0], mid[::step, 1], nrm[::step, 0], nrm[::step, 1],
              color=RED, width=0.0035, scale=22, zorder=5)
    ax.scatter(mid[::step, 0], mid[::step, 1], s=8, color=RED, zorder=5)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Flat-panel discretization: nodes, panels, and outward "
                 "normals at collocation points", loc="left",
                 fontsize=9.5, fontweight="bold", color=INK)
    return _save(fig, out, "panel_method")


def fig_cp(out: Path):
    """Real inviscid Cp, free air vs ground effect, in native panel order.

    Cp is plotted against x without sorting: the panel ordering (TE -> upper
    -> LE -> lower -> TE) traces each surface cleanly, so the curve is a
    smooth loop rather than a sawtooth. A moderate ride height keeps the
    suction peaks readable."""
    cfg = geometry.StackConfig.from_dict({**DEMO, "ride_height_mm": 45})
    design = geometry.build_stack(cfg)
    installed = geometry.install_stack(design, cfg.ride_height_c)
    coords = [e["coords"] for e in installed]
    free, ground = panel.solve_pair(coords, 0.0)
    fig, ax = plt.subplots(figsize=(6.4, 3.2))
    for sol, style, lab in ((free, dict(ls="--", lw=1.1, alpha=0.75),
                             "free air"),
                            (ground, dict(ls="-", lw=1.5), "ground effect")):
        for e in range(len(coords)):
            sel = sol.element_index == e
            x = sol.midpoints[sel, 0]
            cp = sol.cp[sel]
            ax.plot(x, cp, color=SERIES[e], **style,
                    label=f"{design[e]['role']} · {lab}")
    ax.invert_yaxis()
    ax.axhline(0, color=INK3, lw=0.6, zorder=1)
    _style(ax)
    ax.set_xlabel("x  (main-chord units)")
    ax.set_ylabel("pressure coefficient  $C_p$")
    ax.legend(frameon=False, fontsize=7.5, ncol=2, loc="lower center")
    ax.set_title("Inviscid surface pressure — ground effect deepens the "
                 "suction (lower is stronger)", loc="left", fontsize=9.5,
                 fontweight="bold", color=INK)
    return _save(fig, out, "cp")


def fig_ground_model(out: Path):
    """k_g realization curve + real inviscid gain vs ride height."""
    hc = np.linspace(0.005, 0.45, 120)
    kg = np.array([analysis.ground_gain_factor(h) for h in hc])

    # real inviscid ground gain of the demo stack across ride heights
    rides = np.array([12, 18, 25, 35, 50, 70, 100, 140])
    gains, ests = [], []
    for rh in rides:
        cfg = geometry.StackConfig.from_dict({**DEMO, "ride_height_mm": int(rh)})
        design = geometry.build_stack(cfg)
        inst = geometry.install_stack(design, cfg.ride_height_c)
        free, ground = panel.solve_pair([e["coords"] for e in inst], 0.0)
        cf, cg = -free.Cl, -ground.Cl
        gains.append(cg / cf - 1.0)
        est, _ = analysis.corrected_downforce(cf, cg, cfg)
        ests.append(est / cf - 1.0)
    hc_r = rides / DEMO["chord_mm"]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.6, 2.9))
    ax1.plot(hc, kg, color=GREEN, lw=1.8)
    ax1.fill_between(hc, 0, kg, color=GREEN, alpha=0.08)
    _style(ax1)
    ax1.set_xlabel("ride height  $h/c$")
    ax1.set_ylabel("realization factor  $k_g$")
    ax1.set_title("$k_g = 0.85\\,\\tanh(1.4/0.85 \\cdot h/c)$", loc="left",
                  fontsize=8.5, color=INK)
    ax1.set_ylim(0, 0.9)

    ax2.plot(hc_r, 100 * np.array(gains), "o-", color=BLUE, lw=1.5,
             ms=4, label="raw inviscid gain")
    ax2.plot(hc_r, 100 * np.array(ests), "s--", color=RED, lw=1.5,
             ms=4, label="realized (estimate)")
    _style(ax2)
    ax2.set_xlabel("ride height  $h/c$")
    ax2.set_ylabel("downforce gain vs free air  (%)")
    ax2.legend(frameon=False, fontsize=7.5, loc="upper right")
    ax2.set_title("Inviscid gain diverges; the estimate saturates",
                  loc="left", fontsize=8.5, color=INK)
    fig.tight_layout()
    return _save(fig, out, "ground_model")


def fig_induced(out: Path):
    """McCormick ground factor phi vs h/b."""
    hb = np.linspace(0.005, 0.5, 200)
    r = 16.0 * hb
    phi = r ** 2 / (1.0 + r ** 2)
    fig, ax = plt.subplots(figsize=(4.2, 2.6))
    ax.plot(hb, phi, color=GOLD, lw=1.9)
    ax.fill_between(hb, 0, phi, color=GOLD, alpha=0.08)
    _style(ax)
    ax.set_xlabel("height / span  $h/b$")
    ax.set_ylabel("induced-drag factor  $\\phi$")
    ax.set_title("Ground weakens the trailing vortex system",
                 loc="left", fontsize=8.5, color=INK)
    ax.set_ylim(0, 1.02)
    return _save(fig, out, "induced")


def fig_loading(out: Path):
    """Real per-element loading budget bars for the demo stack."""
    # a moderate two-element stack: the main runs over its isolated limit
    # (critical — the classical high-lift situation), the flap comfortably
    # under. The colours and the two threshold lines carry the meaning.
    cfg_l = {
        "elements": [
            {"airfoil": "naca6412", "chord_ratio": 1.0, "deflection_deg": 0},
            {"airfoil": "e423", "chord_ratio": 0.33, "deflection_deg": 16,
             "slot_gap_pct": 2.0, "slot_overlap_pct": 3.0}],
        "stack_aoa_deg": 0.0, "ride_height_mm": 35, "chord_mm": 350,
        "span_mm": 1400, "speed_ms": 16, "ncrit": 7,
        "viscous_efficiency": 0.85, "efficiency_3d": 0.9}
    r = analysis.analyze(geometry.StackConfig.from_dict(cfg_l))
    els = r["elements"]
    fig, ax = plt.subplots(figsize=(6.4, 2.7))
    y = np.arange(len(els))[::-1]
    for i, e in enumerate(els):
        frac = e["loading_fraction"]
        col = (RED if frac > 1.10 else GOLD if frac > 0.90 else GREEN)
        ax.barh(y[i], frac, height=0.52, color=col, alpha=0.85, zorder=3)
        ax.text(frac + 0.03, y[i],
                f"{frac*100:.0f}%   (Cl {e['Cl_checked']:.2f} / "
                f"{e['CL_max_isolated']:.2f})", va="center", fontsize=8,
                color=INK)
    ax.axvline(1.0, color=INK, lw=1.1, ls="--", zorder=4)
    ax.axvline(0.90, color=GOLD, lw=0.8, ls=":", zorder=2)
    ax.text(1.0, -0.75, "isolated $C_{L,max}$ (critical above)", color=INK,
            fontsize=7.5, ha="center", va="top")
    ax.set_yticks(y)
    ax.set_yticklabels([f"E{i+1} {e['role']}" for i, e in enumerate(els)])
    _style(ax, grid=False)
    ax.grid(True, axis="x", color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    ax.set_xlim(0, 1.55)
    ax.set_ylim(-1.0, len(els) - 0.3)
    ax.set_xlabel("working lift / isolated maximum")
    ax.set_title("Element loading budget (free-air load, viscous-realized)",
                 loc="left", fontsize=9.5, fontweight="bold", color=INK,
                 pad=8)
    return _save(fig, out, "loading")


def fig_te_treatment(out: Path):
    """Sharp vs thickened TE on a real section, zoomed on the aft."""
    uc = airfoils.normalize(airfoils.resolve("s1223")[1])
    gap = 3.0 / (0.35 * 350)          # 3 mm on a 122.5 mm flap chord
    thick, _ = mfg.apply_te_treatment(uc, gap, "thicken")
    trunc, meta = mfg.apply_te_treatment(uc, gap, "truncate")
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.6, 2.7))
    for ax, mod, title, col in ((ax1, thick, "Thicken (keep chord)", GREEN),
                                (ax2, trunc, "Cut off the tip", GOLD)):
        ax.plot(uc[:, 0], uc[:, 1], color=INK3, lw=1.0, ls="--",
                label="original (knife edge)")
        ax.plot(mod[:, 0], mod[:, 1], color=col, lw=1.5, label="as-built")
        ax.set_xlim(0.55, 1.02)
        ax.set_aspect("equal")
        _style(ax, grid=False)
        ax.set_yticks([])
        ax.set_xlabel("x")
        ax.legend(frameon=False, fontsize=7, loc="upper right")
        ax.set_title(title, loc="left", fontsize=9, fontweight="bold",
                     color=INK)
    fig.suptitle("Trailing-edge treatments — a knife edge cannot be built",
                 x=0.02, ha="left", fontsize=9.5, fontweight="bold",
                 color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    return _save(fig, out, "te_treatment")


def fig_thickness_floor(out: Path):
    """Thickness distribution: how the floor removes the waist; the plate."""
    xs = np.linspace(0.02, 0.995, 400)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.6, 2.7))

    # left: as6099 buildable — floor lifts the aft to the TE gap
    c_mm = 0.31 * 350
    gap = 3.0 / c_mm
    uc = airfoils.normalize(airfoils.resolve("as6099")[1])
    t0 = mfg.thickness_at(uc, xs) * c_mm
    treated, _ = mfg.apply_te_treatment(uc, gap, "thicken")
    t1 = mfg.thickness_at(treated, xs) * c_mm
    ax1.plot(xs, t0, color=INK3, lw=1.1, ls="--", label="original")
    ax1.plot(xs, t1, color=GREEN, lw=1.6, label="floored (as-built)")
    ax1.axhline(3.0, color=RED, lw=0.9, ls=":", label="TE gap 3 mm")
    _style(ax1)
    ax1.set_xlabel("x")
    ax1.set_ylabel("thickness  (mm)")
    ax1.legend(frameon=False, fontsize=7, loc="upper right")
    ax1.set_title("Buildable section — no waist survives", loc="left",
                  fontsize=8.5, fontweight="bold", color=INK)
    ax1.set_ylim(0, max(t0.max(), 7))

    # right: as6097 plate — thickest point below the requested TE
    c_mm2 = 0.20 * 350
    uc2 = airfoils.normalize(airfoils.resolve("as6097")[1])
    t2 = mfg.thickness_at(uc2, xs) * c_mm2
    ax2.plot(xs, t2, color=RED, lw=1.6, label="original (thickest 3.0 mm)")
    ax2.axhline(3.0, color=INK, lw=0.9, ls=":", label="TE gap 3 mm")
    ax2.fill_between(xs, t2, 3.0, where=t2 < 3.0, color=RED, alpha=0.08)
    _style(ax2)
    ax2.set_xlabel("x")
    ax2.legend(frameon=False, fontsize=7, loc="upper right")
    ax2.set_title("Plate — flagged, not reshaped", loc="left",
                  fontsize=8.5, fontweight="bold", color=INK)
    ax2.set_ylim(0, max(t2.max(), 7))
    fig.tight_layout()
    return _save(fig, out, "thickness_floor")


def fig_shaping(out: Path):
    """Hicks-Henne bumps + a real shaped section."""
    x = np.linspace(0, 1, 300)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.6, 2.7))
    for x0, col, lab in zip(shaping.BUMP_X, (BLUE, GREEN, GOLD),
                            ("front @25%", "mid @55%", "aft @80%")):
        ax1.plot(x, shaping._hh(x, x0), color=col, lw=1.5, label=lab)
    _style(ax1)
    ax1.set_xlabel("x")
    ax1.set_ylabel("bump height")
    ax1.legend(frameon=False, fontsize=7.5, loc="upper right")
    ax1.set_title("Hicks–Henne camber levers", loc="left", fontsize=8.5,
                  fontweight="bold", color=INK)

    uc = airfoils.normalize(airfoils.resolve("s1223")[1])
    shaped = shaping.apply_shape(uc, [0.006, -0.003, 0.010], 1.12)
    ax2.plot(uc[:, 0], uc[:, 1], color=INK3, lw=1.1, ls="--",
             label="base s1223")
    ax2.plot(shaped[:, 0], shaped[:, 1], color=RED, lw=1.5,
             label="shaped (+camber, ×1.12 t)")
    ax2.set_aspect("equal")
    _style(ax2, grid=False)
    ax2.set_yticks([])
    ax2.set_xlabel("x")
    ax2.legend(frameon=False, fontsize=7, loc="lower center")
    ax2.set_title("Shape applied to an existing section", loc="left",
                  fontsize=8.5, fontweight="bold", color=INK)
    fig.tight_layout()
    return _save(fig, out, "shaping")


def fig_optimizer(out: Path):
    """A real (small) optimization run's convergence history."""
    from app.core import optimizer
    job = optimizer.Job(DEMO, {"target_downforce_n": 250, "budget": 800,
                               "mode": "global"})
    job.run()
    hist = job.snapshot()["history"]
    ev = [h["eval"] for h in hist]
    dn = [h["downforce_n"] for h in hist]
    jj = [h["J"] for h in hist]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(6.6, 2.6))
    ax1.plot(ev, dn, color=BLUE, lw=1.5)
    ax1.axhline(250, color=INK, lw=1.0, ls="--")
    ax1.text(ev[-1], 250, " target", color=INK, fontsize=7.5, va="bottom",
             ha="right")
    _style(ax1)
    ax1.set_xlabel("evaluation")
    ax1.set_ylabel("downforce  (N)")
    ax1.set_title("Convergence to target", loc="left", fontsize=8.5,
                  fontweight="bold", color=INK)
    ax2.semilogy(ev, jj, color=GREEN, lw=1.5)
    _style(ax2)
    ax2.set_xlabel("evaluation")
    ax2.set_ylabel("objective  $J$  (log)")
    ax2.set_title("Objective descent", loc="left", fontsize=8.5,
                  fontweight="bold", color=INK)
    fig.tight_layout()
    return _save(fig, out, "optimizer")


# ------------------------------------------------ schematic diagrams

def _box(ax, xy, w, h, title, fill, ec, sub=None, fs=8.5, tc=None):
    """Rounded box with a bold title and an optional subtitle. Titles are
    drawn with real font weight (no mathtext), so underscores and multi-word
    names render literally."""
    ax.add_patch(FancyBboxPatch(
        (xy[0], xy[1]), w, h, boxstyle="round,pad=0.02,rounding_size=0.04",
        linewidth=1.1, edgecolor=ec, facecolor=fill, zorder=2))
    cx = xy[0] + w / 2
    if sub:
        ax.text(cx, xy[1] + h * 0.62, title, ha="center", va="center",
                fontsize=fs, fontweight="bold", color=tc or INK, zorder=3)
        ax.text(cx, xy[1] + h * 0.28, sub, ha="center", va="center",
                fontsize=fs - 1.1, color=tc or INK2, zorder=3)
    else:
        ax.text(cx, xy[1] + h / 2, title, ha="center", va="center",
                fontsize=fs, fontweight="bold", color=tc or INK, zorder=3)


def _arrow(ax, a, b, color=INK2, style="-|>"):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle=style, mutation_scale=11,
                                 color=color, lw=1.0, zorder=1,
                                 shrinkA=2, shrinkB=2))


def fig_architecture(out: Path):
    """Block diagram of the application architecture."""
    fig, ax = plt.subplots(figsize=(6.7, 3.7))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 8)
    ax.axis("off")

    _box(ax, (0.3, 6.6), 4.0, 1.0, "Desktop window", "#eaf0f8", BLUE,
         sub="WebView2 / pywebview", fs=9)
    _box(ax, (0.3, 5.1), 4.0, 1.0, "Browser UI  (static/js)", "#eaf0f8", BLUE,
         sub="vanilla JS, no build step", fs=9)
    _arrow(ax, (2.3, 6.6), (2.3, 6.1))

    _box(ax, (0.3, 3.4), 4.0, 1.1, "FastAPI server  (server.py)", "#eef7ee",
         GREEN, sub="REST · loopback-pinned · /api/…", fs=9)
    _arrow(ax, (2.3, 5.1), (2.3, 4.5), color=GREEN)
    ax.text(2.5, 4.8, "HTTP / JSON", fontsize=7, color=INK3, style="italic")

    core = [
        ("geometry", "stack, slots, frames"),
        ("panel", "Hess–Smith + images"),
        ("analysis", "estimate, loading, drag"),
        ("viscous", "NeuralFoil · XFOIL"),
        ("optimizer", "DE + Nelder–Mead"),
        ("mfg · shaping", "TE prep · Hicks–Henne"),
        ("airfoils", "resolve · library"),
        ("export", "DXF/DAT/SVG/ZIP"),
    ]
    x0, y0, w, h, gx, gy = 5.0, 5.55, 2.15, 0.86, 0.35, 0.26
    for k, (nm, sub) in enumerate(core):
        cx = x0 + (k % 2) * (w + gx)
        cy = y0 - (k // 2) * (h + gy)
        _box(ax, (cx, cy), w, h, nm, "#fbf7ec", GOLD, sub=sub, fs=8)
    ax.text(5.0, 6.6, "app.core  — pure NumPy, framework-free",
            fontsize=8.5, color=INK, fontweight="bold")
    _arrow(ax, (4.3, 3.95), (5.0, 4.7), color=GOLD)

    _box(ax, (0.3, 1.6), 4.0, 1.1, "NeuralFoil surrogate", "#f8ecec", RED,
         sub="+ bundled XFOIL 6.99", fs=9)
    _box(ax, (0.3, 0.3), 4.0, 1.0, "UIUC airfoil database", "#f8ecec", RED,
         sub="2,174 sections", fs=9)
    _arrow(ax, (5.6, 5.4), (3.7, 2.7), color=RED)
    _arrow(ax, (5.6, 5.4), (3.7, 1.3), color=RED)

    ax.set_title("Architecture — one local process, aerodynamics isolated "
                 "in app.core", loc="left", fontsize=9.5, fontweight="bold",
                 color=INK, y=1.0)
    return _save(fig, out, "architecture")


def fig_spec_chain(out: Path):
    """The airfoil spec resolution chain."""
    fig, ax = plt.subplots(figsize=(6.7, 3.3))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 7.4)
    ax.axis("off")

    _box(ax, (3.2, 6.3), 3.6, 0.9, "airfoils.resolve(spec)", "#eef1f5", INK2,
         fs=9)
    layers = [
        ("shape:  b · b · b · ts · <base>", "camber bumps + thickness scale",
         GOLD),
        ("mfg:  mode · gap · <base>", "trailing-edge treatment", GREEN),
        ("custom:<id>", "uploaded .dat  (in-memory)", BLUE),
        ("nacaXXXX", "4-digit, generated analytically", BLUE),
        ("UIUC name   /   path.dat", "bundled library · project file", RED),
    ]
    y = 5.2
    for i, (nm, sub, col) in enumerate(layers):
        _box(ax, (1.0, y), 5.4, 0.72, nm, "white", col, fs=8.5)
        ax.text(6.7, y + 0.42, sub, fontsize=7.3, color=INK2, va="center")
        if i < len(layers) - 1:
            ax.text(6.7, y + 0.08, "unwraps to base ↓", fontsize=6.5,
                    color=INK3, style="italic", va="center")
        _arrow(ax, (3.6, 6.3), (3.7, y + 0.72))
        y -= 1.0
    ax.text(0.2, 0.05,
            "Wrappers nest outward: a shaped, manufactured library section is "
            "shape:…:mfg:…:s1223 — every layer\nis a first-class airfoil to "
            "the solver, polars, screener and exporters.",
            fontsize=7.2, color=INK2)
    ax.set_title("Airfoil spec resolution — one string names any section, "
                 "raw or modified", loc="left", fontsize=9.5,
                 fontweight="bold", color=INK, y=1.0)
    return _save(fig, out, "spec_chain")


def fig_pipeline(out: Path):
    """The analysis pipeline from config to reported forces."""
    fig, ax = plt.subplots(figsize=(6.7, 2.4))
    ax.set_xlim(0, 12.5)
    ax.set_ylim(0, 3)
    ax.axis("off")
    steps = [
        ("StackConfig", "validated inputs", BLUE),
        ("build_stack", "slots solved", BLUE),
        ("solve_pair", "free + ground", GREEN),
        ("corrected", "η,  k_g", GREEN),
        ("viscous", "budget + drag", GOLD),
        ("forces", "+ warnings", RED),
    ]
    w, gx = 1.72, 0.30
    x = 0.15
    for i, (nm, sub, col) in enumerate(steps):
        _box(ax, (x, 1.0), w, 1.1, nm, "white", col, sub=sub, fs=8)
        if i < len(steps) - 1:
            _arrow(ax, (x + w, 1.55), (x + w + gx, 1.55))
        x += w + gx
    ax.set_title("Analysis pipeline (analysis.analyze)", loc="left",
                 fontsize=9.5, fontweight="bold", color=INK, y=0.98)
    return _save(fig, out, "pipeline")


ALL = [
    fig_architecture, fig_pipeline, fig_spec_chain, fig_slot_definition,
    fig_panel_method, fig_cp, fig_ground_model, fig_induced, fig_loading,
    fig_te_treatment, fig_thickness_floor, fig_shaping, fig_optimizer,
]


def build_all(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    made = {}
    for fn in ALL:
        name = fn.__name__.replace("fig_", "")
        made[name] = fn(out)
        print(f"  figure: {name}")
    return made


if __name__ == "__main__":
    d = ROOT / "docs" / "_build"
    build_all(d)
    print(f"figures written to {d}")
