#!/usr/bin/env python3
"""Build and validate the Vyasa marketplace index.

Standard library only, on purpose: a registry that distributes plugins
should not itself pull a dependency tree to do it. CI runs this with no
install step. Signing is the one exception and lives in ``sign.py``.

    tools/registry.py validate             # structure only, no network
    tools/registry.py validate --network   # also fetch and open every package
    tools/registry.py build                # listings/ -> index.json

The checks here deliberately mirror what the server refuses at install
time (crates/api/src/registry/install.rs). A contributor should learn
about a typo'd capability in CI, not from an operator's error log.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tomllib
import urllib.request

# Author signatures are verified with the same primitive the server uses;
# a runner without it must fail loudly, never report "does not verify".
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LISTINGS = ROOT / "listings"

SCHEMA_VERSION = 1

# Mirrors of the server's limits: crates/plugins/src/package.rs,
# crates/themes/src/package.rs, crates/api/src/registry/index.rs.
PLUGIN_MAX_BYTES = 10 * 1024 * 1024
THEME_MAX_BYTES = 25 * 1024 * 1024
INDEX_MAX_BYTES = 4 * 1024 * 1024
TOKEN_SCHEMA_VERSION = 1

NAME_RE = re.compile(r"^[a-z0-9-]{1,60}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
HEX128_RE = re.compile(r"^[0-9a-f]{128}$", re.IGNORECASE)
# [0-9], not \d: Python's \d also matches non-ASCII digits ("١", "５"),
# which int() may or may not read and the server's semver parser refuses.
SEMVER_RE = re.compile(
    r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
THEME_VERSION_RE = re.compile(r"^[0-9]+$")
U32_MAX = 2**32 - 1

# crates/plugins/src/capabilities.rs. A literal list, not a pattern:
# unknown capabilities are refused at install, so catch them here.
CAPABILITIES = {
    "db:read:posts",
    "db:read:options",
    "db:read:comments",
    "db:write:posts",
    "db:publish:posts",
    "db:write:meta",
    "kv:read",
    "kv:write",
    "viewer:read",
    "mail:send",
    "log:write",
    "event:emit",
    "ai:text",
    "html:page",
    "assets:script",
}

LISTING_FIELDS = {"kind", "name", "title", "summary", "author", "homepage", "author_key", "versions"}
VERSION_FIELDS = {
    "version",
    "url",
    "sha256",
    "signature",
    "min_host_api",
    "required_api",
    "capabilities",
    "released_at",
}


# Where every published package lives. Sites never trust the host — the
# signature is checked — but one address keeps mirrors simple.
PACKAGES_BASE = "https://marketplace.vyasa.site/packages/"


class Problems:
    """Every failure, collected, so one CI run reports them all."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, message: str) -> None:
        self.items.append(f"{where}: {message}")

    def __bool__(self) -> bool:
        return bool(self.items)


def capability_ok(cap: str) -> bool:
    if cap in CAPABILITIES:
        return True
    if cap.startswith("net:fetch:"):
        pattern = cap[len("net:fetch:") :]
        return bool(pattern) and "/" not in pattern
    return False


# Type mirrors of crates/api/src/registry/index.rs. The server reads the
# index with serde, so a field of the wrong JSON type does not "mostly
# work" -- it makes the whole index unreadable for every operator.
def is_str(value) -> bool:
    return isinstance(value, str)


def is_u32(value) -> bool:
    """serde's u32: a JSON integer in range. ``True`` is an int in Python
    and a bool in JSON; the server refuses it."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= U32_MAX


def listing_paths() -> list[Path]:
    return sorted(LISTINGS.rglob("*.json"))


def kind_of(path: Path) -> str | None:
    parent = path.parent.name
    return {"plugins": "plugin", "themes": "theme"}.get(parent)


def version_sort_key(kind: str, version: str):
    """Ascending, oldest first. Sort with ``reverse=True`` for newest first.

    Ascending rather than negated, because a prerelease sorts *below* its
    release (1.2.0-rc1 < 1.2.0) and a string identifier cannot be negated
    the way a number can. Themes version as integers, plugins as semver.
    """
    if kind == "theme":
        return (int(version),)
    core = version.partition("+")[0]
    core, _, pre = core.partition("-")
    numbers = tuple(int(n) for n in core.split("."))
    if not pre:
        # A release outranks every prerelease of the same version.
        return (numbers, 1, ())
    # Semver: numeric identifiers rank below alphanumeric ones.
    ids = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (numbers, 0, ids)


def validate_listing(path: Path, data: dict, problems: Problems) -> None:
    where = path.relative_to(ROOT).as_posix()

    kind = kind_of(path)
    if kind is None:
        problems.add(where, "must live in listings/plugins/ or listings/themes/")
        return
    if data.get("kind") != kind:
        problems.add(where, f'kind must be "{kind}" to match the directory')

    unknown = set(data) - LISTING_FIELDS
    if unknown:
        problems.add(where, f"unknown field(s): {', '.join(sorted(unknown))}")

    name = data.get("name", "")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.add(where, f"name {name!r} must match [a-z0-9-]{{1,60}}")
    elif path.stem != name:
        problems.add(where, f'file must be named "{name}.json" to match the listing')

    # `title`, `summary` and `author` are `String` with a default: absent
    # is fine, null or any other type makes the index unreadable.
    for field in ("title", "summary", "author"):
        if field in data and not is_str(data[field]):
            problems.add(where, f"{field} must be a string")

    homepage = data.get("homepage")
    if homepage is not None and not (is_str(homepage) and homepage.startswith("https://")):
        problems.add(where, "homepage must be an https string")
    author_key = data.get("author_key")
    if kind == "plugin" and not (is_str(author_key) and HEX64_RE.match(author_key)):
        problems.add(where, "plugin listings must name author_key: the author's 64-hex ed25519 public key")
    elif author_key is not None and not (is_str(author_key) and HEX64_RE.match(author_key)):
        problems.add(where, "author_key must be 64 hex characters")

    versions = data.get("versions")
    if not isinstance(versions, list) or not versions:
        problems.add(where, "needs at least one version")
        return

    seen: set[str] = set()
    for entry in versions:
        validate_version(where, kind, entry, seen, problems)


def validate_version(
    where: str, kind: str, entry: dict, seen: set[str], problems: Problems
) -> None:
    if not isinstance(entry, dict):
        problems.add(where, "each version must be an object")
        return

    unknown = set(entry) - VERSION_FIELDS
    if unknown:
        problems.add(where, f"unknown version field(s): {', '.join(sorted(unknown))}")

    # `version` is a `String` on the server: `5` is not `"5"`, and an
    # index carrying a bare number does not parse at all.
    raw_version = entry.get("version")
    version = raw_version if is_str(raw_version) else ""
    label = f"version {raw_version if raw_version is not None else '?'}"
    if not is_str(raw_version):
        problems.add(where, f"{label}: version must be a string")
    elif version in seen:
        problems.add(where, f"{label} is listed twice")
    seen.add(version)

    # Version grammar follows the manifest type: a theme manifest's
    # version is an integer, a plugin manifest's is semver. Keeping a
    # listing homogeneous also keeps the server's ordering unambiguous.
    if is_str(raw_version):
        if kind == "theme":
            if not THEME_VERSION_RE.match(version) or int(version) < 1:
                problems.add(where, f"{label}: theme versions are integers >= 1")
        elif not SEMVER_RE.match(version):
            problems.add(where, f"{label}: plugin versions are semver (1.2.0)")

    url = entry.get("url")
    if not is_str(url) or not url.startswith("https://"):
        problems.add(where, f"{label}: url must be https (the server refuses others)")
    elif not url.startswith(PACKAGES_BASE):
        problems.add(where, f"{label}: url must start with {PACKAGES_BASE}")

    sha = entry.get("sha256")
    if not is_str(sha) or not HEX64_RE.match(sha):
        problems.add(where, f"{label}: sha256 must be 64 hex characters")

    signature = entry.get("signature")
    if signature is not None and not (is_str(signature) and HEX128_RE.match(signature)):
        problems.add(where, f"{label}: signature must be 128 hex characters")

    released_at = entry.get("released_at")
    if released_at is not None and not is_str(released_at):
        problems.add(where, f"{label}: released_at must be a string")

    # `Option<u32>` on the server: null or absent, else an integer in
    # range -- never a bool, a negative, a float or a string.
    for field in ("min_host_api", "required_api"):
        value = entry.get(field)
        if value is not None and not is_u32(value):
            problems.add(where, f"{label}: {field} must be a non-negative integer")

    caps = entry.get("capabilities", [])
    if not isinstance(caps, list) or not all(is_str(c) for c in caps):
        problems.add(where, f"{label}: capabilities must be a list of strings")
        caps = [c for c in caps if is_str(c)] if isinstance(caps, list) else []
    if kind == "theme":
        if caps:
            problems.add(where, f"{label}: themes hold no capabilities")
        required_api = entry.get("required_api")
        if not is_u32(required_api) or required_api != TOKEN_SCHEMA_VERSION:
            problems.add(
                where, f"{label}: required_api must be {TOKEN_SCHEMA_VERSION}"
            )
    else:
        for cap in caps:
            if not capability_ok(cap):
                problems.add(where, f"{label}: unknown capability {cap!r}")
        if len(set(caps)) != len(caps):
            problems.add(where, f"{label}: duplicate capabilities")
        if not is_u32(entry.get("min_host_api")):
            problems.add(where, f"{label}: min_host_api is required for plugins")


def fetch(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "vyasa-registry-ci"})
    with urllib.request.urlopen(request, timeout=120) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError(f"package exceeds {limit} bytes")
    return body


BUNDLED_EXT = {
    "images": {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg"},
    "fonts": {"woff2", "woff", "ttf", "otf"},
}


def bundled_file_ok(path: str) -> bool:
    """Mirror of the server's rule for assets/images/* and assets/fonts/*:
    two or three lowercase segments of [a-z0-9._-], none starting with a
    dot, an extension the directory allows. A package the server would
    refuse must not validate here."""
    segs = path.split("/")
    if not 2 <= len(segs) <= 3 or segs[0] not in BUNDLED_EXT or len(path) > 120:
        return False
    if any(not seg or seg.startswith(".") for seg in segs):
        return False
    if not all(c.islower() or c.isdigit() or c in "._-" for seg in segs for c in seg):
        return False
    ext = segs[-1].rsplit(".", 1)[-1] if "." in segs[-1] else ""
    return ext in BUNDLED_EXT[segs[0]]


def check_package(where: str, kind: str, entry: dict, blob: bytes, problems: Problems) -> None:
    """Cross-check the artifact against what the listing promises.

    This is the check that matters. The listing is a claim; the package
    is the fact. The server compares them at install time and refuses a
    mismatch, so a listing that disagrees with its own artifact is a
    listing that will never install.
    """
    label = f"version {entry.get('version', '?')}"

    digest = hashlib.sha256(blob).hexdigest()
    if digest.lower() != str(entry.get("sha256", "")).lower():
        problems.add(where, f"{label}: sha256 is {digest}, listing says {entry.get('sha256')}")
        return

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        names = archive.namelist()
        manifest = tomllib.loads(archive.read("manifest.toml").decode("utf-8"))
    except KeyError:
        problems.add(where, f"{label}: package has no manifest.toml")
        return
    except Exception as exc:  # noqa: BLE001 - report, never crash the run
        problems.add(where, f"{label}: package is not readable: {exc}")
        return

    # Entry names, mirroring crates/themes/src/package.rs and
    # crates/plugins/src/package.rs. A package carrying a stray file is
    # refused at install, and CI that does not check this lets a listing
    # through that can never be installed -- which is exactly what
    # happened with a blog theme whose templates/ held a .html file.
    allowed_exact = ({"manifest.toml", "plugin.wasm", "signature.txt"}
                     if kind == "plugin" else
                     {"manifest.toml", "tokens.json", "layout.json",
                      "screenshot.png", "assets/theme.css", "assets/theme.js"})
    for n in names:
        if n.endswith("/"):
            continue
        if n in allowed_exact:
            continue
        if kind == "theme" and n.startswith("templates/") and n.lower().endswith(".tera"):
            continue
        if kind == "theme" and n.startswith("assets/") and bundled_file_ok(n[len("assets/"):]):
            continue
        problems.add(
            where,
            f'{label}: unexpected file "{n}" in the package. '
            f"The server refuses it at install.",
        )

    if str(manifest.get("name", "")) != entry["_name"]:
        problems.add(
            where,
            f'{label}: listed as "{entry["_name"]}" but the package is '
            f'"{manifest.get("name")}"',
        )

    if kind == "plugin":
        if "signature.txt" not in names:
            problems.add(
                where,
                f"{label}: unsigned package. The server refuses marketplace "
                f"plugins that carry no author signature.",
            )
        elif entry.get("_author_key"):
            # The server checks this at install, against the listing's key:
            # hex ed25519 over sha256(manifest.toml) || sha256(plugin.wasm).
            try:
                raw = archive.read("signature.txt").decode("utf-8")
                sig = bytes.fromhex("".join(ch for ch in raw if ch in "0123456789abcdefABCDEF"))
                msg = (hashlib.sha256(archive.read("manifest.toml")).digest()
                       + hashlib.sha256(archive.read("plugin.wasm")).digest())
                Ed25519PublicKey.from_public_bytes(bytes.fromhex(entry["_author_key"])).verify(sig, msg)
            except Exception:  # noqa: BLE001 - any failure is "does not verify"
                problems.add(
                    where,
                    f"{label}: the author signature does not verify under the listing's author_key",
                )
        if str(manifest.get("version", "")) != str(entry.get("version")):
            problems.add(
                where,
                f'{label}: package version is "{manifest.get("version")}"',
            )
        declared = sorted(str(c) for c in entry.get("capabilities", []))
        actual = sorted(str(c) for c in manifest.get("capabilities", []))
        if declared != actual:
            problems.add(
                where,
                f"{label}: capabilities disagree. Listing says {declared or '[]'}, "
                f"package requests {actual or '[]'}. The listing is what an "
                f"operator reads before installing, so it must be exact.",
            )
        if manifest.get("min_host_api", 0) != entry.get("min_host_api"):
            problems.add(
                where,
                f'{label}: min_host_api is {manifest.get("min_host_api", 0)} '
                f'in the package',
            )
    else:
        if int(manifest.get("version", 0)) != int(entry.get("version", 0)):
            problems.add(
                where, f'{label}: package version is {manifest.get("version")}'
            )
        if manifest.get("required_api") != entry.get("required_api"):
            problems.add(
                where,
                f'{label}: required_api is {manifest.get("required_api")} in the package',
            )


def load_all(problems: Problems) -> list[tuple[Path, dict]]:
    loaded = []
    for path in listing_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.add(path.relative_to(ROOT).as_posix(), f"invalid JSON: {exc}")
            continue
        if not isinstance(data, dict):
            problems.add(path.relative_to(ROOT).as_posix(), "must be a JSON object")
            continue
        loaded.append((path, data))
    return loaded


def validate_all(problems: Problems) -> list[tuple[Path, dict]]:
    """Every structural check ``validate`` runs, without the network."""
    loaded = load_all(problems)
    names: dict[tuple[str, str], Path] = {}
    for path, data in loaded:
        validate_listing(path, data, problems)
        key = (str(data.get("kind")), str(data.get("name")))
        if key in names:
            problems.add(
                path.relative_to(ROOT).as_posix(),
                f"duplicate listing; also defined in {names[key].name}",
            )
        names[key] = path
    return loaded


def report(problems: Problems) -> None:
    print(f"{len(problems.items)} problem(s):\n", file=sys.stderr)
    for item in problems.items:
        print(f"  - {item}", file=sys.stderr)


def cmd_validate(args: argparse.Namespace) -> int:
    problems = Problems()
    loaded = validate_all(problems)

    if args.network and not problems:
        for path, data in loaded:
            where = path.relative_to(ROOT).as_posix()
            kind = kind_of(path) or "plugin"
            limit = PLUGIN_MAX_BYTES if kind == "plugin" else THEME_MAX_BYTES
            for entry in data["versions"]:
                try:
                    blob = fetch(entry["url"], limit)
                except Exception as exc:  # noqa: BLE001
                    problems.add(where, f"version {entry['version']}: {exc}")
                    continue
                check_package(
                    where, kind,
                    {**entry, "_name": data["name"], "_author_key": data.get("author_key")},
                    blob, problems,
                )

    if problems:
        report(problems)
        return 1

    scope = "structure and packages" if args.network else "structure"
    print(f"OK: {len(loaded)} listing(s), {scope} valid.")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    # The full listing validation, every time. Validating only when a
    # file failed to parse let a listing with a bad type or an http url
    # into index.json whenever every file was at least valid JSON.
    problems = Problems()
    loaded = validate_all(problems)
    if problems:
        report(problems)
        print("index.json not written", file=sys.stderr)
        return 1

    listings = []
    for path, data in loaded:
        kind = kind_of(path) or "plugin"
        data["versions"] = sorted(
            data["versions"],
            key=lambda v: version_sort_key(kind, str(v["version"])),
            reverse=True,
        )
        listings.append(data)

    listings.sort(key=lambda d: (d["kind"], d["name"]))
    index = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "listings": listings,
    }
    body = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    if len(body.encode("utf-8")) > INDEX_MAX_BYTES:
        print("index exceeds the 4 MiB the server will read", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out} — {len(listings)} listing(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="check every listing")
    validate.add_argument(
        "--network",
        action="store_true",
        help="also download each package and cross-check it against its listing",
    )
    validate.set_defaults(func=cmd_validate)

    build = sub.add_parser("build", help="generate index.json from listings/")
    build.add_argument("--out", default=str(ROOT / "index.json"))
    build.set_defaults(func=cmd_build)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
