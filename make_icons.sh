#!/usr/bin/env bash
# Regenerates every icon in the repo from one master image.
#
#   ./make_icons.sh [master.png]     # default: docs/icon_large.png
#
# Nothing here is hand-edited: replace the master with a larger export and run
# this again. The master should be square-ish and at least 512px on its long
# side - everything below is a downscale from it, and upscaling a small master
# only produces a blurry icon in more places.
#
# What it writes, and who reads it:
#   addon/icon.png            Home Assistant add-on store, square tile
#   addon/logo.png            Home Assistant add-on store, wide header
#   webui/public/favicon.png  browser tab
#   webui/public/icon-*.png   manifest.webmanifest, phone home screen
#   webui/public/apple-touch-icon.png   iOS, which composites on an opaque
#                                       background - so this one gets BG.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

MASTER="${1:-docs/icon_large.png}"
BG="#0b1120"   # matches <meta name="theme-color"> in webui/index.html

[[ -f "$MASTER" ]] || { echo "no master image at $MASTER" >&2; exit 1; }
command -v magick >/dev/null || { echo "needs ImageMagick (magick)" >&2; exit 1; }

# Square variants: pad the master onto a transparent square rather than
# stretching it, so a wide wordmark keeps its proportions.
# The mark is scaled to ~86% of the canvas, leaving a margin so a maskable
# phone icon is not clipped at the corners when the launcher rounds it.
square() {   # square <size> <out>
    local inner=$(( $1 * 86 / 100 ))
    magick "$MASTER" -background none -gravity center -filter Lanczos \
        -resize "${inner}x${inner}" -extent "$1x$1" "$2"
}

square 256 addon/icon.png
square 512 webui/public/icon-512.png
square 192 webui/public/icon-192.png
square 48  webui/public/favicon.png

# iOS ignores transparency and composites on black, which hides a dark mark.
magick "$MASTER" -background "$BG" -gravity center -filter Lanczos \
    -resize "154x154" -extent "180x180" webui/public/apple-touch-icon.png

# The store header is wide, so this one keeps the master's aspect ratio.
magick "$MASTER" -background none -filter Lanczos -resize "250x100" addon/logo.png

echo "wrote:"
for f in addon/icon.png addon/logo.png webui/public/favicon.png \
         webui/public/icon-192.png webui/public/icon-512.png \
         webui/public/apple-touch-icon.png; do
    printf '  %-36s %s\n' "$f" "$(magick identify -format '%wx%h' "$f")"
done
