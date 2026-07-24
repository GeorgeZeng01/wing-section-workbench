# Wing Section Studio

Desktop workbench for designing multi-element wing sections in ground effect:
configure a stack, analyze it in milliseconds, optimize it to a target
downforce, and export CAD-ready geometry.

## Start

**Double-click `Wing Section Studio`** (the shortcut in this folder or on the
desktop). The app opens in its own native window — no browser, no terminal.
It runs entirely on your machine.

The window is a self-contained application: it opens immediately on a loading
screen while a local server starts on a private port inside the same process,
and shuts down when you close the window. Your configuration is saved
automatically — server-side, in `app_data/session.json`, so it survives
restarts regardless of which window or port served the UI — and restored the
next time you open the app. Exports are saved to the project's `exports/`
folder — a dialog shows the exact location with a **Show in folder** button.

<details>
<summary>Other ways to launch</summary>

- `Wing Section Studio.bat` — same native window, from a batch file.
- Headless server (for the REST API or scripting), then open the URL in any
  browser:
  ```powershell
  .venv\Scripts\python.exe -m uvicorn app.server:app --port 8642
  ```
  Interactive API docs live at <http://127.0.0.1:8642/api/docs>.

The native window uses the Windows WebView2 runtime (bundled with Windows 11).
If it is ever missing, the launcher falls back automatically to an Edge/Chrome
application window, then to the default browser.
</details>

## What it does

- **Stack configuration** — 1 to 4 elements. Any of the 2,174 bundled UIUC
  sections, 4-digit NACA codes, or uploaded Selig `.dat` files. Per-flap
  chord ratio, deflection, and slot geometry set directly as **gap** and
  **overlap** (percent of chord — the classical high-lift parameters);
  placement is solved so the achieved values equal the requested ones.
  Raw dx/dy offsets remain accepted through the API for scripting.
- **Operating point** — speed, ride height (to the stack's lowest point),
  chord, span, stack incidence, air properties, N<sub>crit</sub>.
- **Analysis** — a multi-element Hess–Smith panel method (constant sources +
  per-element vortex, one dense system across all elements) solves the exact
  inviscid flow in free air and in ground effect via mirror images, in
  ~10–100 ms. NeuralFoil supplies each element's isolated viscous polar at
  its own Reynolds number for load budgeting and profile-drag estimates.
  XFOIL runs are available as a slower reference overlay on any polar.
- **Manufacturing prep** (optional) — real parts cannot carry the knife-edge
  trailing edge that theoretical coordinates close to. Enable
  **Manufacturing** to open every element's TE to a minimum thickness in
  millimeters: **Thicken** (default) blends extra thickness in over the rear
  of the profile, keeping chord and camber — costs ~1 % downforce — and then
  **floors the aft thickness distribution at the TE gap**, so no station
  behind the end of the nose taper is ever thinner than the TE it was
  opened to (thin sail-like sections would otherwise keep an unbuildable
  waist ahead of the blunt TE — the floor runs from the first station that
  reaches the TE gap all the way back). A section whose untreated thickest
  point is not meaningfully above the requested TE is flagged **critically
  as a plate** — the treatment cannot make an airfoil out of it — and the
  optimizer/screener exclude such sections up front (candidates must be at
  least 1.5× the TE gap thick at the element's own chord);
  **Cut off** truncates the tip straight where
  the section last reaches the target thickness and rescales — for when a
  part or mold must literally be cut back (thin-tailed sections like the
  S1223 lose noticeably more; no floor is applied, and any remaining waist
  is measured and warned about). An optional **minimum buildable thickness**
  warns when an element's thickest point is below what you can make, floors
  the optimizer's airfoil candidates (at the smallest chord the search may
  pick when chords are free), and prefills the screener filter. Every
  element card shows its as-built **TE**, max thickness, and measured
  **waist** (thinnest aft station) in mm — all measured on the exact
  contours the solver and exports use; the analysis, polars, optimizer and
  all exports use the treated shapes, so what you cut is what was analyzed.
- **Optimization** — differential evolution plus Nelder–Mead refinement over
  stack angle, flap deflections, slot gap/overlap, optionally flap chords,
  optionally **each element's airfoil**, chosen from the library's
  strongest candidates at that element's own Reynolds number (the screener
  ranking, with the currently selected airfoil always in the running), and
  optionally **each element's shape**: three Hicks–Henne camber bumps
  (front / mid / aft loading) plus a thickness-envelope scale per element,
  applied on top of whatever airfoil the element uses — the search modifies
  the already-optimal section to push further, and every constraint still
  holds (manufacturing TE prep wraps the shaped section, the buildable
  minimum floors the thickness scale, the stall budget uses the shaped
  section's own polar). Shaped sections are first-class `shape:` specs, so
  polars, exports and saved projects all carry them; re-running the
  optimizer keeps refining the current shape instead of stacking changes.
  Effort levels trade speed for certainty: **Fast/Standard** stop as soon
  as the target is reached; **Thorough** searches the whole space with a
  wider population and refines from several diverse starts at full panel
  resolution — it takes minutes, and repeated runs converge to the same
  lowest-drag design (verified: 0.0 N drag spread across jittered starting
  points that scattered by ~9 N under early stopping). The
  candidate pool for airfoil selection is selectable: **best from
  library**, **library + my uploads** (every uploaded .dat guaranteed a
  slot), or **my uploads only** —
  optimize over just the sections you can actually build. The
  objective drives predicted downforce to a target while penalizing drag,
  slot geometry outside the healthy band, element overload, and
  intersections. Runs on a worker thread with live convergence charts and
  progress. A finished run returns the best design **plus up to three
  candidate designs** — on-target alternates chosen farthest-point-first
  from everything the search evaluated, so they are genuinely different
  set-ups (different slot geometry, incidence, or airfoils) rather than
  jitter around one optimum; each card shows its own analyzed downforce,
  drag, and L/D, and one click applies any of them to the configuration.
- **Airfoil screener** — ranks the entire bundled library at a chosen
  Reynolds number (CL_max, L/D, drag at a reference CL, thickness, camber,
  surrogate confidence) and assigns the pick to an element.
- **Export** — true-scale millimeter DXF (spline or polyline entities, one
  layer per element; opens directly as a sketch in SolidWorks, Fusion,
  Onshape, NX), CSV, and a 1:1 SVG drawing individually, or a ZIP bundle
  that additionally carries per-element XYZ point files, Selig `.dat`
  contours (installed positions and unit-chord profiles), and a JSON
  manifest. Plus a ready-to-run **OpenFOAM 2D RANS case** — mesh, boundary
  conditions, solver settings and a run script (see RANS handoff below).
  Both the installed frame (ground at y = 0) and the upright design frame.
  Every export is written to the project's `exports/` folder with a
  timestamped name, and a dialog shows exactly where it went with a
  **Show in folder** button (plus an optional browser download of a copy).
  With manufacturing prep enabled, all exports carry the as-built contours:
  blunt trailing edges get a straight base (a separate LINE in the spline
  DXF so the corners stay sharp), and the manifest records the achieved TE
  thickness per element.

## RANS handoff

The estimates above screen and rank; OpenFOAM decides. **Export → OpenFOAM
case** turns the current stack into a complete 2D RANS case under
`exports/cfd_case_<stamp>/` (this implements the repository roadmap's mesh
and case-template items):

- gmsh mesh of the installed section in meters — unstructured triangles
  graded from the wing outward, quad boundary layers on the airfoil walls
  (each element's stack capped by the clearances it actually faces) and a
  refinement box across every slot throat so the jet keeps at least eight
  cells. The ground carries no layer stack — gmsh can only terminate one
  mid-wall by staircasing it into slivers that break the solve — so the
  y+-adaptive wall treatment handles it. The first wall layer targets
  y+ ≈ 1 at the configured Reynolds number; the solved `yPlus` field
  beside each written time step is the measured value. Three presets on
  the two-element baseline: coarse ≈ 21k, medium ≈ 46k, fine ≈ 95k cells
  (a three-element stack runs higher).
- `simpleFoam` + k-ω SST with a ground plane **moving at the freestream
  speed** (the wing-fixed frame of a car), y+-adaptive wall treatment, and
  a `forceCoeffs` function object with `liftDir (0 -1 0)` — reported Cl is
  downforce-positive, referenced to the main chord.

Run it from Windows (requires the repository's one-time OpenFOAM/WSL
setup):

```powershell
wsl -d Ubuntu -- bash run.sh
```

The script converts the mesh, fixes patch types, runs `checkMesh` and
`simpleFoam`, and writes the final coefficients to `results.txt` — with the
Cl drift across the trailing iterations and a `NOT CONVERGED` banner when
the history is still trending. Expect a loaded high-lift case to use its
whole iteration budget: the `residualControl` thresholds are a backstop
that does not fire on this class of case. Sectional downforce per unit span
is `L' = Cl · ½ρU²c`; compare it with the studio's estimate and recalibrate
the `k_g` / viscous-efficiency knobs against it. Each case's `README.txt`
documents its exact numbers and conventions, including the domain size and
the fully-turbulent turbulence treatment.

### In-app verification (Docker)

The **RANS verify** tab runs the same case without leaving the app: it
builds the identical mesh and case, runs it in a local Docker container
(official ESI image, `opencfd/openfoam-run` — the same `run.sh`, so WSL and
Docker results are interchangeable), shows live solver convergence, and
finishes with the RANS coefficients side by side with the studio estimate —
including the pinned `k_g` that would make the estimate reproduce the RANS
sectional load, applied with one click (offered only from a run whose force
history actually converged). Each run also reports its measured y+ and a
per-element attachment verdict read from the wall shear field, so a high
load can be told apart from a separated one; a run still trending labels
its own mean a bound rather than a result. Entirely opt-in: the button only
enables when Docker Desktop is running, one run at a time, cancellable.
Working cases land under `app_data/rans/` (the newest few are kept) with
full logs and a ParaView-openable `case.foam`. The 2D case's drag is
profile-only, so it is compared against the stack's profile CD, not the
induced-drag-bearing total.

## Prediction model

The panel solution is exact for inviscid flow; its ground-effect gain grows
without bound as ride height shrinks, where real flows saturate. The reported
estimate bounds and chokes the realized share of that gain:

```
C_est  = eta_visc * [ C_free + G_real ]
G_real = cap * tanh( k_g * (C_ground - C_free) / cap ) * tanh( (h/c) / h_choke )
cap    = gain_cap_ratio * |C_free|
```

At ordinary ride heights the tanh terms are near-linear/near-1 and this
reduces to the familiar `eta_visc * [C_free + k_g * (C_ground - C_free)]`;
close to the ground the cap keeps the realized gain to a few times the
free-air load and the choke term takes it back down, so the estimate peaks
around h/c ≈ 0.06–0.1 and falls below that — the shape measured in published
ground-effect experiments (Zerihan & Zhang) — instead of diverging.

Model knobs, all editable in **Air properties & calibration** (or the
config/API): `eta_visc` (default 0.85), `k_g` (blank = the automatic
ride-height curve `0.85·tanh(1.4/0.85·h/c)`, a number pins it),
`gain_cap_ratio` (default 3.0) and `choke_h_c` (default 0.045, API-level).

Element loading is budgeted two ways: classically — each element's free-air
inviscid load (× `eta_visc`) against its isolated CL_max at the element's
own Reynolds number, warnings from 90 %, criticals from 110 % — and at the
realized ground-effect operating point (the same per-element CL the drag
lookup uses; these compose exactly to `C_est`). A realized operating point
past 2× the isolated stall limit flags the estimate as optimistic.

Drag has two parts: per-element profile drag from each section's polar at
its realized ground-effect operating CL — clamped to the pre-stall branch of
the polar, and each element reports when that clamp engaged
(`cd_lookup_capped`). Ground effect routinely pushes an element past its
isolated stall CL, so that clamp is the normal case rather than an edge
case: the stack profile drag and the L/D built on it are then floors and
ceilings respectively, reported as `drag_profile_is_lower_bound` and shown
with `≥`/`≤` in the UI. Plus induced drag from the reported downforce,
`CDi = CL²/(π·AR·e)·φ`, where `e` is the span-efficiency input and
`φ = (16h/b)²/(1+(16h/b)²)` is the classical ground-effect reduction —
induced drag dominates the total for a loaded front wing, and wings gain
efficiency as they approach the road. Interference and support drag are not
modeled.

All raw inviscid values are reported next to every estimate. Treat the
estimate as a screening and ranking number: confirm shortlisted designs with
RANS (the repository's OpenFOAM pipeline) or tunnel data, and recalibrate
the knobs against those results.

The `k_g` curve was calibrated on the **two-element** baseline and is blind
to how strongly a given stack couples to the road, so its error is
configuration-dependent rather than a fixed offset. On three-plus-element,
heavily loaded sections fine-mesh RANS has measured implied `k_g` of
0.37–0.43 against the curve's 0.12–0.16 — the estimate runs 35–65 %
conservative there, and says so in a warning. Below the choke onset
(h/c ≈ 0.08) it runs optimistic instead. Both bands are recorded with their
evidence in `docs/calibration/`; the honest fix in either direction is to
verify in RANS and pin `k_g` from a converged run, not to nudge the curve.

Validation: the panel method agrees with the exact Kármán–Trefftz
closed-form solution within ~0.5 %, with an independent linear-vorticity
formulation (AeroSandbox, force taken by pressure integration) within ~3 %
in free air and in ground effect, and with XFOIL's inviscid solution on
single elements when `xfoil.exe` is installed.
`app/tests/test_panel_validation.py` reruns all of it, and
`app/tests/test_model_and_data.py` pins the estimate's required shape
(bounded, force-reduction peak) and the induced-drag formula.

## Layout

```
app/
  desktop.py           native desktop launcher (WebView2 window + local server)
  server.py            FastAPI app + REST API (interactive docs at /api/docs)
  core/
    airfoils.py        airfoil resolution, UIUC library, uploads
    manufacturing.py   TE treatments (thicken/truncate) + derived specs
    geometry.py        stack construction, frames, slot metrics
    panel.py           multi-element panel method with ground effect
    viscous.py         NeuralFoil polars + XFOIL reference runs
    analysis.py        combined analysis and the corrected estimate
    optimizer.py       target-downforce search (background jobs)
    screener.py        library-wide airfoil ranking
    export.py          DXF / DAT / TXT / CSV / SVG / ZIP writers
    cfd.py             OpenFOAM 2D RANS case generation (gmsh mesh + case)
  static/              the web UI (no build step, no external dependencies)
  tests/               validation suite
```

## Conventions

- Design frame: upright, lift +y, main leading edge at the origin, main
  chord = 1. Installed frame: y-flipped (downforce down), ground at y = 0,
  stack translated so its lowest point sits at the ride height.
- Positive stack angle = more incidence = more downforce, the same sense as
  flap deflection. (The legacy `stack_builder.py` script uses the opposite
  rotation for its `--stack-aoa` flag.)
- Flap placement: scaled about its leading edge, rotated TE-down by the
  deflection, then positioned to the requested slot gap and overlap
  (equivalently via raw dx/dy offsets from the previous trailing edge, in
  main-chord fractions). Slot metrics are wing-relative: measured before
  the stack rotation, so they do not change with stack angle.
- All stack coefficients are referenced to the main chord; per-element CL
  values to the element's own chord.
- Millimeter exports are true scale; `.dat` stack exports are in main-chord
  units for meshing workflows.
