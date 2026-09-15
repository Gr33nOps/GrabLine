# Releasing GrabLine

## The one rule: history is append-only

Earlier releases rebuilt `main` as a **single orphan commit** and force-pushed
it. Do not do this. It cost the project real things:

- `main` had no ancestry, so `git blame`, `git bisect` and `git log --follow`
  all stopped at the release commit;
- release tags pointed at commits that were not reachable from any branch, so
  "which code shipped in 1.29.25?" had no answer git could give;
- every force-push orphaned the previous release's commit, and a tag pointing
  into an orphaned object is one `gc` away from dangling;
- Pages could not diff the rewritten ref, which is why that workflow still
  carries a comment about disabling its paths filter.

`main` is now an ordinary branch with ordinary history. Never force-push it.
Never force-push a published tag.

## Cutting a release

1. Land the work on `dev` as normal commits.
2. Bump the version in the four places that carry it:
   `app/__init__.py`, `pyproject.toml`, `extension/manifest.json`,
   `website/index.html`.
3. Run the full check suite locally (see CONTRIBUTING / the CI workflow):
   `ruff check . && ruff format --check . && mypy app && pytest`.
4. Commit, then **merge** `dev` into `main` (a fast-forward or a merge commit -
   both keep ancestry; a rebuild does not).
5. Tag the commit on `main` and push the tag:
   `git tag -a vX.Y.Z -m "X.Y.Z" && git push origin main vX.Y.Z`.

The Release workflow builds Windows, macOS and Linux installers from the tag.
All three must succeed: the publish job verifies every expected asset is
present and fails the release rather than publishing a partial one. If a build
leg fails, fix it and cut the next patch tag - do not hand-publish the assets
that did build.

## If a release goes out wrong

Publish a new version. Do not retag, move a tag, or rewrite `main`: anyone who
already fetched the old tag would silently keep different code under the same
name, which is exactly the property a tag exists to rule out.
