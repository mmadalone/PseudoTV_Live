#!/usr/bin/env bash
# Sync custom logos from the live Kodi addon into the fork's workspace addon.
# Excludes addon.xml so the fork's custom one (different id/name/version) is preserved.
# Run before bumping a release whenever new logos have been added to the live addon.
set -euo pipefail

SRC="$HOME/.kodi/addons/resource.images.pseudotv.logos/"
DST="$(dirname "$(readlink -f "$0")")/resource.images.pseudotv.logos.madteevee/"

[[ -d "$SRC" ]] || { echo "missing source: $SRC" >&2; exit 1; }
[[ -d "$DST" ]] || { echo "missing dest: $DST" >&2; exit 1; }

rsync -av --exclude='addon.xml' "$SRC" "$DST"
