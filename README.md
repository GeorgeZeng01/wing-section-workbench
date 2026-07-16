# FSAE Front Wing — 2D Airfoil & Multi-Element Stack Pipeline

Toolchain for designing a multi-element front wing section: XFOIL for the
individual elements, fast surrogates and an inviscid multi-element panel
method for screening, and OpenFOAM (2D RANS in WSL) as the truth model for
slot/stack optimization.

**Start here: [Wing Section Studio](app/README.md)** — the interactive design
app. A sendable, illustrated introduction to the complete workflow is in
[docs/Wing Section Studio - Workflow Guide.pdf](docs/Wing%20Section%20Studio%20-%20Workflow%20Guide.pdf). After the one-time setup below, launch it by
double-clicking `Wing Section Studio.bat`, which opens the app in its own
native window — no browser or terminal needed. It wraps stack construction, a
fast multi-element ground-effect panel method, NeuralFoil viscous checks,
target-downforce optimization, library-wide airfoil screening, and CAD-ready
DXF export in a single UI. Design decisions are recorded in
[DECISIONS.md](DECISIONS.md). The command-line scripts below remain available
for batch work.

> Note: the app's panel method computes ground-effect forces by pressure
> integration; the inviscid screening numbers printed by the older
> `stack_builder.py` (`Cl = 2Γ`) understate inviscid ground effect and the
> two will not match in ground effect. See DECISIONS.md.

**Why three tools?** XFOIL is strictly single-element — it cannot model the
slot interaction between a main plane and its flaps, which is the physics
that makes a stack work. And a front wing runs in ground effect, which
changes loads dramatically (the inviscid screen on the demo stack shows
>2x downforce at h = 0.15c vs free air). So: XFOIL picks the element
profiles, the panel method screens stack layouts, RANS decides.

## Setup

Requires Python 3.11 on Windows. From the repository root:

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Use the venv Python for everything: `.venv\Scripts\python.exe` (or activate
with `.venv\Scripts\Activate.ps1`).

**XFOIL (optional).** The XFOIL cross-check and the `scripts/xfoil_runner.py`
CLI call a local `xfoil.exe`. XFOIL is third-party GPL software and is not
redistributed here; download the Windows build (`xfoil.exe`, and optionally
`pplot.exe`/`pxplot.exe`) from the official page and place the executables in
`xfoil/`:

<http://web.mit.edu/drela/Public/web/xfoil/>

Everything except the XFOIL cross-check works without it — the app's default
viscous data comes from the bundled NeuralFoil surrogate.

**OpenFOAM (optional).** Only needed for the 2D RANS truth runs; see the
OpenFOAM section below.

## Layout

```
app/                    Wing Section Studio (see app/README.md)
xfoil/                  XFOIL 6.99 executables (user-provided; see Setup)
.venv/                  Python 3.11 venv (created during setup)
scripts/
  xfoil_runner.py       batch-drive xfoil.exe, parse polars (module + CLI)
  compare_airfoils.py   overlay polars for candidate airfoils (XFOIL + NeuralFoil)
  stack_builder.py      multi-element geometry: slots, ride height, exports,
                        inviscid ground-effect screening
wsl/                    OpenFOAM (WSL) helpers
  install_openfoam.sh   one-time install (run as root; adds the OpenFOAM repo)
  foam.sh               source newest OpenFOAM env, then exec a command
  cfd_smoketest.sh      run the airFoil2D tutorial end-to-end (install check)
docs/
  Wing Section Studio - Workflow Guide.pdf   the user & reference guide
  build_guide.py        regenerates the guide (figures drawn from app.core)
  guide_figures.py      the figure generators
exports/                files exported from the app land here (generated)
results/                polars, comparison plots, stack geometries (generated)
```

Rebuild the user guide (every figure is drawn from the live core, so it
tracks the shipping behavior):

```powershell
.venv\Scripts\python.exe docs\build_guide.py
```

Run the entire validation suite — including a scratch server for the API
checks — with:

```powershell
.venv\Scripts\python.exe app\tests\run_all.py
```

## Quick start

```powershell
# One airfoil, one polar (requires xfoil.exe in xfoil/)
.venv\Scripts\python.exe scripts\xfoil_runner.py s1223 --re 3e5 --alphas 0 16 1

# Screen candidates for a front-wing element (writes PNG + CSVs to results\)
.venv\Scripts\python.exe scripts\compare_airfoils.py --airfoils s1223 e423 ch10sm fx74modsm --re 3e5

# Build a 2-element stack and screen it in ground effect
.venv\Scripts\python.exe scripts\stack_builder.py --name mywing --main s1223 `
    --flap s1223:0.35:28 --stack-aoa 2 --ride-height 0.15 --chord 0.35 --speed 15 --analyze
```

Airfoil specs anywhere: UIUC names (`s1223`, `e423`, ... — 2,174 foils ship
inside AeroSandbox), `nacaXXXX`, or a path to a Selig `.dat`.

## Design workflow

1. **Element selection** (`compare_airfoils.py`) — compare candidates at the
   element's actual Reynolds number. XFOIL is the reference; NeuralFoil is a
   fast surrogate (fine inside optimization loops, check `analysis_confidence`).
2. **Stack layout** (`stack_builder.py`) — set chord ratios, deflections, and
   slot geometry (`DX:DY` in the flap spec; the computed gap/overlap are
   reported and written to `manifest.json`). Classic high-lift starting range:
   gap 1–2.5 %c, overlap 2–4 %c. `--analyze` gives inviscid trends with and
   without ground — **trends only**: no stall, no drag. Keep element loadings
   sane by checking each element's CL_max from step 1.
3. **Truth runs** (OpenFOAM 2D RANS, below) — the stack `.dat` exports in
   `results/stacks/<name>/installed/` plus `manifest.json` are the meshing
   input. Only RANS resolves slot merging/separation and real ground effect.

### FSAE numbers to keep in mind

- Re = V·c/ν with ν ≈ 1.5e-5: a 0.35 m main at 15 m/s → Re ≈ 3.5e5; a 35 %
  flap runs at only ~1.2e5 — check flap airfoils at *their* Re, not the main's.
- `--ncrit 9` is the standard "clean tunnel"; on-track turbulence arguably
  justifies Ncrit ≈ 4–7. Bracket both when it matters.
- Polar gaps near stall are XFOIL non-convergence; points simply drop out
  (noted on stderr). NeuralFoil fills those regions smoothly but with lower
  confidence — trust it less exactly where CL_max lives.
- Some sections resist convergence at low angles (e.g. `fx74modsm` over the
  −4°..0° region): sweep upward from 0° and add panels — `xfoil_runner.py
  fx74modsm --re 3e5 --alphas 0 16 1 --n-panels 240 --iter 100` converged
  17/17 where the default settings did not.

## OpenFOAM (WSL2 Ubuntu) — one-time install

The install runs as root through WSL, which does not require a Linux password
(`wsl -u root`); `apt` installs system-wide as root in any case. The steps are
scripted in [`wsl/install_openfoam.sh`](wsl/install_openfoam.sh). Replace
`/mnt/c/path/to/repo` with this repository's location as WSL sees it (Windows
drives are mounted under `/mnt`):

```powershell
wsl -d Ubuntu -u root -- bash /mnt/c/path/to/repo/wsl/install_openfoam.sh
```

> Security note: the install script adds the official openfoam.com apt
> repository, which involves fetching and running the vendor's
> `add-debian-repo.sh` as root. Review the script before running it, or add
> the repository and signing key manually if your environment requires it.

The script selects the newest `openfoamNNNN-default` package and prints the
path to its environment file.

Every OpenFOAM command must run in a shell that has first `source`d the
install's `etc/bashrc` (it sets `$FOAM_*` paths and the solver `PATH`).
`wsl/foam.sh` does that and auto-detects the newest install, so it keeps
working across version bumps:

```powershell
# run any OpenFOAM command:
wsl -d Ubuntu -- bash /mnt/c/path/to/repo/wsl/foam.sh simpleFoam -help
# full end-to-end install check (meshless serial airFoil2D solve):
wsl -d Ubuntu -- bash /mnt/c/path/to/repo/wsl/cfd_smoketest.sh
```

Two WSL specifics the scripts handle:

- **Source the environment with no `set -u`/`-e`/`pipefail` active** —
  OpenFOAM's `config.sh/setup` corrupts its shell variable-context stack under
  those flags (`pop_var_context: ... not a function context`). Enable
  strictness *after* sourcing.
- **Do not nest `bash -lc`** after sourcing — a fresh login shell drops the
  `$FOAM_*` environment. `foam.sh` sources and then `exec`s directly.
- Solver runs work as the normal WSL user; only the *install* needs root.

### Enabling the normal user's `sudo` (optional)

To use the normal user's `sudo`, set its password as root (WSL grants root
access without the old password). Run this in an **interactive** `wsl` window
so the new password can be entered:

```powershell
wsl -d Ubuntu -u root passwd <your-username>
```

Find `<your-username>` with `wsl -d Ubuntu -- whoami`.

## Roadmap

- [ ] gmsh-based 2D mesh generator around the exported stack (`pip install
      gmsh`, Python API) with a moving-ground boundary and y+ ≈ 1 wall layers
- [ ] OpenFOAM case template: `simpleFoam`, k-ω SST, moving ground + rotating
      wheels excluded (2D section first), automated Cl/Cd extraction
- [ ] Optimizer loop: NeuralFoil/panel pre-screen → RANS on the shortlist
      (scipy/optuna; AeroSandbox's CasADi optimizer for the smooth parts)
- [ ] Validation anchor: Zerihan & Zhang single-element ground-effect data,
      then the team's own tunnel/track data if available

## Conventions

- Design frame: upright, lift +y, main LE at origin, main chord = 1. Install
  frame: y-flipped, ground at y = 0. Deflections positive = TE-down in the
  design frame (= loading the wing). `stack_builder.py` writes both frames.
- All polars/coefficients are per-element-chord unless a file says otherwise;
  stack downforce coefficients are referenced to the **main** chord.

## Credits

This project builds on several third-party tools and datasets, each under its
own license:

- **XFOIL** (Mark Drela & Harold Youngren) — single-element viscous/inviscid
  airfoil analysis. GPL; downloaded separately (see Setup).
- **NeuralFoil** — neural-network surrogate of XFOIL for fast viscous polars.
- **AeroSandbox** — geometry utilities and the bundled UIUC airfoil database
  (~2,174 sections).
- **OpenFOAM** (openfoam.com / ESI) — 2D RANS truth runs.
- **NumPy, SciPy, Matplotlib, FastAPI, Uvicorn, ezdxf, pywebview, Pillow** —
  see `requirements.txt`.
