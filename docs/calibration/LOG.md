# RANS calibration campaign log

Chronological record of the cross-referencing campaign: every RANS run
that informs a calibration constant is one row in `runs.csv` (UTC
timestamps, machine-readable); this file records the order, the intent
and the conclusions. Constants changed on the strength of these runs
cite the run labels that justified them.

> **PROVENANCE WARNING (2026-07-24).** Every row in `runs.csv`, and every
> number in every entry below, was solved in the **8-chord-tall domain**
> used before the 2026-07-24 round. That round measured the 8-chord slip
> ceiling inflating Cl by ~5 % (and Cd far more) on an aggressive
> 3-element case and raised `Y_TOP_C` to 16 — so the recorded deltas sit
> roughly that much high, and nothing here has been re-measured at the
> new domain. Blockage scales with loading, so lightly loaded two-element
> rows shift less than the ~5 % measured on the loaded probe, but the sign
> is one-directional. Treat the recorded bands as advisory and re-anchor
> (`anchor-h30-fine`, `stage2_clean-fine`, `stage2_flagged-fine`) before
> letting a near-threshold verdict decide anything. The verdict bands in
> `app/core/rans_queue.py` carry the same warning at their definition.

Method notes, fixed up front:

- **Geometry**: the sharp (no manufacturing prep) two-element baseline —
  the same section as the historical verified comparison at 30 mm, so
  old and new points stay on one curve. `scripts/rans_calibration.py`
  pins the exact config.
- **Calibration points must be attached-flow cases.** Steady RANS on a
  separated high-lift stack settles into a bounded limit cycle, not a
  number (measured: the validity-boundary 3-element probe at Cl 8.6 ± —,
  the 2-element coarse case Cl 2.71 ± 0.15). Separated or heavily
  oscillating runs mark the model's validity boundary and test the
  trust badges; they are never curve-fit points. (That Cl 8.6 belongs to
  the boundary probe alone — the 2026-07-24 entry below records a
  different, converged 3-element case at 7.59 with the flow attached, so
  do not quote 8.6 as "the" three-element number.)
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

## 2026-07-24 — Domain/turbulence A-B on the user's live 3-element case

Trigger: a saved 3-element project (all-S1223, deflections 8.9/27 deg,
gaps 1.3 %c, h/c 0.114, Re 467k) read RANS Cl 7.48 (hand-stopped at
8,204 of 10,000, still climbing) against C_est 4.51 — "+66 %", and the
run was suspected unrealistic. Four probes on the exact saved config:

- `probe-base` (coarse 16k cells, old 8-chord domain, 8,000 iters):
  Cl 7.587 ± 0.088, flat (−0.04 %/window). Coarse ≈ fine for THIS
  config — the −29 % coarse bias recorded on the 2-element baseline
  does not generalize.
- `probe-ytop` (identical but 16-chord domain): Cl 7.210, Cd 0.130 vs
  0.171. THE 8-CHORD SLIP CEILING WAS INFLATING Cl ~5 % AND Cd ~24 %
  (tunnel confinement). Cost of the taller box: +5.6 % cells.
  ACTION: Y_TOP_C raised 8 -> 16 in cfd.py.
- `probe-lm` (kOmegaSSTLM gamma-ReThetat transition, coarse): Cl 9.588,
  tightly converged — the transition model reads 26 % HIGHER than
  fully-turbulent SST (long laminar runs thin the boundary layers in
  the favorable ground-effect gradients). Fully-turbulent SST is the
  CONSERVATIVE turbulence treatment here, not an optimistic one; no
  model switch shipped, verdict documented in the case README.
- `probe-finecont` (the user's retained fine case continued 8,204 ->
  12,000): Cl 7.587 ± 0.014, drift 0.077 %/window — converged. The
  memory that "the aggressive 3-element converges at Cl 8.6" belongs
  to a HOTTER config from the validity-boundary probe, not this one.

Wall-state truth on the retained fine fields (new wallShearStress
diagnostics): main element attached (5.7 % reversed faces), element 2
carries a separation pocket (21.7 %), the 27-deg flap is ATTACHED
(2.7 %) — the high Cl is the self-consistent product of an attached
multi-slot system in strong ground effect, not a solver artifact.

Conclusion: RANS-implied k_g for this case is ~0.37 (after removing
the ~5 % confinement) vs the auto curve's 0.158 — a second recorded
point in the high-coupling conservative class flagged in the 07-23
entry (stage3b knee, implied 0.431). The estimate, not the RANS, is
the outlier; analyze() now emits an estimate-fidelity note on 3+
element heavily loaded stacks, and the RANS card explains the
conservatism instead of presenting a bare "+66 %".

## 2026-07-24 — Wall-shear truth set and the wake-shadow screen

Trigger: repeated user-observed early separation on RANS solves of
optimizer products ("the airflow never reaches the trailing edge"),
suspected optimizer blindness. The retained cases under app_data/rans
carry wallShearStress fields, so the flow state is measurable per
element rather than argued from pictures. Extracted verbatim into
`wall_truth.json` (five cases; two fine, one medium converged, one
coarse duplicate, one 628-iteration transient snapshot):

- EVERY converged 3-element case runs its second element separated
  (reversed-face fractions 0.217 / 0.392 / 0.397), mains attached
  (0.057–0.078), final flaps attached-to-partial (0.027–0.118, one
  0.298 in the slot corner at 37.6° deflection).
- The transient snapshot reads the MAIN at 0.316 reversed at 628
  iterations; its converged continuation relaxes the main to 0.076
  and keeps e2 at 0.397 — early-iteration flow states are not verdicts
  (matches the drift-verdict rules from the 07-24 realism round).
- The optimizer graded all of these clean: the dying elements sit at
  free-air loading fractions 0.48–0.77, inside every band. Loading is
  the wrong axis for this failure mode.

Candidate screens measured against that record
(`scripts/separation_metric_check.py`, executable evidence):

1. Integral-BL march (Thwaites → Michel/short-bubble → Head with
   Ludwieg–Tillman, both sides, booked realized field), stop at first
   H ≥ 2.4: flags the validated baseline's flap (nose-spill bubble,
   lost-arc 0.93) — sep-truth 0.030–0.942 vs att-class 0.000–0.927,
   OVERLAPS. REJECTED.
2. Same march, marched through with re-heal (terminal-run rule):
   Head's entrainment re-attaches every dead element — sep-truth
   0.000–0.049, OVERLAPS. REJECTED. Root cause for both: at the booked
   realization (r ≈ 0.15) the free-air-dominated Ue distributions of
   dying and healthy elements differ by less than the method can use.
3. Wake-shadow screen (min realized upper-side Ue, 15–92 % arc,
   elements after the first): separated-measured 0.389–0.459 vs
   attached/partial-measured 0.533–0.619, gap +0.074, SEPARATES. The
   stage-2 flagged-class flaps (no wall truth, delta −37…−42 %) land
   at 0.519–0.524, inside the gray band — consistent with their class.
   Stable: paneling 45/50/70 drifts < 0.01; r swept 0 → 0.35 shifts
   ~0.03 uniformly with ordering intact. SHIPPED at SHADOW_SEP 0.50,
   SHADOW_WARN 0.53 (wake_shadow.py; wired per DECISIONS.md
   "Separation awareness").

Scope: front-wing climbing stacks, h/c 0.086–0.114, s1223-class
sections, fully-turbulent SST truth runs. The screen says nothing about
tight-gap slot-jet merging (still invisible inviscidly — slot-signature
advisory + RANS remain the referees) and exempts the first element
(no upstream wake; mains measured attached at upper-side minima down
to 0.44). Re-run the check script after any panel-solver or geometry
change: it exits nonzero when the thresholds stop separating this
record.

## 2026-08-02 — Scope, not weakness: why the screen missed a collapse

Five converged Fluent 2D solves (studio conventions, studio-yplus1 sizing,
3060 iterations each, force history converged). Field readings are
`foam_post.recirculation_report`'s near-wall reversed-station fraction at
the headline probe offset; wall-shear values are the preserved
`wall_truth.json` record from the (now deleted) OpenFOAM cases.

Element 2 in every row:

| design | screen | screen says | field (Fluent) | wall shear (OpenFOAM) | Δ Cd |
|---|---|---|---|---|---|
| 1c4bf2d726c3 | 0.4591 | collapse | 0.0000 | 0.217 separated | +523.9 % |
| df1865bb82a3 | 0.4022 | collapse | 0.3263 | 0.392 separated | +395.5 % |
| 859e79a7486d | 0.3893 | collapse | 0.2893 | 0.397 separated | +582.1 % |
| 09c6de53265a | 0.5471 | **ok** | 0.3848 | *(out of scope)* | +442.7 % |

**The screen is right inside its envelope and wrong outside it — 3/3 vs
0/1.** The first three sit inside the declared scope (h/c 0.0857–0.1143,
sections s1223/as6099/be6699, non-main chord ratios 0.20–0.381). The fourth
runs s1223rtl and mid53b at a 0.5 second-element chord ratio, outside on two
axes, and drew a confident "ok" at 0.5471 — above SHADOW_WARN, so zero
penalty — while carrying 0.3848 reversed. An A/B confirmed the metric is not
even directional out there: repairing it to 0.5607 left the flow at 0.3895.

So the metric is not weak; it was being asked questions it was never
validated for and answering silently. `wake_shadow.scope_check()` now
reports which axes a design leaves, with the envelope derived from this
record and re-derived by `separation_metric_check.py` on every run.

**The field probe reads low against wall shear, and near the grading line it
reads nothing.** Wall 0.392 → field 0.326 (0.83×), wall 0.397 → 0.289
(0.73×), but wall 0.217 → **0.0000**. The documented one-sided smearing bias
is real and is worst exactly where a line has to live: 0.217 sits just past
the 0.20 separated line and the probe cannot see it. A field-side grading
line fitted on deep collapses would not transfer to the boundary. Note this
is cross-engine and cross-mesh — suggestive, not a calibration — but enough
to stop anyone fitting C2 on field data alone.

**Δ Cd has four readings and all four are positives** (395–582 % on designs
every channel calls separated). No attached design has been solved on this
engine, so the column's discrimination is untested: it flags separation
loudly, but whether it stays quiet on a healthy stack is unmeasured.

## 2026-08-02 — Negative controls kill the drag column as a detector

Two more converged Fluent 2D solves, the record's own validated two-element
baselines (30 mm ~0 %, 40 mm −1 %), same settings as the six above. Both
read attached: screen 0.6032 / 0.6073, field probe 0.0000 on element 2.

All six solves, sorted by Δ Cd:

| design | truth | screen | field e2 | Δ Cd |
|---|---|---|---|---|
| baseline 30 mm | **attached** | 0.6032 | 0.0000 | **+626.1 %** |
| 859e79a7486d | separated | 0.3893 | 0.2893 | +582.1 % |
| baseline 40 mm | **attached** | 0.6073 | 0.0000 | **+524.2 %** |
| 1c4bf2d726c3 | separated | 0.4591 | 0.0000 | +523.9 % |
| 09c6de53265a | separated | 0.5471 | 0.3848 | +442.7 % |
| df1865bb82a3 | separated | 0.4022 | 0.3263 | +395.5 % |

**Δ Cd does not discriminate attachment.** Attached spans 524–626 %,
separated 396–582 %; the classes overlap completely and the healthiest
design in the set reads highest. `delta_cd_is_upper_bound` was True on all
six — the polar lookup was capped every time — so what the column measures
is how far the capped attached-flow estimate falls short of a loaded
ground-effect stack's real drag, and that gap is 4–6× regardless of flow
state. The cap dominates; separation does not move it enough to see.

This retracts the claim made when the column shipped ("separation reads far
louder in drag than in lift", "drag is ~7× the signal"). That was inferred
from separated cases only, with no negative control. The column is kept and
still reported — a 5× gap says the estimate's drag is unusable for this
design — but it is information about the ESTIMATE, not about the flow, and
the code now says so at every site.

**The two channels that DO discriminate, on the same six:**

- **Screen, inside its validated envelope: 5/5.** Three separated designs
  all read collapse (< 0.50); two attached read 0.60+. Outside the
  envelope: 0/1.
- **Field probe: 5/6.** Attached 0.0000 / 0.0000, separated 0.2893 / 0.3263
  / 0.3848 — a clean gap — with one miss on the boundary case whose wall
  shear was 0.217, consistent with the smearing bias noted above.

## 2026-08-02 — How much of the search space the screen was ever checked on

Per-variable, the calibration record against `optimizer.DEFAULT_BOUNDS`:

| variable | record span | optimizer bounds | % of box inside the record |
|---|---|---|---|
| stack_aoa_deg | −4.0 … 0.0 | −4.0 … 12.0 | **25.0 %** |
| deflection_deg | 0.0 … 37.6 | 0.0 … 60.0 | 62.7 % |
| flap chord_ratio | 0.200 … 0.381 | 0.15 … 0.50 | 51.7 % |
| slot_gap_pct | 0.80 … 2.00 | 0.80 … 3.50 | 44.4 % |
| slot_overlap_pct | 0.00 … 3.40 | 0.00 … 5.00 | 68.0 % |

Multiplying those gives **2.45 %** of the default search box. Treat that
number as indicative only: it is a box-volume statistic assuming uniform
independent sampling, and the optimizer runs a directed search seeded from a
real design, so it does not sample the box uniformly. The per-axis figures
are the defensible ones and they are stark enough on their own — the screen
has never been checked above stack AoA 0°, and the search goes to +12°.

**The three shipped presets sit inside every one of those spans**, so this
is not a claim that ordinary use is unvalidated. It is a claim about where a
*search* can walk to, and the design that drew a confident "ok" while
carrying 0.38 reversed flow had walked out on three axes at once: sections
absent from the record, a flap chord ratio of 0.5 against a 0.381 maximum,
and slot gaps of 3.0 and 2.1 against a 2.00 maximum.

`scope_check` now covers all seven axes (sections, h/c, element count, flap
chord ratio, deflection, slot gap, slot overlap, stack AoA). Widening it left
every calibration case in scope and two of the three presets quiet; the third
(E423) legitimately trips on sections and overlap.

## 2026-08-02 — Seven one-factor probes: no single axis explains the failure

Base = the validated 30 mm two-element baseline, measured ATTACHED (screen
0.6032, field 0.0000, cl 3.5089). Each probe moved **exactly one** variable
outside the record's span, everything else held inside it. All seven came
back `clean_probe=True`, converged at 3060 iterations, Fluent 2D studio
settings.

| probe | screen e2 | field e2 | cl |
|---|---|---|---|
| stack AoA 0 → +6 | 0.5680 | 0.0546 | 3.097 |
| deflection 12 → 45 | **0.2736** | 0.0000 | 4.440 |
| chord ratio 0.35 → 0.45 | 0.6027 | 0.0000 | 4.209 |
| slot gap 1.5 → 3.0 | 0.6002 | 0.0000 | 3.730 |
| main → s1223rtl | 0.6029 | 0.0000 | 3.261 |
| flap → mid53b | 0.7727 | 0.0000 | 3.245 |
| flap → e423 | 0.6965 | 0.0000 | 3.486 |

**Six of seven agree with the flow. No single-axis excursion reproduces the
false negative** found on 09c6de53265a (screen 0.5471 "ok" against field
0.3848). The working hypothesis that sections was the culprit "by
elimination" is refuted by its own three probes: s1223rtl, mid53b and e423
all leave the screen reading healthy on a flow that is healthy.

The deflection-45 row is the exception and points the other way — a possible
**false positive**. 0.2736 is deeper than any design ever measured separated
and carries a penalty near 513 (~1700 N at dscale 200), which would ban the
design, while the field reads zero and cl is the highest in the set. Held as
unconfirmed: the field probe read 0.0000 on a case whose wall shear was
0.217, so zero is not proof of attachment, and a 45° flap is where a
thin-reversal miss is most plausible.

**Where this leaves the cause.** Walking the gate case the screen gets right
toward the design it gets wrong, changing one thing at a time (panel model,
no solve needed for the screen readings):

| step | screen e2 |
|---|---|
| gate 1c4bf2d726c3 | 0.4591 collapse (correct; wall 0.217) |
| A: e2 chord 0.28 → 0.50 | 0.4764 |
| B: A + e2 deflection 8.9 → 0.8 | 0.4953 |
| C: B + slot gaps 3.0 / 2.1 | **0.5145** (first step above SHADOW_SEP) |
| D: C + e3 0.26 / 26.7° + aoa −1.23 | 0.4770 |
| the failing design (D + sections) | **0.5471** |

D and the failing design differ **only in sections**, and that difference
alone lifts the reading 0.4770 → 0.5471, across the line. So sections do
matter here — but only inside the three-element topology, which the
two-element section probes could not reach. That is an interaction, not an
axis. C and D are being solved to see whether the flow moves with the screen
or stays put.

## 2026-08-02 — Adversarial review: the probe round was invalid, and the
## false negative reproduces without sections

An adversarial pass over the previous entry's conclusions refuted all three,
with measurements the original reading did not have. Recording the
refutations, because the entry above is wrong in ways worth knowing.

**A zero from the near-wall probe is not "attached".** The Fluent 2D path
writes no wall shear at all, so every "attached" label in the probe round was
a field-probe `0.0000` — the same reading already documented wrong on the
design whose wall-shear fraction was 0.217. Under the project's own doctrine
those are unmeasurable cases, and an unmeasurable case is never a pass. Six
of the seven probes were graded on that mistake.

**The probes were run on a 2-element base, where the screen's mechanism
cannot occur.** The wake-shadow screen models an element sitting in its
neighbours' circulation shadow. On a 2-element stack, element 2 has nothing
behind it. Every 2-element solve on record reads element 2 attached (8/8);
every 3-element solve reads it separated (4/4). Element count is a perfect
confound, and no 2-element probe could have reproduced a confluence failure.

**Two of the probed axes have almost no leverage on the screen at all.**
Swept over the geometry validator's full range on the panel model: flap chord
ratio 0.20 → 0.90 moves `shadow_min` by **0.0039**; slot gap 0.8 → 10.0 moves
it by **0.0184**. Both are inside the module's own `SHADOW_KNIFE_BAND` of
0.03, and the status string is a constant `"ok"` across both entire ranges.
Those probes measured specificity where no positive was possible. This also
bears on the scope check shipped earlier: flagging chord ratio as
out-of-envelope is true but nearly irrelevant to the screen's answer.

**The deflection-45 probe was NOT a false positive — the screen was right.**
Its field carries one reversed region of 7531 cells, 0.190 c², 1.30 chords
long, `reattaches: False`, anchored on the **main element's** suction side.
The near-wall probe read 0.0000 on element 2 and 0.0609 on element 1 and saw
none of it, because the separation is off-body in extent and on a different
element. The screen raised a correct alarm — though by an invalid route: on
that design the inviscid window minimum is pinned at the window's lower edge
(arc 0.153) against arc 0.47–0.91 for every calibrated case, so the 0.2736
and its ~513 penalty are not calibrated quantities.

**And the false negative reproduces with in-record sections.** Walking the
gate case toward the failing design, step C (e2 chord ratio 0.50, e2
deflection 0.8°, slot gaps 3.0/2.1 — **s1223 throughout**) measures:

| | screen e2 | field e2 |
|---|---|---|
| step C | **0.5145** (above SHADOW_SEP; ~0.1 N penalty) | **0.3872** |

That is the same failure as 09c6de53265a, reached without changing a single
section. **Sections are not the cause.** It is a three-element geometry
effect — a large, nearly-undeflected second element with wide slot gaps — and
step C is now a reproducible test case for it.

**Consequence for the two field channels.** The near-wall probe and the
region census fail in opposite directions: the probe caught the confluence
collapses the census was silent on, and the census caught the large off-body
separation the probe read as 0.0000. Neither is primary; `foam_post`'s
module comment previously said Channel A was, and no longer does.

### The path, solved: the screen swings across its line while the flow sits still

Four converged 3-element solves, Fluent 2D studio settings, element 2:

| design | sections | screen e2 | field e2 | screen verdict |
|---|---|---|---|---|
| gate 1c4bf2d726c3 | s1223 ×3 | 0.4591 | 0.0000 | collapse — correct (wall 0.217; the *field* missed it) |
| step C | s1223 ×3 | **0.5145** | **0.3872** | ok — **false negative** |
| step D | s1223 ×3 | 0.4770 | 0.4062 | collapse — correct |
| 09c6de53265a | rtl/s1223/mid53b | **0.5471** | 0.3848 | ok — **false negative** |

**The measured collapse is flat at 0.385–0.406 across C, D and the failing
design. The screen moves 0.4770 → 0.5471 over the same three, crossing
SHADOW_SEP twice.** In this corner of the space the screen is not tracking
the flow; it is responding to geometry changes the separation is indifferent
to. That is a sharper statement than "it is miscalibrated" and a different
problem from "it is out of scope" — step C sits inside the record on
sections and is still wrong.

Step C is the minimal reproducer: gate case, all in-record sections, three
changes (e2 chord ratio 0.28 → 0.50, e2 deflection 8.9 → 0.8°, slot gaps
1.3/1.3 → 3.0/2.1). It should be added to the wall-truth record once an
OpenFOAM solve can give it a wall-shear label.

Note both instruments fail here, in opposite directions. At the gate the
field probe reads 0.0000 against a wall-shear 0.217 and the screen is right;
at C, D and the failing design the field is right and the screen swings. No
single channel is trustworthy alone on this family.

## 2026-08-02 — On the extended record the shipped minimum overlaps; a
## two-route compound separates it (unadjudicated)

The harvest's field-labeled cases (docs/calibration/field_truth.json: the
09c6 false negative, step C, step D — probe readings ≥ 0.385, and the probe
reads low, so those are strong separation evidence) extend the labeled pool
to 7 separated + 3 attached non-exempt elements. On that pool the shipped
minimum **overlaps**: separated up to 0.547 against attached from 0.533.
The record outgrew the threshold, which is what the harvest exists to show.

Windowed profile shapes show two distinct collapse routes:

| route | shape | example |
|---|---|---|
| 1 | enter fast (0.77–0.99), decelerate 0.38–0.47 through the window | every labeled e2 |
| 2 | enter already buried (0.475), no deceleration (0.060) | df1865's e3 (wall 0.298) |

No single statistic catches both — the minimum misses route 1's high-entry
family (their minima land above any absolute line), any deceleration measure
misses route 2. The compound `decel > 0.35 OR min < 0.47` reads **7/7
separated caught, 3/3 attached clean** on this pool.

**Not adjudicated, not wired**: two fitted constants on ten points, an
attached pool of three, and field labels are weaker than wall shear. It is
in `separation_metric_check.py` as candidate 4 so every run re-measures it
as the harvest grows. Six further solves straddling the failure region
(screen 0.476–0.522) are running now and will add up to 12 labeled elements.

## 2026-08-02 — Failure-region batch: the repair axis is e2 deflection, and
## candidate 4 survives out-of-sample

Six converged solves straddling the screen's failure region (screen e2
0.4764–0.5224), element 2:

| design | screen e2 | probe e2 | census | reading |
|---|---|---|---|---|
| A: gate + e2 cr 0.50 | 0.4764 collapse | 0.0214 | **869c on e2**, reatt None | census-flagged; screen plausibly right, probe missed it |
| B: A + defl 0.8 | 0.4953 collapse | 0.0024 | 542c on main | ambiguous |
| Cmid: B + gaps 2.0/1.7 | 0.5052 **ok** | **0.3729** | 489c | **false negative** |
| Ccr42: C, e2 cr 0.42 | 0.5094 **ok** | **0.3588** | 351c | **false negative** |
| Cdefl9: C, e2 defl 8.9 | 0.5034 ok | 0.0024 | **25c** | genuinely quiet |
| FAILtight: fail, gaps 1.3 | 0.5224 **ok** | **0.4086** | 815c spans e2+e3 | **false negative** |

**The repair axis for this family is e2 deflection, not gaps.** Re-deflecting
e2 from 0.8° to 8.9° (Cdefl9) is the only change that recovers the flow —
25 reversed cells, baseline-quiet — and it lands at **cl 8.878, the highest
lift measured this session**. Tightening the gaps back to 1.3/1.3
(FAILtight) does *not* recover the failing design: still 0.4086 reversed.
The collapse follows the nearly-undeflected oversized second element, and
fixing it costs nothing — it *gains* lift. (The screen cannot see any of
this: C reads 0.5145 and Cdefl9 reads 0.5034, nearly identical, while the
flow goes 0.387 → 0.002.)

The gap transition sits between 1.3 and 2.0 in the step family (B 0.0024 →
Cmid 0.3729) but gaps alone cannot recover the failing design — interaction
again.

**Candidate 4 survives out-of-sample.** The three new false negatives were
not used to fit its constants; re-measured on the grown pool it reads
**10/10 separated caught, 3/3 attached clean** (shipped minimum still
overlaps, gap −0.014). Still two fitted constants, still an attached pool
of three, still unadjudicated — but it has now predicted unseen cases.

A and B are recorded as census-flagged and UNLABELED: the probe read near
zero while the census saw 869/542-cell regions, the same probe blind spot
the deflection-45 case exposed. No label channel for census evidence exists
yet; defining one needs wall-shear truth (blocked on Docker).

## 2026-08-02 — The A/B that matters: screen-driven repair walks backwards,
## compound-driven repair finds the solved fix

Seed = step C (the minimal false-negative reproducer; field 0.387, screen
0.5145 "ok"). The solves already established the fix: e2 deflection 0.8° →
8.9° recovers the flow (field 0.002) and raises cl 5.86 → 8.88. The screen
reads that fix at 0.5034 — *worse* than the broken seed.

**Screen-driven `repair.repair()`** (853 evals): moved e2 deflection to
**0.00** — the opposite direction — flattened e3 27° → 15.1°, paid 4.9 %
downforce, and declared "repaired" at screen 0.5601. Its output's compound
reading is unchanged from the seed (decel 0.474, margin −0.124): it raised
the window *entry* to 1.034, lifting the minimum without touching the
deceleration. The true fix sits *below* the repair target, so the search
structurally cannot return it.

**The three-way ranking test.** shadow_min vs the compound margin
`min(0.35 − decel, min − 0.47)`:

| design | shadow_min | compound | flow truth |
|---|---|---|---|
| step C (broken) | 0.5145 ✗ | −0.124 FLAGGED ✓ | separated 0.387 |
| Cdefl9 (fixed) | 0.5034 ✗ | **+0.027 healthy** ✓ | attached 0.002 |
| repair()'s output | 0.5601 ✗ | −0.124 FLAGGED ✓ | (unsolved; predicted bad) |

The shipped metric ranks all three exactly backwards; the compound gets all
three right.

**Compound-driven search** (same seed, same knobs, same Nelder-Mead
multi-start, 750 evals, margin term = the compound, downforce floor 95 %):
found **e2 deflection 8.37°** (solved fix: 8.9°) at **+18.4 % downforce** —
it rediscovered the fix without being told, and the fix pays for itself.
Its candidate (defl 8.37, gap 3.5, e3 17.4°, aoa −1.68) is being solved for
verification.

Honesty: step C's label is IN candidate 4's fitting pool, so this is not a
fully independent test of the compound — but Cdefl9's attachment was never a
label, and the search was never shown it. Directional success on this
family; generalization stands on 13 labeled points and stays open.

### Verified: the compound-driven repair is real

The compound-repair candidate (e2 deflection 8.37°, gap 3.5, e3 17.4°, aoa
−1.68 — found by the search, not by hand) solved converged at 3060
iterations: **field 0.000 on every element** (probe; census pending), screen
0.5389/0.5674, **cl 7.5366** against the broken seed's 5.864.

The loop closed end-to-end with solver verification: a design the screen
called healthy was measured separated; a statistic measured from the record
flagged it; a repair search driven by that statistic found the fix on its
own (deflection up, not down); and the solver confirms the fix — separation
gone, +29 % lift. The screen-driven repair on the same seed walked the
opposite direction and its output still reads FLAGGED on the compound.

Three bracket solves are running to test candidate 4's thresholds with
stated predictions: defl 5.0 (−0.052, SEP), 6.5 (−0.009, knife-edge SEP),
16.0 (−0.002, SEP via the route-2 min term — incipient-stall if real).

## 2026-08-02 — Bracket solves: two predictions confirmed, one hair-width
## miss, and the deflection map is monotone

Three solves with predictions registered before solving (step-C family, e2
deflection only):

| defl | compound (pre-registered) | field e2 | census | scored |
|---|---|---|---|---|
| 5.0 | −0.052 → SEP | **0.3349** | 322c on e2 | **CONFIRMED** |
| 6.5 | −0.009 → knife-edge | 0.1259 | 109c | **faithful** — "on the line" landed on a genuinely transitional flow |
| 16.0 | −0.002 → SEP via route-2 min | 0.0000 | 26c (quiet) | **miss by −0.002** |

The defl-16 miss is the route-2 term at its exact edge: min 0.468 against
the 0.47 constant, on a heavily loaded but attached design whose window
minimum drifts down with loading. Two honest readings: `CAND4_MIN` is a few
thousandths too high on this family, or margins within ~±0.02 of zero should
carry a knife-edge band rather than a side — under which both 6.5 and 16.0
were called correctly. Either is a calibration choice, not the designer's.

The full deflection map on this family is **monotone in the flow and rising
in lift** while the screen drifts the wrong way:

| e2 defl | field e2 | cl | screen e2 |
|---|---|---|---|
| 0.8 | 0.387 | 5.86 | 0.5145 |
| 5.0 | 0.335 | 7.37 | 0.5097 |
| 6.5 | 0.126 | 8.03 | 0.5075 |
| 8.9 | 0.002 | 8.88 | 0.5034 |
| 16.0 | 0.000 | **9.50** | 0.4678 |

The flow transition sits between 6.5° and 8.4°. The screen's reading falls
as the flow improves — anti-correlated across the entire physical
transition. defl-16 at cl 9.502 is the highest lift measured this session,
and the shipped screen would penalize it as a deep collapse (0.4678 →
penalty ~14) while flagging nothing at 0.8° where the flow is dead.

Candidate 4 on the grown pool: **11/11 separated, 3/3 attached** (defl-5's
label is its fourth consecutive out-of-sample confirmation). Unadjudicated
as ever: attached pool still 3, and the defl-16 edge case is exactly the
kind of point the attached pool needs.

## 2026-08-02 — Generalization batch: 5/6 pre-registered predictions
## confirmed, and the deflection fix works on the original failing design

Six solves across three families, every prediction registered before
solving. Element 2:

| design | pre-registered | field e2 | census | scored |
|---|---|---|---|---|
| stepC defl 11 | ATT +0.026 | 0.0000 | 32c (quiet) | **CONFIRMED** — cl 9.098 |
| FAIL + e2 defl 8.9 | ATT +0.053 | 0.0000 | 104c (near-quiet, on main) | **confirmed** |
| gate aoa 0 | SEP −0.031 | **0.3178** | 1467c | **CONFIRMED** |
| FAIL e3 defl 35 | SEP −0.031 | **0.3967** | 707c | **CONFIRMED** |
| FAIL aoa 0 | SEP −0.066 | **0.3967** | 748c | **CONFIRMED** |
| gate e2 defl 20 | SEP −0.071 (route-2) | 0.0000 | **387c, touches no element** | ambiguous — census-flagged, unlabeled |

**The deflection repair generalizes to the original failing design.**
`09c6de53265a` with e2 deflection raised 0.8° → 8.9°: attached (field
0.0000, census 104c) at **cl 8.106** against the broken original's 7.104.
The design that started this investigation has a measured fix: deflect its
second element, gain 14 % lift, kill the separation.

The gate-defl-20 ambiguity is the same shape as steps A/B: probe zero,
census sees a moderate detached reversed region (346c touching no surface).
The route-2 term fired on something real, but not surface separation the
probe can read. Unlabeled pending a census label channel (needs wall shear).

Shipped-screen scorecard on the same six: two more **false negatives**
(FAIL e3 35 reads 0.522 "ok" at field 0.397; FAIL aoa 0 reads 0.540 "ok" at
0.397) and one more effective **false positive** (stepC defl 11 reads 0.496
"collapse" on a quiet flow at cl 9.098).

Candidate 4 on the grown pool: **14/14 separated, 3/3 attached** — seven
consecutive out-of-sample confirmations. The attached pool remains 3 by
doctrine (no wall shear on this engine, so quiet cases stay unlabeled);
the quiet confirmations are recorded here as evidence, not labels.

## 2026-08-02 — Second repair test: transitional, and the knife band becomes
## a measured object

Compound-repair of FAIL_aoa0 (the aoa-break, a different break mode from the
step-C deflection break). The search again found deflection (e2 0.8° → 6.86°)
and also restored the broken axis (aoa 0 → −1.26). Pre-registered ATT at
margin **+0.020**, with the registered caution that 6.86° sits in the
deflection map's transitional zone.

Solved: **field e2 0.1591 — transitional.** The repair improved the flow by
60 % (0.3967 → 0.1591) and gained 9.4 % lift (cl 7.208 → 7.888) but did not
fully attach. Scored as a knife-zone miss for the ATT call.

### The margin → measured-flow curve, all solved cases

| compound margin (pre-computed) | field e2 (solved) | case |
|---|---|---|
| −0.124 | 0.387 | step C |
| −0.066 | 0.397 | FAIL_aoa0 |
| −0.052 | 0.335 | bracket defl 5 |
| −0.031 | 0.318 / 0.397 | gate_aoa0 / FAIL_e3d35 |
| −0.009 | 0.126 | bracket defl 6.5 |
| −0.002 | 0.000 | bracket defl 16 |
| **+0.020** | **0.159** | repair2 (this entry) |
| +0.026 | 0.000 | stepC defl 11 |
| +0.027 | 0.002 | Cdefl9 |
| +0.053 | 0.000 | FAIL + defl 8.9 |

Near-monotone: margins below −0.03 always read 0.32–0.40; margins above
+0.026 always read ≤ 0.002; the band between −0.01 and +0.02 produces
0.000–0.159 — genuinely transitional flows. One inversion (+0.020 → 0.159 vs
−0.002 → 0.000) sizes the band's noise. **An empirical knife band of ±0.02
to ±0.03 around zero margin is now supported by five points, not asserted.**
Two probes inside the band (+0.017, +0.009, 2-element family) are solving.

## 2026-08-02 — Knife probes expose the first-element exemption; census term
## measured across all 40 solved cases

The two 2-element knife-band probes (+0.017, +0.009) read **e2 probe 0.0** —
the e2-margin knife-band prediction holds — but the census found **3489 and
8274 cells** of `reattaches=False` separation anchored on the **main
element**, which the entire screen family (shadow_min, candidate 4) exempts
by construction. The 8274-cell case is the session's largest reversed
structure. The first-element exemption ("mains measured attached") was
calibrated on 3-element stacks with moderate flaps; under a hot 2-element
flap in ground effect it does not hold.

**Candidate 5 — open-region census cells, measured on all 40 solved cases:**

| tier | open (reatt=False) cells | cases |
|---|---|---|
| main-separation mode | **3459–8272** | defl-45, defl-30, aoa6/defl25 (all 2-elem hot-flap) |
| small open structures | 216–377 | three single-axis probes (flow state unknown) |
| everything else | ≤ 40 | including every 3-element e2 collapse — their bubbles close (`reatt=True`), consistent with the closure-orthogonality finding |

The two failure modes have disjoint sensors: the near-wall probe catches the
3-element confluence collapse (0.28–0.41) and reads ≤ 0.063 on the
main-separation cases; the open-census catches main separation (≥ 3459) and
reads ≤ 40 on the confluence collapses. A two-mode flag — `worst_probe ≥
~0.28 OR open_cells ≥ ~1000` (the line placed in a 377 → 3459 gap) — covers
every labeled failure on the record with no false alarm among the quiet
cases. Same status as candidate 4: measured, re-runnable, NOT adjudicated
(the open-cells line sits in a 10× gap with nothing between, and the
216–377 tier's flow state is unlabeled).

Scoring the knife probes themselves: the e2-margin band behaved (probe 0.0
inside the band), but the DESIGN verdict "marginal-healthy" was wrong for
both — by the exempted element. A margin on the flap says nothing about the
main, and the session now has three measured cases proving it.

## 2026-08-02 — PROPOSED field-side grading, awaiting adoption (session deliverable)

Consolidates ~30 converged Fluent 2D solves (studio conventions,
studio-yplus1, 3060 iters each). Every number below is measured in the
entries above; nothing is wired. **Adoption is a design decision, not
yet taken.**

### Proposal 1 — RANS-side separation flag (per solved case)

Flag a design as separated when **either** mode fires:

| mode | term | proposed line | evidence |
|---|---|---|---|
| confluence collapse | worst per-element `near_wall_reversed_frac` | **≥ 0.28** | separated labels span 0.318–0.409; attached/quiet read ≤ 0.176; the 0.28 line sits in the measured gap |
| main/open separation | total census cells in `reattaches=False` regions | **≥ 1000** | failing tier 3459–8272; everything else ≤ 377 (10× gap); catches the mode the probe reads 0.0 on |

Falsified by: a wall-shear-verified attached case above either line, or a
verified separated case under both. The three 216–377 open-cell cases are
unlabeled — wall-shear solves of those three are the sharpest adoption test.

### Proposal 2 — optimizer screen (panel-side, replaces bare shadow_min)

Candidate 4's compound margin `min(0.35 − decel, min − 0.47)` on the
windowed upper-side curve, with a **knife band of ±0.02**: negative margin
below the band = predicted separated; positive above = predicted clean;
inside = knife-edge, not evidence either way. Evidence: 14/14 + 3/3 on the
labeled pool, 7 consecutive pre-registered out-of-sample confirmations, and
a 10-point margin→flow curve that is monotone outside the band. Known
limits, all measured: says nothing about the exempt first element (proposal
1 mode 2 covers it), constants fitted on 17 points, field labels are the
weaker channel.

### Proposal 3 — repair objective

The compound margin as the repair search's margin term (pattern in LOG
above): it rediscovered the deflection fix the screen walks away from,
verified attached at cl 7.54, and repaired the original failing design
(cl 8.106, +14 %). Margins landing inside the knife band should be treated
as unverified candidates requiring a solve — the +0.020 repair came back
transitional.

### What stays blocked, and on what

- **Wall-shear pairing** (adjudicates everything above): needs Docker up
  and one image pull, then OpenFOAM re-solves of the calibration configs
  plus the three unlabeled open-cell cases.
- **Adoption of any line above**: pending design decision.
- **First-element exemption fix** in wake_shadow: design decision, needs
  the census channel wired first — which needs proposal 1 adopted.

## 2026-08-02 — The wall-shear pairing batch: five OpenFOAM solves, and the
## probe's bias is now measured same-solve

Docker returned; five OpenFOAM medium-mesh solves (n_ranks 1, force-stop),
each writing wallShearStress + C/U/p + a harvest row on the SAME case — the
pairing every proposal was waiting for.

| case | conv | wall e1 / e2 / e3 | OF probe e1 / e2 / e3 | screen e2 |
|---|---|---|---|---|
| step C | no (cap) | 0.076 / **0.291** / **0.282** | 0.014 / 0.273 / 0.055 | 0.5145 "ok" |
| baseline30 | **yes** | 0.192 / **0.458** | 0.081 / 0.263 | 0.6032 "ok" |
| aoa6/defl25 | **yes** | **0.287** / 0.127 | 0.220 / 0.0 | 0.4787 |
| defl30 | no | 0.174 / 0.055 | 0.087 / 0.0 | 0.4869 |
| bl30+mid53b | **yes** | 0.207 / 0.113 | 0.086 / 0.0 | 0.7727 "ok" |

**Same-solve probe bias (C1/C2 data at last).** The probe under-reads the
wall on every element of every solve. Ratios probe/wall: e2 0.94 (step C)
but 0.57 (bl30); e1 0.42–0.77; step C's e3 0.20 — and four elements at
partial-band wall fractions (0.113–0.174) read 0.0. The bias is real,
element-dependent, and worst exactly in the partial band. **A field-side
grading line cannot replace wall shear; the probe is a screen, not a
truth channel.** Proposal 1's 0.28 line survives as a high-confidence
SEPARATED flag (everything the probe reads ≥ 0.26 is wall-separated) but a
probe zero says nothing — as doctrine already held.

**Wall-verified findings:**
- Step C's false negative confirmed on the truth channel (e2 0.291), plus a
  third screen miss on the same design (e3 0.282, screen 0.5092 "ok").
- The first-element exemption is broken on a converged solve: aoa6/defl25's
  main at **0.287 separated**, which candidate 5's census term (8272 cells)
  called and everything in the screen family exempts. Census scale tracks
  the wall (8272→0.287, 3459→0.174, 377→0.207/0.113 across the pair).
- The small-open tier is real separation, not benign structure.

**And the anomaly that outranks all of it: the record's validated baseline
measures e2 wall 0.458 SEPARATED, converged**, while every Fluent
studio-yplus1 solve of the same design read attached (field 0.000, census
~35 cells) and the July record "validated" it at delta_cl ~0 on fine mesh.
"Validated" only ever meant Cl-accurate — this is the design's first wall
measurement. Either OF-medium over-separates (mesh grade), or Fluent
under-separates (engine/wall treatment), or the baseline genuinely runs
separated while matching the panel Cl. Every ATT label in the pools rides on
this. A FINE-mesh OpenFOAM solve of bl30 is running as the discriminator;
until it lands, the attached pool is SUSPENDED as evidence.

## 2026-08-03 — The discriminator: fine agrees with medium, and the attached
## pool dissolves

bl30 FINE, converged at 14 000 iterations: **e2 wall 0.437 separated** (vs
medium's 0.458 — agreement within 0.02 across mesh grades), e1 0.155
knife-partial, cl 3.4969.

The tell: fine's cl matches Fluent's (3.497 vs 3.509) almost exactly —
which is why the July record "validated" this design. **Cl-validation never
meant attached flow.** The validated baseline runs its flap 44 % reversed
while producing the validated lift, the same lesson as July's 3-element
finding (22–40 % reversed under a clean optimizer read), now on the
2-element baseline itself.

**Engine-level disagreement on flow state.** OpenFOAM reads e2 separated on
both mesh grades; Fluent studio-yplus1's exported field reads the same
design attached (probe 0.000, census ~35 cells). This is not a probe
artifact — the exported near-wall velocities genuinely differ. Which engine
is closer to physical truth is not determinable inside this toolchain;
Fluent owns absolute coefficients by the July reference flip, but
attachment class now disagrees between engines and neither has tunnel
validation. Every conclusion in this log built on a single channel inherits
that uncertainty, and the wall-shear channel is the declared truth channel
by long-standing doctrine, so:

- **All Fluent-zero "attached" evidence is dissolved** — including the
  session's 3/3 ATT leg for candidate 4 and both "validated baseline"
  labels.
- **All SEP findings survive** (wall-verified in six cases; the probe's
  high readings were confirmed, its zeros were not).
- **A new in-scope false negative**: bl30 sits fully inside the screen's
  validated envelope on every axis, reads 0.6032 — the healthiest-looking
  reading of the session — and is wall-separated at 0.437. The "scope, not
  weakness" story was scored with the field probe and is now overstated:
  scope violations correlate with failures, but in-scope does not imply
  correct.

**Candidate 4's ledger after dissolution**: SEP leg intact (wall-verified),
ATT leg = one solid point (gate e3: wall 0.027, margin +0.063, correct) plus
one knife on the correct side (defl30 e2: wall 0.055, +0.017). The ATT side
must be rebuilt from wall labels; an OpenFOAM batch of compound-ATT-predicted
designs is the direct way and is queued.

## 2026-08-03 — Wall check of the repairs: the direction is real, the
## absolute claims were channel artifacts, and defl ~11° is the wall optimum

Four converged OF-medium solves of the Fluent-attached designs:

| design | wall e1 / e2 / e3 | e2 class | cl (OF) | compound margin |
|---|---|---|---|---|
| step C (ref) | 0.076 / 0.291 / 0.282 | separated | 6.22 | −0.124 |
| Cdefl9 (the "fix") | 0.076 / **0.148** / 0.093 | **partial** | 8.50 | +0.027 |
| repair candidate | 0.104 / **0.241** / 0.036 | knife-separated | 6.20 | +0.017 |
| stepC defl 11 | 0.065 / **0.076** / 0.161 | **ATTACHED** | 8.63 | +0.026 |
| stepC defl 16 | 0.070 / **0.050** / 0.232 | ATTACHED (e3 knife-sep) | 8.02 | −0.002 |

- **The deflection recovery is real on the truth channel** — e2 wall falls
  monotonically 0.291 → 0.050 with deflection, agreeing with Fluent's trend.
  The engines agree on direction and disagree on absolute class throughout.
- **The Fluent verifications do not transfer**: the 8.9° fix is partial
  (0.148), and the "verified attached, cl 7.54" repair candidate is
  knife-separated (0.241) on the wall.
- **The wall-clean optimum is defl ≈ 11°**: e2 0.076 attached at cl 8.63 —
  the first wall-verified attached flap in the step-C family. e3 rises with
  e2 deflection (0.093 → 0.232), so more deflection is not free; 11° is the
  measured sweet spot of this 1-D slice.
- **Candidate 4's fine ordering is channel-specific**: on the wall, its
  margin does not rank these four (the −0.002 sits most-attached). Its
  coarse direction (strongly negative = separated) holds on every wall
  label; its knife band and fine ordering were fitted to Fluent fields and
  should be re-fitted on wall labels before any adoption.

Round verdict on the separation loop: detection, repair and
verification all work end-to-end, and the wall channel now grades each
piece. The optimizer-facing screen replacement (candidate 4) needs its
constants re-fitted on the wall-labeled record accumulated today; the
repair objective walks the right direction on the truth channel; and the
engine-state disagreement (Fluent attached vs OF separated on identical
designs) is the standing open question that no further solve on this
machine can settle — it is a physical-validation question.

## 2026-08-03 — Final: on wall labels, no panel statistic separates

Re-fitting on the wall-labeled record only (11 converged non-exempt
elements, partial band excluded): the shipped minimum overlaps by −0.135,
raw deceleration by −0.273, and candidate 4's compound by −0.027. The
in-scope false negative bl30 e2 (wall 0.437) reads healthier than every
attached element on every panel statistic; the repair candidate's e2 (wall
0.241) reads +0.019.

**Candidate 4 predicted the Fluent field, not the wall.** Its 14/14 + 3/3
record was real — against Fluent-field labels — and both channels' labels
are internally consistent; they simply disagree with each other, and the
wall is the declared truth. Conclusion: the panel model cannot grade
attachment against wall truth with any statistic measured this session.
Re-thresholding is not the fix; the screen's job on wall truth requires a
solver-side channel.

**What this leaves standing, by proposal:**
1. RANS-side two-mode flag — SURVIVES and is now wall-verified on both
   modes (probe ≥ 0.28 always wall-separated; census term tracks the main's
   wall fraction). This is the adoptable piece.
2. Panel-side screen replacement — DOES NOT SURVIVE wall labels. The
   optimizer's cheap screen cannot be fixed by any measured re-weighting;
   its honest role is candidate generation with RANS as the gate, which is
   what the queue demotion already implements on the OpenFOAM engine.
3. Repair objective — the compound walks the wall-verified direction
   (e2 wall 0.291 → 0.050 along its gradient) and found defl ≈ 11° which
   IS wall-attached at cl 8.63; usable as a search direction, never as a
   verification. Verification is a solve.

Open and outside this machine: which engine's near-wall physics is right
(Fluent attached vs OF separated on identical converged designs — a
physical-validation question), and every adoption decision above.

## 2026-08-03 — The accuracy reference gets the truth channel, and the
## engine disagreement is measured at wall level

Rulings (recorded): both engines remain options; ANSYS is the
accuracy reference where they disagree; nothing merges or publishes without
review. Delegated decisions (recorded): the two-mode field verdict is
adopted and wired; the panel-screen replacement is retired; the compound
repair objective is direction-only.

**The Fluent wall-shear channel exists.** Spiked on the retained collapse
case (the ASCII export accepts x/y-wall-shear on the profile zone, per-node
rows), then productionized: the 2D engine exports `wall_shear.csv` beside
C/U/p, `foam_post.fluent_wall_report` attributes nodes to elements (same
cKDTree pattern as the recirculation report), and the shared
`cfd_run.wall_verdict_text` grading produces identical verdict prose on
both engines. Sign conventions are opposite and both correct: Fluent
exports shear ON the wall (attached = +x), OpenFOAM traction on the fluid
(attached = −x).

**Measured, wall vs wall, per engine:**

| design | element | Fluent wall | OpenFOAM wall | field probe |
|---|---|---|---|---|
| collapse case | e2 | **0.378** | (class: separated) | 0.385 |
| collapse case | e1 / e3 | 0.090 / 0.141 | — | 0.014 / 0.005 |
| bl30 baseline | e1 | **0.153** | 0.155–0.192 | 0.065 |
| bl30 baseline | e2 | **0.102** | **0.437–0.458** | 0.000 |

The picture sharpens: on the collapse case every channel agrees
(separated); on bl30 the engines agree on the main and genuinely differ on
the flap at wall level — 0.102 (knife-edge partial, per the reference
engine) against 0.437 (deep separated, OpenFOAM). The dissolution entry's
"Fluent under-reads" was half right: its FIELD probe under-read badly
(0.000), but its own WALL channel reads 0.102 — the baseline flap is
knife-partial per the accuracy reference, not attached and not deeply
separated. The remaining engine gap on this flap is a real modeling
difference, in the partial band where everything is hardest.
