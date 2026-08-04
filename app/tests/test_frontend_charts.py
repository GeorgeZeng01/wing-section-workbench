"""Chart lifecycle contract: hidden-tab charts must self-heal (no server).

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_frontend_charts.py

A chart drawn into a hidden tab measures clientWidth 0 and bakes the 480px
fallback into its viewBox; until this round nothing redrew it on tab
activation, so the svg letterboxed (compressed drawing, off-register hover)
and the optimizer grid's 190px row minimum stretched the content apart
vertically. Static contract checks pin the cure:
  * charts.js keeps a redraw registry behind one shared ResizeObserver,
    recorded BEFORE the no-data early return so empty charts heal too;
  * the observer skips zero-width (still hidden) containers and mirrors
    lineChart's width clamp, so a replay cannot loop;
  * the hover math inverts the svg's xMidYMid-meet mapping instead of
    assuming the drawing fills its rect;
  * the tooltip's left offset is floored and clamped by its measured width
    (the magic 150 is gone), and .chart-tip carries a max-width;
  * .opt-charts no longer forces 190px rows (the svgs are fixed-height;
    the minimum only manufactured voids) and zeroes .chart-host's floor;
  * no stylesheet rule uses an :empty selector on chart hosts (documented
    collision with lineChart emptying the container before measuring);
  * the exchange-rate label is short enough for the 300px column, with the
    full name in the title, and .f-label ellipsizes instead of overflowing;
  * the theme-change listener re-inks settled optimizer/RANS results
    (their colors are resolved at draw time), guarded against live polls;
  * the cache-bust versions ratcheted so clients drop the stale copies.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

charts = (ROOT / "app" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
css = (ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def block(src, marker, end):
    i = src.index(marker)
    return src[i:src.index(end, i)]


# ---- the redraw registry -----------------------------------------------
check("charts.js keeps a redraw registry behind one shared ResizeObserver",
      "new ResizeObserver(" in charts and "REDRAW" in charts)
check("registration happens before the no-data early return, so empty "
      "charts self-heal too",
      charts.index("watchRedraw(container, spec, W)")
      < charts.index('"no data"'))
replay = block(charts, "function replayIfStale", "\n}")
check("the replay skips still-hidden containers and mirrors lineChart's "
      "width clamp — the anti-loop contract",
      "clientWidth" in replay and "cw > 0" in replay
      and "Math.max(cw, 280)" in replay)
check("the observer is guarded for environments without ResizeObserver",
      'if (typeof ResizeObserver === "undefined") return;' in charts)
check("a synchronous sweep exists for tab activation — observer callbacks "
      "ride the rendering pipeline and a non-compositing page defers them",
      "export function redrawStaleCharts()" in charts
      and "replayIfStale(c)" in block(charts, "export function redrawStaleCharts", "\n}"))
tab_handler = block(js, 'document.querySelectorAll(".tab").forEach', "\n});")
check("the tab handler replays stale charts right after the page classes "
      "flip, before any per-tab hook",
      "redrawStaleCharts()" in tab_handler
      and tab_handler.index("tab-${t.dataset.tab}`)")
      < tab_handler.index("redrawStaleCharts()"))
check("app.js imports the sweep from the chart engine",
      "redrawStaleCharts } from \"./charts.js\"" in js)

# ---- hover math ---------------------------------------------------------
check("the old fill-assuming hover formula is gone",
      "* (W / r.width)" not in charts)
check("the hover inverts xMidYMid meet (scale + centering offset)",
      "Math.min(r.width / W, r.height / H)" in charts
      and "(r.width - W * sc) / 2" in charts)

# ---- tooltip clamp ------------------------------------------------------
check("the tip's left offset is floored and clamped by its measured width",
      "Math.max(4, Math.min(cx + 14," in charts
      and "tipG.offsetWidth" in charts)
check("the magic 150px clamp is gone",
      'r.width - 150) + "px"' not in charts)
tip_rule = block(css, ".chart-tip {", "}")
check(".chart-tip cannot exceed its host (max-width) and stays inert at "
      "z-index 20", "max-width" in tip_rule and "z-index: 20" in tip_rule
      and "pointer-events: none" in tip_rule)

# ---- optimizer grid layout ---------------------------------------------
opt_rule = block(css, ".opt-charts {", "}")
check(".opt-charts rows size to content — the 190px minimum is gone",
      "minmax(190px" not in opt_rule)
check(".opt-charts packs rows at the top, else the parent grid's stretch "
      "re-inflates the note row", "align-content: start" in opt_rule)
check(".opt-charts chart hosts drop the generic 190px floor",
      ".opt-charts > .chart-host" in css
      and "min-height: 0" in block(css, ".opt-charts > .chart-host", "}"))
check("the generic .chart-host floor survives for the other tabs",
      "min-height: 190px" in block(css, ".chart-host {", "}"))
check("no stylesheet rule uses :empty on a host (comments may mention it)",
      re.search(r"(?m)^\s*[#.\w][^{}/*\n]*:empty[^{}\n]*\{", css) is None)

# ---- label overflow -----------------------------------------------------
check("the exchange-rate label fits the 300px column; the full name lives "
      "in the title",
      ">Drag exchange</span>" in html and "Drag exchange rate" in html)
check(".f-label ellipsizes instead of bleeding out of its cell",
      "text-overflow: ellipsis" in block(css, ".f-label {", "}"))

# ---- theme re-ink gap ---------------------------------------------------
theme = block(js, 'window.addEventListener("wss-themechange"', "\n});")
check("theme change re-inks a settled optimizer result, never a live poll",
      "renderOptimizer(state.optResult)" in theme
      and "!state.optJob" in theme)
check("theme change re-inks a stored RANS result",
      "renderRans(state.ransResult)" in theme)

# ---- cache-busts --------------------------------------------------------
css_v = int(re.search(r"app\.css\?v=(\d+)", html).group(1))
js_v = int(re.search(r"app\.js\?v=(\d+)", html).group(1))
check("cache-bust ratchet: app.css >= 27, app.js >= 29",
      css_v >= 27 and js_v >= 29, f"(css v{css_v}, js v{js_v})")

print(f"\n{sum(results)}/{len(results)} frontend chart checks passed")
sys.exit(0 if all(results) else 1)
