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

## Known limitations

Documented, not fixed. The custom-airfoil registry lives in server memory
and grows while the app is running (each distinct upload also invalidates
the screener cache — a re-screen costs seconds); at this scale a cap is not
worth the spec-invalidation complexity it would add. Optimization searches
run at coarsened paneling (45–60/side) for speed — candidate cards and Apply
re-analyze at full resolution, and "on target" is judged on the
full-resolution number. NeuralFoil confidence and the k_g/eta knobs remain
screening-grade until recalibrated against RANS or tunnel data.
