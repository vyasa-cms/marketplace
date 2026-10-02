#!/usr/bin/env python3
"""Registry signing: ed25519 over raw package bytes.

The one module here that needs a dependency:

    pip install cryptography

Key and signature formats match what the server expects
(crates/api/src/signing.rs): a trusted key is a hex-encoded 32-byte
ed25519 public key, and a signature is the hex-encoded 64-byte raw
signature over the package bytes exactly as downloaded.

    tools/sign.py keygen
    tools/sign.py sign storefront-1.2.0.vyplugin --key-env REGISTRY_KEY
    tools/sign.py verify storefront-1.2.0.vyplugin --signature <hex> --public <hex>
    tools/sign.py stamp listings/plugins/storefront.json 1.2.0 \
        --package storefront-1.2.0.vyplugin --key-env REGISTRY_KEY

``stamp`` is the one to reach for day to day: it reads the built package,
writes its real sha256 and signature into the listing, and never invents
either. A digest typed by hand is a digest that does not match.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover - the message is the whole point
    print(
        "this command needs `cryptography`:  pip install cryptography",
        file=sys.stderr,
    )
    raise SystemExit(2) from None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry  # noqa: E402 - the one source of truth for the cross-check

RAW = serialization.Encoding.Raw


def private_from_hex(value: str) -> Ed25519PrivateKey:
    raw = bytes.fromhex(value.strip())
    if len(raw) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(raw)}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def resolve_key(args: argparse.Namespace) -> Ed25519PrivateKey:
    """Prefer an environment variable; a key on a command line is a key in
    the shell history and in the CI log."""
    if args.key_env:
        value = os.environ.get(args.key_env)
        if not value:
            raise SystemExit(f"${args.key_env} is not set")
        return private_from_hex(value)
    if args.key:
        return private_from_hex(args.key)
    raise SystemExit("pass --key-env (preferred) or --key")


def cmd_keygen(_args: argparse.Namespace) -> int:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        RAW, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    ).hex()
    public = key.public_key().public_bytes(RAW, serialization.PublicFormat.Raw).hex()
    print("private (keep secret, put it in a CI secret):")
    print(f"  {private}\n")
    print("public (publish this; operators set it as registry_trusted_keys):")
    print(f"  {public}")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    key = resolve_key(args)
    blob = Path(args.package).read_bytes()
    print(key.sign(blob).hex())
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    blob = Path(args.package).read_bytes()
    public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(args.public))
    try:
        public.verify(bytes.fromhex(args.signature), blob)
    except Exception:  # noqa: BLE001 - any failure is the same answer
        print("SIGNATURE DOES NOT MATCH", file=sys.stderr)
        return 1
    print("signature OK")
    return 0


def cmd_stamp(args: argparse.Namespace) -> int:
    path = Path(args.listing)
    data = json.loads(path.read_text(encoding="utf-8"))
    blob = Path(args.package).read_bytes()

    entry = next(
        (v for v in data.get("versions", []) if str(v.get("version")) == args.version),
        None,
    )
    if entry is None:
        raise SystemExit(f"{path} has no version {args.version}")

    # Refuse to vouch for a package that is not what the listing says it
    # is: the name, version and capabilities an operator reads before
    # installing must be the ones inside the bytes being signed. The same
    # cross-check `registry.py validate --network` runs, against the
    # digest this stamp is about to write.
    kind = registry.kind_of(path.resolve()) or data.get("kind")
    if kind not in ("plugin", "theme"):
        raise SystemExit(f"{path}: cannot tell whether this is a plugin or a theme listing")
    digest = hashlib.sha256(blob).hexdigest()
    problems = registry.Problems()
    registry.check_package(
        str(path),
        kind,
        {**entry, "sha256": digest, "_name": data.get("name")},
        blob,
        problems,
    )
    if problems:
        for item in problems.items:
            print(f"  - {item}", file=sys.stderr)
        raise SystemExit(f"{path}: the package does not match the listing; nothing written")

    signing = bool(args.key or args.key_env)
    key = resolve_key(args) if signing else None
    entry["sha256"] = digest
    if key is not None:
        entry["signature"] = key.sign(blob).hex()
    else:
        # A signature over the previous bytes is not a signature over
        # these. Leaving it in place published a listing whose signature
        # the server refuses -- and said "signed" while doing it.
        entry.pop("signature", None)

    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    signed = "signed" if key is not None else "unsigned"
    print(f"{path}: {args.version} stamped ({signed}, {len(blob)} bytes)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="print a fresh keypair")
    keygen.set_defaults(func=cmd_keygen)

    def key_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--key-env", help="env var holding the hex private key")
        p.add_argument("--key", help="hex private key (avoid: it lands in history)")

    sign = sub.add_parser("sign", help="print the signature for a package")
    sign.add_argument("package")
    key_args(sign)
    sign.set_defaults(func=cmd_sign)

    verify = sub.add_parser("verify", help="check a signature")
    verify.add_argument("package")
    verify.add_argument("--signature", required=True)
    verify.add_argument("--public", required=True)
    verify.set_defaults(func=cmd_verify)

    stamp = sub.add_parser("stamp", help="write sha256 (+signature) into a listing")
    stamp.add_argument("listing")
    stamp.add_argument("version")
    stamp.add_argument("--package", required=True)
    key_args(stamp)
    stamp.set_defaults(func=cmd_stamp)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
