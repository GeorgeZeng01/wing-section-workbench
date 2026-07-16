#!/usr/bin/env bash
# Install OpenFOAM (openfoam.com / ESI build) on Ubuntu in WSL.
# Run as ROOT via Windows — no Linux password required:
#   wsl -d Ubuntu -u root -- bash /mnt/c/path/to/repo/wsl/install_openfoam.sh
#
# WSL grants Windows root access to the distro by design, so this works even
# when the normal user's sudo password is unknown. apt installs system-wide as
# root regardless, so nothing here depends on the interactive user.
set -euo pipefail

echo "== 1/4 add OpenFOAM apt repository =="
curl -fsSL https://dl.openfoam.com/add-debian-repo.sh | bash

echo "== 2/4 apt-get update =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -y

echo "== 3/4 pick newest openfoamNNNN-default =="
PKG=$(apt-cache search --names-only '^openfoam[0-9]+-default$' \
        | awk '{print $1}' | sort -V | tail -1)
if [ -z "${PKG:-}" ]; then
    echo "ERROR: no openfoamNNNN-default package found in the repo" >&2
    exit 1
fi
echo "installing: $PKG"
apt-get install -y "$PKG"

echo "== 4/4 locate the environment file =="
VER=$(echo "$PKG" | grep -oE '[0-9]+')
BASHRC="/usr/lib/openfoam/openfoam${VER}/etc/bashrc"
ls -l "$BASHRC"

echo
echo "OPENFOAM_INSTALLED=$PKG"
echo "OPENFOAM_BASHRC=$BASHRC"
echo "Done. Verify with:  wsl -d Ubuntu -- bash -lc 'source $BASHRC && simpleFoam -help | head -5'"
