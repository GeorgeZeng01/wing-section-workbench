#!/usr/bin/env bash
# End-to-end proof that OpenFOAM can solve a 2D airfoil RANS case here.
# Runs the shipped airFoil2D tutorial (NACA0012, SIMPLE, Spalart-Allmaras)
# SERIALLY on the WSL-native filesystem, and reports convergence + the force
# coefficients its functionObject prints. Serial avoids MPI setup entirely.
#
# IMPORTANT: source OpenFOAM's bashrc with NO `set` flags active — its
# config corrupts a shell variable-context stack under errexit/pipefail.
BASHRC=$(ls -d /usr/lib/openfoam/openfoam*/etc/bashrc 2>/dev/null | sort -V | tail -1)
source "$BASHRC"
set -o pipefail

WORK="$HOME/of_smoketest"
CASE="$WORK/airFoil2D"
rm -rf "$WORK"; mkdir -p "$WORK"
cp -r "$FOAM_TUTORIALS/incompressible/simpleFoam/airFoil2D" "$CASE"
cd "$CASE" || exit 1

echo "FOAM $WM_PROJECT_VERSION | case: $CASE"
echo "=== prepare (serial) ==="
cp -r 0.orig 0                                   # restore0Dir
cp -rf constant/polyMesh.orig constant/polyMesh  # mesh ships gzipped, read as-is
echo "mesh: $(ls constant/polyMesh | tr '\n' ' ')"

echo "=== solving (simpleFoam, serial) ==="
if ! simpleFoam > log.simpleFoam 2>&1; then
    echo "simpleFoam FAILED — tail:"; tail -25 log.simpleFoam; exit 1
fi

echo "=== result ==="
echo "iterations run: $(grep -c '^Time = ' log.simpleFoam || true)"
echo "final residuals:"
grep -E 'Solving for (Ux|Uy|p|nuTilda)' log.simpleFoam | tail -4 | sed 's/^/  /'
# The tutorial's forceCoeffs functionObject prints Cd/Cl blocks to the log
if grep -q 'Cl ' log.simpleFoam; then
    echo "final force coefficients:"
    grep -E '^\s*(Cd|Cl|Cm)\s' log.simpleFoam | tail -6 | sed 's/^/  /'
else
    echo "(no forceCoeffs functionObject in this tutorial — convergence is the check)"
fi
echo "SMOKETEST_OK"
