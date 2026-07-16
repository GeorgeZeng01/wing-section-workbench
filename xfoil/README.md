# XFOIL executables

XFOIL is third-party GPL software by Mark Drela and Harold Youngren and is not
redistributed with this project. To enable the XFOIL cross-check in the app and
the `scripts/xfoil_runner.py` CLI, download the Windows build and place the
executables in this directory:

- `xfoil.exe` (required for XFOIL runs)
- `pplot.exe`, `pxplot.exe` (optional polar plotters)

Official download page:

<http://web.mit.edu/drela/Public/web/xfoil/>

Everything except the XFOIL cross-check works without these — the app's default
viscous data comes from the bundled NeuralFoil surrogate.
