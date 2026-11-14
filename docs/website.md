# Website maintenance

The public site is https://grabline.github.io/. Its source and preserved history
live in [GrabLine/grabline.github.io](https://github.com/GrabLine/grabline.github.io).
Desktop code, GrabLine Connect, packaging, documentation and QA stay in
GrabLine/GrabLine. The website has no separate app-version bump.

## Deployment

The website's Pages workflow runs after a push to its `main`, a manual dispatch,
a `release-published` repository dispatch or the weekly Monday fallback. It
fetches the latest published GrabLine release, builds `latest.json`, stamps the
sitemap date and sets JSON-LD `softwareVersion` from that release tag. Tests run
before the artifact is uploaded. Actions use full commit SHAs with version
comments; keep that style when reviewing dependency updates.

After publishing all installers, the desktop release workflow sends the dispatch.
If its optional secret is absent or the request fails, the workflow emits a
warning and preserves the successful application release. Deploy manually:

```sh
gh workflow run pages.yml -R GrabLine/grabline.github.io --ref main
gh run list -R GrabLine/grabline.github.io --workflow pages.yml --limit 5
```

GitHub may disable schedules after 60 days without repository activity; do not
rely on the weekly fallback as the primary release-update mechanism. Release
dispatch requires the secret below. Changes prepared only on GrabLine's `dev`
branch do not alter the existing `main` release workflow until normally merged.

## Dispatch token: maintainer setup

Use GitHub Settings → Developer settings → Personal access tokens → Fine-grained
tokens → Generate new token. Set:

- Resource owner: `GrabLine`.
- Repository access: only `grabline.github.io`.
- Repository permission: Contents, read and write; leave other permissions at
  their minimum defaults, including GitHub's required read-only Metadata.
- Expiration: one year, subject to organization policy.

Store the value at the local terminal prompt; never paste it into an issue or chat:

```sh
gh secret set WEBSITE_DISPATCH_TOKEN -R GrabLine/GrabLine
```

The token is not needed in the website repository. Verify its permissions by
observing a real release dispatch after the desktop workflow is merged. Merely
seeing a secret name is not proof that the request works.

## Compatibility checks

After a deployment, check the root page, every desktop package button and
`latest.json`. Its version and asset URLs must match the latest GitHub release;
the JSON-LD version must agree. In a JavaScript-capable browser, verify:

- `https://grabline.github.io/GrabLine/` → `https://grabline.github.io/`.
- The same old URL with `?source=old#download` preserves both query and fragment.
- `https://grabline.github.io/GrabLine/assets/og.png` reaches `/assets/og.png`.
- An unrelated missing path stays a not-found page with working root assets.
- `https://gr33nops.github.io/GrabLine/#download` goes directly to the root site.

GitHub Pages serves old asset paths through a JavaScript 404 fallback, not a
server-side HTTP 301. Browsers navigate to the correct asset; HTTP-only clients
can still receive 404 HTML at the old address. Do not describe that as an HTTP
redirect or use an HTTP client to certify fragment-preserving browser behavior.

Keep Gr33nOps/gr33nops.github.io: removing it breaks installed-app website links.
Never recreate Gr33nOps/GrabLine; GitHub's repository/API redirect is required by
older app versions. Do not re-enable Pages in GrabLine/GrabLine during ordinary
deployments: the root website owns the old project-path redirects now.
