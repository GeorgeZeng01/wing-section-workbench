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
