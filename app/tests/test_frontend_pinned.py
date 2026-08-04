"""Pinned-library frontend contract (no server).

Static source checks in the test_frontend_fl2d_state.py spirit, pinning the
two-tier design:
  * workspace pins keep their exact semantics — pinCurrent still pushes,
    renders, persists; the preset switch still wipes state.pins; project
    open still replaces them; sessionSnapshot does NOT carry the library;
  * every pin is ALSO auto-mirrored to the durable library, and the mirror
    path never touches state.pins (a mirror failure must not eat the
    workspace pin);
  * the library pin embeds custom-airfoil .dat text and the Load path
    re-registers + remaps it (uploads live in server memory — a pin that
    outlives the process must not die with it), behind the jobsRunning
    gate and resetWorkspaceResults;
  * the tab shell exists with all its ids, no existing id was renamed, and
    the compare renderer builds DOM via createElement/textContent — the
    only innerHTML writes are the "" clears;
  * <img> sources are assigned only after the data-URI shape check
    (library files can be hand-edited);
  * flow thumbnails are downscaled before storage (canvas -> JPEG);
  * the api wrappers exist and the save carries the rev token;
  * savePinnedLib resolves a 409 by re-GET + re-apply + retry-once.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_frontend_pinned.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
api = (ROOT / "app" / "static" / "js" / "api.js").read_text(encoding="utf-8")
charts = (ROOT / "app" / "static" / "js" / "charts.js").read_text(encoding="utf-8")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
css = (ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


def body(src, marker):
    i = src.index(marker)
    j = src.index("\n}", i)
    return src[i:j]


# ---- tab shell ----------------------------------------------------------
check("nav carries the Pinned tab button",
      'data-tab="pinned"' in html and 'id="tab-pinned"' in html)
for i in ["pinned-grid", "pinned-empty", "pinned-note",
          "btn-pinned-compare", "pinned-compare", "pinned-cmp-table",
          "pinned-cmp-outline", "pinned-cmp-images", "btn-pinned-cp",
          "pinned-cmp-note", "pinned-cmp-cp"]:
    check(f"#{i} present in index.html", f'id="{i}"' in html)
for i in ["pins-list", "pins-empty", "btn-pin"]:
    check(f"existing #{i} still present", f'id="{i}"' in html)
for t in ["pressure", "polars", "optimizer", "screener", "maps", "rans",
          "fluent2d", "export"]:
    check(f"existing data-tab={t} still present", f'data-tab="{t}"' in html)
check("tab handler enters the pinned tab",
      'if (t.dataset.tab === "pinned") enterPinnedTab();' in js)
check("theme change re-inks the pinned tab when active",
      'active.dataset.tab === "pinned"' in js)

# ---- two-tier contract --------------------------------------------------
pc = body(js, "function pinCurrent")
check("pinCurrent still pushes, renders and persists the workspace pin",
      "state.pins.push" in pc and "renderPins()" in pc
      and "persistSession()" in pc)
check("pinCurrent also mirrors to the durable library",
      "mirrorPinToLibrary(pin)" in pc)
check("pins now carry an id (the library's merge identity)",
      "id: randId()" in pc)
check("the pin headline carries the bound mark + trust context",
      "drag_is_lower_bound" in pc and "frac_max" in pc
      and "confidence_min" in pc)
mirror = body(js, "async function mirrorPinToLibrary")
check("the mirror never touches state.pins (a mirror failure must not eat "
      "the workspace pin)", "state.pins" not in mirror)
check("a mirror failure surfaces as a toast, not a lost pin",
      "toast(" in mirror and "not updated" in mirror)
check("the mirror embeds custom .dat text and airfoil names",
      "usedCustomSpecs()" in mirror and "state.customDat" in mirror
      and "airfoil_names" in mirror)
check("the mirror captures the outline and flow thumbnails",
      "outlineSnapshot()" in mirror and "captureFlowThumbs()" in mirror)
check("sessionSnapshot does NOT carry the library",
      "pinnedLib" not in body(js, "function sessionSnapshot"))
check("the preset switch still wipes workspace pins",
      "state.pins = []" in js)
check("project open still replaces workspace pins from the file",
      "state.pins = Array.isArray(p.pins)" in js)

# ---- store sync ---------------------------------------------------------
sv = body(js, "async function savePinnedLib")
check("savePinnedLib sends the rev token",
      "state.pinnedLib.rev" in sv)
check("a 409 is resolved by re-GET + re-apply + retry-once (the body is "
      "not parseable through ApiError)",
      "409" in sv and "loadPinnedLib()" in sv
      and sv.count("await attempt()") == 2)
check("api wrappers exist and the save carries rev",
      "pinnedDesigns: ()" in api
      and "pinnedDesignsSave: (pins, rev)" in api
      and '{ pins, rev }' in api)
check("the tab re-GETs on every entry (two-window honesty)",
      "await loadPinnedLib()" in body(js, "async function enterPinnedTab"))

# ---- load path ----------------------------------------------------------
lp = body(js, "async function loadLibraryPin")
check("Load is gated on running jobs",
      "jobsRunning()" in lp)
check("Load re-registers embedded uploads and remaps wrapped specs",
      "uploadAirfoil" in lp and "custom:[a-z0-9_-]+$" in lp)
check("Load is a full context switch",
      "resetWorkspaceResults()" in lp and "configRevision++" in lp
      and "writeConfigToForm()" in lp)

# ---- rendering safety ---------------------------------------------------
rt = body(js, "function renderPinnedTab")
bc = body(js, "function buildCompare")
check("the tab renderer builds DOM via textContent (no interpolated "
      "innerHTML)",
      "textContent" in rt
      and not re.search(r'innerHTML\s*=(?!\s*"";)', rt))
check("the compare renderer builds DOM via textContent (no interpolated "
      "innerHTML)",
      "textContent" in bc
      and not re.search(r'innerHTML\s*=(?!\s*"";)', bc))
th = body(js, "function pinnedThumb")
check("card <img> src is assigned only after the data-URI shape check",
      "PIN_IMG_RE.test(uri)" in th)
check("compare images pass the same check",
      "PIN_IMG_RE.test(uri)" in bc)
check("bounded values are excluded from the compare winner computation",
      "excluded" in bc and "cannot win" in bc)
shrink = body(js, "async function shrinkDataURI")
check("flow thumbnails are downscaled before storage (canvas -> JPEG)",
      "drawImage" in shrink and 'toDataURL("image/jpeg"' in shrink)
check("stackPreview exists in the chart engine (shared bounds, equal "
      "aspect, one closed subpath per contour)",
      "export function stackPreview" in charts)
check("the Cp overlay is button-gated, serial, cached per pin id",
      "pinnedCp" in js and 'id="btn-pinned-cp"' in html
      and "api.analyze" in body(js, "async function overlayCompareCp"))

# ---- css + cache-busts --------------------------------------------------
check("the pinned grid and thumb styles exist",
      ".pinned-grid" in css and ".pin-thumb" in css
      and ".pin-cmp-images" in css)
css_v = int(re.search(r"app\.css\?v=(\d+)", html).group(1))
js_v = int(re.search(r"app\.js\?v=(\d+)", html).group(1))
check("cache-busts ratcheted for the round (css >= 27, js >= 29)",
      css_v >= 27 and js_v >= 29, f"(css v{css_v}, js v{js_v})")

print(f"\n{sum(results)}/{len(results)} pinned frontend checks passed")
sys.exit(0 if all(results) else 1)
