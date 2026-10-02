#!/usr/bin/env python3
"""Tests for the registry tooling. Stdlib unittest, no dependencies.

    python3 -m unittest discover -s tools -p 'test_*.py' -v

The point of these is the cross-check in ``check_package``: CI is only
worth having if it actually catches a listing that disagrees with its
own artifact, because that is the mismatch the server refuses at install
time and the one a contributor cannot see by reading their own JSON.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import registry


def vyplugin(
    name: str = "storefront",
    version: str = "1.2.0",
    capabilities: tuple[str, ...] = ("kv:read",),
    min_host_api: int = 2,
    signed: bool = True,
) -> bytes:
    caps = ", ".join(f'"{c}"' for c in capabilities)
    manifest = (
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f"min_host_api = {min_host_api}\n"
        f"capabilities = [{caps}]\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.toml", manifest)
        archive.writestr("plugin.wasm", b"\x00asm\x01\x00\x00\x00")
        if signed:
            archive.writestr("signature.txt", "ab" * 64)
    return buffer.getvalue()


def vytheme(name: str = "aurora", version: int = 3, required_api: int = 1) -> bytes:
    manifest = (
        f'name = "{name}"\n'
        f"version = {version}\n"
        f'author = "Vyasa"\n'
        f"required_api = {required_api}\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.toml", manifest)
        archive.writestr("tokens.json", "{}")
        archive.writestr("layout.json", "{}")
    return buffer.getvalue()


def vytheme_with_stray(name: str = "aurora", version: int = 3) -> bytes:
    """A theme package with a non-.tera file under templates/ — valid to a
    naive checker, refused by the server."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.toml",
                         f'name = "{name}"\nversion = {version}\n'
                         f'author = "V"\nrequired_api = 1\n')
        archive.writestr("tokens.json", "{}")
        archive.writestr("layout.json", "{}")
        archive.writestr("templates/single.tera", "x")
        archive.writestr("templates/login.html", "<html></html>")
    return buffer.getvalue()


def plugin_listing(**overrides) -> dict:
    listing = {
        "kind": "plugin",
        "name": "storefront",
        "title": "Storefront",
        "summary": "Sell things.",
        "author": "Vyasa",
        "homepage": "https://example.com/storefront",
        "versions": [
            {
                "version": "1.2.0",
                "url": "https://cdn.example.com/storefront-1.2.0.vyplugin",
                "sha256": "0" * 64,
                "min_host_api": 2,
                "capabilities": ["kv:read"],
            }
        ],
    }
    listing.update(overrides)
    return listing


class Capabilities(unittest.TestCase):
    def test_known_capabilities_pass(self):
        for cap in ("db:read:posts", "kv:write", "ai:text", "mail:send"):
            self.assertTrue(registry.capability_ok(cap), cap)

    def test_hyphenated_spelling_is_rejected(self):
        # The grammar is colon-separated. A hyphenated capability parses
        # nowhere and would be refused at install; catch it in CI.
        self.assertFalse(registry.capability_ok("db-read-posts"))

    def test_net_fetch_needs_a_host_and_no_path(self):
        self.assertTrue(registry.capability_ok("net:fetch:*.stripe.com"))
        self.assertFalse(registry.capability_ok("net:fetch:"))
        self.assertFalse(registry.capability_ok("net:fetch:example.com/hook"))

    def test_invented_capability_is_rejected(self):
        self.assertFalse(registry.capability_ok("db:delete:everything"))


class Ordering(unittest.TestCase):
    """The key is ascending; `build` sorts newest-first with reverse=True."""

    @staticmethod
    def newest_first(kind: str, versions: list[str]) -> list[str]:
        return sorted(
            versions,
            key=lambda v: registry.version_sort_key(kind, v),
            reverse=True,
        )

    def test_plugin_versions_sort_newest_first(self):
        # 1.10.0 above 1.2.0: numeric, not lexicographic.
        self.assertEqual(
            self.newest_first("plugin", ["1.0.0", "1.10.0", "1.2.0", "2.0.0"]),
            ["2.0.0", "1.10.0", "1.2.0", "1.0.0"],
        )

    def test_prerelease_sorts_below_its_release(self):
        self.assertEqual(
            self.newest_first("plugin", ["1.2.0-rc1", "1.2.0"]),
            ["1.2.0", "1.2.0-rc1"],
        )

    def test_later_prerelease_outranks_earlier(self):
        self.assertEqual(
            self.newest_first("plugin", ["1.2.0-rc1", "1.2.0-rc2"]),
            ["1.2.0-rc2", "1.2.0-rc1"],
        )

    def test_build_metadata_is_ignored(self):
        self.assertEqual(
            self.newest_first("plugin", ["1.2.0+abc", "1.3.0"]),
            ["1.3.0", "1.2.0+abc"],
        )

    def test_theme_versions_sort_numerically(self):
        self.assertEqual(self.newest_first("theme", ["2", "10", "1"]), ["10", "2", "1"])


class ListingStructure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "listings" / "plugins").mkdir(parents=True)
        (self.root / "listings" / "themes").mkdir(parents=True)
        self._root, registry.ROOT = registry.ROOT, self.root
        self._listings, registry.LISTINGS = registry.LISTINGS, self.root / "listings"

    def tearDown(self):
        registry.ROOT, registry.LISTINGS = self._root, self._listings
        self.tmp.cleanup()

    def check(self, data: dict, kind: str = "plugins", stem: str | None = None):
        name = stem or data.get("name", "x")
        path = self.root / "listings" / kind / f"{name}.json"
        path.write_text(json.dumps(data))
        problems = registry.Problems()
        registry.validate_listing(path, data, problems)
        return problems.items

    def test_a_good_listing_passes(self):
        self.assertEqual(self.check(plugin_listing()), [])

    def test_a_good_theme_passes(self):
        theme = {
            "kind": "theme",
            "name": "aurora",
            "versions": [
                {
                    "version": "3",
                    "url": "https://cdn.example.com/aurora-3.vytheme",
                    "sha256": "a" * 64,
                    "required_api": 1,
                }
            ],
        }
        self.assertEqual(self.check(theme, kind="themes"), [])

    def test_filename_must_match_the_name(self):
        problems = self.check(plugin_listing(), stem="something-else")
        self.assertTrue(any("must be named" in p for p in problems), problems)

    def test_kind_must_match_the_directory(self):
        problems = self.check(plugin_listing(kind="theme"))
        self.assertTrue(any("kind must be" in p for p in problems), problems)

    def test_http_urls_are_refused(self):
        listing = plugin_listing()
        listing["versions"][0]["url"] = "http://cdn.example.com/x.vyplugin"
        problems = self.check(listing)
        self.assertTrue(any("must be https" in p for p in problems), problems)

    def test_bad_digest_length_is_caught(self):
        listing = plugin_listing()
        listing["versions"][0]["sha256"] = "abc"
        problems = self.check(listing)
        self.assertTrue(any("64 hex" in p for p in problems), problems)

    def test_unknown_capability_is_caught(self):
        listing = plugin_listing()
        listing["versions"][0]["capabilities"] = ["db-read-posts"]
        problems = self.check(listing)
        self.assertTrue(any("unknown capability" in p for p in problems), problems)

    def test_plugin_needs_min_host_api(self):
        listing = plugin_listing()
        del listing["versions"][0]["min_host_api"]
        problems = self.check(listing)
        self.assertTrue(any("min_host_api" in p for p in problems), problems)

    def test_duplicate_version_is_caught(self):
        listing = plugin_listing()
        listing["versions"].append(dict(listing["versions"][0]))
        problems = self.check(listing)
        self.assertTrue(any("listed twice" in p for p in problems), problems)

    def test_unknown_field_is_caught(self):
        problems = self.check(plugin_listing(price="9.99"))
        self.assertTrue(any("unknown field" in p for p in problems), problems)

    def test_theme_version_must_be_an_integer(self):
        theme = {
            "kind": "theme",
            "name": "aurora",
            "versions": [
                {
                    "version": "3.0.0",
                    "url": "https://cdn.example.com/aurora.vytheme",
                    "sha256": "a" * 64,
                    "required_api": 1,
                }
            ],
        }
        problems = self.check(theme, kind="themes")
        self.assertTrue(any("integers" in p for p in problems), problems)


class PackageCrossCheck(unittest.TestCase):
    """The listing is a claim; the package is the fact."""

    def run_check(self, entry: dict, blob: bytes, kind: str = "plugin", name="storefront"):
        problems = registry.Problems()
        registry.check_package("x.json", kind, {**entry, "_name": name}, blob, problems)
        return problems.items

    def entry_for(self, blob: bytes, **overrides) -> dict:
        entry = {
            "version": "1.2.0",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "min_host_api": 2,
            "capabilities": ["kv:read"],
        }
        entry.update(overrides)
        return entry

    def test_matching_package_passes(self):
        blob = vyplugin()
        self.assertEqual(self.run_check(self.entry_for(blob), blob), [])

    def test_digest_mismatch_is_caught(self):
        blob = vyplugin()
        problems = self.run_check(self.entry_for(blob, sha256="0" * 64), blob)
        self.assertTrue(any("sha256 is" in p for p in problems), problems)

    def test_capability_disagreement_is_caught(self):
        # The listing under-declares: the package also wants the network.
        blob = vyplugin(capabilities=("kv:read", "net:fetch:api.stripe.com"))
        problems = self.run_check(self.entry_for(blob), blob)
        self.assertTrue(any("capabilities disagree" in p for p in problems), problems)

    def test_name_mismatch_is_caught(self):
        blob = vyplugin(name="something-else")
        problems = self.run_check(self.entry_for(blob), blob)
        self.assertTrue(any("but the package is" in p for p in problems), problems)

    def test_version_mismatch_is_caught(self):
        blob = vyplugin(version="9.9.9")
        problems = self.run_check(self.entry_for(blob), blob)
        self.assertTrue(any("package version" in p for p in problems), problems)

    def test_unsigned_plugin_is_caught(self):
        blob = vyplugin(signed=False)
        problems = self.run_check(self.entry_for(blob), blob)
        self.assertTrue(any("unsigned package" in p for p in problems), problems)

    def test_min_host_api_mismatch_is_caught(self):
        blob = vyplugin(min_host_api=5)
        problems = self.run_check(self.entry_for(blob), blob)
        self.assertTrue(any("min_host_api" in p for p in problems), problems)

    def test_a_package_that_is_not_a_zip_is_reported_not_raised(self):
        blob = b"this is not a zip"
        entry = {"version": "1.2.0", "sha256": hashlib.sha256(blob).hexdigest()}
        problems = self.run_check(entry, blob)
        self.assertTrue(any("not readable" in p for p in problems), problems)

    def test_theme_required_api_mismatch_is_caught(self):
        blob = vytheme(required_api=2)
        entry = {
            "version": "3",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "required_api": 1,
        }
        problems = self.run_check(entry, blob, kind="theme", name="aurora")
        self.assertTrue(any("required_api" in p for p in problems), problems)

    def test_stray_file_in_a_theme_package_is_caught(self):
        blob = vytheme_with_stray()
        entry = {"version": "3", "sha256": hashlib.sha256(blob).hexdigest(),
                 "required_api": 1}
        problems = self.run_check(entry, blob, kind="theme", name="aurora")
        self.assertTrue(any("unexpected file" in p and "login.html" in p
                            for p in problems), problems)

    def test_bundled_images_and_fonts_are_allowed(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("manifest.toml",
                             'name = "aurora"\nversion = 3\nauthor = "V"\nrequired_api = 1\n')
            archive.writestr("tokens.json", "{}")
            archive.writestr("layout.json", "{}")
            archive.writestr("assets/images/hero.jpg", b"\xff\xd8")
            archive.writestr("assets/images/icons/mark.svg", "<svg/>")
            archive.writestr("assets/fonts/serif.woff2", b"wOF2")
        blob = buffer.getvalue()
        entry = {"version": "3", "sha256": hashlib.sha256(blob).hexdigest(),
                 "required_api": 1}
        self.assertEqual(self.run_check(entry, blob, kind="theme", name="aurora"), [])

    def test_a_bundled_file_the_server_refuses_is_caught(self):
        for name in ["assets/hero.jpg", "assets/images/Hero.JPG",
                     "assets/images/page.html", "assets/fonts/x.exe",
                     "assets/images/.hidden.png", "assets/images/a/b/c/d.png"]:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("manifest.toml",
                                 'name = "aurora"\nversion = 3\nauthor = "V"\nrequired_api = 1\n')
                archive.writestr("tokens.json", "{}")
                archive.writestr("layout.json", "{}")
                archive.writestr(name, b"x")
            blob = buffer.getvalue()
            entry = {"version": "3", "sha256": hashlib.sha256(blob).hexdigest(),
                     "required_api": 1}
            problems = self.run_check(entry, blob, kind="theme", name="aurora")
            self.assertTrue(any("unexpected file" in p for p in problems), (name, problems))

    def test_a_tera_template_is_allowed(self):
        blob = vytheme()
        entry = {"version": "3", "sha256": hashlib.sha256(blob).hexdigest(),
                 "required_api": 1}
        self.assertEqual(self.run_check(entry, blob, kind="theme", name="aurora"), [])

    def test_matching_theme_passes(self):
        blob = vytheme()
        entry = {
            "version": "3",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "required_api": 1,
        }
        self.assertEqual(self.run_check(entry, blob, kind="theme", name="aurora"), [])


if __name__ == "__main__":
    unittest.main()


class TypesMatchTheServer(unittest.TestCase):
    """crates/api/src/registry/index.rs reads the index with serde: a field
    of the wrong JSON type makes the whole index unreadable, so these must
    be refused here exactly as strictly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "listings" / "plugins").mkdir(parents=True)
        (self.root / "listings" / "themes").mkdir(parents=True)
        self._root, registry.ROOT = registry.ROOT, self.root
        self._listings, registry.LISTINGS = registry.LISTINGS, self.root / "listings"

    def tearDown(self):
        registry.ROOT, registry.LISTINGS = self._root, self._listings
        self.tmp.cleanup()

    def check(self, data: dict, kind: str = "plugins"):
        path = self.root / "listings" / kind / f"{data.get('name', 'x')}.json"
        path.write_text(json.dumps(data))
        problems = registry.Problems()
        registry.validate_listing(path, data, problems)
        return problems.items

    def theme(self, **version) -> dict:
        entry = {
            "version": "3",
            "url": "https://cdn.example.com/aurora-3.vytheme",
            "sha256": "a" * 64,
            "required_api": 1,
        }
        entry.update(version)
        return {"kind": "theme", "name": "aurora", "versions": [entry]}

    def test_a_numeric_version_is_refused(self):
        problems = self.check(self.theme(version=5), kind="themes")
        self.assertTrue(any("must be a string" in p for p in problems), problems)
        listing = plugin_listing()
        listing["versions"][0]["version"] = 1.2
        self.assertTrue(any("must be a string" in p for p in self.check(listing)))

    def test_non_ascii_digits_are_not_a_version(self):
        problems = self.check(self.theme(version="５"), kind="themes")
        self.assertTrue(any("integers" in p for p in problems), problems)
        listing = plugin_listing()
        listing["versions"][0]["version"] = "١.2.0"
        self.assertTrue(any("semver" in p for p in self.check(listing)))

    def test_min_host_api_must_be_a_u32(self):
        for bad in (True, -1, 2**32, 2.0, "2"):
            listing = plugin_listing()
            listing["versions"][0]["min_host_api"] = bad
            problems = self.check(listing)
            self.assertTrue(any("min_host_api" in p for p in problems), (bad, problems))

    def test_required_api_true_is_not_one(self):
        problems = self.check(self.theme(required_api=True), kind="themes")
        self.assertTrue(any("required_api" in p for p in problems), problems)

    def test_display_fields_must_be_strings(self):
        for field in ("title", "summary", "author"):
            for bad in (None, 3, ["x"]):
                problems = self.check(plugin_listing(**{field: bad}))
                self.assertTrue(
                    any(f"{field} must be a string" in p for p in problems), (field, bad)
                )

    def test_other_version_fields_are_typed(self):
        cases = {
            "url": ["https://x"],
            "sha256": 0,
            "signature": 12,
            "released_at": 20260101,
            "capabilities": [1],
        }
        for field, bad in cases.items():
            listing = plugin_listing()
            listing["versions"][0][field] = bad
            self.assertTrue(self.check(listing), (field, bad))

    def test_the_new_capabilities_are_known(self):
        self.assertTrue(registry.capability_ok("html:page"))
        self.assertTrue(registry.capability_ok("assets:script"))


class BuildValidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "listings" / "plugins").mkdir(parents=True)
        (self.root / "listings" / "themes").mkdir(parents=True)
        self._root, registry.ROOT = registry.ROOT, self.root
        self._listings, registry.LISTINGS = registry.LISTINGS, self.root / "listings"
        self.out = self.root / "index.json"

    def tearDown(self):
        registry.ROOT, registry.LISTINGS = self._root, self._listings
        self.tmp.cleanup()

    def build(self) -> int:
        import argparse
        import contextlib

        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            return registry.cmd_build(argparse.Namespace(out=str(self.out)))

    def test_a_listing_that_parses_but_is_invalid_is_not_built(self):
        listing = plugin_listing()
        listing["versions"][0]["url"] = "http://cdn.example.com/x.vyplugin"
        (self.root / "listings" / "plugins" / "storefront.json").write_text(json.dumps(listing))
        self.assertEqual(self.build(), 1)
        self.assertFalse(self.out.exists())

    def test_a_valid_listing_is_built(self):
        (self.root / "listings" / "plugins" / "storefront.json").write_text(
            json.dumps(plugin_listing())
        )
        self.assertEqual(self.build(), 0)
        index = json.loads(self.out.read_text())
        self.assertEqual(index["listings"][0]["name"], "storefront")


class Stamp(unittest.TestCase):
    def setUp(self):
        try:
            import sign  # noqa: F401 - needs `cryptography`
        except SystemExit:
            self.skipTest("cryptography is not installed")
        self.sign = sign
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "plugins").mkdir()
        self.listing = self.root / "plugins" / "storefront.json"
        self.package = self.root / "storefront-1.2.0.vyplugin"

    def tearDown(self):
        self.tmp.cleanup()

    def stamp(self, listing: dict, blob: bytes, key: str | None = None):
        import argparse
        import contextlib

        self.listing.write_text(json.dumps(listing))
        self.package.write_bytes(blob)
        args = argparse.Namespace(
            listing=str(self.listing),
            version="1.2.0",
            package=str(self.package),
            key=key,
            key_env=None,
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.sign.cmd_stamp(args)
        return json.loads(self.listing.read_text()), out.getvalue()

    def test_restamping_without_a_key_drops_the_stale_signature(self):
        listing = plugin_listing()
        listing["versions"][0]["signature"] = "ab" * 64
        blob = vyplugin()
        stamped, said = self.stamp(listing, blob)
        entry = stamped["versions"][0]
        self.assertNotIn("signature", entry)
        self.assertEqual(entry["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertIn("unsigned", said)
        self.assertNotIn("(signed", said)

    def test_stamping_with_a_key_signs_the_new_bytes(self):
        stamped, said = self.stamp(plugin_listing(), vyplugin(), key="07" * 32)
        self.assertRegex(stamped["versions"][0]["signature"], r"^[0-9a-f]{128}$")
        self.assertIn("(signed", said)

    def test_a_package_that_disagrees_with_its_listing_is_not_stamped(self):
        for blob in (
            vyplugin(name="other"),
            vyplugin(version="1.3.0"),
            vyplugin(capabilities=("kv:read", "net:fetch:*.evil.example")),
        ):
            listing = plugin_listing()
            before = json.dumps(listing)
            with self.assertRaises(SystemExit):
                self.stamp(listing, blob, key="07" * 32)
            self.assertEqual(self.listing.read_text(), before, "nothing written")
