#!/usr/bin/env bash
# Package this checkout as a local Home Assistant add-on, for testing without
# publishing anything.
#
#   scripts/package-addon.sh         -> build/mesh_vnode/      (a folder)
#   scripts/package-addon.sh --zip   -> build/mesh_vnode.zip
#
# Put the folder in /addons on the Home Assistant machine, by whichever route
# you already have: the Samba add-on shares it as \\<ha>\addons (drag it in),
# or `scp -r build/mesh_vnode root@<ha>:/addons/`.
#
# Then: Settings > Add-ons > Add-on store > (three dots) > Check for updates.
# It appears under "Local add-ons". After a re-copy use (three dots) > Rebuild,
# since the version has not changed.
#
# Two things here are not cosmetic. `image:` is dropped, because that key is
# what tells the Supervisor to pull a published tag instead of building the
# Dockerfile next to it. And .venv/node_modules/data stay out: they would bloat
# the copy and shadow what the build produces.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

OUT="build/mesh_vnode"
rm -rf "$OUT" "$OUT.zip"
mkdir -p "$OUT"

tar -c \
    --exclude=.git --exclude=.venv --exclude=data --exclude=node_modules \
    --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
    --exclude=wip --exclude=.env --exclude=dist --exclude=build \
    --exclude=sync_to_pi.sh --exclude=scripts \
    --exclude=addon --exclude=.github --exclude=repository.yaml \
    -f - . | tar -x -C "$OUT" -f -

# config.yaml belongs on top, with `image:` dropped so the Supervisor builds the
# Dockerfile next to it instead of pulling the published tag. `url:` stays - it
# points at the real repository and renders as the add-on's documentation link.
grep -vE '^image:' addon/config.yaml > "$OUT/config.yaml"
cp addon/DOCS.md "$OUT/DOCS.md"
# The Supervisor reads these from the add-on folder, not from the image.
cp addon/icon.png addon/logo.png "$OUT/"

if [[ "${1:-}" == "--zip" ]]; then
    (cd build && zip -qr mesh_vnode.zip mesh_vnode)
    echo "build/mesh_vnode.zip - unzip it into /addons on the HA machine"
else
    echo "$OUT - copy this folder into /addons on the HA machine"
fi
