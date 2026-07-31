# Wing Section Studio

A desktop workbench for designing multi-element wing sections in ground
effect — configure a stack of airfoils, analyze it in milliseconds with a
purpose-built panel method, optimize it to a target downforce, verify the
result with an in-app 2D RANS run, and export CAD-ready geometry.

Built for front-wing development at student-formula scale, and applicable to
any low-Reynolds multi-element wing operating near the ground.

**[Application guide →](app/README.md)** · **[Design decisions →](DECISIONS.md)** ·
**[Illustrated workflow guide (PDF) →](docs/Wing%20Section%20Studio%20-%20Workflow%20Guide.pdf)**

---

## Setup

### Requirements

| Component | Requirement |
|---|---|
| OS | Windows 11 (Windows 10 works; the native window uses WebView2, bundled with Windows 11) |
| Python | 3.11, 64-bit ([python.org](https://www.python.org/downloads/) or `winget install Python.Python.3.11`) |
| Disk | ~2 GB for the Python environment and bundled airfoil database |
| Optional | XFOIL for reference polars · Docker Desktop or WSL2 for OpenFOAM RANS verification · licensed ANSYS 2024 R2+ including Fluent, SpaceClaim/Discovery and Workbench for the Fluent 2D tab (no Docker in that chain) |

### Install

From a terminal in the repository root:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

That is the whole installation. Everything else — the 2,100+ section airfoil
database, the viscous surrogate, the panel solver — ships with the
dependencies.

### Launch

Double-click **`Wing Section Studio.bat`** in the repository root. The app
opens in its own native window, runs entirely on your machine, and saves your
working state automatically between sessions.

Alternatively, run a plain server and open it in any browser:

```powershell
.venv\Scripts\python.exe -m uvicorn app.server:app --port 8642
```

then visit <http://127.0.0.1:8642> (interactive API documentation at
`/api/docs`).

### Verify the installation

```powershell
.venv\Scripts\python.exe app\tests\run_all.py
```

This runs the full validation suite (solver physics, model shape, geometry
pipeline, manufacturing prep, exports, CFD case generation, optimizer, and an
adversarial API round against a scratch server). Every check should pass.

---

## Optional components

The core workflow — analysis, optimization, screening, exports — needs
nothing beyond the install above. Two optional integrations extend it:

### XFOIL reference polars

The app's default viscous data comes from the bundled NeuralFoil surrogate.
To overlay classical XFOIL runs as a cross-check, download the Windows build
of XFOIL from the [official page](http://web.mit.edu/drela/Public/web/xfoil/)
and place `xfoil.exe` in the `xfoil/` folder. XFOIL is third-party GPL
software and is not redistributed with this repository.

### RANS verification (OpenFOAM)

The panel-method estimates screen and rank designs; a 2D RANS solve is the
truth check. Two ways to run one, both optional:

- **In-app (Docker)** — with [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  running, the **RANS verify** tab solves the current design in a local
  container (official `opencfd/openfoam-run` image, ~1 GB downloaded once,
  overridable via the `WSS_OPENFOAM_IMAGE` environment variable). Live
  convergence, automatic stopping when the force history flattens, a
  side-by-side comparison against the studio estimate, and a flow-field
  view — velocity and Cp renders plus a particle animation through the
  solved field, the same panel the Fluent 2D tab runs.
- **WSL** — **Export → OpenFOAM case** writes a complete, ready-to-run case
  under `exports/`; run it with `wsl -d Ubuntu -- bash run.sh`. One-time
  OpenFOAM installation is scripted in
  [`wsl/install_openfoam.sh`](wsl/install_openfoam.sh) (see
  [WSL setup](#openfoam-under-wsl-one-time-setup) below).

### ANSYS Fluent — reference of record

With a locally installed, licensed ANSYS installation (Fluent plus the
Workbench toolchain), the **Fluent 2D** tab replicates the documented
manual ANSYS 2D workflow end to end with ANSYS's own tools: the studio
writes a DXF of the section and the standard domain rectangle, a batch
SpaceClaim/Workbench chain fills the fluid region and meshes it in
Mechanical on the walkthrough's recipe (0.1 mm profile edge
sizing, 1 mm first-layer inflation, 10 layers) — with the layer stack
capped to the tightest slot or ground clearance it faces, and dropped
entirely, with the degradation recorded in the run, if Mechanical still
returns an empty mesh — and Fluent solves it as
a true 2D double-precision case with the walkthrough's boundary
conditions, report definitions, and 500-iteration budget. The
walkthrough's numbers are the `default` sizing and `default`
conventions, so a hand-run of the manual workflow reproduces the tab's
numbers; the studio's improvements — a resolved y+ ≈ 1 wall sizing, and
`studio` conventions (pinned SST k-ω with matched inlet turbulence, the
force-drift stopping doctrine, chord-referenced downforce-positive
coefficients) — are explicit options, and an **ANSYS settings** panel
exposes the individual mesh, domain, solver and stage-budget numbers
for anyone who needs to move one. Results always show both the raw
coefficients on Fluent's own reference and the chord-referenced
conversion, with the reference stated. The tab runs the same flow panel
as the OpenFOAM tab — velocity and Cp renders plus the particle animation,
which here traces the converged steady solution and says so, being a
path picture rather than a time-accurate simulation.
**Export → ANSYS 2D mesh** writes the same
DXF + ANSYS-meshed case-ready mesh + recipe README as a bundle under
`exports/`. (The previous 3D slab engine remains API-reachable as
`engine="fluent"` for reverting; it no longer appears in the UI.)

Separately,
[`scripts/fluent_mcp.py`](scripts/fluent_mcp.py) exposes a headless Fluent
session to MCP-capable clients (registration is a machine-local,
gitignored `.mcp.json`). Meshing is ANSYS-native by default: the studio
writes a watertight named-solid STL slab of the section and **Fluent
Meshing's watertight workflow** cuts the cells (`mesh_native` — no
Docker in the chain). The studio's own gmsh mesh remains selectable
(`mesher="gmsh"`, converted through `foamMeshToFluent` in the same
Docker image as the RANS tab) for cross-checking the two solvers on an
identical mesh. Either way it applies the standard 2D
external-aero recipe: velocity inlet, 0 Pa outlet, no-slip profile,
shear-free ceiling, moving ground, SST k-ω — with coefficients
chord-referenced and lift downforce-positive to match the studio's
conventions, plus the studio's three-window drift verdict on the force
history. A solver session holds an ANSYS license only between the
`launch` and `shutdown` tools. Two of its tools, `tui` and `scheme`,
are deliberate raw escape hatches: whatever string they are handed runs
verbatim inside the Fluent session, unescaped and unfiltered. They are
reachable only from an MCP client you connect yourself — neither the
web UI nor the HTTP API routes anything to them. Absolute coefficient
levels follow Fluent (the designer's 2026-07 judgment — see the
equivalence record in `DECISIONS.md`); the in-app OpenFOAM pipeline
remains the fast screening and ranking engine, whose orderings never
depended on absolute levels.

The whole manual chain (CAD → Workbench → Discovery → Mechanical mesh →
Fluent) is automated behind one `run_case` call in
[`scripts/fluent_workflow.py`](scripts/fluent_workflow.py) +
`fluent_mcp.py`: a walkthrough-style **DXF goes in unmodified** (profile
splines/polylines; if the domain rectangle is drawn it is used
verbatim), gets meshed ANSYS-natively by default (Fluent Meshing's
watertight workflow on a studio-built STL slab — no Docker), and solves
with the recipe above; pass `mesh.mesher="gmsh"` for the studio's gmsh
machinery when the identical-mesh cross-check against OpenFOAM is the
point. Every stage is configurable per
run — geometry source (`dxf_path` / `app_config` / `msh_path`), meshing
(`mode="resolved"` for the studio's y+ ≈ 1 wall doctrine or
`"walkthrough"` for the millimetre inflation; `edge_size_m`,
`first_layer_m`, `n_layers`, `growth`, domain multipliers
`front_l/back_l/top_h`, `ground_y`, `far_size_m`), physics
(`velocity_ms`, `rho`, `mu`, `chord_m`, `depth_m`, `moving_ground`,
`downforce_positive`), solver (`iterations`, `processors`,
`precision`), and `conventions` — `"studio"` (default: referee-grade
corrections, residual auto-stop disabled, drift verdict) or
`"walkthrough"` (literal manual-workflow parity, kept for measurement).
Defaults and the full spec schema live in `run_case`'s docstring; the
measured manual-vs-automated equivalence record lives in
`DECISIONS.md`.

---

## What the app does

- **Stack configuration** — 1–4 elements from the UIUC database that ships
  inside AeroSandbox (2,100+ sections, read at runtime), 4-digit NACA codes,
  or uploaded Selig `.dat` files.
  Slot geometry is set directly as gap/overlap in percent of chord; the
  placement solver achieves the requested values exactly.
- **Analysis** — a multi-element Hess–Smith panel method solves the exact
  inviscid flow in free air and in ground effect (method of images) in
  ~10–100 ms; a bounded correction model turns that into a realistic
  downforce estimate, and NeuralFoil budgets each element's loading against
  its own stall limit at its own Reynolds number.
- **Optimization** — global (differential evolution) or local refinement to
  a target downforce, over stack angle, deflections, slot geometry,
  optionally flap chords, airfoil selection from screened candidates, and
  per-element shape refinement (camber bumps + thickness scale). Returns the
  best design plus genuinely distinct on-target alternates.
- **Rule checks** — an optional geometric envelope checked on the installed
  wing: length, height above the road and ground clearance, plus per-element
  leading-edge radius and trailing-edge thickness floors measured on the
  as-built contour. Every limit is user-entered and saveable as a named
  preset; the optimizer treats the envelope as hard legality, refusing to
  start from or return a violating design. Built-in FSAE 2027 station presets
  are included as a starting point and are labelled throughout as coming from
  a **public-comment draft** that is not valid for competition. Only
  section-computable rules are checked — endplate edges, plan-view keep-outs,
  span limits and mount rules are not decidable from a 2D section, and the app
  says so rather than implying clearance.
- **Manufacturing prep** — opens every trailing edge to a buildable
  thickness, floors the aft thickness distribution, and flags unbuildable
  sections; every analysis and export then uses the as-built contours.
- **Operating maps** — sweep ride height or speed to see how the design
  behaves away from its design point.
- **Airfoil screener** — ranks the entire library at your Reynolds number in
  seconds.
- **Exports** — true-scale DXF (SolidWorks/Fusion/Onshape/NX-ready), CSV,
  SVG, per-element point files, Selig contours, a JSON manifest, and a
  complete OpenFOAM case.

The [application guide](app/README.md) covers each feature in depth; the
[prediction model notes](app/README.md#prediction-model) document exactly how
the estimates are computed and what their limits are.

## Command-line tools

The `scripts/` folder carries batch equivalents of the core workflows:

```powershell
# One airfoil, one polar (requires xfoil.exe in xfoil/)
.venv\Scripts\python.exe scripts\xfoil_runner.py s1223 --re 3e5 --alphas 0 16 1

# Compare candidate airfoils at a Reynolds number (XFOIL + NeuralFoil)
.venv\Scripts\python.exe scripts\compare_airfoils.py --airfoils s1223 e423 ch10sm --re 3e5

# Build and screen a multi-element stack from the command line
.venv\Scripts\python.exe scripts\stack_builder.py --name mywing --main s1223 `
    --flap s1223:0.35:28 --stack-aoa 2 --ride-height 0.15 --chord 0.35 --speed 15 --analyze
```

Airfoil specs anywhere: UIUC names (`s1223`, `e423`, …), `nacaXXXX`, or a
path to a Selig `.dat` file.

> Note: `stack_builder.py` prints inviscid screening numbers using the
> classical `Cl = 2Γ` shortcut, which understates inviscid ground effect;
> the app's panel method integrates surface pressure and is the number to
> trust in ground effect. See [DECISIONS.md](DECISIONS.md).

## Project layout

```
app/                    the application (server, core models, UI, tests)
scripts/                command-line tools (XFOIL runner, comparisons, stacks)
docs/                   illustrated workflow guide + its generator
wsl/                    OpenFOAM-under-WSL install and run helpers
xfoil/                  place user-provided XFOIL executables here (optional)
exports/                files exported from the app land here (generated)
results/                command-line tool outputs (generated)
app_data/               per-machine working state (generated)
```

Rebuild the illustrated guide (every figure is drawn from the live core, so
it always matches shipping behavior):

```powershell
.venv\Scripts\python.exe docs\build_guide.py
```

## OpenFOAM under WSL (one-time setup)

Only needed for the WSL path of RANS verification — the in-app Docker path
needs nothing but Docker Desktop.

The installation is scripted; replace `/mnt/c/path/to/repo` with this
repository's location as WSL sees it:

```powershell
wsl -d Ubuntu -u root -- bash /mnt/c/path/to/repo/wsl/install_openfoam.sh
```

> The script adds the official openfoam.com apt repository, which involves
> fetching and running the vendor's repository-setup script as root. Review
> it before running, or add the repository and signing key manually if your
> environment requires it.

Check the installation end-to-end (a small serial tutorial case):

```powershell
wsl -d Ubuntu -- bash /mnt/c/path/to/repo/wsl/cfd_smoketest.sh
```

Two WSL specifics the provided scripts already handle: OpenFOAM's
environment must be sourced *before* enabling `set -e`/`set -u` (its setup
scripts are incompatible with strict mode), and commands must run in the
same shell that sourced it (`wsl/foam.sh` does both).

## Troubleshooting

| Symptom | Resolution |
|---|---|
| `py -3.11` not found | Install Python 3.11 from python.org and re-open the terminal. |
| The window opens but stays on the loading screen | Close it and run the server manually (see [Launch](#launch)) to read the error. |
| "xfoil.exe not found" when requesting an XFOIL polar | Place `xfoil.exe` in `xfoil/` (see [Optional components](#optional-components)); the NeuralFoil engine works without it. |
| RANS verify button disabled | Start Docker Desktop and reopen the RANS verify tab (OpenFOAM runs in a local container). |
| Fluent 2D tab reports ANSYS unavailable | It needs a local licensed ANSYS installation with both SpaceClaim and Workbench present; the tab's status line names what is missing. |
| First RANS run is slow to start | The OpenFOAM image (~1 GB) downloads once; later runs start immediately. |
| A polar warns about a clamped Reynolds number | The element's Re is below the surrogate's training floor (10k); results there are extrapolations. |

## Conventions

- Design frame: upright, lift +y, main-element leading edge at the origin,
  main chord = 1. Installed frame: y-flipped (downforce down), ground at
  y = 0. Deflections are positive trailing-edge-down in the design frame.
- All polars and per-element coefficients are referenced to the element's
  own chord; stack downforce coefficients are referenced to the main chord.
- Reported `Cl` from RANS cases is downforce-positive (`liftDir (0 -1 0)`).

## Third-party components

This project builds on several third-party tools and datasets, each under
its own license:

- **XFOIL** (Mark Drela & Harold Youngren) — single-element viscous/inviscid
  airfoil analysis. GPL; downloaded separately, never redistributed here.
- **NeuralFoil** — neural-network surrogate of XFOIL for fast viscous polars.
- **AeroSandbox** — geometry utilities and the bundled UIUC airfoil database.
- **OpenFOAM** (openfoam.com / ESI) — 2D RANS verification runs.
- **gmsh** — mesh generation for the RANS cases.
- **NumPy, SciPy, Matplotlib, FastAPI, Uvicorn, ezdxf, pywebview, Pillow,
  ReportLab** — see `requirements.txt`.
