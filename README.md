# Vyasa marketplace

The plugin and theme marketplace for [Vyasa](https://github.com/vyasa-cms/vyasa).

A registry is a **static signed document**, not a service. One `index.json`
lists every plugin and theme; the packages live wherever you like. Every
install verifies the package by checksum, and by ed25519 signature when
you sign, so **the host does not have to be trusted — only reachable.**
That is why running a marketplace costs a static file, and why a mirror
of it is exactly as safe as the original.

## Using it

Every Vyasa install uses this marketplace out of the box — nothing to
configure. It is served as static files at `https://marketplace.vyasa.site`
and verified by keys compiled into every Vyasa binary. Operators can
mirror it (`[marketplace] mirror_url` in `vyasa.toml`); see Vyasa's
`docs/MARKETPLACE.md`.

## Publishing

```bash
tools/build-site.sh [--packages ./dist]   # index.json + every package into site/dist
(cd site && npx wrangler deploy)          # to marketplace.vyasa.site
```

## What an operator sees before installing

Every plugin listing declares exactly which capabilities it requests, and
the admin shows that list before anything is installed. Because Vyasa's
broker *enforces* capabilities at runtime, that list is a fact rather
than a promise — the plugin cannot do anything outside it.

On an update, capabilities the installed version does not already hold
are marked **(new)** and named in the confirmation. Nothing auto-updates:
an update can widen what a plugin may do, and a person should see that.

## Layout

```
listings/plugins/<name>.json   one file per plugin
listings/themes/<name>.json    one file per theme
schema/index.schema.json       what the built index looks like
tools/registry.py              build + validate  (standard library only)
tools/sign.py                  keygen / sign / stamp  (needs `cryptography`)
```

One file per listing, rather than a single checked-in `index.json`: a
monolithic document is a merge conflict on every pull request, and a
second source of truth that can disagree with the first. `index.json` is
generated at publish time and is deliberately not committed.

## Publishing

Packages and the index are files on this repository's
[`packages` release](https://github.com/vyasa-cms/marketplace/releases/tag/packages).
Every site downloads them over https and verifies each package against the
checksum and signature in its listing, so GitHub only has to be reachable,
not trusted.

1. **Package it.** Authors build and sign with the Vyasa CLI:

   ```bash
   vyasa plugin pack ./my-plugin --key <author-key>
   ```

2. **Open a pull request** adding or updating the listing (see
   [CONTRIBUTING.md](CONTRIBUTING.md)) and attach the package to the pull
   request. CI runs the tooling's tests and validates every listing.

3. **A maintainer uploads the package** to the release and stamps the real
   digest and registry signature into the listing — never typed by hand:

   ```bash
   gh release upload packages dist/storefront-1.2.0.vyplugin -R vyasa-cms/marketplace
   REGISTRY_KEY=<hex> python3 tools/sign.py stamp \
       listings/plugins/storefront.json 1.2.0 \
       --package dist/storefront-1.2.0.vyplugin --key-env REGISTRY_KEY
   ```

   The listing's `url` is
   `https://marketplace.vyasa.site/packages/<file>`.
   **A published file name is never reused for different bytes** — a site
   that installed that version recorded its digest. New bytes get a new
   version and a new file name.

4. **Merge.** On every push to `main`, CI rebuilds `index.json`, uploads it
   to the release, and fetches every listed package to prove it resolves
   and still matches its listing.

### Running your own marketplace

A marketplace is static files, so you can host one anywhere https reaches.
`tools/publish-local.sh --site /path/to/vyasa --packages ./dist` publishes
to a Vyasa install's `registry/` directory (copying packages before the
index names them, and swapping the index in atomically); any static host
works the same way.

## The marketplace's key

Every package here is signed with the marketplace's ed25519 key, and
every Vyasa binary carries the matching public key (and a spare, for
rotation) in `crates/api/src/official.rs`. Sites never configure it.
Plugins also carry their author's signature, checked against the
`author_key` their listing names.

## Signing

```bash
python3 tools/sign.py keygen
```

Keep the private half in a CI secret. Publish the public half — that is
the key compiled into every Vyasa binary.

Two independent signatures end up protecting a marketplace plugin:

- the **author** signature inside the `.vyplugin`, checked against the
  site's `plugin_trusted_keys`;
- the **registry** signature over the downloaded bytes, checked against
  the keys compiled into the binary.

They are separate claims and both must hold. Marketplace plugin installs
are refused outright on a site with no plugin signing keys configured.

**Silence is never trust, in either direction.** Once a site lists
trusted keys, an unsigned artifact is refused rather than waved through;
and a signed artifact is refused when no key is configured to check it.
Anything else lets an attacker downgrade security by omitting a field.

**Code is never installed unsigned.** A package that can run code — every
plugin, and any theme with `assets/theme.js` or a template that writes
script — needs a registry signature from a key the site trusts, even on a
site that has configured none (it is then refused). Only a theme that is
pure data (tokens, layout, CSS, pictures, script-free templates) installs
unsigned, and only over https with a matching sha256.

### What the signature does and does not cover

The signature is over each *package*, not over the index document. So
someone who controls the index host cannot serve a package your key did
not sign — that is the attack that matters, and it is closed.

They could still remove a listing, or point a version at older bytes you
signed previously. Serve the index over https, and keep in mind that
installs are deliberate acts by a signed-in operator who is shown the
capability list first. Nothing here installs itself.

### Hosting, and why it is really a visibility question

A Vyasa site needs exactly two things of a registry: an `index.json`
reachable over https, and package URLs over https. No CORS (the server
fetches the index itself, not the browser), no auth, no particular host.
Redirects are followed, which is what makes release assets work.

This marketplace uses the simplest answer: the public repository's
release assets. The `packages` release holds every package and the
`index.json` built from the listings; GitHub's download URLs redirect to
its CDN, which installs follow.

If you run your own marketplace, any static host works — a bucket behind a
CDN, another repository's releases, or a Vyasa install, which can serve its
`registry_dir` at two public paths and nothing else:

```
GET /registry/index.json              the catalogue
GET /registry/packages/{file}         one package
```

Both 404 unless the operator has actually put files there, so an install
that hosts no marketplace gains no surface. Package filenames are checked
against an allowlist rather than scanned for traversal, and symlinks are
refused. Nothing in the format or the verification depends on who serves
the bytes: change the URLs in the listings and publish again.

### Do not hide the index behind credentials

Do not gate a published `index.json` behind a token unless you really mean
to: every install would need one, a token shared with other people's
installs is effectively public, and the checksum and signature — not the
transport — are what make a package trustworthy.

## Working on it locally

```bash
python3 tools/registry.py validate             # structure only, no network
python3 tools/registry.py validate --network   # also fetch and open every package
python3 tools/registry.py build                # listings/ -> index.json
python3 -m unittest discover -s tools -p 'test_*.py' -v

tools/publish-local.sh --site /path/to/vyasa --packages ./dist
```

The tooling is standard library only, so there is no install step and the
registry that distributes your plugins has no dependency tree of its own.
`tools/sign.py` is the single exception and needs `cryptography`.

## Multiple registries

The index is a static document, so a per-tier catalogue is just a
different URL and a private one is a URL behind whatever auth your CDN
offers. Nothing in the format assumes a single registry.

## Licence

The tooling and listings are licensed under either of [Apache License, Version 2.0](LICENSE-APACHE) or [MIT license](LICENSE-MIT) at your option. Each plugin and theme carries its own licence, stated by its author.
