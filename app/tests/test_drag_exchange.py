"""The drag exchange rate: the optimizer's balance knob in physical units.

The objective always had a downforce/drag trade buried in `drag_weight`,
but at its old default (0.10) a newton of drag cost 1/60 of a newton of
downforce — drag was so nearly free that "maximize downforce" behaved as
pure maximization. The knob is now expressed as the rate it actually is:

    k = newtons of downforce given up to remove one newton of drag
    w = 6k   (terms are 60*(1 - D/scale) and 10w*drag/scale)

Pins the conversion, the neutral-trade algebra it claims, and the wiring.

Run directly:  .venv\\Scripts\\python.exe app\\tests\\test_drag_exchange.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import optimizer  # noqa: E402

results = []


def check(label, ok, extra=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {label} {extra}")


# ---- the conversion, both ways ----------------------------------------
check("k -> w is 6k", optimizer.drag_weight_from_k(0.25) == 1.5
      and optimizer.drag_weight_from_k(1.0) == 6.0)
check("w -> k round-trips",
      abs(optimizer.k_from_drag_weight(optimizer.drag_weight_from_k(0.37))
          - 0.37) < 1e-12)
check("k = 0 is a legal rate meaning 'ignore drag'",
      optimizer.drag_weight_from_k(0.0) == 0.0)
check("the OLD default w=0.10 was k = 1/60 — drag very nearly free, which "
      "is why max mode read as pure maximization",
      abs(optimizer.k_from_drag_weight(0.10) - 1.0 / 60.0) < 1e-9)
check("the NEW default w=1.5 is k = 0.25, the FSAE 'aero pays above L/D 4' "
      "rule of thumb",
      abs(optimizer.k_from_drag_weight(1.5) - 0.25) < 1e-12)

# ---- the algebra the rate CLAIMS, against the real objective terms -----
# max mode: J = 60*(1 - D/scale) + w*10*drag/scale + penalty.
# A trade of dD downforce for ddrag drag is neutral when the two deltas
# cancel; that ratio must equal k, or the label lies.
for k in (0.05, 0.25, 1.0, 3.0):
    w = optimizer.drag_weight_from_k(k)
    scale = 400.0
    d_down = 60.0 / scale          # dJ per newton of downforce (magnitude)
    d_drag = w * 10.0 / scale      # dJ per newton of drag
    check(f"at k={k} the objective's neutral trade really is {k} N "
          f"downforce per N drag",
          abs(d_drag / d_down - k) < 1e-12,
          f"(ratio {d_drag / d_down:.4f})")

# ---- validator + wiring ------------------------------------------------
src = (ROOT / "app" / "core" / "optimizer.py").read_text(encoding="utf-8")
check("the stored default stays 0.10 — API callers and target mode keep "
      "the behaviour every recorded baseline was measured under",
      '("drag_weight", 0.10, 0.0, 60.0)' in src)
check("the range reaches k = 10 (heavily drag-averse) without clipping",
      optimizer.drag_weight_from_k(10.0) <= 60.0)
check("drag_weight is still the wire field — saved automations keep working",
      src.count('options.get("drag_weight"') >= 2)

js = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
check("the panel asks for k and converts to the stored weight",
      'opt-drag-k' in js and "6 * dragK" in js)
check("the rate is transmitted in BOTH modes — the old client-side pin "
      "that silently discarded target-mode k is gone",
      ": 0.1;" not in js.split("const dragK")[1][:400]
      and "isMax ?" not in js.split("const dragK")[1][:200])
check("the attain phase caps the rate at the descend floor (its drag term "
      "shapes the approach path; uncapped it defeats the early stop)",
      "min(w_drag, DESCEND_DRAG_W)" in src)
check("descend and _rescore PIN the drag weight — at a held level drag "
      "minimization is rate-independent, and a measured 2.0 cap still "
      "walked 7% off-level (both sites, or ranking disagrees with search)",
      src.count('DESCEND_DRAG_W * ev["drag_n"]') == 1
      and src.count('DESCEND_DRAG_W * a["drag_n"]') == 1
      and "max(w_drag, DESCEND_DRAG_W)" not in src)
check("the panel's field is labelled as a rate, not an opaque penalty "
      "(short span so the 300px column never clips; full name in the title)",
      'id="opt-drag-k"' in html and ">Drag exchange</span>" in html
      and "Drag exchange rate" in html and 'value="0.25"' in html)
check("the old opaque field is gone everywhere",
      "opt-drag-w" not in js and "opt-drag-w" not in html)
check("the rate persists with the rest of the panel",
      '"opt-drag-k", "opt-minld"' in js)

# ---- the clamps, on a live Job (no search needed) --------------------
CFG = {
    "elements": [
        {"airfoil": "s1223", "chord_ratio": 1.0, "deflection_deg": 0},
        {"airfoil": "s1223", "chord_ratio": 0.35, "deflection_deg": 12,
         "slot_gap_pct": 1.5, "slot_overlap_pct": 3.0}],
    "stack_aoa_deg": 0.0, "ride_height_mm": 30, "chord_mm": 350,
    "span_mm": 1400, "speed_ms": 15,
}


def rescore_at(w):
    job = optimizer.Job(dict(CFG), {"target_downforce_n": 250.0,
                                    "budget": 400, "mode": "global",
                                    "drag_weight": w})
    job.dstar = 250.0
    return job._rescore({"downforce_n": 250.0, "drag_n": 10.0,
                         "penalty": 0.0})


r_vals = [rescore_at(w) for w in (0.1, 2.0, 6.0, 60.0)]
check("descend re-scoring is rate-independent — every w lands on the "
      "pinned 0.30 (the level is the contract)",
      all(abs(r - 0.30 * 10.0 / 250.0 * 10.0) < 1e-12 for r in r_vals),
      f"({[round(r, 4) for r in r_vals]})")

# ---- the promise the cap protects: a k = 10 target run holds its level --
job = optimizer.Job(dict(CFG), {"target_downforce_n": 250.0,
                                "budget": 500, "mode": "global",
                                "drag_weight": 60.0})
job.run()
s = job.snapshot()
w0 = ((s["candidates"] or [{}])[0].get("summary")) or {}
check("k = 10: the run completes", s["state"] == "done",
      f"({s['state']})")
check("k = 10: the winner still holds the level (drag aversion cannot buy "
      "drag by walking off target)",
      w0 and abs(w0["downforce_n"] - 250.0) <= 0.03 * 250.0,
      f"(got {w0.get('downforce_n')} N)")
check("the snapshot echoes the run's own drag_weight — the Pareto marker "
      "must mark the trade THIS run made, not a form value edited since",
      s.get("drag_weight") == 60.0, f"({s.get('drag_weight')})")

print(f"\n{sum(results)}/{len(results)} drag-exchange checks passed")
sys.exit(0 if all(results) else 1)
