# Submitting a plugin or theme

One JSON file per listing, under `listings/plugins/` or `listings/themes/`.
The file must be named after the package: `storefront.json` holds the
listing named `storefront`.

## A plugin listing

```json
{
  "kind": "plugin",
  "name": "storefront",
  "title": "Storefront",
  "summary": "Products, a cart and checkout.",
  "author": "Your Name",
  "homepage": "https://example.com/storefront",
  "versions": [
    {
      "version": "1.2.0",
      "url": "https://cdn.example.com/storefront-1.2.0.vyplugin",
      "sha256": "<filled in by tools/sign.py stamp>",
      "signature": "<filled in by tools/sign.py stamp>",
      "min_host_api": 2,
      "capabilities": ["db:read:posts", "kv:read", "kv:write"],
      "released_at": "2026-09-01"
    }
  ]
}
```

## A theme listing

```json
{
  "kind": "theme",
  "name": "aurora",
  "title": "Aurora",
  "summary": "A quiet editorial theme.",
  "author": "Your Name",
  "versions": [
    {
      "version": "3",
      "url": "https://cdn.example.com/aurora-3.vytheme",
      "sha256": "<filled in by tools/sign.py stamp>",
      "required_api": 1
    }
  ]
}
```

Theme versions are integers and plugin versions are semver, matching the
type in each package's own `manifest.toml`.

## Capabilities

`capabilities` must list **exactly** what the plugin's manifest requests —
no more, and no fewer. The admin shows that list to an operator before
installing and sends it back as the accepted set; if the package asks for
anything else, the install is refused. A catalogue edited between reading
and clicking therefore cannot grant powers nobody agreed to.

The grammar is colon-separated:

```
db:read:posts     db:read:options    db:read:comments
db:write:posts    db:publish:posts   db:write:meta
kv:read           kv:write           viewer:read
mail:send         log:write          event:emit
ai:text           html:page          assets:script
net:fetch:<host-pattern>
```

`net:fetch:api.stripe.com` and `net:fetch:*.stripe.com` are both valid; a
pattern may not be empty and may not contain a path.

Ask for the narrowest set that works. `db:publish:posts` is separate from
`db:write:posts` precisely because the difference between a draft awaiting
review and text live on the site is the whole reason the list gets read.

## Before you open a pull request

```bash
python3 tools/registry.py validate --network
```

That is what CI runs. It downloads each package and checks it against the
listing: digest, name, version, `min_host_api` / `required_api`, and that
the declared capabilities match the manifest. It also refuses a plugin
package with no author signature, because a site will too.

Publish the artifact **before** listing it — the URL has to resolve for
validation to pass.

## Updating an existing listing

Add a new entry to `versions`; do not edit a published one. A site that
already installed a version recorded its digest, and changing the bytes
behind a URL someone already trusts is the thing this whole design exists
to prevent.
