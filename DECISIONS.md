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
