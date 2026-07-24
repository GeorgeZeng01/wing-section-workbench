# Optimizer & rules upgrade — before/after record (2026-07-23)

Branch `roadmap-rules-and-objectives`. Nothing here replaces the older
records: `DECISIONS.md` carries every decision with its alternatives,
`docs/calibration/LOG.md` carries the measured-physics log (including this
round's rejected slot metric). This file is the behavior comparison the
upgrade was judged by.

Reproduce any number here:

    .venv\Scripts\python.exe scripts\benchmark_optimizer.py --out <file>
    .venv\Scripts\python.exe scripts\benchmark_compare.py docs\benchmarks\baseline_main.json docs\benchmarks\after_upgrade.json

Scenarios are fixed and seeded (DE seed 1); the "measured" numbers are a
fresh full-paneling `quick_objective_eval` of each winner, so they compare
like for like across versions. Before = `fd6f3d5` (main), after =
`12dbfcd` (this branch).

## What changed

* **Rule envelopes** (user-entered mm limits, named presets): drawing
  overlay, analysis warnings and optimizer enforcement from one
  `geometry.envelope_check` — hard constraints, eager refusal on
  violating starts.
* **Target mode is two-phase**: attain, then descend drag along the
  achievable level D*; unreachable targets are measured and reported
  instead of faked.
* **Maximize downforce (trusted)**: new objective held inside the 90%
  loading line RANS validated, with full-fidelity output verification.
* **min L/D and min confidence floors**: soft hinge in the search, hard
  filter on the output, loud failure when nothing passes.
* **Pareto front** (clickable, trust-colored) + **RANS re-rank queue**
  (measured re-ranking of the shortlist).
* **RANS Stop & keep fields**, **DXF HITBOX layer**, **slot-corner
  advisory** (see the rejected-metric record in LOG.md).

## The numbers

| scenario | metric | before (fd6f3d5) | after (12dbfcd) |
|---|---|---|---|
| low_target_60n | downforce N | 60.0 | 59.6 |
| low_target_60n | drag N | 3.60 | 3.58 |
| low_target_60n | L/D | 16.68 | 16.63 |
| low_target_60n | max loading frac | 0.256 | 0.254 |
| low_target_60n | slot gaps %c | 0.80 | 0.80 |
| preset_target_250n | downforce N | 249.4 | 246.2 |
| preset_target_250n | drag N | 32.02 | 31.08 |
| preset_target_250n | L/D | 7.79 | 7.92 |
| preset_target_250n | max loading frac | 0.849 | 0.841 |
| preset_target_250n | slot gaps %c | 0.80 | 0.80 |
| thorough_250n | downforce N | 249.4 | 246.1 |
| thorough_250n | drag N | 32.02 | 31.05 |
| thorough_250n | L/D | 7.79 | 7.92 |
| thorough_250n | max loading frac | 0.849 | 0.840 |
| thorough_250n | slot gaps %c | 0.80 | 0.80 |
| hot_start_430n | downforce N | 396.4 | 382.5 |
| hot_start_430n | drag N | 93.79 | 86.26 |
| hot_start_430n | L/D | 4.23 | 4.43 |
| hot_start_430n | max loading frac | 1.199 | 1.168 |
| hot_start_430n | notes | – | unreachable_high (measured ceiling reported) |
| max_downforce | downforce N | 199.6 | 259.1 |
| max_downforce | drag N | 19.62 | 34.99 |
| max_downforce | L/D | 10.18 | 7.40 |
| max_downforce | max loading frac | 0.716 | 0.876 |
| max_downforce_ld14 | notes | option silently ignored | failed loudly: "no design holds L/D >= 14.0 (best found 12.7)" |

## How to read it

**Target mode buys drag with the budget it used to waste.** At the 250 N
preset the winner's drag drops 32.0 → 31.1 N (−2.9%) with L/D 7.79 → 7.92,
and the first design that hit the target during the search carried
35.4 N — the old early stop would have kept it in fast runs. Fast runs
now end where thorough runs do (31.08 vs 31.05 N) instead of wherever the
first on-target basin happened to be. Note the winners sit ~1.5% under
the exact target: the descend deadzone (±0.8%) deliberately trades the
last fraction of tracking for drag; the 3% on-target tolerance is
unchanged.

**Unreachable targets are measured, not faked.** Asked for 430 N, the old
search returned its best attempt with no explanation; the new run reports
`unreachable_high`, quotes the measured clean ceiling, and delivers it at
8% less drag (L/D 4.23 → 4.43). Asked for 5 N (below the floor — see
`test_optimizer_target_modes.py`), it reports the ~59 N clean floor in
plain language instead of choking its slots toward a fantasy number.
At 60 N — which the model CAN reach cleanly — behavior is unchanged by
design.

**Maximize downforce now exists and is trusted.** The old code silently
ignored `objective=max_downforce` (the "before" row is really a 200 N
target run). The new mode returns 259.1 N at loading 0.876 — inside the
90% line where the record says clean designs over-claim ~14%, not the
23–42% regime past it — verified at full fidelity on every returned
candidate. The recorded near-stall specimen (loading 1.12) runs through
this mode and comes back at 0.90. With an impossible L/D floor the run
fails and names the best achievable value (12.7) rather than returning
something that quietly violates it.

**The slot corner is now visible.** Every winner in every scenario, old
and new, still parks the slot gap at 0.80 %c — the inviscid preference the
surface-metric investigation could not price (LOG.md 2026-07-23). Those
designs now carry the slot-corner advisory and badge; the gap-axis RANS
leg (planned in LOG.md) is the experiment that will either raise the
gap floor or clear the corner at moderate loading.

**Suite**: 304 → 405 checks across 14 suites, all green
(`app/tests/run_all.py`).
