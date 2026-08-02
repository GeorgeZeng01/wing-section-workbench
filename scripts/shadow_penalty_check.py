"""Stage 0 diagnostic: is the wake-shadow screen's problem penalty SHAPE or
screen BLINDNESS?

Measures only. Changes nothing: shipped thresholds and weights are not this
script's to move. It exists to produce the numbers that decision needs.

The question: the optimizer keeps returning designs that RANS later finds
separated. Two different failures look identical from outside --

  (a) gate exploitation: the screen is right, but the penalty is so cheap
      that the optimizer parks candidates just inside the gate;
  (b) screen blindness: the screen genuinely cannot see the failure.

They need opposite fixes, so distinguishing them comes first.

Method: convert the shadow penalty into the only unit the objective really
trades in -- newtons of downforce. In max-downforce mode

    j = 60.0 * (1.0 - downforce_n / dscale) + w_drag*... + j_pen

so dJ/dD = -60/dscale, and a penalty p is worth p*dscale/60 newtons.

Run:  .venv\\Scripts\\python.exe scripts\\shadow_penalty_check.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.core import optimizer, wake_shadow  # noqa: E402

SEP = wake_shadow.SHADOW_SEP
WARN = wake_shadow.SHADOW_WARN
BAND = WARN - SEP


def shadow_penalty(v, sep_i=SEP, slid=False):
    """The shipped absolute-line penalty, transcribed from optimizer.py:816-827."""
    if v is None:
        return 0.0
    if v < sep_i:
        return ((0.0 if slid else optimizer.SHADOW_RAMP_W)
                + optimizer.SHADOW_WALL_W
                * ((sep_i - v) / optimizer.SHADOW_SCALE) ** 2)
    if not slid and v < sep_i + BAND:
        return optimizer.SHADOW_RAMP_W * ((sep_i + BAND - v) / BAND) ** 2
    return 0.0


def newtons(pen, dscale):
    """A penalty's worth in downforce, at the max-downforce exchange rate."""
    return pen * dscale / 60.0


print("wake-shadow penalty, expressed in the unit the objective trades in")
print(f"  SHADOW_SEP  = {SEP}   (measured separation line)")
print(f"  SHADOW_WARN = {WARN}   (caution band top)")
print(f"  RAMP_W = {optimizer.SHADOW_RAMP_W}  WALL_W = {optimizer.SHADOW_WALL_W}"
      f"  SCALE = {optimizer.SHADOW_SCALE}  GATE_SLACK = {optimizer.SHADOW_GATE_SLACK}")
print()

for dscale in (200.0, 500.0, 1000.0):
    print(f"--- dscale = {dscale:.0f} N (max-downforce reward scale) ---")
    print(f"  {'shadow_min':>10}  {'penalty':>9}  {'worth (N)':>10}   note")
    rows = [
        (0.620, "healthiest attached element in the record"),
        (0.560, "comfortably clear of the band"),
        (0.533, "lowest ATTACHED-measured element in the record"),
        (0.530, "top of the caution band (penalty starts here)"),
        (0.524, "top of the no-wall-truth 'flagged' gray"),
        (0.519, "bottom of that gray"),
        (0.509, "THE SAVED BEST CANDIDATE (shadow_warn=True)"),
        (0.505, "SEP + GATE_SLACK, the effective gate"),
        (0.500, "the measured separation line"),
        (0.459, "shallowest SEPARATED-measured element"),
        (0.389, "deepest separated-measured element"),
    ]
    for v, note in rows:
        p = shadow_penalty(v)
        print(f"  {v:>10.3f}  {p:>9.3f}  {newtons(p, dscale):>10.2f}   {note}")
    full_ramp_n = newtons(optimizer.SHADOW_RAMP_W, dscale)
    print(f"  => the ENTIRE gray-band ramp is worth {full_ramp_n:.2f} N of downforce")
    print()

print("Reading:")
print("  The ramp is deliberately capped at RAMP_W=0.3 so that, per the source")
print("  comment, 'a graze never leaves the clean pool on penalty alone'. The")
print("  consequence is that the whole caution band costs single-digit newtons.")
print("  If moving out of the band costs more downforce than that -- and on a")
print("  loaded 3-element stack it does -- then sitting in the band is the")
print("  optimizer's CORRECT play under the objective as written.")
print()
print("  The saved best candidate sits at 0.509: inside the caution band, and")
print(f"  {0.509 - (SEP + optimizer.SHADOW_GATE_SLACK):.3f} above the effective gate.")
print("  That is the gate-exploitation signature, not screen blindness.")
print()
print("  n=1 on the candidate. What would settle it: a full optimizer run,")
print("  histogramming shadow_min across the archive. If the mass piles against")
print("  0.505-0.53, shape is the problem and the fix is weights, not a new metric.")
