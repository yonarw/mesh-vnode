#!/usr/bin/env bash
# Set the project version.
#
#   scripts/bump-version.sh 0.1.2
#
# `version` in pyproject.toml is the only place the number is written by hand.
# Two other files have to carry a literal copy and cannot read it at build time:
#
#   addon/config.yaml  the Home Assistant Supervisor clones the default branch
#                      and compares its `version` against the installed add-on.
#                      It never looks at the registry, so an unsynced value here
#                      means "no update available" no matter what was pushed.
#   uv.lock            records the project's own version; the Dockerfile runs
#                      `uv sync --frozen`, which refuses a stale lock.
#
# This script writes both, so they are generated files that happen to be
# committed. CI re-checks them - see .github/workflows/addon-image.yml.
set -euo pipefail

cd "$(dirname "$0")/.."

new=${1-}
if [[ ! $new =~ ^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$ ]]; then
  echo "usage: $0 <version>   e.g. $0 0.1.2" >&2
  exit 2
fi

old=$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)

sed -i "0,/^version = \".*\"$/s//version = \"$new\"/" pyproject.toml
sed -i "0,/^version: \".*\"$/s//version: \"$new\"/" addon/config.yaml
uv lock --quiet

echo "$old -> $new"
git --no-pager diff --stat -- pyproject.toml addon/config.yaml uv.lock
cat <<MSG

Next:
  git commit -am "Release v$new" && git push
The add-on image workflow builds on push to main, tags ghcr.io/...:$new
and creates the v$new git tag.
MSG
