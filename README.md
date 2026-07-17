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
| Optional | XFOIL for reference polars · Docker Desktop or WSL2 for RANS verification |

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
  side-by-side comparison against the studio estimate, and a rendered
  flow-field view.
- **WSL** — **Export → OpenFOAM case** writes a complete, ready-to-run case
  under `exports/`; run it with `wsl -d Ubuntu -- bash run.sh`. One-time
  OpenFOAM installation is scripted in
  [`wsl/install_openfoam.sh`](wsl/install_openfoam.sh) (see
  [WSL setup](#openfoam-under-wsl-one-time-setup) below).

---

## What the app does

- **Stack configuration** — 1–4 elements from the bundled UIUC database
  (2,100+ sections), 4-digit NACA codes, or uploaded Selig `.dat` files.
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
| RANS verify button disabled | Start Docker Desktop and reopen the RANS verify tab. |
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
