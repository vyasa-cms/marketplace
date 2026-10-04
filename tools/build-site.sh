#!/usr/bin/env bash
# Assemble site/dist: index.json plus every package the listings name.
# Packages come from --packages <dir> (freshly built) and, for versions
# already published, from the `packages` GitHub release.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR=""
while [ $# -gt 0 ]; do case "$1" in --packages) PKG_DIR="$2"; shift 2;; *) echo "unknown $1" >&2; exit 2;; esac; done
DIST="$ROOT/site/dist"; rm -rf "$DIST"; mkdir -p "$DIST/packages"
python3 "$ROOT/tools/registry.py" build --out "$DIST/index.json"
python3 - "$DIST" "$PKG_DIR" <<'PY'
import json, sys, hashlib, pathlib, subprocess
dist, pkg_dir = pathlib.Path(sys.argv[1]), sys.argv[2]
for listing in json.loads((dist / "index.json").read_text())["listings"]:
    for v in listing["versions"]:
        name = v["url"].rsplit("/", 1)[1]; out = dist / "packages" / name
        local = pathlib.Path(pkg_dir) / name if pkg_dir else None
        if local and local.exists():
            out.write_bytes(local.read_bytes())
        else:
            subprocess.run(["gh", "release", "download", "packages", "-R", "vyasa-cms/marketplace",
                            "-p", name, "-O", str(out), "--clobber"], check=True)
        if hashlib.sha256(out.read_bytes()).hexdigest() != v["sha256"]:
            sys.exit(f"{name}: sha256 does not match its listing")
print("site/dist ready")
PY
