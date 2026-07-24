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

## 2026-07-23 — Stage 2: the trust badge predicts real divergence

Both Stage 2 winners RANS-verified fine-mesh at the validated 30 mm
height (so ride-height model error cannot confound the comparison):

| winner | badge | C_est claimed | RANS Cl | Δ |
|--------|-------|--------------:|--------:|---:|
| `stage2_clean` (260 N target)   | none       | 4.267 | 3.661 | **−14.2 %** |
| `stage2_flagged` (430 N target) | near stall | 5.960 | 3.477 | **−41.7 %** |

The badge separates winners exactly as designed: the flagged design's
claim is off by 3× the clean one's. Better: in absolute terms the
flagged winner — which claims 40 % more downforce than the clean one —
actually DELIVERS LESS (3.48 vs 3.66). Pushing element loading past the
viscous envelope bought nothing in reality; that is precisely the
"separated-flow fantasy design" failure the trust penalty pushes down
and the badge marks for RANS verification before trusting.

Honest nuance: the clean winner still over-claims by 14 % — optimism
grows continuously with aggression (the seed itself measured −0 % here);
the badge marks where it becomes severe, not where it begins. The
penalty weights (a flagged element ≈ an 11 % target miss) are left
as shipped: the penalty's job is to prefer trustworthy equals during
search, and the badge + RANS verify is the honesty mechanism for what
survives. Data to revisit the weights now exists in `runs.csv`.

## 2026-07-23 — Generalization leg: the two axes separate

Does the validity map measured on one config generalize? Three fine-mesh
25/30 mm pairs at off-baseline configs answer it, with each config's own
loading-budget verdict alongside:

| config | main loading frac | budget verdict | Δ at 25 mm | Δ at 30 mm |
|--------|------------------:|----------------|-----------:|-----------:|
| baseline (defl 12, 15 m/s) | 0.889 | ok, 0 warnings | −19.0 % | −0.0 % |
| speed 25 m/s               | 0.878 | ok, 0 warnings | −12.3 % | +6.0 % |
| stack aoa +2°              | 0.997 | **warning**    | −23.1 % | −25.8 % |
| flap 20°                   | 1.098 | **warning**    | −30.0 % | −26.3 % |

Synthesis — the tool's two trust mechanisms own two orthogonal axes,
and both are now RANS-measured:

1. **Ride height** (healthy designs): the choke boundary generalizes.
   Both healthy configs are optimistic at h/c 0.071 (−19 % / −12 %,
   milder at higher Re — thicker-boundary-layer choke arrives slightly
   later) and honest at 0.086 (−0 % / +6 %). `HC_CHOKE_OPTIMISM = 0.08`
   stands unchanged.
2. **Loading**: both configs past the 90 % free-air warning line
   measure −23…−30 % optimistic at BOTH heights — including the height
   where healthy designs are validated — and the Stage 2 near-stall
   winner measured −42 %. The classical Smith budget's warning line is
   almost exactly where measured RANS optimism switches on. The
   validity bands therefore describe designs *inside the loading
   budget*; loading warnings trump the bands, and the wording in the
   app and guide now says so.

## 2026-07-23 — Leg B: conservative band generalizes; Stage 2 replicates

`gen-h60-fine-v25` (healthy config, h/c 0.171 at 25 m/s): Cl 5.641 ±
0.009 vs C_est 3.601 — **+56.6 %**, beside the baseline's +67.2 % at
15 m/s. The mid-height conservatism holds across Reynolds number on
healthy designs; the blue band stands as drawn.

Stage 2 replication, second seeded pair at the same validated height:

| winner | badge | Δ (pair 1) | Δ (pair 2) |
|--------|-------|-----------:|-----------:|
| clean   | none       | −14.2 % | −14.4 % |
| flagged | near stall | −41.7 % | −36.5 % |

Both rows replicate. The clean-winner over-claim is stable to within
0.2 points (~−14 % for on-budget optimizer winners at 30 mm); flagged
winners over-claim 2.5–3× that, and in both pairs the flagged design —
claiming 31–40 % more downforce than the clean one — delivered less
(3.69 vs 3.78 RANS Cl in pair 2, 3.48 vs 3.66 in pair 1). The badge's
verdict is not just directionally right; its magnitude is repeatable.

Cl 3.718 ± 0.044 vs C_est 3.754 — **Δ −1.0 %, implied k_g 0.154 vs
curve 0.158: a third validated point.** Final fine-fidelity map of the
racing band: −19 % at h/c 0.071, −0 % at 0.086, −1 % at 0.114, +67 % at
0.171 — the model is essentially exact through the core racing band
(30–40 mm on the 350 mm chord), optimistic only below the choke onset,
conservative above ~50 mm. The `HC_CHOKE_OPTIMISM = 0.08` boundary sits
exactly in the measured gap (broken at 0.071, validated at 0.086); no
adjustment needed. Stage 1 verdict stands as recorded above.

## 2026-07-23 — slot-relief surface metric: measured and rejected

A canonical-pressure-recovery trust metric (Smith-1975 dumping-velocity
relief on the coupled inviscid solution) was proposed to detect the
observed optimizer failure — flow detaching ahead of the main TE while
the flap sits above the slot flow. Five variants were measured against
every configuration with a fine-mesh delta on this log
(`scripts/recovery_metric_check.py`, rerunnable):

  variant A (ground solution, peak→TE): 0.977–0.990 for EVERY design —
    ground-image suction peaks (cp_min to −49) swamp the normalization;
  variants B/C/D (free air, TE-offset, aft-only): clean and warned
    classes overlap (gaps −0.06 … −0.14);
  variant E (TE dumping-velocity ratio, free air): nearly separates the
    classes (clean ≤ 1.172, warned ≥ 1.175) but is a loading proxy — and
    on a gap sweep at fixed healthy loading it moves the WRONG way
    (gap 3.0 → 0.8 %c lowers E from 1.24 to 1.12): the inviscid solver
    reads a tighter slot as MORE relief. The real tight-gap failure is
    boundary-layer merging, invisible to any inviscid quantity.

Verdict: rejected for optimizer use; the loading fraction remains the
validated trust separator. Corollary worth recording: the stage-2
CLEAN winners (−14.2/−14.4 %) share the flagged winners' exact slot
corner — gap 0.80 %c, overlap ≈ 0 — while the gap-1.5 %c baseline at
comparable downforce measured ~0 %. The healthy-winner −14 % bias may
therefore be partly the tight-slot cost. That hypothesis is the
gap-axis leg below; until it lands, the corner carries a geometric
advisory (`analysis.slot_signature_warnings`) quoting these numbers.

PLANNED — gap-axis leg (D7): baseline geometry, healthy loading, gaps
0.8 / 1.3 / 2.0 %c at 30 mm, medium mesh for the trend plus one fine
anchor, via `scripts/rans_calibration.py --config`. Outcome: a measured
gap floor (raise `GAP_WORKABLE_PCT[0]`) or a measured all-clear for the
corner at moderate loading.

## 2026-07-23 — stage-3 verdict runs (fine mesh) + first D7 anchor

Three fine-mesh runs on the upgraded optimizer's outputs, all
force-history converged (`runs.csv` labels stage3-*, d7anchor-*):

    max-downforce winner (pre-review-fix, loading 0.876, claim 4.263):
      RANS Cl 3.446 — Δ −19.2 %
    pareto knee (191 N claim, L/D 10.6, claim 3.146):
      RANS Cl 3.019 — Δ −4.0 %
    D7 fine anchor — gap 0.80 %c at HEALTHY loading (defl 12, aoa 0,
      claim 3.812): RANS Cl 3.762 — Δ −1.3 %

Readings. (1) The trusted-max guardrail held: −19.2 % sits between the
−14 % clean anchor and the −23 % warned onset — optimism grows toward
the 0.90 line but the mode never entered the −37…−42 % regime; the
post-review-fix winner (loading 0.900) is queued as stage3b. (2) A
front-knee pick from a properly searched run is essentially honest
(−4 %) — contrast the −59 % measured (coarse) on a junk 170 N front
point scavenged from an unrelated low-target run: fronts are only as
real as the search that populated them. (3) FIRST D7 EVIDENCE: the
tight-gap corner at moderate loading measures −1.3 % at fine mesh —
statistically indistinguishable from the gap-1.5 %c baseline (−0/−1 %).
The corner alone is NOT the over-claim driver at healthy loading; the
−14 % clean-winner bias tracks their higher loading, not their slot.
The medium-trend legs (0.8/1.3/2.0) and the stage3b verdicts are
running; the slot-corner advisory's wording gets recalibrated against
the full set when they land.

## 2026-07-24 — D7 trend + stage3b verdicts: three lessons, one surprise

All five follow-up runs force-history converged (`runs.csv` d7-*,
stage3b-*):

    D7 medium trend, healthy loading (fine anchor at g0.8: −1.3 %):
      gap 0.8 %c → −14.7 %   gap 1.3 %c → −20.0 %   gap 2.0 %c → −2.0 %
    stage3b max winner (post-review fix, loading 0.900, claim 4.429):
      RANS Cl 3.346 ± 0.061 — Δ −24.4 %
    stage3b pareto knee (gap 0.89, ovl 0.31, defl 12.6, aoa −2.6,
      claim 3.054): RANS Cl 5.457 ± 0.021 — Δ +78.7 %

(1) MEDIUM MESH IS NOT CALIBRATION-GRADE AT RACING HEIGHT. The medium
trend scatters −2…−20 % across tiny geometry changes and disagrees with
the fine anchor by 13 points at the identical config — limit-cycle
sampling plus an under-resolved venturi. The coarse caution extends:
at 30 mm treat medium as screening too; only fine runs calibrate. The
D7 verdict therefore rests on the fine anchor alone: the tight-gap
corner at moderate loading is essentially exact (−1.3 %), so
GAP_WORKABLE_PCT keeps its 0.8 floor and the slot-corner advisory is
reworded from "no recorded point supports the corner" to the measured,
loading-conditional truth.

(2) THE LOADING-OPTIMISM GRADIENT IS NOW MAPPED: ~−14 % near 0.85
loading, −19.2 % at 0.876, −24.4 % at 0.900. Optimism is a continuum
rising toward the warning line, not a step past it — a max-downforce
winner that rides the line pays about a quarter of its claim. The 0.90
trust boundary stands (past it the collapse regime begins, −37…−42 %
measured), but at-the-line winners should be read through this gradient
— and the RANS re-rank measures each one individually.

(3) NEW MEASURED FAILURE MODE — CONSERVATIVE, AT RACING HEIGHT. The
stage3b knee under-claims by 79 %: its geometry carries an extreme
inviscid ground coupling (c_ground/c_free ≈ 6.5; implied k_g 0.431 vs
the curve's 0.119) that the bounded-gain model crushes. The recorded
conservative band was mid-height (+35…+67 % at h/c 0.17–0.26); this
shows the same under-claim can appear at h/c 0.086 for
high-coupling geometries. Direction is safe (the wing delivers MORE
than claimed) but Pareto fronts are shape-distorted in that class.
Candidate predictor for a future leg: the c_ground/c_free ratio,
already computed per evaluation. Not acted on yet — one point.
