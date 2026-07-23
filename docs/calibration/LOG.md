# RANS calibration campaign log

Chronological record of the cross-referencing campaign: every RANS run
that informs a calibration constant is one row in `runs.csv` (UTC
timestamps, machine-readable); this file records the order, the intent
and the conclusions. Constants changed on the strength of these runs
cite the run labels that justified them.

Method notes, fixed up front:

- **Geometry**: the sharp (no manufacturing prep) two-element baseline —
  the same section as the historical verified comparison at 30 mm, so
  old and new points stay on one curve. `scripts/rans_calibration.py`
  pins the exact config.
- **Calibration points must be attached-flow cases.** Steady RANS on a
  separated high-lift stack settles into a bounded limit cycle, not a
  number (measured: the aggressive 3-element case at Cl 8.6 ± —, the
  2-element coarse case Cl 2.71 ± 0.15). Separated or heavily
  oscillating runs mark the model's validity boundary and test the
  trust badges; they are never curve-fit points.
- **Verdict discipline**: only runs whose convergence verdict is
  "force history converged" or "residuals converged" contribute a
  `suggested_k_g` (cfd_run refuses otherwise); cap-limited trending
  runs are logged as failures of the point, not data.
- **Mesh error is bounded, not assumed away**: one fine-mesh anchor at
  the 30 mm calibration point quantifies the coarse-mesh bias before
  any constant is refitted from coarse-mesh points.

## 2026-07-22 — Stage 0: probe (30 mm coarse)

Purpose: price a coarse run's wall time on this machine, prove the
headless campaign loop end-to-end, and cross-check the runner against
the historically verified point (coarse 2-element at 30 mm force-stopped
~2,170 iterations at Cl 2.71 ± 0.15, +1.3 % vs C_est). A headless
result materially different from the historical one would mean the
campaign harness — not the physics — is broken, and would stop the
campaign here.

Result (`probe-h30-coarse`, 95 s wall, force-converged at 2,196
iterations): **harness validated** — Cl 2.7123 ± 0.151 reproduces the
historically verified run (Cl 2.71 ± 0.15, ~2,170 iterations) to within
the limit-cycle noise. The comparison column, however, reframes the
historical "+1.3 %" figure: that was against a *pinned* k_g from a prior
Apply loop; against the **default auto curve** (`ground_gain_factor`,
k_g = 0.119 at h/c 0.0857) the panel estimate is C_est 3.838 vs RANS
2.712 — **-29.3 %**, with the RANS-implied k_g at 0.034. The default
curve materially over-realizes the inviscid ground gain at racing ride
height on this section; mapping how that error moves with h/c is
Stage 1's job. Wall-time price: ~95 s per coarse point, so the five-point
sweep is ~10 minutes — cheap enough to extend if the curve shape asks
for it.

## 2026-07-22 — Stage 1: coarse ride-height sweep (15/25/40/60/90 mm)

Purpose: one point per ride height across the operating range
(h/c 0.043–0.257, straddling the choke constant 0.045 and the model's
C_est peak near h/c 0.065), each contributing a RANS-implied k_g from a
converged run only. Deliverable: the measured k_g(h/c) set against the
current `ground_gain_factor()` curve, a refit proposal, and a fine-mesh
anchor at 30 mm bounding the coarse-mesh bias — reported for review
BEFORE any constant changes.

Result (5/5 converged, ~100–145 s each; runs `sweep-h15…h90-coarse`,
fit via `scripts/rans_calibration_fit.py`):

| h (mm) | h/c   | Cl RANS        | C_est (auto) | Δ      | implied k_g |
|-------:|------:|---------------:|-------------:|-------:|------------:|
| 15     | 0.043 | 1.193 ± 0.052  | 3.804        | −68.6 % | — (below floor) |
| 25     | 0.071 | 2.433 ± 0.079  | 3.867        | −37.1 % | 0.011 |
| 30     | 0.086 | 2.712 ± 0.151  | 3.838        | −29.3 % | 0.034 |
| 40     | 0.114 | 2.804 ± 0.081  | 3.754        | −25.3 % | 0.057 |
| 60     | 0.171 | 3.936 ± 0.042  | 3.601        | +9.3 %  | 0.294 |
| 90     | 0.257 | 4.590 ± 0.016  | 3.440        | +33.4 % | 0.691 |

(c_free is 2.651 throughout; c_ground falls 45.2 → 6.8 across the sweep.)

Two structural findings, not parameter tweaks:

1. **The measured realization curve is convex in h/c** — near-zero
   through racing heights (0.01–0.06 below h/c 0.115), then steeply
   rising (0.29 at 0.171, 0.69 at 0.257). The concave
   `k_g = A·tanh(S/A · h/c)` form cannot represent this at any (A, S):
   the least-squares refit lands at rms 0.15 in k_g units and makes the
   racing-height error WORSE (+85 % at 25 mm). Refitting parameters
   inside the current form is therefore not an option; the curve (or the
   gain model around it) needs a different shape.
2. **At 15 mm the RANS Cl (1.19) sits far below the model's floor**
   η·C_f = 2.25. The model's choke term multiplies only the *gain*, so
   C_est can never fall below η·C_f — but the flow near choke loses
   lift outright (venturi separation). The model cannot represent
   ground-induced loss at all.

Caveats before any constant changes (why the campaign continues rather
than concluding here): coarse mesh (~15k cells) with wall functions and
fully-turbulent k-ω SST at Re ≈ 3.5×10⁵ on a laminar-design section
(s1223) plausibly separates earlier than transitional reality — the
low-height collapse may be partly mesh/turbulence-model artifact, in the
direction that exaggerates finding 2. Queued to resolve:
`anchor-h30-fine` (mesh sensitivity at the calibration point) and
`tail-h150/h300-coarse` (the h→∞ limit: RANS Cl must return toward the
viscous free-air value, which prices the η = 0.85 knockdown directly —
at 90 mm RANS is already 73 % above η·C_f, so η as a flat knockdown is
also suspect at height).

## 2026-07-23 — Stage 1 continued: fine anchor + tall tails flip the verdict

`anchor-h30-fine` (~90k cells, 17 min, force-converged at 3,457 iters):
**Cl 3.836 ± 0.263 — dead on the current model's C_est 3.838 (Δ −0.0 %,
implied k_g 0.119 = the auto curve's own value at h/c 0.0857).** The
coarse mesh at the same height had read 2.712 — 29 % low. The
racing-height "collapse" Stage 1's coarse sweep measured is therefore
substantially a mesh artifact: the under-resolved venturi gap separates
early. Note the fine run's ±0.26 limit cycle (~7 % band): the flow at
racing height is genuinely unsteady, so any single number carries that
band as irreducible uncertainty.

`tail-h150-coarse` (h/c 0.429): Cl 3.222 ± 0.005 vs C_est 3.215 —
**+0.2 %, implied k_g 0.521 vs curve 0.517.** The current curve is also
validated here, on a steady, well-resolved point.

`tail-h300-coarse` (h/c 0.857): Cl 2.352 ± 0.035, only +4.3 % above
η·C_f = 2.254 — **η = 0.85 is honest against RANS near free air.** The
−17.5 % C_est delta at this height is the curve still claiming k 0.755
where the measured realization is ~0.12; in absolute terms ~0.5 Cl of
optimism at a height nobody races at. Worth a curve-tail note, not a
recalibration on its own.

Revised hypothesis, to be settled by the mesh-check leg
(`mesh-h30-medium`, `meshchk-h25/60/90-fine`): the current calibration
is far better than the coarse sweep suggested — validated at fine-30 and
coarse-150 — and the remaining live questions are (a) whether the coarse
60/90 mm overshoot (+9 %/+33 %, i.e. the model UNDER-predicting
mid-height gains) survives fine meshing, and (b) where genuine
venturi-choke loss begins (the coarse 15 mm point, to be re-read in
fine-mesh light). Also actionable regardless of the verdict: the in-app
RANS verify tab DEFAULTS TO THE COARSE MESH — which just mis-read a
validated operating point by −29 % — so the tab needs at least a
low-ride-height coarse-mesh warning.

Timestamps note: the two tail rows in `runs.csv` were reconstructed from
their retained case directories after a CSV schema migration dropped
them mid-append (solver numbers are exact; their timestamps are accurate
to a few minutes).

## 2026-07-23 — mesh-check leg: the verdict, and the no-refit decision

Mesh line at 30 mm: coarse 2.712 ± 0.151 / medium 3.687 ± 0.367 /
fine 3.836 ± 0.263 — medium and fine agree within their limit-cycle
bands, so fine is treated as mesh-converged and coarse reads ~29 % low.
Fine-mesh re-reads of the sweep points:

| h (mm) | h/c   | coarse Cl | fine Cl        | C_est | Δ (fine) | implied k_g (fine) |
|-------:|------:|----------:|---------------:|------:|---------:|-------------------:|
| 25     | 0.071 | 2.433     | 3.130 ± 0.022  | 3.867 | −19.0 %  | 0.053 |
| 30     | 0.086 | 2.712     | 3.836 ± 0.263  | 3.838 | −0.0 %   | 0.119 |
| 60     | 0.171 | 3.936     | 6.020 ± 0.024  | 3.601 | +67.2 %  | 0.728 |
| 90     | 0.257 | 4.590     | 4.644 ± 0.024* | 3.440 | +35.0 %  | (cap-limited) |

*fine-90 hit the 10k iteration cap with a nearly flat tail; treated as
"at least ≈ 4.64", not a calibration point.

**Decision: no global curve refit.** Reasons, in order of weight:

1. The best-fidelity implied k_g set (0.053 → 0.119 → 0.728 → ~0.5 →
   0.124 across h/c 0.071 → 0.857) is **non-monotonic**. A k_g(h/c)
   refit through it would encode solver bias at the extremes (fully
   turbulent k-ω SST loses the s1223's transitional high-lift near free
   air, where RANS Cl 2.352 vs η·C_f 2.254 pins the small-gain limit),
   not ground-effect physics.
2. The current calibration is **validated at two heights** (h/c 0.086,
   0.429) at the best fidelity run; a refit chasing the mid-height peak
   would degrade verified points to fit points with a ±7 % limit-cycle
   band and one cap-limited read.
3. Single section, single config, one truth pipeline. The published
   ground-effect experiments the curve was shaped to (peak h/c 0.06–0.1)
   and our 2D fully-turbulent RANS (peak ≈ 0.17) disagree; with no
   tunnel data to arbitrate, encoding *knowledge of the disagreement*
   beats overwriting one model with the other.
4. The app already has the right per-design mechanism: RANS verify at
   the user's own operating point + pinned k_g — now guarded against
   the coarse-mesh trap (below).

**What ships instead** (all cited to this log):
- `analysis.HC_CHOKE_OPTIMISM = 0.08`: standing sub-choke warning —
  below h/c 0.08 the estimate is an upper bound (fine-25 measured
  −19 %, deepening toward the ground). Mirrored in the operating map's
  warning count. The mid-height conservatism (up to +67 %) is
  deliberately NOT a warning — it means more downforce than claimed,
  and polluting warning counts would wrongly mark those designs as
  suspect on candidate cards; it lives in the model dialog and here.
- `cfd_run` results carry `mesh_caution` on every coarse run; the RANS
  tab renders it next to the k_g suggestion — a k_g pinned from a
  coarse run (which mis-read a validated point by −29 %) would bake
  that bias into every estimate in the session.
- Model-dialog text updated to the measured validity map.

Open point queued: `meshchk-h40-fine` completes the racing band
(25/30/40) and settles whether the choke-warning boundary at h/c 0.08
needs to move.

## 2026-07-23 — Stage 1 closed: `meshchk-h40-fine` confirms the boundary

Cl 3.718 ± 0.044 vs C_est 3.754 — **Δ −1.0 %, implied k_g 0.154 vs
curve 0.158: a third validated point.** Final fine-fidelity map of the
racing band: −19 % at h/c 0.071, −0 % at 0.086, −1 % at 0.114, +67 % at
0.171 — the model is essentially exact through the core racing band
(30–40 mm on the 350 mm chord), optimistic only below the choke onset,
conservative above ~50 mm. The `HC_CHOKE_OPTIMISM = 0.08` boundary sits
exactly in the measured gap (broken at 0.071, validated at 0.086); no
adjustment needed. Stage 1 verdict stands as recorded above.
