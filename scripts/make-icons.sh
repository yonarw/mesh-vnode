#!/usr/bin/env bash
# Regenerates every icon in the repo from one master image.
#
#   scripts/make-icons.sh [master.png]     # default: docs/images/icon-master.png
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
#   docs/images/social-preview.png      GitHub link cards; uploaded by hand under
#                                       Settings > Social preview, not read from here.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

MASTER="${1:-docs/images/icon-master.png}"
BG="#0b1120"   # matches <meta name="theme-color"> in webui/index.html
TAGLINE="One Meshtastic® node, many apps, no missed messages"
HALO="#7b7b7b"  # outline around the social preview text

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

# GitHub's recommended 1280x640, transparent for the README. The text gets a
# light halo - its own shape grown by a disk - so it reads on light and dark
# themes. Fonts via fontconfig, so any sans works.
text() {   # text <font> <size> <halo px> <y> <string>: one layer, composited
    printf '%s\n' '(' -size 1280x640 xc:none -fill '#1f2937' \
        -font "$(fc-match -f '%{file}' "$1")" -pointsize "$2" -annotate "+0+$4" "$5" \
        '(' +clone -alpha extract -morphology Dilate "Disk:$3" -background "$HALO" -alpha shape ')' \
        +swap -composite ')' -composite
}
mapfile -t name < <(text 'sans:bold' 76 2 385 'mesh-vnode')
mapfile -t tag  < <(text 'sans' 36 1 495 "$TAGLINE")
magick -size 1280x640 xc:none -gravity north \
    \( "$MASTER" -filter Lanczos -resize "512x" \) -geometry +0+110 -composite +geometry \
    "${name[@]}" "${tag[@]}" docs/images/social-preview.png

echo "wrote:"
for f in addon/icon.png addon/logo.png webui/public/favicon.png \
         webui/public/icon-192.png webui/public/icon-512.png \
         webui/public/apple-touch-icon.png docs/images/social-preview.png; do
    printf '  %-36s %s\n' "$f" "$(magick identify -format '%wx%h' "$f")"
done
