#!/usr/bin/env bash
# Source OpenFOAM into the current shell, then exec whatever is passed.
# Usage: wsl -d Ubuntu -- bash /mnt/c/path/to/repo/wsl/foam.sh <cmd...>
# Auto-detects the newest installed openfoamNNNN so it survives version bumps.
#
# NOTE: OpenFOAM's etc/bashrc is NOT written to tolerate `set -u` (it reads
# WM_PROJECT_DIR before defining it), so we deliberately do not enable it.
BASHRC=$(ls -d /usr/lib/openfoam/openfoam*/etc/bashrc 2>/dev/null | sort -V | tail -1)
if [ -z "$BASHRC" ]; then
    echo "ERROR: no OpenFOAM install found under /usr/lib/openfoam" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$BASHRC"
exec "$@"
