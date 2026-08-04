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
check("the rate drives MAX mode only; target mode keeps the legacy weight "
      "(it balances by construction — attain the level, descend on drag)",
      '"max_downforce"' in js.split("const dragK")[1][:400]
      and ": 0.1;" in js.split("const dragK")[1][:400])
check("the panel's field is labelled as a rate, not an opaque penalty "
      "(short span so the 300px column never clips; full name in the title)",
      'id="opt-drag-k"' in html and ">Drag exchange</span>" in html
      and "Drag exchange rate" in html and 'value="0.25"' in html)
check("the old opaque field is gone everywhere",
      "opt-drag-w" not in js and "opt-drag-w" not in html)
check("the rate persists with the rest of the panel",
      '"opt-drag-k", "opt-minld"' in js)

print(f"\n{sum(results)}/{len(results)} drag-exchange checks passed")
sys.exit(0 if all(results) else 1)
