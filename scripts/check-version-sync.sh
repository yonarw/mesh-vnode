#!/usr/bin/env bash
# Verify the three files that carry the version agree, and print it.
#
#   scripts/check-version-sync.sh      -> prints e.g. 0.1.2, exit 0
#
# The version on stdout, complaints on stderr, so CI can do
# `version=$(scripts/check-version-sync.sh)` and still show why it failed.
#
# `version` in pyproject.toml is the source. The other two carry a literal copy
# because neither can read it at build time:
#
#   addon/config.yaml  the Home Assistant Supervisor clones the default branch
#                      and compares its `version` against the installed add-on.
#                      It never looks at the registry, so a stale value here
#                      means "no update available" however good the image is.
#   uv.lock            records the project's own version; the Dockerfile runs
#                      `uv sync --frozen`, which refuses a stale lock.
#
# scripts/bump-version.sh writes all three. This only ever reports.
set -euo pipefail

cd "$(dirname "$0")/.."

version=$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')
addon=$(sed -n 's/^version: "\(.*\)"$/\1/p' addon/config.yaml | head -1)

fail=0
if [[ $addon != "$version" ]]; then
    echo "addon/config.yaml says $addon, pyproject.toml says $version" >&2
    fail=1
fi
if ! uv lock --check >/dev/null 2>&1; then
    echo "uv.lock is stale for version $version" >&2
    fail=1
fi
if ((fail)); then
    echo "run: scripts/bump-version.sh $version" >&2
    exit 1
fi

echo "$version"
