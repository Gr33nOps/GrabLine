# Releasing GrabLine

Ordinary source and documentation changes do not need a new release. A version
tag publishes installers and requests a deployment in the separate website repo. Read
[Contributing](CONTRIBUTING.md), [Testing](docs/testing.md) and
[Packaging](packaging/README.md) before publishing.

## Preserve history

`dev` is the integration branch; `main` receives reviewed stable changes through
ordinary merges or fast-forwards. Never recreate `main` as an orphan, force-push
it, or move an existing published tag. Older historical workflows did rewrite
history; those instructions are obsolete.

## Prepare a version

1. Inspect the working tree, fetch `origin` and combine reviewed changes on `dev`.
2. Update the version consistently in `app/__init__.py`, `pyproject.toml` and
   `extension/manifest.json`. Desktop and extension versions stay aligned. The
   website sets its own `softwareVersion` from release metadata at deploy time.
3. Write `docs/releases/<version>.md` and update the
   [release-note index](docs/releases/README.md). Describe resulting behaviour,
   verification and material limitations.
4. Run lint, formatting, dependency/type checks, the full non-public-network
   Windows/Python suite and Deno extension suite. Reproduce affected browser,
   engine or GUI cases; save revision-specific evidence. Run packaging separately
   from the ordinary suite, then smoke-test the packaged GUI/native host and a
   hashed transfer. CI must pass before publication.
5. Commit, push `dev` and merge it into `main` without rewriting history. A
   documentation-only merge can retain the existing app version.

## Publish

After the stable commit is verified, create an **annotated new tag** on `main`.
Replace `X.Y.Z` with the reviewed version:

```sh
git switch main
git tag -a vX.Y.Z -m "GrabLine X.Y.Z"
git push origin main vX.Y.Z
```

The [Release workflow](.github/workflows/release.yml) builds on native Windows,
macOS and Linux runners. All platforms must succeed before publication. Its
publish job checks expected assets and generates `SHA256SUMS.txt`. Manual
workflow dispatch produces build artifacts without publishing a release.

After publication:

1. Verify all 12 expected assets: six desktop packages, two extension ZIPs,
   three dependency manifests and `SHA256SUMS.txt`.
2. Verify actual downloaded package hashes against the published checksums and
   GitHub digests; smoke-test the published Windows portable artifact.
3. Give the release a readable title/body based on the checked-in release notes.
4. Verify the `release-published` dispatch and successful Pages run in
   [GrabLine/grabline.github.io](https://github.com/GrabLine/grabline.github.io/actions).
   Check https://grabline.github.io/latest.json, website download selection and
   the app's latest-release discovery. The site fetches the latest published
   release and stamps its JSON-LD version automatically. The release stays
   successful if `WEBSITE_DISPATCH_TOKEN` is missing or dispatch fails; in that
   case deploy the site manually. See [Website maintenance](docs/website.md).
5. Inspect the Packages workflow; if its earlier `main` run resolved the previous
   release, dispatch it again after publication. Its image is an informational
   repository-linked package, not a runnable GrabLine application container.

Do not claim a full platform audit from build success. Retain VM, account,
live-site and hardware limitations in the verification record.

## If publication fails

For a confirmed product defect, fix it and publish a **new patch version**. Do not
retag different code under the same version or hand-publish a partial platform
set. For an infrastructure retry, verify the source identity and build inputs;
never silently replace verified public artifacts with different content.

## Published tags and release history

Published version tags are immutable: do not move, update or delete them.
Preserve branch history, including `main` and `dev`; never force-push or recreate
either branch. Repository rulesets protect those branches and `v*` tags while
allowing ordinary maintainer pushes and creation of new release tags.

The repository retains `v1.32.15` following an earlier owner-approved cleanup.
Historical release notes remain in [docs/releases](docs/releases/README.md);
older download/tag URLs removed by that cleanup may no longer resolve. Future
release work must preserve published tags rather than repeat that cleanup.

There is no automatic release deletion workflow. Any future cleanup of hosted
release assets requires separate owner approval and a verified archive when
rollback or upgrade tests need it. Saving metadata does not preserve installers.
Never delete a branch or published tag as part of a release cleanup.
