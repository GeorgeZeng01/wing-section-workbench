# Design decisions

Design decisions made during development of Wing Section Studio, with the
alternatives that were considered. Recorded so any of them can be revisited.

## Physics

**Solver: purpose-built Hess–Smith panel method instead of AeroSandbox's
`AirfoilInviscid`.**
Options: (a) AeroSandbox's solver as-is, (b) wrap XFOIL, (c) write a
dedicated multi-element panel method. AeroSandbox needs 8–24 s per solve
(it builds a CasADi problem each call), which rules out optimization loops;
XFOIL is strictly single-element and cannot model slot interaction. The
dedicated NumPy solver runs 10–100 ms per solve and was validated against
AeroSandbox (pressure-integrated) to within ~3 % in free air and ground
effect, and against XFOIL inviscid on single elements.

**Ground-effect force from surface-pressure integration, not
Kutta–Joukowski.** During validation the two disagreed by 2×. Pressure
integration of AeroSandbox's own velocity field reproduced this solver's
value, confirming that `Cl = 2Γ` (which AeroSandbox reports) is invalid in
ground effect because it ignores image-induced velocities. Consequence: the
inviscid ground-effect numbers here are substantially larger than the ones
the older `stack_builder.py` screening printed; the older numbers understated
inviscid ground effect.

**Corrected estimate `C_est = η_visc·[C_free + k_g·(C_ground − C_free)]`
with `k_g = clip(0.25 + h/c, 0.30, 0.85)`.**
Options: (a) report raw inviscid only, (b) a single constant knockdown
factor, (c) a ride-height-dependent realization of the ground-effect gain.
Raw inviscid diverges as h→0 and misleads; a single constant cannot cover
both free air (~0.85 realistic) and strong ground effect (~0.3 at h/c≈0.1).
(c) was chosen. Both knobs are user-editable and the raw inviscid values are
always shown beside the estimate. The coefficients are starting values meant
to be recalibrated against RANS or tunnel data. *(Superseded twice: by the
tanh curve below, then by the bounded saturation model in the hardening
round at the end of this file — a k_g curve alone cannot bound the estimate,
because the inviscid gain it multiplies diverges faster than any reasonable
curve vanishes.)*

**Stack-angle sign convention: positive = more incidence = more downforce.**
The legacy `stack_builder.py` applies the opposite rotation for its
`--stack-aoa` flag (its "positive = more incidence" comment does not match
its code); a review of the app identified the same inversion here and it was
corrected — positive stack angle now loads the wing, consistent with flap
deflection, the presets, and the optimizer bounds. The legacy script was
left untouched.

**Element loading budgeted on free-air loads (classical high-lift practice),
not ground-effect loads.** Ground-effect inviscid element loads grow without
bound near the ground and made the check meaningless (330 % "overload" on a
plausible design). Each element's inviscid ground/free multiplier is shown
for transparency instead.

**Drag: profile + induced.** A loaded finite-span wing pays an induced
drag bill that dwarfs section drag (~2 N profile vs ~30–100 N induced at
FSAE loads), so an earlier profile-only figure was unrealistically low and
produced an implausible L/D ≈ 300. Now: profile drag is looked up at the
ground-effect operating point per element, and induced drag is computed from
the same downforce the tool reports, `CDi = CL²/(π·AR·e)·φ`, with `e` a span
efficiency input (default 0.9; endplates push it toward ~1) and
`φ = (16h/b)²/(1+(16h/b)²)` McCormick's ground-effect reduction at the
section mid-height. Options considered: leaving it out with a caveat
(exactly what produced the misleading number), a lifting-line solve
(overkill for a screening tool), the classical closed form (chosen). The
results tile shows the total with the split visible beneath it.

**Ground-gain realization recalibrated: `k_g = clip(1.4·h/c, 0.10, 0.85)`.**
Closing the loop on drag exposed that the previous floor (0.30) realized
far too much of the diverging inviscid gain at low ride height — wing CL
came out ≈ 11, and squaring that in the induced-drag term gave L/D ≈ 2.
The realization now falls toward zero as the wing approaches the road
(where real flow chokes), landing defaults at wing CL ≈ 4–6 and L/D ≈ 4–8 —
the band measured front wings occupy. Both knobs stay user-editable for
RANS/tunnel calibration.

**Airfoil selection as an optimizer variable (opt-in).** The requirement was
to let the optimizer choose each element's airfoil too, while keeping manual
selection and the screener. Options for a categorical variable over ~2,200
sections: (a) integer-encode the raw library index into the evolutionary
search — the alphabetical ordering makes neighboring indices unrelated
shapes, degenerating the search to random sampling; (b) exhaustive search
over screener shortlists — combinatorial across elements; (c) chosen: a
per-element shortlist of ~16 candidates built from the screener at that
element's own Reynolds number (blend of best CL_max, best L/D, and lowest
drag at the working CL, plus the currently selected airfoil always
included), encoded as one continuous [0,1) dimension mapped onto the list
ranked by CL_max — neighboring values are similarly aggressive sections, so
the evolutionary search remains meaningful. Practical thickness floors are
applied to candidates (5 %c main, 3.5 %c flaps — thinner sections are
structurally impractical). Shortlists reuse the screener cache, so
preparation is seconds cold and instant warm; chosen names are shown live
in the best-design panel and applied back to the element cards. Off by
default — enabling it is the "Airfoil selection" checkbox under Free
variables.

**Optimizer target term stiffened (60·err², drag penalty default 0.10).**
With realistic drag in the objective, the old weights let the optimizer
profitably trade ~5 % of the target away for induced-drag savings (induced
drag falls with downforce squared). The target term now dominates inside
~1 % of target; verified to converge to 249.5 N on a 250 N request while
still stopping early (~8 s).

### Solver and geometry corrections

A model review confirmed and fixed the following. (1) The contour-winding
test used an unclosed shoelace sum, which is not translation-invariant for
open-TE contours — a far-enough translated blunt-TE flap could be silently
re-wound and its load zeroed; the closing edge is now included. (2) `k_g`'s
hard floor of 0.10 kept 10 % of a *diverging* inviscid gain, so the headline
estimate diverged toward the ground — replaced with `0.85·tanh(1.4/0.85·h/c)`
(matching the old clip within ~1 % only for h/c ≈ 0.07–0.12; at moderate
ride heights it runs below it, up to ~24 % near h/c ≈ 0.6, shifting C_est by
a few percent there — and, as the later hardening round measured, it still
did NOT bound the estimate: k_g vanishes linearly in h while the inviscid
gain grows faster, so C_est kept growing all the way to the input floor.
The bounded saturation model at the end of this file is the actual fix). (3) `Cm_le` was reported CCW-positive
(nose-down-positive); now standard nose-up-positive, with `x_cp = −Cm/Cl`
updated so the center of pressure is numerically unchanged. (4) The
optimizer's load-envelope penalty (weight 8) was weak enough that searches
settled 27–44 % past isolated CL_max — separated-flow designs reported as
wins; weight 40 makes an honest target miss cheaper than a fake hit. (5) The
slot gap was measured from the TE base *midpoint*; with manufacturing prep
the base is 1–2 mm wide and the midpoint overstates the physical throat by
half of it — gap is now solved and measured against the whole base segment.
(6) `normalize()` promised "chord along +x" but never rotated: uploads with
baked-in incidence kept it silently, skewing thickness and deflection
semantics — it now de-rotates (incidence belongs to the stack, not the
profile). (7) `resolve()` intercepted every "naca"+digits spec, making 61
bundled 5/6-digit and modified-series NACA files unloadable — only 4-digit
codes are generated analytically now, the rest fall through to the library.
(8) An unreachable requested slot gap (flap far behind the TE) was silently
replaced by whatever the geometry gave; the report now says so.
(9) Truncate mode solved its cut station pre-rescale, overshooting the
requested TE by the rescale factor; it now solves t(x) = gap·x so the final
gap is exact. (10) NeuralFoil CL_max taken at the last grid angle (polar
never stalled in range) is now flagged as a lower bound instead of being
passed off as a stall value.

## Optimization

**Differential evolution + Nelder–Mead polish, on a worker thread with
polling.** Options: gradient-based (CasADi/IPOPT), Bayesian, CMA-ES, DE.
The objective contains penalty kinks and geometric infeasibility cliffs, so
a derivative-free global method is the robust choice; DE is reproducible
(fixed seed) and needs no extra dependencies. Budgets are exposed as
Fast/Standard/Thorough (~600/1500/4000 evaluations).

**Objective: squared relative downforce error + weighted drag + soft
penalties** for slot gap (0.8–3.5 %c), overlap (−1–5 %c), element loading
(>105 % of CL_max), and hard penalties for intersection/solver failure.
Ride height is preserved by construction (the installed frame re-seats the
stack at the configured height every evaluation).

**Progress, ETA, and early stopping.** Testing showed a 3-element
"Standard" run taking ~90 s with only a spinning status line — indistinguishable
from a hang. The job now reports a planned-evaluation progress fraction and
ETA (progress bar in the UI), the global search stops as soon as the target
is met within 0.8 % (the local refinement still tightens the result), and the
search phase uses coarser paneling for 3–4 element stacks (50/45 per side vs
60; the final analysis always runs at full resolution). Typical 3-element
runs dropped from ~90 s to ~15–25 s.

**Bound-pinning hints instead of a silently misleading result.** When the
target is far below what a stack can produce, the optimizer legitimately
parks stack angle and deflections at their minimum bounds — which reads as a
broken result. The UI now detects loading variables pinned at a bound and
says so plainly ("this stack can make far more than the target — raise the
target, drop an element, or shrink the chord", and the mirror case for
unreachable targets). Options considered: silently widening bounds (hides
the real message), auto-suggesting a target (too opaque), plain-language
hint (chosen).

**Optimizer returns a candidate set, not a single design.** Every feasible
evaluation is archived; on finish the job returns the best design plus up to
three alternates chosen farthest-point-first (worst normalized variable
distance, airfoil changes counting as maximal) from the on-target (±3 %),
low-penalty pool within 15 % of the best objective. This surfaces genuinely
different set-ups — e.g. 250 N at slot gap 0.8 %c/33 N drag vs 3.1 %c/37 N
drag — rather than jitter around one optimum, so build-tolerance and
packaging trade-offs stay visible. Each candidate is re-analyzed through the
full pipeline and rendered as a card with its own Apply. Options considered:
proper multi-objective (NSGA-style) Pareto set — rejected as overkill for
one target + soft penalties; archive mining is free and keeps the
single-objective machinery.

**Soft-penalty bands are baseline-relative: the starting design is always
penalty-free and representable.** The weight-40 load-envelope penalty
(correction 4 above) fixed fantasy designs from healthy starts but
introduced a worse failure: designs that already run past ~105 % of
isolated CL_max — which includes shipped multi-element presets at higher
stack angles and any aggressively loaded stack — could never be *matched*,
so every optimization of such a design returned less downforce than the
user started with (measured on the original model: 3-element preset,
baseline 745 N, target 819 N → 573 N, 23 % *below* baseline). The analysis
page reports those baselines' downforce as the headline number (warnings at
most), so the optimizer refusing the same operating point contradicted the
tool's own output. Now each job widens its soft bands once at start
(`Job._baseline_allowance`): every element may carry
`max(105 % CL_max, its own baseline loading + headroom)`, the realized
ground-loading allowance likewise stretches to
`max(2x CL_max, baseline + headroom)` per element (the hardening round's
weight-25 ground penalty would otherwise re-open the same hole from the
ground side), and slot gap/overlap bands stretch to include the baseline's
achieved values; the default search bounds also stretch to include the
baseline's variable values (explicit API bounds stay hard). From a healthy
start nothing changes — the absolute envelope still bars separated-flow
wandering; from a hot start the penalty now punishes getting *hotter than
the baseline*, not matching it (regression: two-element at stack angle 6°,
loading fraction 1.52, baseline 471 N, target +5 % → 492 N, no loss).
Options considered: reverting the weight to 8 (re-opens the fantasy-design
hole), hard-capping the estimate at CL_max in analysis (changes every
reported number, not a bugfix), baseline-relative allowance (chosen — the
guard becomes "do no harm" instead of "deny the baseline"). The regression
suite pins the invariant: optimizing a hot design toward a higher target
must never lose downforce. This work started on a parked WIP branch
(`wip-optimizer-baseline`) written against the pre-hardening optimizer; it
was reconciled here with the hardening round's eager bounds validation,
ground-loading penalty and full-fidelity winner selection, and the branch
is superseded.

**Thorough mode: no early stop, wider population, multi-start full-res
polish — reproducible by construction.** Testing surfaced non-reproducible
drag between runs that should have landed in the same ballpark. Diagnosis:
identical inputs were already deterministic (seeded DE), but the
apply-tweak-rerun loop starts each run from a slightly different config, and
with early stopping the global phase quit in whatever basin first hit the
target (measured: 31.7 vs 40.6 N drag across ±0.7° starting jitter —
different aoa/deflection basins, not noise). Fixes: (1) the final polish
always runs at FULL panel resolution and the reported best must come from
full-fidelity evaluations (coarse-panel bias exceeded the drag differences
that matter); (2) budgets ≥ 3000 ("Thorough") disable the target
early-stop, widen DE to popsize 16, and polish from the best plus two
maximally-diverse on-target archive points. Measured after: 0.0 N spread
across four jittered starts (each ~2 min). Fast/Standard keep the early stop
— their contract is speed. The regression suite pins both determinism and
the jitter spread.

## Shape refinement

**Hicks–Henne camber bumps + thickness scale as first-class `shape:`
specs.** The goal behind "custom airfoils" was to modify the already-optimal
sections to push the numbers, under the same constraints. Chosen
parameterization: three camber bumps (25/55/80 %c, power-3 Hicks–Henne —
the classical front/mid/aft loading levers) plus one thickness-envelope
scale per element; four dimensions per element keeps DE tractable and every
shape stays smooth and airfoil-like, so NeuralFoil's polars (and its
confidence signal) remain meaningful. Alternatives considered: full
CST/Kulfan re-parameterization (~18 dims/element — slow, and free
coefficients wander into shapes the surrogate can't rate) and
gradient/adjoint methods (no trustworthy gradients through the
panel+NeuralFoil chain). Implementation mirrors the mfg: pattern —
`shape:<b25>:<b55>:<b80>:<ts>:<base>` resolves through `airfoils.resolve`,
parameters quantized (0.05 %c / 0.01) for cache stability, `mfg:` wraps
outside so the as-built prep applies to the shaped section, the buildable
minimum floors each element's thickness-scale bound, and the stall budget
evaluates the shaped section's own polar. Re-optimizing a shaped design
unwraps and seeds from its parameters instead of nesting. Repanel/polar
caches grew (2048/4096 entries) because shape sweeps generate many distinct
specs; the UI warns the search is slower since every new shape needs fresh
aerodynamics.

## Manufacturing prep

**TE treatment offered as thicken (default) and truncate; no V-notch.**
Options considered: (a) XFOIL-TGAP-style blended thickening, (b) straight
truncation at the thickness target, (c) a V-shaped notch cut. Thicken keeps
chord, planform and camber, so the section keeps its design loading — on the
two-element S1223 baseline it costs well under 1 % downforce (233.2 →
232.5 N at the default 1.2 mm TE; 229.1 N at 3 mm) and is the standard
aerodynamic prep, hence the default. Truncate exists because sometimes a
finished part or mold must literally be cut back; on thin-tailed,
aft-loaded sections it is expensive (S1223: −17.5 % downforce at a 1.5 mm
target — the curled tail the cut removes is where the load lives), and the
UI tooltip says so. A V-notch was rejected: it replaces one knife edge with
two, adds a stress riser, and has no aerodynamic benefit at these speeds.

**Treated sections are derived airfoil specs (`mfg:<mode>:<gap_c>:<base>`),
not a geometry post-pass.** Resolution dispatches through
`airfoils.resolve`, so the panel solver, NeuralFoil polars, XFOIL reference
runs, screener and every exporter all see the as-built shape with no special
cases. The TE gap is quantized UP to a 0.025 %c grid so spec strings — and
the repanel/polar caches keyed on them — stay stable while the optimizer
sweeps chord ratios ("at least this thick" semantics make rounding up
correct).

**No bolt-on base-drag model.** NeuralFoil's Kulfan parameterization includes
TE thickness, so the polar of the treated section already carries the
viscous penalty of the blunt base as XFOIL would predict it; adding a
Hoerner-style increment on top would double-count. At the thicknesses in
play (≲1 %c) the effect is small; flatback-scale bases (>3 %c of an
element's chord) trigger an explicit warning instead.

**Minimum buildable thickness is a check plus a selection floor, never a
reshape.** If an element's thickest point is below the set minimum, the tool
warns (plain language, with the mm numbers) and keeps thinner airfoils out
of optimizer shortlists and the prefilled screener filter — but it does not
silently fatten a section that was chosen; that would change the aero far
more than any TE treatment. Uploaded airfoils are exempt from the floor (an
explicit choice); analysis warnings still flag them. Airfoil shortlist
thickness floors are computed at the *lower bound* of each element's chord
ratio when chords are free variables — previously a shrunk chord could carry
candidates below the buildable minimum.

**Blunt TEs in the panel solver are the open contour, not a closed base.**
The Hess–Smith Kutta condition (tangential velocities at the two TE panels)
handles small open TEs exactly as it does the many open-TE UIUC files;
paneling the base would create a closed polygon with an ambiguous Kutta
point. Exports close the base explicitly: polyline DXF via `close=True`,
spline DXF as an open surface spline plus a straight LINE (sharp corners),
SVG/viewport via path closure.

**Optimizer airfoil candidates: selectable pool (library / library + uploads
/ uploads only).** The screener already ranked uploads on merit, but merit
is the wrong bar when the question is "which of the molds I own is best" —
`custom_only` restricts the search to the uploaded set, `include_custom`
guarantees every upload a shortlist slot alongside the library's best.
Uploads outside the screener's confidence filter get their CL_max measured
directly so the random-key ranking stays meaningful.

**Thicken floors the whole aft thickness distribution at the TE gap.**
Testing found that with manufacturing prep on, parts of some sections were
*thinner than the trailing edge they had just been opened to*. Verified by a
library-wide scan: 1,629 of 2,174 sections kept such a waist at some
realistic gap, because the quadratic TGAP-style ramp only opens the TE —
for sections already thinner than the gap ahead of it (thin sail-like or
blunt-TE profiles) the region between the natural crossing and the ramp was
untouched. The reason the TE is opened is that anything thinner cannot be
built, and that argument applies to a waist just as much, so `thicken` now
adds material symmetrically about the camber line wherever the thickness aft
of the section's thickest point falls below the gap (with a 0.015 %c pad,
tapered off at the TE base, absorbing the downstream spline repanel's
undershoot at the plateau kinks; the deficient window is densified so the
floor holds between nodes). Options considered: (a) warn only — rejected,
the treatment's whole point is buildable geometry; (b) floor at the separate
min-thickness value — rejected, that field is a whole-section buildability
check with different semantics. `truncate` intentionally applies no floor
("literally cut the part"); instead `geometry_report` now *measures* the
thinnest aft station of every as-built repaneled contour
(`min_aft_thickness_mm`, the "waist" badge) and warns when it undercuts the
TE — verification, not trust, for both modes.

**The thickness floor starts where the nose taper ends, and "plates" are
called out.** A later review found a residual gap: ultra-thin sail sections
(as6097 on a 70 mm flap with a 3 mm TE) passed with green badges. Two
causes: (1) the floor window began at the thickest point, so a thin stretch
between the nose and the thickest point stayed thinner than the TE (as6099
dipped to 2.35 mm); the window now begins at the FIRST station that reaches
the TE gap. (2) A section whose untreated thickest point isn't meaningfully
above the requested TE (as6097: 2.97 mm vs 3 mm) cannot honor the treatment
at all — flooring turns it into a constant-thickness plate with a knife
nose. That is now flagged critically ("a plate, not an airfoil") rather than
reshaped into something that was never chosen, and both the optimizer
shortlists and the screener prefill exclude sections thinner than 1.5× the
TE gap at the element's own chord. Options considered: silently flooring the
nose region too — rejected, it destroys exactly the thinness that makes such
sections attractive and hides the mismatch; blocking analysis — rejected,
plate wings are legitimate.

**Manufacturing toggle is reversible; the one leak was the legacy dx/dy
migration.** Verified: the treatment itself never touches the config — sharp
builds before and after a treated build are bit-identical (regression test),
and gap/overlap-parameterized configs round-trip the toggle exactly. The
real leak: flaps placed by legacy dx/dy offsets get migrated once to the
gap/overlap parameterization by adopting the achieved values — and if that
first refresh ran with manufacturing ON, the adopted values were the
AS-BUILT ones (measured 1.52 mm of slot-gap difference on the demo stack),
so unchecking re-solved the flap somewhere else, permanently. The server
now reports the sharp-geometry slot metrics alongside the treated ones
whenever a flap still lacks slot parameters, and the migration adopts those;
toggling off reproduces the original placement to within the pre-existing
0.1 %c display rounding.

**The "warp" is camber-preserving by design, and that is the defensible
optimum.** Measured on the two-element baseline: the default 1.2 mm TE costs
0.3 % downforce, 3 mm costs 1.8 % — the price of buildability, and the
optimizer sees the treated shapes, so with manufacturing enabled its optimum
IS the buildable optimum. Distribution study (TE opened to 1 %c on the flap,
material split symmetric / all-upper / all-lower): symmetric keeps max
camber at the base value to 4 decimals; the one-sided variants are just
±0.25 %c camber shifts in disguise (all-lower: +0.9 % downforce for +2.0 %
drag; all-upper the reverse) — re-trims the optimizer's real variables
(deflection, stack angle, shape refinement) already control explicitly, with
neither variant dominating. Symmetric-about-camber is also what XFOIL's TGAP
does. NeuralFoil confidence on treated sections is unchanged (0.91–0.97), so
the surrogate is not upset by the blunt base or the floor plateau.

## Exports

**DXF as the primary CAD format; no STEP.** Options: DXF (ezdxf), STEP
(needs OpenCascade bindings ~500 MB, or fragile hand-written AP203), IGES.
DXF splines/polylines import directly as sketches in SolidWorks, Fusion,
Onshape, and NX, which covers the stated goal (usable in CAD right away)
without a heavyweight dependency. Per-element XYZ point files cover
curve-through-points workflows. STEP can be added later via `build123d` if
solid models are ever needed.

**DXF defaults: millimeters, R2010, spline entities, one layer per
element, ground line on its own layer.** Polyline entities are offered for
exact-point fidelity.

**Exports save to `exports/` and report their location.** Browser downloads
landed "somewhere" (worse in the WebView2 shell); now every export is
written server-side to the project's `exports/` folder with a timestamped
name and the UI shows a dialog with the full path, a **Show in folder**
button (Explorer with the file selected; the endpoint refuses paths outside
`exports/`), and an optional browser download of a copy.

## Application

**Local FastAPI + vanilla-JS single-page UI, zero frontend dependencies.**
Options: desktop GUI (Qt/tkinter), Jupyter, Electron, web app. A local web
app gives a modern UI with no packaging burden, works offline (no CDN
imports anywhere), and exposes a clean REST API (`/api/docs`) that scripts
can drive directly. Charts and the drawing viewport are hand-rolled SVG.

**Presented as a native desktop window (pywebview + WebView2), not a browser
tab.** Options for the "app, not browser" requirement: Electron (~150 MB
bundle, Node toolchain), a Qt/Tk native GUI (would mean rebuilding the whole
UI), packaging the web app with pywebview, or an Edge/Chrome `--app` window.
pywebview hosts the existing web UI in a genuine OS window using the WebView2
runtime that ships with Windows 11 — its own taskbar entry and icon, no
address bar or tabs, and no extra megabytes. The launcher (`app/desktop.py`)
runs the server in-process on a private random port, waits for health, then
opens the window; closing the window stops the server. It falls back to an
Edge/Chrome application window and then the default browser if the native
runtime is ever unavailable, so it always opens. Launched via `pythonw.exe`
(logging routed to a null handler since the windowless host has no console
streams) so there is no terminal window. One-click entry points: a
`Wing Section Studio` shortcut in the project root and on the desktop, plus a
`.bat` equivalent.

**Dynamic local port instead of a fixed one.** The frontend calls the API
with same-origin relative paths, so the port is irrelevant to API calls;
choosing a free port per launch means the app never collides with another
instance or a stray server. (The headless `uvicorn` command still uses 8642
for scripting.) One thing the changing port is NOT irrelevant to is
origin-keyed browser storage — which is why the working state persists
server-side, not in localStorage (see the hardening round below).

**Existing scripts left untouched.** The new app lives entirely in `app/` and
reuses `xfoil.exe` and the venv; `scripts/` keeps working as before.

**Screener runs synchronously (~7–20 s) rather than as a job.** NeuralFoil
sweeps the whole 2,174-section library fast enough that a progress pipeline
was not worth the complexity; results are cached per operating point.

**Uploaded airfoils live in server memory and are embedded into saved
project files,** so projects survive a server restart; re-opening a project
re-registers them.

**UI assets are served with `Cache-Control: no-cache` plus a one-time
version bump on the two direct asset links.** The first field update shipped
new HTML against a browser-cached old script (heuristic caching, since no
cache headers were set). Revalidation headers fix all future updates; the
`?v=` bump flushes caches created before the header existed.

**Local-only hardening.** The API now rejects non-loopback Host headers
(closes DNS rebinding — the one remote path to a 127.0.0.1 server),
filesystem airfoil specs are confined to the project folder (was: any
`.dat` on disk readable via `GET /api/airfoil`), raw `mfg:` element specs
are held to the same physical TE bounds as the validated manufacturing
block (and may not nest), and malformed optimizer options return 422
instead of 500. Same-named uploads with different geometry get
disambiguated display names. Deliberately NOT added: auth, CORS
lockdowns beyond Host pinning, HTTPS — this is a single-user local tool.

**Tests runnable as one command; pytest collection no longer fails.** The
suites are standalone scripts by design (they exercise a live server); a
conftest now stops pytest from collecting them (previously: INTERNALERROR
plus live API traffic fired during *collection*), `run_all.py` runs
everything including booting a scratch server for the API suite, and the API
suite honors WSS_TEST_BASE. Coverage includes optimizer candidates, export
save/reveal, SVG and CSV content, the thickness-floor guarantee, and the
winding regression.

## Usability review

**Slots are configured as Gap / Overlap, not dx/dy offsets.** The original
flap-position controls were raw LE offsets from the previous trailing edge —
correct but jargon that forced users to iterate toward the gap/overlap
values they actually think in. Flap placement is now solved from requested
slot gap and overlap (percent of chord, the parameters every high-lift
reference uses); the achieved values equal the requested ones to <0.001 %c.
The vertical placement has no closed monotone bracket (the flap crest sweeps
past the TE on the way down), so the solver uses the exact per-point circle
construction plus a segment-exact touch-up rather than bisection. dx/dy
remain accepted for legacy projects and are migrated to gap/overlap on load.
The optimizer's position variables switched accordingly, making its results
directly readable ("slot gap 1.4 %c" instead of "dx −0.2 %c").

**Slot metrics are wing-relative.** Gap/overlap are measured before the
stack rotation is applied, so the overlap reading no longer drifts with
stack angle (it is an x-projection; the gap was already
rotation-invariant).

**Launch shows a window immediately.** Double-clicking previously did
nothing visible for several seconds while the aerodynamic models loaded.
The native window now opens instantly on a branded loading screen and swaps
to the app when the embedded server reports healthy; a plain-language error
page appears if startup fails.

**Exports in the desktop window use the native Save As dialog.** pywebview
cancels downloads by default (`ALLOW_DOWNLOADS=False`), which would have
made every export button in the desktop app silently inoperative — enabling
it surfaces each export as a standard Windows save dialog. Browser sessions
keep the normal download behavior.

**The working state persists — server-side.** Every configuration change is
saved (debounced, with an unload flush) to `app_data/session.json` through
the API, with localStorage kept as a fallback; reopening the app continues
where the last edit left off, with a toast pointing at Load preset… for a
fresh start. Server-side rather than localStorage because the desktop
window runs the server on a fresh random port each launch — a new browser
origin whose localStorage is empty — and WebView2's default profile is
in-private besides; origin-keyed storage could never survive a desktop
restart. Uploaded airfoil geometry is embedded in the saved state and
re-registered with the server on the next launch. Nothing is stored outside
the local machine.

**Plain-language microcopy.** "Model settings" → "Air properties &
calibration"; "Drag weight" → "Drag penalty" with an explanation; tooltips
on slot gap, overlap, and the screener's reference CL; an ⓘ on the
Estimated-downforce hero stat opens the model explanation directly.

## Interface

**Dark "drawing office" theme with a dimensioned live technical drawing as
the centerpiece; all hue reserved for element identity and status.** The
four element colors were validated for color-vision-deficiency separation
and contrast on the app surface (worst adjacent ΔE 61.6, all ≥3:1).
Numerals are monospace throughout; status is never conveyed by color alone.

**Defaults:** 350 mm chord, 1400 mm span, 15 m/s, 30 mm ride height,
Ncrit 7 (on-track turbulence rather than the clean-tunnel 9), 250 N target,
s1223 main + 35 % flap at 24° — a well-documented FSAE-style starting point.
Presets cover a 3-element aggressive stack and a lower-drag E423 pair.

**Orientation naming: "As driven / Upright".** The original "Installed /
Design" frame toggle read as a mystery button that "flips the airfoils
upside down". The physics point it encodes — front wings run inverted, and
the upright view is only a catalog/CAD convention — is now stated where it
matters: renamed toggle with tooltips, a one-line note beside it describing
the active orientation, matching labels on the export orientation selector,
and a dismissible first-launch explainer in the viewport. Options
considered: removing the toggle entirely (upright viewing genuinely helps
when comparing against catalog profiles; kept), defaulting to upright (would
misrepresent what the analysis solves; rejected).

**Boot state: template drawn, numbers on demand.** The app used to
auto-analyze the starting template, so it opened onto a finished-looking
design with results already populated — confusing about what had happened
and what to do next. It now boots with the template drawn and dimensioned
but waits for an explicit Analyze/Optimize; the results panel explains both
actions, and the first-launch chip names the template. Loading a preset
still analyzes immediately (an explicit user action).

## Hardening round (adversarial review)

A full adversarial review of the code *and of this document's claims*
confirmed 67 defects — from a divergent estimate model to false statements
in the docs. Everything below was fixed and is pinned by the test suite
(now 100+ checks including closed-form anchors and export-content
verification).

**Bounded ground-gain model with a force-reduction peak.** The tanh k_g
curve did not bound the estimate: on the shipped default, C_ground scales
roughly h^-1.7 near the ground while k_g vanishes only linearly, so C_est
grew monotonically all the way to the h/c = 0.005 input floor (17.0 there
vs 6.4 at the default ride height) — no force-reduction peak, trend
inverted vs published data below h/c ≈ 0.08. Now:
`G_real = cap·tanh(k_g·G_inv/cap)·tanh((h/c)/h_choke)` with
`cap = gain_cap_ratio·|C_free|` (default 3.0) and `h_choke = 0.045`;
`C_est = η·(C_f + G_real)`. At ordinary ride heights this reduces to the
old form (−3.9 % at the default h/c = 0.086); toward the ground the cap
bounds the realized gain and the choke term takes it down, giving a peak at
h/c = 0.065 on the two-element baseline and monotone force reduction below
it — the Zerihan & Zhang shape. Options considered: warn-only below a
validity floor (keeps publishing numbers the model itself calls wrong);
blending in tabulated experimental curves (false precision from one
geometry's data); the saturation form (chosen — smooth, 2 constants, exact
reduction to the calibrated behavior where that behavior was right).

**k_g is now actually user-editable — the docs said it, the code now does
it.** Four documentation sites called k_g a user-editable knob; no such
field existed anywhere (a POSTed `k_g` was silently dropped by config
parsing). `k_g` is now a real config field (blank/None = the automatic
curve, a number pins it, validated 0..1, shown in the UI next to the
effective value), and `gain_cap_ratio` / `choke_h_c` are config fields for
RANS/tunnel recalibration.

**Per-element loading gets a ride-height-aware second check.** The
free-air Smith budget is translation-invariant, so it could not see ride
height at all — while four lines later the drag path computed a realized
ground-effect operating CL for the same element. Each element now reports
that operating CL (realized at the same ratio as the global estimate, so
element numbers compose exactly to C_est) and its fraction of isolated
CL_max; past 2× (measured sections rarely sustain more) the analysis warns
that the estimate is optimistic and drag understated, and the optimizer
penalizes the regime. The profile-drag lookup's pre-stall clamp is reported
per element (`cd_lookup_capped`) instead of silently engaging.

**The shipped defaults now pass their own budgets.** The default template
booted at 126 % of isolated CL_max — the tool's own "expect separation"
critical. Defaults and presets were retuned (two-element: flap 12°, stack
angle 0°, 233 N estimate, L/D 7.4, zero warnings; three-element and E423
presets likewise) so a new user's first Analyze is green.

**The .dat parser accepts exactly two numeric fields per coordinate
line.** The lax "first two parseable tokens" rule corrupted 22 bundled
sections: MSES-format files (all 20 tasopt-*) turned their plot-window
header into a (-2, 3) "coordinate", naca23021's parenthesized TE ordinates
were dropped, and nm26-3smoothed's trailing "a -> b" edit notes became
contour points (the "fold" downstream code choked on was parser garbage).
Parenthesized fields are unwrapped, everything else non-2-field is
commentary. Verified against all 2,174 bundled files: exactly the 22
known-bad ones changed.

**normalize() no longer tilts cambered sections.** The de-rotation angle
was measured LE-vertex-to-TE, but a cambered section's farthest-from-TE
vertex sits off the chord line, so every cambered airfoil was silently
rotated (naca6412: 0.32°) and camber misreported (naca2412: 1.83 % instead
of the defining 2.00 %). Tilts below 0.5° — indistinguishable from the
vertex offset — are now treated as catalog alignment and left alone;
genuine baked-in incidence above that is still removed. The y-origin moved
from the (thickness-dependent) LE vertex to the TE midpoint, the stable
catalog convention; measured camber/thickness now match defining values to
4 decimals.

**Manufacturing floor verified through the spline it must survive.** The
aft-thickness floor held at the polyline nodes but the downstream cosine
repanel fits a spline through them, which sagged below the floor between
sparse nodes (goe652 flap: 2.90 mm against a 3.0 mm floor — twice the
waist tolerance). The treatment now pre-repanels the base section onto a
dense grid (also making surface-x monotonicity — which the vertical
thickness measure silently requires — an explicit precondition instead of
an accident), densifies the floored window, and verifies through an actual
repanel round-trip, raising the pad adaptively until the floor survives.
Contours that fold back in x are rejected with a clear error; a
post-treatment crossed-surface check backstops everything.

**Intersection checked across all element pairs.** Only adjacent pairs
were tested, so a third element overlapping the MAIN (legal dx/dy inputs)
analyzed silently — returning 148 kN of fantasy downforce in the review's
reproduction. All pairs are checked now; analysis refuses and the
optimizer marks infeasible.

**Optimizer failures are diagnosable, never silent.** A job whose every
evaluation raised (e.g. a custom airfoil lost to a restart) finished
"done" with no best, no result, no error. Now: specs are resolved at job
creation (422 up front), evaluation exceptions and infeasibilities are
counted with their last messages, and a search that never saw a feasible
design fails with that diagnosis. Also fixed in the same pass: malformed
options/bounds 422 at creation instead of crashing the worker thread;
progress re-planned when the refinement phase starts (the bar froze at
~30 % after an early stop and the ETA claimed minutes seconds before
completion); "thorough" now really uses popsize 16 (the old formula capped
it below 16 for >10 variables — exactly the spaces the mode targets);
the final polish really runs at the configured full resolution (was capped
at 70/side); the reported best and candidate #1 must come from
full-fidelity evaluations; cancelled jobs finalize under a non-terminal
"finalizing" state so a poller that sees a terminal state always sees the
result beside it; with airfoil selection + shape refinement combined, the
thickness-scale floor is recomputed from the thinnest shortlist candidate.

**Error paths are consistent.** Unresolvable airfoil specs returned 500
from /api/analyze and every export endpoint (but 422 from /api/geometry);
KeyError messages carried their Python quotes into the UI; the XFOIL
engine without xfoil.exe returned a bare 500. All are 422/424 with clean
text now. CSV export quotes airfoil names containing commas (the column
count used to silently shift).

**Session persistence that actually works in the desktop shell** — see the
amended "working state persists" entry above. The browser-tab fallback of
the launcher also now exits once the UI stops heartbeating instead of
keeping the server alive forever, and a second instance no longer kills its
own server through the shared browser profile.

**Test suite hardened against its own weaknesses.** The review demonstrated
the old quantitative gate tolerated a 2×-broken induced-drag formula, and
the only external truth anchor (XFOIL) silently self-disabled when
xfoil.exe was absent — the default state. Added: a Kármán–Trefftz
closed-form anchor (exact potential-flow lift, no external tools; the
solver lands within 0.5 %), Cm/x_cp sign-and-value anchors, an exact
induced-drag recomputation that provably rejects a broken formula, export
CONTENT verification (CSV/DXF/SVG/ZIP geometry equals the analyzed
geometry, frames and y-sense included), the estimate-model shape (bounded,
interior peak, monotone force reduction), an analysis payload contract for
every field the UI reads, cache-staleness for edited .dat files, and a
loud SKIP when XFOIL is not installed.

## Roadmap execution

**Off-design behaviour as a sweep tab (operating maps), not a multi-point
optimizer objective.** A design tuned at one ride height can be wrong across
the travel, and the bounded estimate model now predicts a force-reduction
peak that a single-point analysis never shows. Options: (a) fold multiple
operating points into the optimizer objective (a weighted sum over ride
heights and speeds) — rejected for now: this is a screening tool, and a
multi-point weighting hides the trade-off inside coefficients the user must
guess at; (b) a Maps tab that sweeps the analyzed design across ride height
or speed and plots the result (chosen) — the single-point objective stays
honest and the trade-off stays in the user's hands, visible on a chart.
`sweep()` re-evaluates the same force/efficiency model at up to 40 points
with paneling capped at 60/side (the cap is reported in the payload); the
UI charts downforce and L/D against the swept value, marks the current
operating point and the downforce peak, and flags swept points past the 2×
ground-loading allowance as model-optimistic ("treat as upper bounds").
Speed sweeps reuse one inviscid solution — it is speed-independent; only
Reynolds numbers and dynamic pressure vary. Measured: the default
two-element template peaks at ~20 mm ride height (h/c ≈ 0.057), 234.7 N,
L/D 8.7 — the Zerihan & Zhang shape, now directly visible; a 23-point
ride-height sweep takes ~1.6 s.

**OpenFOAM case export, meshed with gmsh through its Python API.** The
roadmap's first two items (2D mesh generator, RANS case template) shipped
as one exporter: the OpenFOAM card in the Export tab writes a complete,
ready-to-run simpleFoam k-ω SST case for the installed section. Options for
meshing: (a) snappyHexMesh — needs a 3D STL pipeline and a dictionary stack
an order of magnitude more fragile than the geometry deserves; (b) an
in-repo structured multi-block mesher — months of work; (c) gmsh via its
Python API (chosen) — pip-installable, boundary-layer fields, and direct
physical-group control over the patches. Ground BC options: a slip wall
(no ground boundary layer at all — wrong physics under the wing), a
stationary no-slip wall (grows a spurious boundary layer upstream — the
wrong reference frame), or a no-slip wall moving at the freestream speed
(chosen — the road in the wing-fixed frame). y+ strategy: wall functions on
a y+ ≈ 30 first cell would cut the cell count but smear the very slot and
ground-gap gradients the case exists to resolve, so the first layer is
sized for y+ ≈ 1 from the flat-plate correlation at the config's Reynolds
number — with y+-adaptive wall treatment in the 0/* fields, so the
pure-refinement fallback mesh (gmsh's BoundaryLayer field is its fragile
corner) runs the identical case. The boundary-layer stack is auto-capped at
40 % of the tightest clearance in the geometry, so opposing layers never
collide across a slot or the ground gap. Presets land ~15k / ~40k / ~90k
cells (coarse/medium/fine). (Both superseded by the 2026-07-24 round: the
cap is now per element — measured against the clearances each element
actually faces, because one tight slot was starving every other element's
stack — and the presets land ~21k / ~46k / ~95k after the taller domain,
slot-throat refinement and wall densification.) The exported case is the roadmap's "truth
model" handoff made concrete — panel/NeuralFoil screen, RANS decides:
`run.sh` sources the newest WSL OpenFOAM environment (before enabling shell
strictness — the documented pitfall), converts and checks the mesh, runs
the solver, and greps the final downforce-positive, main-chord-referenced
coefficients into `results.txt`, ready to recalibrate `eta_visc`/`k_g`
against.

**In-app RANS verification runs the exported case in Docker — same case,
same run.sh, one click, opt-in.** The exporter above made "RANS decides"
possible but still asked the user to leave the app, open WSL and babysit a
terminal. The RANS verify tab closes that loop: the server builds the
identical case into `app_data/rans/<job>/`, runs it in a local Docker
container, streams convergence out of `postProcessing/` (live Cl/Cd and a
progress bar in the UI), and finishes with the tail-mean coefficients next
to the panel-model estimate — plus the pinned `k_g` that would make the
estimate reproduce the RANS sectional load (the model's own calibration
knob, invertible in closed form because the saturation model is monotone in
`k_g`), applied back to the config with one click. Runner options
considered: (a) drive WSL directly (`wsl -d Ubuntu bash run.sh`) — free but
distro-specific state the app cannot manage; (b) bundle an OpenFOAM build —
gigabytes in the repo; (c) Docker with the official ESI images (chosen) —
`opencfd/openfoam-*` ships the exact `/usr/lib/openfoam/openfoam*` layout
`run.sh` already sources, so the SAME script runs verbatim in WSL and in
the container (the Foundation images moved to `foamRun`/
`momentumTransport` and cannot run the v2xxx-dialect case; the image is
overridable via `WSS_OPENFOAM_IMAGE`). Deliberately opt-in and
non-blocking: nothing in the design workflow requires it, the button only
enables when Docker responds, one job runs at a time (the solver saturates
every core it gets), runs are cancellable (`docker rm -f`), wall-clocked at
4 h, and working directories are pruned to the newest few — they are
verification artifacts, not exports. The 2D case's Cd is profile drag only,
so the comparison pairs it with the panel stack's profile CD, never the
induced-drag-bearing total. Cross-checked against the earlier WSL runs of
the identical case: Docker (v2406 image) reports Cl 2.678 / Cd 0.222 on
the coarse two-element baseline where WSL (v2506) reported 2.68 / 0.217 —
the runners are interchangeable.

**Force-based convergence: the runner watches the lift history and stops
the solver itself; cap-limited trending runs are labelled NOT CONVERGED
and may not calibrate anything.** Field report: a fine-mesh (96k-cell)
three-element run reached the old 3000-iteration cap with Cl still
climbing +0.49 per 500 iterations (pressure residual 3.1e-3 against the
5e-5 stop target — the mesh was healthy and the solve stable, just far
from finished), yet the tab presented the tail mean of a ramp as the
result and offered a k_g calibrated to it. Residual control alone
under-serves loaded high-lift cases: they converge in forces long before
residuals, or need far more iterations than any fixed default. Now the
poller computes the relative disagreement between the last two
half-window means of Cl and Cd (drift); once flat past a minimum
iteration count it flips the case's controlDict to `stopAt writeNow`
(runTimeModifiable is already on, and ESI builds re-check by mtime, which
propagates through the bind mount) so the solver writes final fields and
exits cleanly through run.sh's extraction. Results carry an explicit
verdict — force history converged / residuals converged / iteration cap
reached — plus the drift value; the UI shouts NOT CONVERGED with the
remedy, and `suggested k_g` is only computed from converged runs. The
default iteration cap rose to 10000: with self-stopping, the cap is an
upper bound, not a duration. Options considered: OpenFOAM's runTimeControl
function object (same idea, but dictionary plumbing per case and no
say-why-it-stopped reporting on the app side), raising relaxation factors
(risks the very divergence the schemes were tuned against), host-side
drift watcher (chosen — one implementation serves every case, and the
runner already reads the force history every second).

Hardening applied after an adversarial review of the detector: the drift
decision EXCLUDES the first 500 rows outright (a decay-then-recover
startup — the usual potentialFoam-initialized shape on a separated case —
has a mean-crossing where naive half-windows cancel and a premature stop
would have been labelled converged; reproduced numerically and pinned in
the suite), the minimum gate is skip + full windows, the criterion
must hold across repeated evaluations, and the finalize verdict checks the
OUTCOME rather than the request — a writeNow the solver never noticed
(the run reached the cap anyway) is judged by its history, never trusted.
A force-stopped case's controlDict is restored to `endTime` afterwards so
the retained case stays manually runnable. Measured end-to-end: the
two-element coarse case force-stops at ~2,200 iterations in a bounded
limit cycle (Cl 2.71 ± 0.15); the fine three-element case that motivated
all of this converges at ~11,000 iterations to Cl 8.62 ± 0.07 — its
3,000-iteration snapshot had been 21 % low, and the graceful stop was
validated live against the real solver through the bind mount.
(Both the detector and that 8.62 figure were superseded in the 2026-07-24
round below: the drift test now spans three windows, re-arms on new rows
rather than wall-clock polls, and gates every verdict branch on the final
history — and 8.62 belongs to a hotter validity-boundary config, not to
the three-element design a user is likely to open.)

**Flow-field view: the solved section rendered in-app, no ParaView.** The
cases are one cell thick, so the internal field IS the cross-section:
run.sh ends with `postProcess -func writeCellCentres`, and the app parses
the ASCII C/U/p fields from the final time directory, interpolates onto a
regular grid (element interiors masked), and renders velocity magnitude
with streamlines or Cp — in the same as-driven orientation as the
drawing, PNG cached in the case directory, served from
/api/rans/{id}/flow. Rendering the diagnosed fine case made the
"inconsistent with the studio" report self-explanatory in one image: the
flap system was fully separated (a large recirculating wake), which the
attached-flow screening model cannot represent — precisely the class of
disagreement the truth runs exist to expose. Options considered: shipping
a ParaView macro (external dependency, manual steps), VTK sampling in
Python (a heavyweight dependency for a 2D slice), parsing the ASCII
fields directly (chosen — ~40 lines of parser, matplotlib is already a
dependency). Rendering uses the object-oriented matplotlib API (no pyplot
global state — the endpoint runs on the server's thread pool) behind a
render lock, and the PNG cache is published atomically (temp file +
rename) so concurrent requests can never read a torn image.

**Optimizer mode "refine" is accepted as an alias for "local".** The UI
had shipped `mode: "refine"` since the mode selector existed; before the
hardening round any non-"global" value silently fell through to the
refinement path, so it worked by accident. The hardening round's eager
options validation then rejected it — every "Refine current" start
422'd. The UI now sends the canonical "local", and the validator maps
"refine" to "local" permanently: saved automation scripts and older
sessions keep working, and the error message names the alias. (A
regression pins the alias; the lesson — when adding validation to a
lenient path, inventory what the lenient path was actually accepting —
is the durable part.)

**The viewport's Upright toggle is gone; orientation is an export
property.** The drawing now always shows the section as driven (the
orientation every number is computed in). The toggle's only real use —
catalog/CAD-convention output — lives where it always did, in the Export
tab's orientation choice. Two rounds of user feedback read the toggle as
a mystery switch; a view mode that changes nothing about the physics but
looks like it might is UI surface with negative value.

Building this surfaced a latent process bug: gmsh's C runtime REPLACES the
real Win32 process PATH during a mesh build (measured 1822 → 357 chars)
without touching Python's `os.environ` snapshot, so every subprocess
spawned afterwards — docker here, but also `explorer` behind the existing
"Show in folder" button after a CFD export — failed to resolve.
`build_case` now snapshots the real environment PATH (ctypes) before gmsh
and restores it after; the regression suite meshes and asserts the PATH
survived, and the docker runner additionally passes `env=os.environ` to
every child as belt and braces.

## Pre-release hardening

A pre-release round drove three successive adversarial review passes over the
whole tool — each pass targeting the code the previous pass had just changed —
followed by a security audit of the application's attack surface. Forty-one
functional defects were confirmed and fixed; the test suite grew to ~290
checks, and every fix below is pinned by a regression.

**Concurrency and lifecycle correctness.** The session store wrote through a
single shared temp file, so the debounced autosave racing the page-close
beacon (or a second window) could raise a Windows sharing violation and lose
the final save; each writer now uses a pid/thread-unique temp file with a
short replace retry. The optimizer's re-attach path was rebuilt to match the
RANS tab's: a reloaded page (or a second window) rediscovers a running search
through a new `/api/optimize/current` endpoint that returns the *most recent*
active job, a single failed status poll no longer orphans a still-running
search, cancelling during the final refinement phase now returns the coarse
winner instead of nothing, and a completed job clears its handle so the tab
can re-attach again. The RANS runner labels its containers with the owning
process id so the orphan sweep force-removes only leftovers from a crashed
server and never a live run started by another window, and its one-job guard
now claims the slot under the lock while doing the slow Docker housekeeping
outside it, so status and cancel stay responsive.

**Windows subprocess robustness.** gmsh replaces the real Win32 process PATH
during a mesh build; a prior round restored it afterwards, but a concurrent
Docker call *during* the build still saw the gutted PATH, and Windows resolves
a bare `docker` through the live PATH regardless of the child's environment.
The runner now resolves `docker` to an absolute path once and restores the
PATH immediately after gmsh initialises (milliseconds, not the whole build).
The desktop launcher's health probe and the test harness now use a
proxy-free opener — an environment or system HTTP proxy was routing the
loopback probe away from the server, so the app reported "server did not
start" on proxied machines although it was up.

**Parser and model edge cases.** `.dat` parsing now handles a UTF-8 BOM on
the file-read path (the locale codec glued it to the first coordinate),
drops exactly-duplicated consecutive points at the door instead of failing
deep in the spline repanel, and rejects a two-number name line while keeping
a genuine first coordinate of a near-vertically stored contour (the
header-artifact test measures span over both axes). The ground-gain cap is
floored at a small `|C_free|` so a symmetric section at zero incidence keeps
its real ground-effect load instead of collapsing to zero, and one cap
definition is now shared between the estimate and the RANS k_g inversion so a
suggested k_g always reproduces the run it came from. Sub-floor Reynolds
numbers are flagged in both the analysis and the operating-map warning count.
The layered-spec generators (manufacturing TE prep, shape refinement) were
made idempotent so they never emit a doubly-wrapped `mfg:`/`shape:` spec that
the resolver would reject — the earlier fix had tightened the resolver's
guards, which then rejected specs the app itself generated.

**Security.** The tool is a single-user local application, so the audit was
scoped to a hostile web page reaching the loopback server and to malicious
input files. Two DOM-XSS sinks were closed: a shared project file controls
airfoil display names and element specs, which were written to `innerHTML` in
the viewport legend and the airfoil-search dropdown; both now build their
nodes with `textContent`, so a crafted name renders as inert text. The upload
endpoint passed its `dat_text` to a parser that treats a newline-free
`.dat`-suffixed string as a filesystem path, bypassing the project-root
containment check that guards the resolver — API input is now always parsed
as literal content. As defence in depth on top of the existing Host-header
allowlist (which blocks DNS-rebinding) and the JSON-only request bodies
(which reject non-preflighted cross-site POSTs), the server now also rejects
any request carrying a present, non-loopback `Origin`; same-origin loopback
traffic and top-level navigations, which carry a loopback Origin or none, are
unaffected. The subprocess, generated-`run.sh`, and OpenFOAM case-file
surfaces were audited and found clean: every subprocess call passes an
argument list, and every case-file interpolation is a number or a
server-generated patch name, never a user string.

## Optimizer guardrails

**Manufacturing guard on Optimize: warn, don't default-on.** With TE prep
off (the default), the optimizer tuned knife-edge trailing edges whose flow
changes once the wing is made buildable. Clicking Optimize with prep off now
raises a dialog — "Enable & optimize" (turns on thicken/1.2 mm and runs) or
"Optimize anyway" (remembered for the session). The alternative, defaulting
manufacturing ON in the seed config, was rejected: it silently shifts every
default-template number the app has ever shown, prep parameters are
workshop-specific (a 3D-printed wing wants a different TE than a layup), and
the failure being fixed is specific to optimization — the guard interposes
exactly there, with consent. Warn-only also keeps existing sessions and
presets bit-identical.

**The optimizer now pays for leaning on data the model itself distrusts.**
Each evaluation already computed per-element NeuralFoil confidence, the
grid-edge flag (polar never stalled by the last analyzed angle, so CL_max is
a lower bound — the self-referential cap that shape refinement can inflate
by adding camber), and the capped-drag-lookup flag; all three were discarded.
`quick_objective_eval` now returns them and the objective adds a soft trust
penalty: quadratic growth as confidence falls below 0.5 (the screener's own
bar), plus a fixed bump per *loaded* element (past the 90 % free-air warning
threshold — in ground effect even the mild seed runs its main-element drag
lookup at the 95 % cap, so any looser gate would flag everything and mean
nothing) at the grid edge or on a clamped lookup. The penalty is
baseline-relative like the load bands: a re-optimized already-shaped design
is charged only for leaning *harder* on low-trust data, never for matching
itself — a standing offset would break early stopping and the candidates'
penalty gate. Weights (6.0 / 0.75) keep a fully flagged element worth about
an 11 % target miss: enough to prefer an equally-performing trustworthy
design, never enough to beat hitting the target. Alternative considered: a
hard confidence floor (reject evaluations below 0.5) — rejected because the
surrogate's confidence is itself screening-grade, and a cliff would
reintroduce the "optimizer refuses designs the analysis page happily
reports" contradiction the baseline-allowance work removed.

**Candidates carry their trust verdict.** Candidate summaries now include
`confidence_min` and `low_confidence` / `near_stall` flags (same gates as
the penalty); cards render them as badges plus the confidence percentage,
and the winner's flags are repeated as a hint beside Best design — the
winner is what gets applied, and a badge only on alternate cards would miss
it. `CAND_MAX` was raised 4 → 6: selection is diversity-gated
(farthest-point-first, 0.12 minimum spread), so the extra slots fill only
when genuinely different on-target designs exist, at the cost of two more
full-fidelity re-analyses at finalize.

**`BOUNDS_BUMP` left at ±0.012.** Tightening shape-refinement bounds was
considered for RANS-divergence reduction but not done: the stated bar was a
*measured* reduction, which needs a battery of Docker RANS runs, and the
trust penalty attacks the same failure (over-aggressive synthesized camber)
at its cause — the data quality — instead of shrinking the design space for
well-behaved shapes too.

## RANS cross-referencing campaign (2026-07)

**A 15-run OpenFOAM campaign cross-referenced C_est against the tool's own
truth model across ride heights and mesh fidelities; the full chronological
record, per-run data and fit tables live in `docs/calibration/`.** Headline
results: the current calibration is *validated* at h/c 0.086 and 0.429
(fine-mesh Δ −0.0 % and +0.2 %); the estimate is *optimistic* below the
venturi choke (−19 % at h/c 0.071, worse toward the ground — outright lift
loss the gain-only model cannot express); and it is *conservative* through
the mid-height gain peak (fine-mesh RANS +67 % at h/c 0.171, +35 % at
0.257 — the truth model's load peaks near h/c ≈ 0.17, not the
published-experiment 0.06–0.1 the curve was shaped to). The coarse mesh
mis-read a validated point by −29 % (under-resolved venturi gap), which
had produced a spurious "racing-height collapse" in the first sweep — a
result measured on THIS two-element baseline, not a general property of
the preset: the 2026-07-24 round found coarse and fine agreeing within
noise on a three-element case, and that round's slot-throat refinement
targets the very gap the −29 % came from. Every number in this section
also predates the 16-chord domain (see the provenance warning in
`docs/calibration/LOG.md`).

**Decision: no global k_g-curve refit — ship the validity map instead.**
The best-fidelity implied k_g set is non-monotonic (0.05 → 0.12 → 0.73 →
~0.5 → 0.12), so any refit curve would encode turbulence-model bias at the
extremes (fully-turbulent k-ω SST loses transitional high-lift near free
air) rather than physics, and both candidate refits measurably break the
two validated heights (fit tables in the log). What shipped: a standing
sub-choke warning below h/c 0.08 (`analysis.HC_CHOKE_OPTIMISM`, mirrored
in the operating map's warning count); a `mesh_caution` flag on every
coarse RANS result rendered next to the k_g suggestion (a k_g pinned from
a coarse run would bake a −29 % bias into the whole session); and the
model dialog updated to the measured validity map. The mid-height
conservatism is documentation, not a warning — it means more downforce
than claimed, and inflating warning counts would wrongly mark those
designs suspect on candidate cards. Alternative considered and rejected:
recalibrating the curve to track RANS Cl(h) pointwise — self-consistent
with the RANS tab but fragile (one section, one config, ±7 % limit-cycle
bands, one cap-limited point) and it would overwrite the two verified
operating points with chased noise.

**The trust badges were validated against fine-mesh RANS.** Two seeded
optimizer winners from the same baseline at the validated 30 mm height:
the unflagged 260 N winner over-claims by −14 %, the near-stall-flagged
430 N winner by −41.7 % — and the flagged design, claiming 40 % more
downforce, actually delivers *less* (RANS Cl 3.48 vs the clean winner's
3.66). The badge marks exactly the separated-flow fantasy designs it was
built for. Penalty weights stay as shipped (they bias search toward
trustworthy equals; the badge + RANS verify carries the honesty burden) —
the data to revisit them is in `docs/calibration/runs.csv`.

**The validity map generalizes as two orthogonal axes, each owned by an
existing mechanism.** A second campaign leg re-measured the 25/30 mm
pair at three off-baseline configs and the 60 mm point at 25 m/s. On
healthy designs (loading budget green) the ride-height map holds across
speeds: optimistic below the choke (−19 %/−12 % at h/c 0.071), honest at
0.086 (−0 %/+6 %), conservative mid-height (+67 %/+57 % at 0.171). On
designs past the 90 % loading warning line, the estimate measured
−23…−30 % optimistic at *both* heights — the Smith budget's warning
threshold is empirically where RANS optimism switches on, so the map's
wording now states that loading warnings trump the bands. A second
seeded Stage 2 pair replicated both badge classes: clean winners
−14.2 %/−14.4 %, flagged winners −41.7 %/−36.5 % — and in both pairs
the flagged design delivered less absolute downforce than the clean one
despite claiming 31–40 % more. Alternative considered: folding loading
into the banded map (a 2-D validity surface) — rejected as false
precision from four configs; the two existing mechanisms already carry
the message at the right granularity.

## Rule envelopes and optimizer objectives (2026-07)

**Rule envelopes are hard constraints, not soft bands.** The optimizer's
loading/gap/overlap bands are preferences: `_baseline_allowance()` widens
them so a start is never penalized for being what it already is
(do-no-harm). Competition rules are legality — a design outside the box is
not a worse design, it is not a legal design — so the envelope check marks
evaluations infeasible by name (`rule: max_length_mm by 15.2 mm`), and a
run started from a violating design refuses eagerly with a plain message
instead of burning its budget on a search where every evaluation would be
infeasible. Options considered: (a) treat the envelope like the soft bands
and widen to the baseline — rejected, do-no-harm would legalize an illegal
start; (b) silently gate per-evaluation only — rejected, a violating start
would run to "no feasible design found" with no hint why; (c) eager
refusal + per-evaluation gate (chosen).

**One envelope object drives everything.** `geometry.envelope_check()` is
the single compliance implementation; `geometry_report` (UI warnings +
viewport box), `quick_objective_eval` (optimizer feasibility), and the
optimizer's eager refusal all call it, so the drawing, the warning list
and the optimizer can never disagree about legality. The verdict echoes
the envelope limits so the viewport draws the box from the same object it
colors violations with.

**Envelope semantics v1: extent box in the installed frame at the
configured ride height.** Limits are mm caps on installed extents (length,
top height, ground clearance), not a positioned rectangle — the section
can be mounted anywhere along the car, so length constrains extent and
the drawn box's x-offset is display-only. All limits optional; values are
always user-entered, never hardcoded (rules change every season).
Alternatives: a positioned box in the wing-local frame (rejected — invites
false violations from an arbitrary mounting origin); checking across a
ride-height range (deferred — the rules FSAE-style teams care about are
measured in static/reference condition; revisit if a real rulebook needs
it).

**Rule presets are a machine-level library; the active envelope travels
with the project.** Named rule sets (`FSAE 2026`, …) live in
`app_data/rule_presets.json` behind `GET/PUT /api/rule-presets` — the same
rulebook applies across projects on one machine. The active envelope is a
`rule_envelope` config field, so sessions and project files carry it
automatically. Alternative considered: presets inside each project file —
rejected, a new project would start with an empty rulebook and teams would
re-type the season's numbers per project.

**Target mode is now two-phase: attain, then descend.** The old search had
two low-target failure modes, both measured on the shipped code: the
non-thorough early stop kept the FIRST design that hit the target (at
250 N the first on-target evaluation carried 35.4 N of drag; the design
the search should have found carries 29.9 N), and a target below the
stack's floor made the 60-weight tracking term reward shedding downforce
by slot abuse — the gap/overlap penalties are weight 2, and with flap
chords off the search has no legitimate shrink lever, so sabotage was the
only move left. Now: phase A is the unchanged tracker; the run then
resolves D* (the target when the clean penalty-gated pool reached it,
else that pool's floor/ceiling) and phase B re-seeds from the lowest-drag
on-level archive entries (diversified, multi-start, full paneling,
reserved 35% of the budget) and minimizes drag inside a 0.8% deadzone
spring at D*. Candidate selection re-scores the whole archive under the
final objective so "candidate #1 has the lowest J" stays true, and the
snapshot reports `dstar_n`/`target_note` so the UI quotes the measured
floor ("about 59 N — more than the 5 N asked") instead of guessing from
pinned variables. Options considered: (a) meet-or-exceed hinge objective —
rejected, target mode serves aero balance where overshoot is also wrong;
(b) leave target mode alone and add a separate min-drag-at-target mode —
rejected, the first-hitter/early-stop behavior is a defect, not a
preference; (c) two-phase with penalty-gated D* (chosen). Measured on the
fixed benchmark (docs/benchmarks): 250 N winner drag 32.02 -> 31.08 N at
the Standard budget, with Fast and Thorough now landing on the same
design (31.08 vs 31.05 N); within a single run the first on-target
tracker hit carried 35.4 N against the descended winner's 29.9 N (the
budget-500 regression test pins that spread). Identical-runs-identical
preserved; the hot-start do-no-harm guarantee (471 N baseline -> 482 N
optimized at full fidelity) preserved. Note: in-band gap shading (0.8 %c
is the band edge) is still penalty-free here by construction — pricing
that is the recovery-metric decision below.

**Max-downforce mode: the loading band hardens at the measured trust
line, cliff-free in the search, hard at the output.** The measured fact
this mode exists to respect: winners past the 0.90 free-air loading line
over-claim 23-42% against fine-mesh RANS (replicated), so "maximize
downforce" without the guardrail would be a fantasy-zone generator. In
this mode the do-no-harm widening deliberately does NOT apply to the
free-air loading band: an approach ramp starts at 0.85 (so the wall is
felt with gradient), a steep smooth wall stands past 0.90 (at loading
0.95 it costs more J than any plausible downforce gain buys), and the
output carries the hard guarantee — every candidate re-checked at full
fidelity, violators dropped, the best survivor promoted, and an explicit
failure ("...best candidate loads 97% — enlarge the chord, add an
element, or use target mode") when nothing survives. A hot baseline is
NOT refused (unlike rule violations — legality vs trust): the run
proceeds under the capped band with a note, and the UI says the winner
may sit below the start's over-claimed number by design. Options
considered: (a) mark loading > 0.90 infeasible mid-search — rejected,
the same cliff the trust-penalty decision already rejected; (b) penalty
scaling only, no output filter — rejected, the mode's entire output
would be the over-claim regime the moment the penalty is out-bid;
(c) ramp + wall + output guarantee (chosen). Reward is linear
(60·(1 − downforce/baseline), scale fixed at run start) — no set-point,
so the gradient must not vanish; measured on the fixed benchmark (after
the review round below fixed the wall continuity and the clean-pool
gate) the mode lifts the two-element baseline's 259.1 N claim to
269.2 N with the winner riding the line at 90.0% loading, and pulls the
recorded near-stall specimen from 112% back to 90%. Fine-mesh RANS then
mapped the mode's own axis: -19.2% at loading 0.876 (pre-review-fix
winner) and -24.4% at 0.900 (post-fix winner riding the line), against
the -14% clean anchor near 0.85 — optimism is a continuum rising toward
the line, not a step past it. Decision on that evidence: the 0.90
boundary STANDS (it is where the recorded -37..-42% collapse regime
begins, and moving it on four loading points would be curve-chasing —
the same reasoning that rejected the global k_g refit); instead the
gradient is documented in the mode's hint and LOG.md, and the RANS
re-rank measures each actual winner. Anyone wanting the -14..-19%
regime simply reads the re-rank table or backs the target off the line.

**min_ld and min_confidence are floors with the same shape: soft hinge in
the search, hard filter at the output.** `min_ld` (efficiency floor, the
usable form of "best ratio" — pure max-L/D degenerates to the smallest
wing) and `min_confidence` (user-set NeuralFoil confidence floor,
default 0.5 = the screener's bar; the penalty knee follows the user's
floor so the search is steered away from designs the filter would
discard, and conf_pen0 keeps the charge baseline-relative). Both filters
drop violating candidates at full fidelity and fail with a message naming
the floor and the best value found when nothing passes. Consequence
accepted at the default: a winner that would previously ship with a
`low confidence` badge is now replaced by the best passing design, and
the run fails outright only when nothing passes (the calibration campaign
measured that badge class over-claiming to -42%, so shipping it as the
winner was the worse default). Alternative considered: default the floor
to "off" and enforce only when set — rejected as keeping the measured
failure mode as the default output; floor 0 restores it explicitly.

**Pareto front: mined from the archive at run end, re-analyzed at full
fidelity, trust-colored, deliberately unfiltered.** The optimizer always
archived every feasible evaluation with downforce AND drag, then threw
the trade-off away; now the clean (penalty-gated) non-dominated set is
downsampled to 24 points (extremes + farthest-point, knee-dense),
re-analyzed at full paneling with the same trust summary candidates
carry, and served in the snapshot as a clickable chart — click applies
the design through the existing applyDesign path. The front is NOT run
through the output filters: it is a view of the whole trade-off, and
hiding the flagged region would misrepresent where the model stops being
trustworthy — flagged points render amber instead. While the search
runs, a strided downsample of the archive streams as a live grey cloud.
Options considered: (a) expose the raw archive to the client and mine
there — rejected, the front needs full-fidelity re-analysis and configs
reconstructed server-side anyway; (b) live full-fidelity front during
the run — rejected, up to 24 analyze() calls per poll; (c) final
finalized front + live search-fidelity cloud (chosen).

**Slot-flow trust metric: built, measured against the RANS record, and
rejected — a geometric advisory ships instead.** The observed failure mode
(flow detaching ahead of the main TE while the flap sits above the slot
flow) suggested a Smith-1975 canonical-recovery metric on the coupled
inviscid solution: a mispositioned flap removes the dumping-velocity
relief, so the upstream element's demanded surface recovery should climb.
Five variants were measured against every configuration with a fine-mesh
RANS delta on record (`scripts/recovery_metric_check.py`, kept as the
executable evidence): the naive ground-solution form reads 0.977-0.990
for EVERY design (ground-image suction peaks, cp_min to -49, dominate the
normalization); free-air and TE-offset variants leave the clean and
warned classes overlapping; and the one variant that nearly separates
them (TE dumping-velocity ratio) is a loading proxy that moves the WRONG
way along the gap axis — the inviscid solver reads a tighter slot as
MORE relief, because the real tight-gap failure is boundary-layer
merging, which no inviscid quantity sees. Options considered: (a) wire
the near-separating variant anyway — rejected, redundant with the
RANS-validated loading machinery and actively wrong on the gap axis;
(b) an unvalidated geometric capture-window with invented thresholds —
rejected, nothing on record calibrates it; (c) ship a signature advisory
+ leave the quantitative floor to the RANS gap-axis leg (chosen). The
check also CORRECTED an assumption this plan carried: the clean -14%
stage-2 winners share the exact slot corner of the flagged ones (gap
0.80, overlap ~0) — the record separates the over-claim classes by
LOADING alone, and since the validated 1.5%c-gap baseline at comparable
downforce measured ~0%, the healthy-winner -14% bias may itself partly
be the tight-slot cost. That hypothesis is exactly what the gap-axis
RANS leg measures. Until then, `analysis.slot_signature_warnings` flags
the corner (gap <= ~1%c AND overlap < 0.5%c) with the recorded numbers
quoted, in the analysis page and every optimizer candidate's warning
count; no optimizer penalty is charged on geometry the record has not
priced.

**Adversarial review round (2026-07-23): 33 confirmed findings fixed;
two accepted with rationale.** A five-dimension adversarial review of
this branch's diff (each finding independently refuted-or-confirmed)
caught, most importantly: the max-mode wall DIPPED to ~0 just past the
0.90 line (the elif dropped the ramp's terminal value — the search was
attracted into 0.90-0.908, the exact band the mode excludes), and the
clean-pool gates tested the raw penalty including the ramp, which
excluded the legitimate 0.885-0.90 shoulder from candidacy while
admitting 0.90-0.9056 — the pool was inverted exactly across the trust
line. Fix: the wall carries the ramp's terminal value, and the pools
gate on `pen_gate` (penalty minus the max-mode loading shaping — the
ramp is search pressure, not a verdict). Measured effect: the benchmark
max-mode winner moved 259.1 -> 269.2 N, now riding the line at 90.0%
loading. Also fixed: rule legality re-certified at full paneling before
any candidate is returned; dropped winners promote the best survivor by
the final objective (not a diversity pick); the solver guard made
two-directional between single runs and the queue; user-stop verdicts
mirror whether the stop demonstrably took effect; and a dozen smaller
UI/report/test defects (see commit cc7d80f). Accepted without code
change: the rule-preset PUT has no concurrency token (single-user
localhost app; last-writer-wins on a hand-edited library is acceptable),
and a ~one-poll race can conservatively label a residual-converged run
"stopped by user" when the stop lands in the solver's final second —
the mislabel direction withholds a k_g suggestion, never invents one.

**RANS re-rank: the maximum-accuracy step is RANS at the END of the
loop, not a different engine in it.** The user's ask was maximum
accuracy. Measured reality: there is no viable viscous 2D multi-element
ground-effect engine to swap in (MSES is licensed and fragile exactly
here; an in-house viscous-inviscid coupling is a research project with
its own unvalidated error bars); RANS-in-the-DE-loop is arithmetic
nonsense (~1500 evaluations x hours); and the coarse mesh — the only
fast RANS — mis-read a validated point by -29% in the campaign, worse
than the panel model inside its trusted band. What ships: a sequential
verification queue (app/core/rans_queue.py) that takes the shortlist —
winner, candidates, the Pareto knee — through the existing cfd_run
pipeline at medium mesh by default (medium agreed with fine in the
campaign; fine for finals), re-ranks by MEASURED downforce, and
classifies each panel-vs-RANS delta against the recorded bands (healthy
band above -20%, over-claims below, conservative above +5%). Every item
passes the rule-envelope gate before a solver hour is spent; the queue
shares the single-solver guard; verdict rows persist with the session.
Alternatives considered: surrogate/EI refinement around the RANS winner
— deferred as the documented extension, the plain re-rank is the 80%
that costs 20%; parallel solves — rejected, Docker/WSL2 is a single-lane
resource and the one-job guard exists for measured reasons.

**RANS "Stop & keep fields": the graceful writeNow path gets a user
trigger, with an honest verdict.** Cancel hard-kills the container
(`docker rm -f`), which preempts run.sh's writeCellCentres step — a
cancelled run can never feed the flow view. The force-based auto-stop
already had the right machinery (controlDict flipped to `stopAt
writeNow`, solver writes fields and exits 0, run.sh completes, finalize
restores the case); the new button routes a user request through exactly
that path. Two deliberate choices: (a) the verdict is "stopped by user
(fields written)", converged = False, and NO suggested_k_g — a
hand-stopped tail must not feed calibration however flat it happens to
look (options considered: offer k_g with a caution flag — rejected, the
calibration log's discipline is that only converged verdicts contribute);
(b) the request is refused with a 409 before the solver runs — there are
no fields to keep yet, and pretending to "stop" a meshing job would just
be a slower cancel. Cancel stays available unchanged.

**DXF bounding box: 4 LINEs on a dedicated HITBOX layer, off by default.**
Horizontals touch the stack's lowest/highest points, verticals its
leftmost/rightmost — instant overall dimensions in CAD, deletable in one
action by killing the layer. Options considered: (a) a closed LWPOLYLINE —
rejected, four independent lines match how CAD users measure and trim
against reference geometry, and either dies with the layer anyway;
(b) always-on — rejected, most exports feed lofts where any non-contour
geometry is noise. Available in both frames (the design frame has no
ground line, but its box is still meaningful).

## RANS realism and verdict honesty (2026-07-24)

A saved three-element design read RANS Cl 7.48 against a panel estimate of
4.51 — "+66 %" — and the run was suspected of being unphysical. Four
controlled A/B solves on that exact configuration (recorded in
`docs/calibration/LOG.md`) found the solver was substantially right and
three separate presentation and setup faults were making it look wrong.

**The domain was a wind tunnel.** At 8 chords the slip ceiling was
inflating Cl by ~5 % and Cd by ~24 % on a section running sectional
Cl ~7.5 — blockage, not aerodynamics. Doubling the height to 16 chords
costs ~6 % more cells because the far field is coarse. Alternatives
considered: a far-field/Riemann boundary condition (correct but changes
the case class and its validation history for a bias the taller box
removes outright), or an analytic blockage correction applied to the
reported forces (rejected — a correction the user cannot see is worse
than a mesh that does not need one).

**Fully-turbulent SST is the conservative choice here, and that was
measured rather than assumed.** The obvious suspicion at Re 4.7e5 on an
S1223 is that ignoring transition inflates lift. The γ-Reθ transition
model was run on the same mesh and read **26 % HIGHER** Cl — laminar runs
thin the boundary layers in the favorable ground-effect gradient. So the
shipped setup understates rather than overstates, and no model switch was
added; the finding is documented in the generated case README instead of
becoming a knob nobody can calibrate.

**The "+66 %" was the estimate being conservative, not the truth model
being wrong.** The k_g realization curve is calibrated on the two-element
baseline; this stack realizes k_g ≈ 0.37 of its inviscid gain against the
curve's 0.158. The curve was NOT refitted — fitting a config-blind curve
to one config is curve-fitting a design, the same reasoning that rejected
a refit in the cross-referencing campaign. Instead the model's scope is
now stated where it is used: `analyze()` warns on three-plus-element
heavily loaded stacks that the estimate is expected to run conservative
there, and the RANS card says so beside the delta rather than presenting a
bare percentage the user must interpret alone.

**A number is now allowed to say it is not finished.** The reported ±
was a population standard deviation over a drifting tail — for a ramp that
is range/√12, i.e. a deterministic function of the drift rate, so a run
still climbing advertised 0.24 % precision. The tail statistic is now
detrended (scatter about the tail's own trend line) and the drift is
reported separately, signed: a rising history labels its own mean a lower
bound. Verdicts were tightened to match: the drift test spans three
windows because two read flat at every zero-crossing (one window past an
overshoot peak, or at a node of a slow oscillation riding a climb), the
stop criterion re-arms on new rows rather than wall-clock polls (a fine
mesh advances only a few iterations per second, so consecutive polls
re-judged the same data), and EVERY finalize branch is gated on the final
history — previously an early exit with exit code 0 could mint
"residuals converged" plus a k_g calibration constant from a drifting
tail. The stop mechanism now explains an exit; only the history certifies
a result.

**Every run measures its own trustworthiness.** Cases write yPlus and
wallShearStress fields beside each field set, and the app reports measured
y+ and a per-element attachment verdict from reversed wall shear. This is
the check a bare Cl cannot give: on the case in question it showed the
main element attached and the 27° flap attached at 2.7 % reversed faces —
the high load is an attached multi-slot system working, which is the
difference between a number to trust and a number to re-run. Rejected
alternative: inferring separation from the force history's oscillation
amplitude — indirect, and it conflates limit-cycle sampling with flow
state.

**Mesh: per-element boundary-layer caps and a resolved slot throat.** The
layer stack was capped globally by the tightest clearance anywhere in the
geometry, so a 1.3 %c slot gap starved the main element's stack to ~19 %
of its physical boundary layer. Each element is now capped by the
clearances it actually faces, and each slot throat carries a refinement
box guaranteeing ≥ 8 cells across the jet on every preset. The meshed wall
polyline is also densified independently of the panel count — surface
resolution used to be whatever the panel solver happened to want.

## Separation awareness (2026-07-24)

The user kept measuring early flow separation on RANS solves of
optimizer products — the flow visibly dead ahead of the trailing edge —
while the optimizer reported clean designs. The retained case record
made the failure precise: on EVERY converged three-element case the
second element ran 22–40 % reversed wall-shear faces (the standing
bubble sits right on top of it in the flow fields), the mains were
attached, and the optimizer's loading budget — an element-integral check
against isolated CL_max — had nothing to say about any of it. The
mechanism is wake confluence, not element stall: the element sits in its
neighbors' circulation shadow, the stream over its upper side (the
stream carrying the upstream wake) decelerates below about half the
freestream, and the wake+boundary-layer system over that side collapses.

**Measure-first, again.** Three candidate screens were built and run
against the wall-shear record before anything was wired
(`scripts/separation_metric_check.py`, truth preserved in
`docs/calibration/wall_truth.json` because run-dir housekeeping deletes
cases). (a) A full integral-BL march — Thwaites, Michel with the
short-bubble rule, Head with Ludwieg–Tillman — stopped at the first
H-crossing: flags the VALIDATED baseline's flap (its nose-spill bubble
reads as total separation) while grading the genuinely dead elements the
same as healthy ones. (b) The same march run through to the trailing
edge with re-heal: Head's entrainment closure, built for attached
layers, pumps every separated stretch back to attached. Both rejected —
at the booked realization the free-air-dominated distributions of dying
and healthy elements are nearly identical pointwise, so no chordwise
march on them can rank designs. (c) The **wake-shadow screen**: minimum
realized upper-side velocity over the 15–92 % arc window, elements after
the first. Separates the record cleanly — separated-measured 0.389–0.459,
attached/partial-measured 0.533–0.619, the no-wall-truth "flagged"-class
flaps in the 0.519–0.524 gray between — and is stable under paneling
45–70 and realization ratio 0→0.35. Shipped with the line at 0.50 (mid-gap, half
the freestream) and the caution band to 0.53.

**Wiring follows the house pattern.** `analyze()` reports per-element
`shadow_min`/status with a collapse warning citing the record; the
optimizer charges the same hardened, cliff-free shape as the max-mode
loading band — a light ramp across the 0.50–0.53 gray band (capped at
0.3, under the clean-pool gate, because legitimate on-target designs and
the no-truth flagged class both live there) and a steep wall below the
line that carries the ramp's terminal value (the shallowest recorded
collapse costs ~17; the two-element benchmark's 300 N chase costs ~0) —
gates the clean pool at the 0.50 line, and badges candidates
(`shadow_collapse`, gray-band `shadow_warn`). Target mode keeps the
do-no-harm rule — a collapsed baseline slides its wall to 0.03 below
its own value and pays NO gray-band ramp in the slid region (sized to
the measured paneling drift plus what a legitimate +5 % target chase
moves the metric; the hot-baseline regression pins this): it is never
refused, matching or modestly out-chasing itself is free, and only
going deeper meets the wall. Max-downforce mode does NOT widen (its contract is trusted
output) and its full-fidelity output filter drops candidates that still
collapse, with the drop named in the failure message. The verification
queue now carries each row's wall verdict and demotes any design
measuring an element separated below every attached row, under both
ranking objectives — measured forces from a separated flow state stop
buying podiums. Rejected alternative: tightening the loading bands
instead — the record shows the dying elements at loading fractions 0.48–
0.77, squarely inside every band; no loading threshold separates these
classes without also strangling healthy designs.

**Descend seeding hardened alongside (measured on the hot-baseline
regression).** Adding any new objective structure reroutes the seeded
differential-evolution path, and the hot-baseline regression exposed a
pre-existing fragility that the reroute tipped over: descend seeded only
from the CLEAN pool's lowest-drag edge, so when the pool thins near the
level (a hot baseline's on-level designs mostly wear gate bumps) every
simplex started below the level, took the easy aoa-relief exit through
the spring's deadzone, and stalled at 477–478 N against a 483 N
equilibrium — while a zero-penalty on-level design demonstrably existed.
Two changes, both to seeding rather than to any recorded dynamic
constant: descend now seeds the attain phase's own best FIRST in target
mode (descend exists to polish what attain found; a seed is a starting
point, not a verdict — the clean-pool gate still owns D*, the candidate
pools and the output, so the sabotage protections stand), and every mode
runs three descend starts instead of two (the third basin is what
recovers the along-level valley; costs ~90 extra full-paneling
evaluations per standard run). Max mode keeps pool-only seeding — its
attain best can sit behind the trust wall it exists to enforce.

**Scope stated where it is used.** The screen is validated on climbing
front-wing stacks in ground effect at h/c 0.086–0.114, s1223-class
sections, against five cases (two fine, one medium, plus a coarse and a
transient snapshot kept for the record); the first element is exempt
(no upstream wake, and the record confirms mains attached at upper-side
minima down to 0.44 — the loading machinery owns that failure mode).
Tight-gap slot-jet merging remains invisible to every inviscid quantity
— the slot-signature advisory and the RANS queue stay the referees
there. The check script exits nonzero the day the thresholds stop
separating the record, and `test_wake_shadow.py` pins the same classes
in the offline suite.

## Parallel solves, opt-in (2026-07-25)

The runner's one-job mutex was justified in code by "simpleFoam is
CPU-bound on every core it gets" — which is false. OpenFOAM's finite-
volume solvers have no threading: every solve was one process on one
core, and the mutex was protecting capacity nothing used. This round
added the two axes that were actually available — MPI ranks within a
solve, and concurrent independent solves in the shortlist queue — as
**explicit opt-ins**. Serial single-run stays the default on every path,
and the serial case carries no parallel artifacts — no MPI invocation,
no decomposeParDict, the same solver chain as always (pinned by test; the
only later serial-script edit is the rerun-reset list gaining the
animated-flow cache file, which touches cleanup, not the solve) — so
recorded baselines stay reproducible without qualification and nothing
changes for a user who never touches the new controls.

**Mechanics.** `n_ranks > 1` writes a scotch `decomposeParDict` beside
the case and swaps run.sh's solve step for `decomposePar -force` /
`mpirun --allow-run-as-root --oversubscribe -np N simpleFoam -parallel`
/ `reconstructPar -latestTime`, then removes the processor directories —
the reconstructed case is laid out exactly like a serial one, so the
flow view, the wall report, results.txt and a later manual WSL rerun all
work unchanged. potentialFoam stays serial, *before* decomposition, so
every rank count starts from the identical initial field. The force and
y+ function objects write merged `postProcessing/` from the master rank
throughout, which is why the convergence poller and the writeNow force
stop needed no changes — both were verified against a live 4-rank
container run, including a mid-solve stop flip propagating through the
bind mount. scotch is deterministic for a fixed mesh and rank count: the
same case at the same N reproduces exactly; only *across* rank counts
does the iteration path legitimately differ.

**The queue schedules against a core budget.** `start_pooled()`
registers solver jobs without the interactive one-at-a-time guard (which
still covers the verify tab — a tab start refuses while a queue runs,
and vice versa), and the queue keeps up to `max_concurrent` solves of
`n_ranks` each in flight. Admission requires ranks × concurrent ≤
`core_budget()` — half the logical CPU count, because SMT contributes
approximately nothing to a memory-bandwidth-bound FV solve
(`WSS_CORE_BUDGET` overrides). At the default 1×1 the scheduler's
observable behavior is the old sequential queue exactly; crash
reconciliation, ranking and prune protection are pinned by the existing
suite.

**Rank-count drift is reported, not suppressed.** 2D RANS in this app is
a comparator — no endplates, infinite span, no wheel wake — so a
decomposition perturbing converged forces by a fraction of a percent
cannot disturb an ordering that already tolerates 30–40 % magnitude
error. The one place it can bite is separation onset, where steady RANS
holds attachment past the point a real flap lets go and the optimizer
deliberately pushes toward the line. Rather than demanding verdict
stability across rank counts, the verdict lines grew **knife-edge
bands**: a reversed-face fraction within ±0.02 of the 0.10 attached
line or ±0.05 of the 0.20 demotion line is labeled `knife-edge` in the
wall verdict (and `sep_knife_edge` in the result), and a wake-shadow
`shadow_min` within ±0.03 of the 0.50/0.53 cutoffs carries a
`knife_edge` flag. A verdict that would flip with core count is a
knife-edge candidate — that is information about the design, not noise
to eliminate. The widths are the measured spread below with margin: the
separating element moved 0.042 across 1→16 ranks while attached
elements held within 0.02, so the demotion line (where the unstable
physics lives) carries the wide band; the shadow skirt is that same
spread mapped through the validation record's class geometry
(attached-class floor 0.533 at frac ≈ 0.12 against separated-class
ceiling 0.459 at frac ≈ 0.22 → ~0.74 shadow-units per frac-unit →
0.042 × 0.74 ≈ 0.03).

**Measured scaling (wall-truth case 1c4bf2d726c3, fine, 111,501 cells,
one mesh shared by every run; all four ran the identical 11,000
iterations to a flat force history, so wall-clock ratios are pure
per-iteration ratios).**

| ranks | wall-clock | s/iter | speedup | parallel efficiency |
|------:|-----------:|-------:|--------:|--------------------:|
|     1 |   60.2 min | 0.3269 |   1.00× | 100 % |
|     4 |   20.9 min | 0.1125 |   2.91× |  73 % |
|     8 |   14.5 min | 0.0771 |   4.24× |  53 % |
|    16 |   11.5 min | 0.0605 |   5.40× |  34 % |

Marginal speedup per added rank: 0.64 (1→4), 0.33 (4→8), 0.15 (8→16) —
**the knee is 8 ranks** for a single attended solve (4.2× for half the
machine; the next doubling buys 1.16× for the other half), and **4
ranks is the throughput-per-core maximum** (73 % efficiency), which is
why the queue's recommended allocation is 4 solves × 4 ranks. Memory
bandwidth, not core count, is the ceiling — as expected on a desktop
part.

**Result spread across rank counts (same case, same mesh, same
iteration count).** Cl 8.3597 / 8.3925 / 8.4174 / 8.4286 at 1/4/8/16
ranks — a 0.069 spread (0.82 % of serial), monotone with rank count in
this study (not established as causal); Cd spread 0.0019 (1.4 %); the
tail limit-cycle std held at ~0.02 Cl on every run. In force terms the
spread is **7.5 N on a 903 N section — fifteen times the ~0.5 N
path-luck margin of the recorded regression baselines**, so parallel
results are NOT comparable to serial baselines at regression tolerance:
re-baseline under the rank count you standardize on (a fixed rank count
reproduces exactly), and compare like with like. As a comparator the
ordering is untouched — 0.8 % cannot reorder designs the model already
ranks through a 30–40 % magnitude tolerance. The verdict-bearing
fractions: the separating element read 0.217 / 0.239 / 0.250 / 0.259
(spread 0.042, "separated" at every rank count — no flip, and the
serial value sat inside the knife-edge band and was labeled so), while
the attached elements held within 0.02 of their serial values.

**Queue sweep headline.** A representative shortlist — four medium-mesh
candidates (e3 deflection 24/27/30/33° on the wall-truth stack),
max_iters 10000 — measured **85.8 min under the sequential serial
behavior and 15.9 min at 4 solves × 4 ranks: 5.4× end to end**. Every
row force-converged in both arms and the measured ranking came out
identical (d33 > d30 > d24 > d27). Per-row Cl agreed within 0.25 % on
three rows; the hottest flap (d33) read 4.4 % apart between arms —
medium-mesh screening scatter plus a path-dependent force-stop point on
the most limit-cycling candidate, consistent with the recorded
medium-mesh caveats — without moving its rank. The 4×4 allocation
follows from the scaling table: 4 ranks is the last near-full-fare
point (73 %), and four such solves fill the 16-core budget.

## Fluent second opinion via MCP (2026-07-29)

A licensed ANSYS Fluent (2026 R1 on this machine) is now drivable from
agent sessions through `scripts/fluent_mcp.py` — a Model Context Protocol
server over ansys-fluent-core's gRPC session. Design decisions, in order
of consequence:

**The slab solves in Fluent 3D, not true 2D.** App meshes arrive through
`foamMeshToFluent` (run in the same ESI container as the RANS tab) with
the `frontAndBack` empty patches retyped to symmetry — so Fluent solves
the IDENTICAL one-cell mesh the OpenFOAM referee solves, and
apples-to-apples cross-solver comparisons need no mesh caveat. True-2D
Workbench meshes load through the same tools with `dimension=2`.

**The documented manual GUI recipe is reproduced, with two deliberate
corrections, both surfaced in the tool output.** The walkthrough's
defaults leave Fluent's reference area at 1 m², so its
"coefficients" are not chord-normalized; the setup tool sets reference area/length explicitly
(chord × depth / chord) and reports them. And lift is reported
downforce-positive (force vector (0,−1,0)) to match the studio's Cl.
Everything else follows the walkthrough: velocity inlet, 0 Pa outlet,
no-slip profile, shear-free ceiling, moving ground at the inlet speed,
hybrid initialization, SST k-ω (set explicitly rather than trusted as
the default), constant-property air at the studio's ρ/μ.

**Verdict honesty carries over.** `solve()` returns tail statistics and
the three-window drift measure beside every coefficient — a
still-trending tail is labeled a bound, never a result — and every setup
step reports applied-or-failed so a partially configured case cannot
pass silently (the failure list caught two real 26.1 API quirks during
bring-up: `depth` is a 2D-only reference value, and converted patches
must be retyped to wall before their momentum settings exist).

**Fluent's own convergence criteria are disabled by the setup tool —
measured, not assumed.** On the bridge case Fluent's default residual
thresholds (1e-3) declared "solution is converged" at iteration 277
while the lift history was still trending at ~15× the studio's drift
bar — the exact failure mode the app's verdict honesty exists to
prevent. The recipe turns per-equation convergence checks off; the
iteration budget and the force-history drift own convergence, same
doctrine as the OpenFOAM runner.

**Converged cross-solver datum (identical mesh, both referees flat).**
The coarse two-element case, one mesh: OpenFOAM (serial, 8000
iterations, drift 0.0004) settles into its recorded limit cycle at
**Cl 2.488 ± 0.132, Cd 0.2181**; Fluent (4 processes, the full 4000
iterations, dead-flat history) lands at **Cl 2.230, Cd 0.1985 — −10.4 %
and −9.0 %** against OpenFOAM. Two honest observations ride with the
numbers: the codes disagree about the *unsteadiness* itself (OpenFOAM
sustains a ±5 % limit cycle where Fluent's steady solver damps flat),
and the Fluent side ran its 26.1 default discretization rather than
schemes pinned to OpenFOAM's (`tui()` can pin them; a scheme-matched
A/B is the natural next probe). A ~10 % cross-code gap on a separating
high-lift case is ordinary solver scatter — and is precisely why the
comparator doctrine (orderings, not magnitudes) governs both referees.
OpenFOAM remains the calibration referee; Fluent is an independent
cross-check, and no studio verdict, calibration constant or queue
behavior depends on it. Both solves plus a JSON-RPC round-trip through
the registered MCP endpoint are the bring-up record; a session checks
out the ANSYS license at `launch` and releases it at `shutdown`/server
exit.

## Fluent workflow equivalence, and the reference flip (2026-07-29)

The manual GUI chain (CAD → DXF → Workbench → Discovery → Mechanical
mesh → Fluent) is now fully automated: `run_case` takes a
walkthrough-style DXF unmodified (the domain rectangle, when drawn, is
used verbatim), meshes it with the same gmsh machinery the OpenFOAM
cases use, and solves with the recipe — every stage configurable per
run (geometry source, mesh mode/sizings/domain multipliers, physics,
conventions, budget, processors; the spec schema lives in `run_case`'s
docstring). The equivalence campaign below is the measured
manual-vs-automated record on the calibration two-element section
(15 m/s, h30). **Reference flip, recorded as the designer's judgment:
absolute coefficient levels now follow Fluent — OpenFOAM levels have
read inaccurate against trusted references — superseding the "OpenFOAM
remains the calibration referee" framing of the entry above. The
in-app OpenFOAM pipeline keeps its screening/ranking role (orderings,
wake-shadow, queue demotion), which never depended on absolute
levels.**

Four runs, one section (Cl downforce-positive and chord-referenced
throughout; the manual row's raw output converted for comparison):

| run | mesh | process | Cl | Cd | vs B |
|---|---|---|---:|---:|---:|
| B automated, studio | app fine 95.1k, y+≈1 | 6000 it, drift 0.0034 | **2.2678** | 0.1926 | — |
| C manual parity | walkthrough 85.6k, 1 mm walls | stopped at **397** it, drift 0.0095 | 2.776 | 0.2687 | +22.4 % |
| D walkthrough mesh, studio process | walkthrough 85.6k | 4000 it, drift 0.0 | 2.7630 | 0.2690 | +21.8 % |
| A OpenFOAM (datum) | app fine 95.1k | 11000 it, converged | 3.6287 | 0.2187 | +60.0 % |

**What the manual workflow actually produces, measured.** Its raw
number for this section is `lift_coef = −0.04463`: +y convention on a
downforce section, referenced to Fluent's default 1 m² — meaningless
until someone hand-applies a 62.2× area factor and a sign flip
(1/(L × 0.1 L) with L = 0.401 m, the DXF-route slab reference — the
app-route 0.35 × 0.035 m² reference would be 81.6× and does not apply
to the walkthrough-mesh rows here). And
its "500 iterations" never happened: Fluent's default residual
criteria stopped the run at 397 with the force history still trending
(drift 0.0095, above the 0.006 bar) — the manual workflow's stopping
point is a hidden criterion nobody chose. On this case the
truncated value landed ~0.5 % from the converged one (C vs D) — path
luck, not process: the drift number says it was still moving.

**Mesh, not solver, carries the recipe difference.** Same solver, same
process, walkthrough mesh vs studio mesh (D vs B): +21.8 % Cl /
+39.7 % Cd. The walkthrough's 1 mm first layer puts a separating
flap's boundary layer on wall functions exactly where they are
weakest; the studio's resolved-wall mode is the better-practice
default, and the walkthrough mode remains available
(`mesh_mode="walkthrough"`) whenever matching legacy numbers matters.
Cross-code, same mesh (A vs B): +60 % — recorded as a
datum under the reference flip above; separation-dominated 2D RANS is
where codes diverge hardest, and it is why orderings, not magnitudes,
remain the decision currency.

**Not worse than manual, by axis:** identical meshing available on
demand (walkthrough mode reproduces the recipe; D equals what an
unhurried manual run would converge to); strictly better wall physics
available by default (resolved mode); coefficients arrive normalized
and signed instead of raw; convergence is verdicted instead of implied;
provenance (every sizing number, zone map, drift, iteration count) is
returned rather than remembered; a run is a JSON spec instead of a
GUI session — reproducible and diffable; and the whole chain runs
unattended in minutes of machine time instead of an hour of clicking.
The GUI's remaining advantages (visual mesh inspection, interactive
contours) are covered by the returned mesh statistics plus
`write_case_data` (ParaView/Fluent-openable) and `contour_png`.

**Walkthrough audit — every instruction, its default, its knob.** The
domain the solver uses always follows the walkthrough (what looks
smaller in the app is the *view* window — the flow panel's View select
now offers "Full domain" to see the whole box). The table as a whole —
the defaults column as well as the knob names — records the 3D-slab
route (`scripts/fluent_workflow.py` mesh spec); the true-2D chain that
is now the default reaches the same instructions through `write_dxf_2d`
and `mesh_sizing`, whose knobs are named in the ANSYS settings
subsection below, and where the two routes' defaults diverge the
true-2D number is given in the row:

| walkthrough instruction | automated default | knob |
|---|---|---|
| front ≥ 3× profile length | 3.0 × L | `mesh.front_l` |
| back 5–10× length behind | 7.0 × L (mid-band) | `mesh.back_l` |
| top ≥ 3× profile height above | 3.0 × H | `mesh.top_h` |
| ground at the rectangle bottom; without ground, mirror the top below | DXF rectangle used verbatim when drawn; else `ground_y` (app sections: y = 0) or 3×H mirrored below | `mesh.ground_y` / the rectangle itself |
| export DXF, splines NOT as polylines | SPLINE entities read natively (plus polylines/arcs/lines) | `mesh.scale` for units |
| named selections (inlet/outlet/upper bound/ground/profile) | inlet / outlet / top / ground / profile_e{i} (true-2D chain: inlet / outlet / upper_bound / ground / profile) | zone-name args on setup |
| profile edge sizing "0.1 mm" — the walkthrough's own meters example says 0.001 m = 1 mm, contradicting itself | 1 mm (the worked example's number); true-2D sizing `default`: the literal 0.1 mm | `mesh.edge_size_m` (1e-4 for the literal 0.1 mm) |
| inflation: first layer 1 mm | 1 mm (slab walkthrough mode; true-2D sizing `default`: the same 1 mm) | `mesh.first_layer_m` |
| maximum 10 layers | 10 (slab walkthrough mode; true-2D sizing `default`: the same 10, capped to the clearances the stack faces) | `mesh.n_layers` |
| growth (unspecified; Mechanical's default is 1.2) | 1.2 | `mesh.growth` |
| double precision | double | `launch.precision` |
| velocity inlet at the desired speed | `physics.velocity_ms` | same |
| pressure outlet at 0 Pa | 0 Pa | fixed (recipe) |
| profile: stationary no-slip wall | yes | fixed (recipe) |
| upper bound: specified shear = 0 | yes | slip-zone args |
| no ground → bottom same as top | shear-free bottom | `physics.moving_ground=false` |
| moving ground: no-slip moving wall, direction (1, 0), speed = inlet | yes | `physics.moving_ground` |
| reference values computed from the inlet | `walkthrough`: literal (area stays 1 m²); studio: explicit chord-referenced — the documented correction | `conventions` |
| lift + drag coefficient reports on the profile, named | `lift_coef` / `drag_coef`, exact names | `physics.downforce_positive` |
| hybrid initialization | yes | `solve` (initialize) |
| 500 iterations | `walkthrough` parity honors the number (Fluent's own criteria may stop earlier — measured at 397); studio: budget + drift verdict | `solve.iterations` |
| probe coefficients; contours for visuals | returned in the result + `contour_png` + the in-app flow views | view-settings row |

**In-app engine.** The verify tab gained an Engine select: the same
one-click workflow can now solve through Fluent
(`app/core/fluent_run.py` — a FluentJob mirroring the OpenFOAM job's
surface, driven through the proven session helpers, solving in chunks
with the studio's drift criterion deciding convergence). Both engines
share the one-job-at-a-time registry, so they cannot collide; the
"Solver cores" knob means MPI ranks on one engine and processor count
on the other; a Fluent run holds the ANSYS license only while solving
and writes `case.cas.h5` beside the run. The flow view and animation
work on both engines: a Fluent run exports its solved cell-centre
field (`export_ascii`, pressure converted to the kinematic convention)
and writes it as OpenFOAM-format C/U/p files in a time directory, so
foam_post renders a Fluent run through the exact pipeline the OpenFOAM
engine uses — one rendering path, no divergence. Wall-shear attachment
verdicts stay OpenFOAM-engine features for now — the result says so
rather than leaving the panel blank silently.

## Flow-view polish round (2026-07-29, designer feedback)

Four reported deficiencies, each fixed with the mechanism stated:

**Fluent views failing on first click** was an ordering bug, not flake:
the job reported "done" before its flow-field export had written the
field files, so the UI's immediate fetch 422'd and only a later retry
worked. "done" is now deferred until the export lands (pinned by a
state-at-export test), so the first click always finds the files.

**Full-domain zoom turning to mush** was a fixed-grid problem: 320
samples across an 18.5-chord box leaves nothing near the wing. The
animated view now refetches **level-of-detail windows**: once the
visible region is a fraction of the base field and the grid reads
coarse on screen, the client requests just that box re-gridded at full
resolution (`/flowfield?x0..y1`, clamped server-side to the solved
cloud, uncached), and swaps it under the unchanged view transform.
Zooming from the whole domain into a slot gap stays sharp; zooming back
out restores the base field.

**Trail rendering** gained presets — comet (the original short wisps),
long streaks, and persistent streaklines — switchable live (fade rate,
lifetime, opacity ramp per preset). Playback also extends to 1/250×
real time, and the scroll-hijack fix (Ctrl+wheel zooms, plain wheel
scrolls) covers both flow views.

**Light-mode rendering** (from the same feedback batch): figure chrome
and the animation canvas are theme-aware, with the light theme
defaulting to the turbo rainbow Fluent users read natively; colormap,
scale clamp, streamlines and view extent are per-request settings on
both engines' renders.

## ANSYS-native meshing becomes the Fluent default (2026-07-29/30)

**The question that prompted it:** is the meshing for the Fluent runs
done outside of ANSYS? It was — entirely. The studio's gmsh cut the
cells, then `gmshToFoam`/`foamMeshToFluent` (OpenFOAM utilities in the
Docker container) converted them; nothing ANSYS touched the mesh until
the solver read it. ANSYS does provide its own meshing for Fluent:
**Fluent Meshing** (the watertight geometry workflow), part of the same
install, driveable headlessly through pyfluent's meshing mode. (The
manual walkthrough's mesher — Workbench/Mechanical — also exists but
has no headless Python route, so it is not an automation candidate.)

**The native route now shipped, and made the default** for every Fluent
path (in-app engine, `mesh_from_dxf`, `mesh_from_app_config`,
`run_case`): the studio writes the fluid slab's boundary as a
watertight multi-solid ASCII STL — box faces, the two z-planes with the
profile tunnels cut out, one wall solid per tunnel — and Fluent
Meshing's watertight workflow cuts every cell the solver sees. Solid
NAMES drive Fluent's boundary-type inference (inlet / outlet auto-type,
`symmetry-front/back` become the slab's symmetry planes, the rest
walls), so no fragile Update-Boundaries scripting is load-bearing.
The writer self-verifies: it refuses to emit a surface whose every
edge is not shared by exactly two facets, or whose divergence-theorem
volume misses box-minus-tunnels.

Three findings from the live bring-up on 2026 R1, each now encoded:
STL enters the workflow as a MESH-format import (`FileFormat "Mesh"`,
`MeshFileName` — the CAD route refuses it), and the import DOES
preserve multi-solid names as zones. The meshing kernel works in
MILLIMETERS (`MeshUnit "m"` scales the model exactly; every size handed
to the workflow is converted m→mm — the first attempt passed SI meters
and asked for a 2.45-micron surface size over a 6 m domain). And the
z-plane tessellation must be quality-bounded: a first-cut ear-clip
triangulation put aspect-1e5 slivers on the 5.6 m planes and TGrid's
import culled ~87% of them as degenerate, leaving free faces the
surface remesher could not recover — so the planes are tessellated by
gmsh as a GEOMETRY step (graded from the polyline spacing to the far
size; hole boundaries transfinite so the profile polylines are never
split; box-face strips follow the plane's edge subdivision, keeping
the closed surface exactly conformal). gmsh here is geometry-file
preparation — the same role SolidWorks plays in the manual chain —
while ANSYS cuts every cell.

Sizing is never duplicated: both meshers resolve their numbers from one
source (`fluent_workflow.resolve_sizes` for the DXF route,
`cfd.section_geometry` for app configs — the same geometry `build_case`
meshes), with the preset's thickness cap converted to an explicit
prism-layer count through the geometric series. Optional refinements
(scoped wall sizing, boundary layers) degrade with the failure RECORDED
in the mesh provenance rather than silently.

**What the flip trades away, and why it is still the default:** the
gmsh route's whole point was an identical mesh under both solvers, and
that remains exactly one select away (Mesher: "Studio gmsh"), labeled
as the identical-mesh cross-check. The native route pays a real cell
tax — a 3D fill of a thin slab is isotropic where the extruded 2D mesh
was one cell thick — in exchange for an all-ANSYS chain with **no
Docker dependency at all**, which is what the workflow owner asked for.
The UI states that the native cell count is not the studio preset's.

**Measured (2026 R1, the coarse two-element calibration section, 4
processors).** The full native chain: STL 26.5k facets (self-checked
conformal), import 0.09 min with every zone named and typed, surface
mesh 0.18 min, share topology 0.06 min, volume fill 0.61 min —
**651,119 cells**, min orthogonal quality 0.09, ~3 minutes of meshing
wall time end to end, written and read back into the solver with the
zones arriving exactly as the setup expects (11/11 recipe steps
applied, none failed). That is ~31× the gmsh coarse preset's 21,005
cells — the honest quasi-2D tax: the wing band carries ~dz/wall ≈ 14
spanwise cells that the extruded mesh never paid for.

**The proximity-scoping lesson that got it there** (two full meshing
runs of evidence): with the workflow's usual face-scoped proximity, the
two symmetry planes of a thin slab SEE EACH OTHER across dz, and
CellsPerGap=8 blankets the entire 35 m² domain at dz/8 ≈ 4.4 mm —
**17.8M cells and ~35 minutes of meshing**, identical with curvature
adaptation on or off (17.782M vs 17.846M — the second run falsified
the initial curvature theory). Scoping proximity to EDGES keeps the
protection the knob exists for — hole-boundary edges still resolve the
slot gap and the ride-height gap — while featureless plane interiors
stop demanding cells: same geometry, same knob values, 651k cells and
a 27× faster mesh. Hardwired in mesh_native, with the story in its
docstring.

The solver leg on the written native mesh: launch 17 s, zones arrive
typed (velocity-inlet/pressure-outlet/symmetry by name; wings, top,
ground as walls), the studio recipe applies 11/11, and a 300-iteration
sanity solve runs with physical-scale coefficients — which also settles
the units chain (the kernel's mm scaling stays internal; the written
mesh reads back in meters, so coefficients, the flow-field export and
the app's views stay dimensionally right). 300 iterations is a
deliberate plumbing check, not a result: the history is mid-transient
(drift ~1.1) exactly as a loaded case should be at that depth. The
open follow-up is a converged native-vs-gmsh comparison at matched
drift (the in-app engine will do it — a fine-grade run on the 651k
native mesh is roughly an hour-class solve at 4–8 cores), to place the
native mesh's absolute levels against the equivalence campaign's
Fluent reference.

**ANSYS export for finished Fluent runs:** `write_case_data` now
honors absolute path stems, so the engine writes `case.cas.h5` +
`case.dat.h5` into the RUN directory (the old call landed them in the
transient session dir under `app_data/fluent/` — the result note
claimed "beside the run" and was wrong until now; regression-pinned).
A finished Fluent run offers **Export case for ANSYS**: copies
case+data+config plus a conventions README (downforce-positive,
chord-referenced, residual auto-stop disabled, slab depth) into
`exports/fluent_case_<stamp>/` with the same mkdir-claim collision
guard and reveal flow as the OpenFOAM case export.

**Animated flow view, same round:** particle **density** is a live
control (800–12,000; pool reallocated on the fly), and the **backdrop**
is selectable — velocity field (unchanged default), **Cp** on the
static views' diverging scale (the field rides along in the same
`/flowfield` payload and every level-of-detail window, so the two views
cannot disagree), or plain chrome. The persistent trail preset dropped
the third-party product name from its label and now fades its tail
(erase alpha 0.0035 → 0.006, base stroke alpha 0.5 → 0.4): streaklines
still linger, but a minute-old stroke no longer sits at near-full ink.

**First real use of the native engine surfaced four defects, all
fixed the same night.** A phantom "0 cells" rode two bugs stacked: the
meshing transcript parser missed 2026 R1's actual completion line
("N cells were created"), and the status line coerced the resulting
null to 0 — patterns fixed, a solver-side `mesh/size-info` fallback
added, and the UI no longer renders a null count. "Feels stagnant" was
real pacing, not perception: the first solve chunk was 250 iterations,
which on a 651k-cell native mesh at the serial default is an hour of
radio silence before the first chart point — the first chunk is now 60
iterations and big meshes (>400k cells) continue in 100-iteration
chunks. "Cancel isn't working" was also real: the flag was only polled
between stages/chunks, so a blocking native-mesh or long solve chunk
ignored it for many minutes — cancel now HARD-KILLS the live Fluent
session (the blocked gRPC call raises within seconds, the runner
converts the exception to 'cancelled' when the flag is set, and the
license is released; pinned by a blocked-solve kill test). And the
ANSYS case export gained its Export-tab home: **ANSYS Fluent case**
meshes natively at export time and writes slab.stl + native.msh.h5
(zones pre-typed) + a recipe README + config.json under
`exports/fluent_mesh_*` — the session's scratch swept, the license
held only while it meshes; the RANS tab's post-run export remains the
one carrying solved case+data.

## Adversarial attack round on the Fluent integration (2026-07-30)

**The whole uncommitted Fluent round was attacked before it could land.**
Twelve independent review dimensions (the new engine, the MCP scripts,
the server diff, the core diffs, the frontend diff, the new tests, the
docs, an end-to-end physics/units trace, a hunt for code still assuming
OpenFOAM is the only engine, and two latent-bug sweeps of older core)
produced 72 raw findings; triage merged duplicates to 52 and per-file
adversarial verification confirmed 51 — exactly one claim did not
survive scrutiny. All 51 were fixed the same day. The ones that changed
behavior, and the decisions inside them:

**The critical one: a licensed Fluent session leaked on every raise
after launch.** The runner released the session only through the handle
its inner pipeline *returned* — any exception between license checkout
and that return (a corrupt bridge mesh, a diverged AMG solve, a gRPC
fault) marked the job failed and left a headless Fluent process holding
an ANSYS seat until the next job or server exit, despite the module
contract saying the license is held only while a job runs. Cleanup now
fires on the raise path itself, with the caller's idempotent shutdown
kept as backstop, and the two missing failure-mode tests (mesher raise,
uncancelled solve raise) pin license release alongside job failure.

**Fluent's early stop now clears the same bar as OpenFOAM's.** The
chunk loop had been stopping on Cl drift alone, as early as 1,100
iterations — no Cd gate, windows a third of the doctrine's size, no
persistence. The review demonstrated a slow climb plus a mesh-scale
oscillation that the Fluent stop would call converged ~4.7 % below the
settled value while the OpenFOAM stop correctly kept solving. The
option of leaving the reference engine looser than the screening engine
was rejected as indefensible; the stop now requires the Cl *and* Cd
drift bars, three full post-transient windows, and the criterion
holding on two consecutive chunk boundaries, and the result carries
`cd_drift` so the verdict can be audited.

**All in-process Fluent activity is now mutually exclusive by
construction.** The mesh export's refuse-if-verifying guard was
one-directional and check-then-act: a verify starting mid-export would
`exit()` the export's live Fluent Meshing session (both engines share
the MCP module's globals). An exclusive claim now lives beside the job
registry — the export takes it, both start paths and the queue respect
it — and the MCP layer itself gained a session-lifecycle lock that
*refuses* to kill a live session it does not own rather than assuming
the previous owner is gone. The orphan sweep also learned to reap
wedged gmsh-bridge containers (owner pid rides in the container name;
a live owner, this process included, is always left alone).

**Studio conventions now pin inlet turbulence on both engines.** The
trace found OpenFOAM solving at 1 % inlet intensity while Fluent ran
its 5 % default — a silent cross-engine inconsistency in exactly the
comparison the identical-mesh route exists for. Under `studio`
conventions Fluent now mirrors the OpenFOAM inlet (1 %, viscosity
ratio 10); `walkthrough` keeps Fluent's defaults, because reproducing the
manual walkthrough is that mode's entire purpose.

**No wall-clock policing of individual chunks.** A wedged Fluent
session used to leave a job "running" forever. The rejected fix was a
per-chunk timeout — a 651k-cell native mesh legitimately spends an hour
per chunk, and any bar tight enough to catch wedges would kill honest
solves. Instead: cancel already hard-kills the live session (and after
the leak fix, the resulting raise releases everything), plus a 12-hour
absolute backstop checked at chunk boundaries with an honest failure
message.

Other confirmed-and-fixed findings worth one line each: `run_case`
session reuse concatenated the previous case's force history into the
new run's verdicts (histories now purged on reuse); DXF spline
flattening used a drawing-unit tolerance that faceted meter-unit
profiles (now a 25 µm physical sagitta, unit-invariant); the flow-field
window accepted aspect ratios that allocated multi-GB grids under the
render lock (pixel budget now bounded); the flat-file export save had
the same claim-by-probe race its sibling directory exports were already
hardened against (O_EXCL claim now); the registry test was running the
real Docker orphan sweep (it could force-remove a genuine container);
and the wake-shadow knife-edge label that the docs promised was
computed, exported, and never rendered — it renders now.

**User-facing copy was brought level with the two-engine reality in
the same round.** Every tooltip, dialog, status line, and exported
report that still described the RANS tab as OpenFOAM-only was reworded;
repo-internal citations (`DECISIONS.md`, `docs/calibration/...`) were
removed from copy a student running the packaged app cannot follow —
the candid substance stays, the dead reference goes. The validation
suite grew by roughly forty regression checks across the round — every
fixed behavior is pinned by a test that fails on the pre-fix code —
and stands at 747 checks across 20 suites.

## The 2D flip: the manual ANSYS walkthrough becomes the Fluent default (2026-07-30)

**The slab default was measured and found indefensible.** The in-app
Fluent engine's native route meshes a thin 3D extrusion of the section
— Fluent Meshing's watertight workflow on a studio STL slab — and on
the single-element bring-up case that produced **3.35 million cells for
a problem with no third dimension**. A 3D volume mesher cannot know the
depth is fake: it grades cells in z exactly as carefully as in x and y,
so the slab carries on the order of 14–50 spanwise divisions of pure
redundancy, every one of them solved every iteration. The documented
manual ANSYS workflow — a true-2D Workbench chain run by hand for every
design — meshes the same section in **119,410 cells**, and the
automated replica of that chain (spike-verified on this machine, ANSYS
2026 R1) completes the whole SpaceClaim → Workbench → Mechanical
meshing pass in about 5.5 minutes. A 28× cell count for zero physics is
not a default; it is a bug with a license fee.

**The decision: replicate the documented manual 2D workflow faithfully,
as the default, with every meshing step done by ANSYS's own tools.** The
new chain writes a DXF of the profile(s) plus the domain rectangle the
walkthrough draws by hand (front boundary 3 profile-lengths ahead,
outlet 7 behind, ceiling 3 stack-heights above the ground plane, ground
at y = 0), imports it
into a Workbench "Fluid Flow (Fluent)" system pinned to 2D analysis,
fills the fluid region in SpaceClaim (profile interiors stay unfilled),
and meshes in Mechanical with the walkthrough's exact settings: named
selections `inlet`/`outlet`/`upper_bound`/`ground`/`profile`/`fluid`,
0.1 mm edge sizing on the profile edges, and inflation from the profile
boundary by the first-layer-thickness method — first height 1 mm,
maximum 10 layers, growth 1.2. Fluent then runs 2D double precision
with the walkthrough's boundary conditions (velocity inlet at the configured
speed, 0 Pa pressure outlet, no-slip stationary profile, zero-shear
ceiling, ground moving in x at the inlet speed), reference values set
explicitly from the inlet with the 2D defaults of area 1 m² and length
1 m so they are deterministic, `lift_coef`/`drag_coef` report
definitions on the profile zone with Fluent's default force vectors,
hybrid initialization, 500 iterations, residual criteria left at
Fluent's defaults. That is the walkthrough, number for number.

**The batch chain is quirk-hardened, and the guard style is
distrust-by-default.** Automating Workbench and SpaceClaim headless
surfaced a dozen behaviors that would silently corrupt a naive script:
the 2D analysis type must be pinned on the geometry properties *before*
the file is attached or the import is 3D; a headless DXF import lands
its curves on a datum plane rather than the root part's curve list;
filling the region yields the fluid face *and* one face per profile
island, which must be deleted by area ranking; the named-selection
creator ignores the requested name (everything is born "Group1" and
renamed after, checking a rename call that reports failure by return
value instead of raising); edges are classified into zones by measured
vertex coordinates against the domain bounding box, with closed-spline
profile edges recognized by their zero-vertex signature; and the mesh
lands at a fixed project-relative path because no export API exists.
The governing quirk shaped the whole error-handling design: **both
batch runners exit 0 even when the script inside them failed**, and a
failed Mechanical pass still emits a default mesh file that exists and
parses. So no stage is ever judged by exit code or file presence —
every stage writes its own result JSON, and the mesh is accepted only
after the `.msh` itself is parsed and found to declare 2D, a sane cell
count, and all six zone names. Timeouts kill the stage's process tree,
and each stage has its own budget (SpaceClaim 300 s, Workbench 900 s).

**Walkthrough numbers are the defaults, studio improvements are
options.** The walkthrough's numbers above are what sizing `default`
and conventions `default` produce, because parity with the manual
workflow is the point: a student can reproduce any in-app result by
hand, step for step. The studio's improvements are opt-in, never
silently applied. Sizing
`studio-yplus1` replaces the 1 mm first layer with the y+ ≈ 1 height
the app already computes for OpenFOAM, chord-proportional edge sizing
(0.2 % of chord), and the fine preset's layer schedule. Conventions
`studio` pins SST k-ω with the studio inlet turbulence (1 % intensity,
viscosity ratio 10), disables Fluent's residual auto-stop in favor of
the three-window force-drift verdict (with the run's iteration count as
a cap rather than a script), and presents downforce-positive. Under
`default` conventions the run goes exactly the requested iterations unless
Fluent's own residual criteria end it early — and when that happens
the result says so (`residual_stop`) instead of letting a
default-criteria stop masquerade as a force-converged one.

**One conversion bridges the two worlds honestly.** The `default`
convention reports Fluent's raw coefficients against the 2D reference area
of 1 m² per meter of depth, lift-positive-up; the studio is
chord-referenced and downforce-positive. Both are shown:
`cl_chord = -cl_raw / chord` (and `cd_chord = cd_raw / chord`), with a
note stating the reference so neither number can be mistaken for the
other. The `k_g` suggestion is always computed from the chord-referenced
downforce-positive value, whichever conventions ran the solve.

**The UI splits.** The 2D chain gets its own **Fluent 2D** tab beside
RANS verify — sizing, iterations, cores, conventions, live convergence,
flow view, `k_g` apply — and the RANS verify tab returns to
OpenFOAM-only, dropping its engine and mesher selects. Two engines
sharing one tab's controls made every label a conditional; two tabs
make each one honest. The Export tab's ANSYS card becomes the 2D
bundle: DXF + the ANSYS-meshed `FFF.msh` + a README carrying the
meshing and solve recipe.

**The revert path stays live.** The 3D slab engine was not deleted or
degraded: `fluent_run.py`, its MCP meshing tools, and its tests are all
intact, and `cfd_run.start(engine="fluent")` still dispatches to it —
it just has no UI control anymore. Restoring it to the interface is a
frontend-only change (re-add the engine/mesher selects the RANS tab
carried before this round and pass `engine="fluent"` through the
existing start call); nothing server- or core-side needs to move. It
remains the right tool if a genuinely 3D question ever appears, and the
identical-mesh gmsh cross-check route rides with it.

**The live bring-up bought three more rules the code now enforces.**
An end-to-end run on the real installs failed twice before it
succeeded, and each failure became a fence. First: both ANSYS script
hosts run IronPython 2.7, where a single non-ASCII byte in a generated
script is a compile error that `SendCommand` swallows silently — the
run then dies minutes later on the default-mesh guard with no visible
cause (two em dashes in template comments cost a full meshing round
each). The template filler now refuses to emit any non-ASCII script,
and the suite pins all three templates pure ASCII. Second: v261's 2D
mesher crashes outright ("software execution error") when the
walkthrough's inflation stack is asked to grow from multi-element
profile loops — opposing 26 mm fronts in a 5.25 mm slot gap. The chain now caps the
layer stack against the clearances it actually faces (0.4 x the slot
gap per side, matching the studio's own boundary-layer share; 0.9 x
the ground clearance, which keeps the spike-proven single-element
walkthrough stack uncapped), and when Mechanical still returns an
empty mesh it retries once without inflation and records the degradation in the
result — a usable mesh with an honest caveat instead of an opaque
failure. The no-inflation fallback is less of a loss than it sounds:
at 0.1 mm edge sizing the first cell centroid sits near y+ 2, finer
than the walkthrough's intended 1 mm first layer. Third: the domain ceiling
follows the walkthrough's measured reading — three stack heights above
the ground plane, ride height included — after the bbox-only reading
was caught tightening vertical confinement by exactly 3x the ride
height on the parity path. The verified end state: a two-element stack
meshes to ~52k true-2D cells in ~5 minutes and solves with live
convergence, honest verdicts, flow export and the solved case written
— against 3.35M cells and an unfinishable solve the slab default
produced the same morning.

## Section-level rule checks, and one flow view for both engines (2026-07-30)

**Only rules a 2D section can actually decide are implemented.** The
envelope already checked the box (length, height, ground clearance);
this round adds the two edge rules a section carries in its own
geometry — a leading-edge radius floor and a trailing-edge thickness
floor — and stops there. Deliberately left out, and named as such in
the UI: endplate and vertical edge radii (the vertical-edge figure in
T.7.1.4 applies to surfaces a section drawing does not contain), the
plan-view keep-outs and span limits, mount frangibility and the
stiffness load case. Every one of them is a real rule; none of them is
decidable from a section. Options considered: (a) a full rules
checklist with a user-entered number per rule — rejected, a green tick
on a limit the section cannot see is worse than no check at all,
because it reads as clearance; (b) check nothing and leave rules to
the entrant's own paperwork — rejected, the two edge limits *are*
section-computable and the optimizer was already free to design a
knife-edged nose; (c) check what the geometry decides, and print the
list of what it cannot (chosen).

**The leading-edge radius is measured by fitting the nose parabola —
and the first version of that fit was accurate against the wrong
yardstick.** Near the nose the surface follows `y² = 2rx`, so
`le_radius()` least-squares fits x against y over the first 2 % of
chord using both surfaces; the quadratic coefficient is 1/(2r). A
constant and a linear term absorb the offset between the frontmost
contour *point* and the true vertex (they differ by up to a panel, and
on a cambered section the vertex sits off the axis), and 1/(x+ε)²
weighting anchors the fit where the parabola is valid rather than at
the far end of the window where paneling artifacts live.
Finite-difference curvature was rejected outright: on a repaneled
spline it differentiates the panel spacing as much as the shape.

The instructive part is what the first implementation got wrong. It
carried a |y|³ term to model the square-root nose, which bought a
headline accuracy of 0.4–0.7 % against the analytic 4-digit NACA
radius `1.1019·t²` — and that number was meaningless, because a NACA
4-digit nose is precisely the shape the basis was built to fit. Over a
2 % window the |y|³ column is near-collinear with y² (measured
condition numbers 10⁷–10⁸), so on any *other* nose the quadratic
coefficient — the one that IS the curvature — was essentially
unconstrained, and `lstsq` reported nothing. The consequence was not
academic: sweeping panel counts from 60 to 200, sd7034 read 3.9 mm to
11.9 mm and s6063 swung 6.6×, so the pass/fail verdict against a 5 mm
floor alternated with a knob the user changes for solver resolution.
The single stability check in the suite passed throughout, because it
tested naca0012 alone.

Dropping the |y|³ column fixes it: panel-count spread over 45–200
panels a side now sits at ~1–9 % across the library (worst measured:
e603 at 9.3 %), at the cost of a systematic −5.8 % bias against the
analytic NACA radius, near-constant from 0006 to 0024. That trade was
taken deliberately. A stable measurement with a known bias is usable;
an unbiased one that moves 6× with an unrelated setting is not. The
bias direction is also the safe one — the fit under-reads, so it flags
compliant noses rather than passing sharp ones.

Two honesty mechanisms follow from those numbers. Legality is measured
at a **fixed** 120 panels a side (`RULE_CHECK_PANELS`) regardless of
what the solver or the optimizer's search is running, because whether
a design is legal cannot depend on a speed setting — the optimizer
searches 3–4 element stacks at 45–50 panels, where the nose fit
degrades to "unmeasured" on much of the library and preferentially on
the sharp noses the rule exists to catch. And a measurement within
10 % of the floor (`LE_RADIUS_BAND`, sized from the −5.8 % bias plus
the ~9 % scatter) is labelled **knife-edge**: the verdict still stands,
but the design is told the measurement cannot decide it, the same
doctrine the RANS attachment lines already carry. A nose that cannot
support the fit at all — too few points, a contour that folds back, a
recovered radius above half a chord — returns None and the verdict is
**unknown**, never a pass and never a violation. Measurement is on the
as-built contour (`effective_spec`, exactly as the manufacturing
report does), so what is judged is what will be cut.

**Which elements the radius applies to is exposed, not decided.** The
frontmost element is the default scope because the usual FSAE stack
puts one nose in front of a nose cone and shields the flaps behind it
— but whether a shielded flap leading edge is a "forward-facing edge
someone could contact" is an open reading of T.7.1.4, and it is not
this tool's reading to make. So `le_radius_scope` is a two-value
control ("Frontmost element" / "Every element") with the interpretation
spelled out in its tooltip, and an entrant who scrutineers the strict way
sets it to every element. What is *not* left to the user: which
element is frontmost. It is computed from the installed geometry
(smallest x extent), never assumed to be the main — a large negative
overlap or a nose-down stack angle can genuinely put a flap in front.

**The radius cost is an advisory, never a violation.** A required nose
radius above 5 % of an element's chord swallows the suction peak, so
that element's card carries a "nose cost" badge and the warning list
says the rule is being paid for out of section quality. The rule still
stands and the design still passes; the point is that the optimizer
gets blamed for a downforce number the rulebook chose. Alternative
considered: scaling it into the objective as a penalty — rejected,
that would trade legality against performance, which is exactly the
trade the hard-constraint decision refuses.

**T.7.1.5 gives no number, so the trailing-edge floor is the user's,
and the app says so.** The rules require that edges a person may touch
not be sharp, without a dimension. The check therefore ships with no
default value and its warning names the user as the source of the
limit ("the rules give no number for a non-forward-facing edge, so
this limit is yours"). Because the floor is measured on the as-built
contour, the manufacturing TE thickness is what has to meet it — the
two settings are wired to the same number rather than arguing about
it. Alternative considered: inventing a defensible default (3 mm, say)
— rejected, a number with no source in the rulebook would be quoted
back at scrutineering as if it had one.

**The box has two load cases, and the code now honors both.** T.7.3.1
measures aero limits with the wheels straight and no driver in the car
— the car's highest static ride height — while ground clearance is
worst case laden and dynamic. The top and the bottom of the same box
therefore belong to different conditions. `measure_ride_height_mm`
re-evaluates the height caps with the stack rigidly translated to the
entered unladen height (the stack's own height above its lowest point
is a property of the geometry, so the translation is exact), while the
clearance floor stays on the configured ride height; the verdict
reports `measured_at_ride_height_mm` and the warning says which height
the cap was judged at. Options considered: (a) one ride height for
every limit — rejected, that is the status quo and it silently checks
the height cap in the wrong load case; (b) sweep a ride-height range
and check the envelope across it — deferred, it multiplies the check
by a range the rules do not ask for; (c) two heights, one per limit
family (chosen).

**The built-in presets quote a draft, and the centre station's length
is deliberately blank.** Two envelopes ship read-only: an outboard/tip
station (625 mm length, 250 mm height per T.7.7.1.c, 5 mm nose radius)
and a centre station (500 mm height per T.7.7.1.b, same radius, **no
length**). The 625 mm is a rules constant rather than a guess — nothing
may sit more than 700 mm ahead of the front tires (T.7.5.a) and a
75 mm keep-out runs forward of the tire outer diameter in side
elevation (V.1.1.c), so 700 − 75 = 625 mm of chordwise room is left
whatever tire the car runs. The centre station has no such constant:
the 700 mm is measured from the fronts of the tires, so how much chord
the centre section gets depends on where that car's nose sits relative
to the front axle. Blank is the honest value, and each built-in
carries a note in the panel saying exactly that. The built-ins sit in
their own group in the preset select, cannot be overwritten or
deleted (the machine-level preset library stays the user's), and every
surface that quotes them — the group label, the panel note, the report's
"Rule source" row — states that the source is the FSAE 2027
**public-comment draft** (version 0.0, 21 July 2026), a document that
says on its face it is not valid for competition and whose numbers will
move before V1. Alternative considered: shipping the draft numbers as
plain defaults in the fields — rejected, an unlabelled number is
indistinguishable from a rule, and this one is not one yet.

**The animated flow view on the Fluent 2D tab was a frontend gap, and
the fix was one factory rather than a second copy.** Nothing in the
field pipeline was engine-specific: `/api/rans/{id}/flow` and its
`/flowfield` sibling read the finished job's case directory, and a
Fluent 2D run exports its solved field in the same form the OpenFOAM
runs write — so the Fluent tab was already serving static velocity and
Cp renders through the identical endpoints while its Animated button
simply did not exist. The panel behind it is not small: request
sequencing against stale responses, objectURL lifecycle, the
level-of-detail refetch, animation mount/teardown across tab switches,
theme re-ink, and the restored-project cache path. Options considered:
(a) copy the RANS panel under an `fl2d-` prefix — rejected, two copies
of that much stateful code diverge inside one round, and the LOD and
teardown paths are exactly where a divergence would leak a rAF loop or
megabytes of field arrays; (b) one shared panel instance reparented
between tabs — rejected, each tab owns a different job, image cache and
animation mount, and reparenting would make leaving a tab destroy the
other tab's state; (c) `makeFlowView(cfg)` (chosen) — one factory, one
instance per tab, differing only in DOM id prefix, owning tab, the
re-run verb in its "not saved with the project" message, and the
caption's wording. That last difference is the honest one: the
OpenFOAM caption says particles ride the solved field, the Fluent 2D
caption says they are traced through a **converged steady** field — a
path picture, not a time-accurate simulation of the flow developing.
Both captions state the playback slowdown, because real air crosses
the frame in a fraction of a second. Known limit: the animation reads
the live run's gridded velocity field, which is not saved into a
project file (the static renders are), so a restored project offers
the static views and asks for a re-run to animate.

## The ANSYS settings panel (2026-07-30)

**Every number the Fluent 2D tab's runs use is now a control, and the
recipe label still means what it says.** The Fluent 2D tab previously exposed
four knobs — sizing, iterations, solver cores, conventions — while the
rest of the chain lived as literals: the domain rectangle's 3 L ahead,
7 L behind and 3 H above were hardcoded in the DXF writer, and the
per-stage batch budgets (SpaceClaim 300 s, Workbench 900 s) were module
constants. That is stated plainly because it was a real gap: the
walkthrough audit table above lists `front_l` / `back_l` / `top_h` as
knobs, and on the true-2D route they were not knobs at all until this
round. The panel now carries the base mesh recipe (`default` — the
walkthrough's 0.1 mm edges, 1 mm first layer, 10 layers, growth 1.2 —
or `studio-yplus1`), a per-knob override beside each of those four
values (`edge_size_mm`, `first_layer_mm`, `n_layers`, `growth`), the
three domain proportions (`front_l` / `back_l` / `top_h`, multipliers on
the installed stack's streamwise extent and its height above the ground,
not on the reference chord), iteration count, solver cores, conventions
(`default` or `studio`), and the two stage budgets (`sc_budget_s`,
`wb_budget_s`). The same nine overrides ride on the Export tab's ANSYS
2D mesh bundle, which drives the identical `write_dxf_2d` + `run_chain`
path: a bundle meshed for a hand check has to be the mesh the tab would
build, or the check is of a different mesh.

**A blank override means "use the recipe", not "use zero".** Any
override left empty falls back to the sizing recipe's value for the
mesh knobs, or to the documented default for the domain proportions and
budgets — so the panel can be opened, read and closed without changing
a run. Overrides never relabel the recipe: a `default` run with a
0.3 mm edge size still reports sizing `default`, and the result carries
the values actually used rather than the recipe's nominal ones, because
a label that silently drifts from the numbers is exactly the provenance
failure the whole verdict doctrine exists to prevent. Carrying them is
not enough on its own: the panel unlocks the instant a run finishes, so
the card **states** its own resolved mesh, domain and budget numbers and
marks the ones that were set by hand, and it reports the layer stack the
mesher actually cut when the clearance cap or the no-inflation retry
moved it. Otherwise a coarse, tightly confined screening run and a
recipe-parity run render identically, which is the same provenance
failure one step further downstream. A result saved before this surface
existed carries no settings at all and says so rather than implying the
recipe's numbers. Ranges are
enforced in the core as well as at the API boundary (the core is
reachable from scripts that never touch the server), and every bound is
a physical or licensing one: growth strictly above 1.0 and at most 3.0,
layers capped at 100, iterations 50–20000, cores 1–32, and stage
budgets floored high enough (30 s SpaceClaim, 60 s Workbench) that an
honest stage cannot be killed mid-mesh by a typo.

**Presets are machine-level, exactly like the rule-envelope presets.**
A named settings preset is a habit of the workstation the licensed
ANSYS installation sits on, not a property of the wing being designed,
so it lives in the app's local data rather than inside a project file —
the same split, and for the same reason, as the rule presets: the
active values travel with the run, the library stays with the machine.

## Known limitations

Documented, not fixed. The custom-airfoil registry lives in server memory
and grows while the app is running (each distinct upload also invalidates
the screener cache — a re-screen costs seconds); at this scale a cap is not
worth the spec-invalidation complexity it would add. Optimization searches
run at coarsened paneling (45–60/side) for speed — candidate cards and Apply
re-analyze at full resolution, and "on target" is judged on the
full-resolution number. The k_g/eta calibration is now RANS
cross-referenced on the two-element baseline (validated h/c 0.086–0.114
and 0.429, optimistic below h/c 0.08, conservative mid-height — see
`docs/calibration/`), but that validity map is single-section,
single-config evidence from a fully-turbulent 2D truth model: it has not
been generalized across stacks or speeds, and tunnel data remains the
unarbitrated referee. Steady RANS at racing heights runs a genuine limit
cycle (±7 % at the 30 mm anchor), so every calibration number carries that
band. Two boundaries were probed in the 2026-07-24 round and remain open:
transition behavior is now measured at ONE point (γ-Reθ read 26 % above
fully-turbulent SST on the three-element case, so the shipped truth model
is conservative there — one configuration, not a trend), and the estimate's
conservatism on multi-element high-coupling stacks is recorded at two
points (implied k_g 0.37 and 0.43 against a curve giving 0.12–0.16)
without a refit, because a config-blind curve cannot be honestly fitted to
configuration-dependent error. The curve's shape is a two-element
artifact; treat estimates on three-plus-element stacks as lower bounds
until RANS says otherwise.
