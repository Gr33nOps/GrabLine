# Releasing GrabLine

Ordinary source and documentation changes do not need a new release. A version
tag publishes installers; `main` also deploys the website. Read
[Contributing](CONTRIBUTING.md), [Testing](docs/testing.md) and
[Packaging](packaging/README.md) before publishing.

## Preserve history

`dev` is the integration branch; `main` receives reviewed stable changes through
ordinary merges or fast-forwards. Never recreate `main` as an orphan, force-push
it, or move an existing published tag. Older historical workflows did rewrite
history; those instructions are obsolete.

## Prepare a version

1. Inspect the working tree, fetch `origin` and combine reviewed changes on `dev`.
2. Update the version consistently in `app/__init__.py`, `pyproject.toml`,
   `extension/manifest.json` and the `softwareVersion` field in `website/index.html`.
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
4. Check Pages' `latest.json`, website download selection and the app's latest
   release discovery. Pages waits for the current version's tag build before
   falling back to the latest available release.
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

## Current retention policy

The repository owner chose to keep **only the newest published release and its
matching version tag** on 14 November 2026. Current retained version: `v1.32.15`.
Historical release notes remain in [docs/releases](docs/releases/README.md).
Older package download/tag URLs may no longer resolve.

There is no automatic deletion workflow. For an authorized future cleanup,
first verify the new release, save release metadata and tag commit references,
identify the exact obsolete versions, and preserve branches/source history.
Removing a GitHub release deletes its hosted assets; removing a tag removes its
named source reference. A metadata backup does not preserve binary installers.
Keep a verified archive before deletion when rollback or upgrade tests need it.
