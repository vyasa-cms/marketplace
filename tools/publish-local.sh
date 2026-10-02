#!/usr/bin/env bash
# Publish this registry to a Vyasa install that hosts it.
#
#   tools/publish-local.sh --site /path/to/vyasa [--packages ./dist] [--no-verify]
#
# The site serves whatever sits in its `registry_dir`, so publishing is a
# copy and a build:
#
#   <site>/registry/packages/*.vyplugin   the artifacts
#   <site>/registry/index.json            the catalogue naming them
#
# Packages are copied *before* the index is written, and the published
# URLs are fetched afterwards to prove they resolve. Publishing an index
# that points at bytes which are not there yet is the one ordering
# mistake that breaks every install reading it.
#
# The official marketplace publishes from CI to a GitHub release instead;
# this script is for running your own marketplace on a Vyasa install.

set -euo pipefail

SITE=""
PACKAGES=""
VERIFY=1
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --site)      SITE="${2:?--site needs a path}"; shift 2 ;;
        --packages)  PACKAGES="${2:?--packages needs a path}"; shift 2 ;;
        --no-verify) VERIFY=0; shift ;;
        -h|--help)   sed -n '2,20p' "$0"; exit 0 ;;
        *)           echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

[ -n "$SITE" ] || { echo "pass --site /path/to/vyasa" >&2; exit 2; }
[ -d "$SITE" ] || { echo "no such directory: $SITE" >&2; exit 2; }

DEST="$SITE/registry"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
step() { printf '  %s\n' "$*"; }

say "Validating"
python3 "$ROOT/tools/registry.py" validate
step "structure ok"

mkdir -p "$DEST/packages"

if [ -n "$PACKAGES" ]; then
    say "Copying packages"
    count=0
    for f in "$PACKAGES"/*.vyplugin "$PACKAGES"/*.vytheme; do
        [ -e "$f" ] || continue
        # Copy to a temporary name then rename: a half-copied package
        # served to an install is a checksum failure at best.
        cp "$f" "$DEST/packages/.tmp.$(basename "$f")"
        mv "$DEST/packages/.tmp.$(basename "$f")" "$DEST/packages/$(basename "$f")"
        step "$(basename "$f")"
        count=$((count + 1))
    done
    [ "$count" -gt 0 ] || step "(nothing to copy)"
fi

say "Building the index"
# Written beside the destination and renamed into place, so a site
# fetching mid-publish never reads a half-written document.
python3 "$ROOT/tools/registry.py" build --out "$DEST/.index.json.tmp"
mv "$DEST/.index.json.tmp" "$DEST/index.json"
step "$DEST/index.json"

if [ "$VERIFY" -eq 1 ]; then
    say "Verifying the published URLs"
    # Fetches every package over https exactly as an install would, and
    # cross-checks it against its listing.
    python3 "$ROOT/tools/registry.py" validate --network
    step "every listed package resolves and matches its listing"
fi

say "Done"
step "the site serves this at <base>/registry/index.json"
