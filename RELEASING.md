# Releasing

Releases are published by the `Release` workflow (`.github/workflows/release.yml`), which runs
only when a tag matching `v*` is pushed.

## One-time setup

1. On PyPI, add a trusted publisher for the `sabatark` project (a "pending publisher" before the
   first upload) with owner `tuudik`, repository `python-sabatark`, workflow `release.yml` and
   environment `pypi`.
2. In the GitHub repository settings, create an environment named `pypi`. Adding required
   reviewers to it makes every upload wait for an approval.

## Cutting a release

1. Bump the version with `uv version X.Y.Z`, which updates `pyproject.toml` and `uv.lock`.
2. In `CHANGELOG.md`, add a `## [X.Y.Z] - YYYY-MM-DD` section with the changes and a
   `[X.Y.Z]: https://github.com/tuudik/python-sabatark/releases/tag/vX.Y.Z` link at the bottom.
3. Check locally: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` and
   `uv build`.
4. Commit, then tag and push:

   ```sh
   git commit -am "chore: release X.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```

5. The workflow then:
   - fails unless the tag equals `v` plus the project version and `CHANGELOG.md` has a section
     for that version;
   - runs the tests and builds the sdist and wheel;
   - publishes both to PyPI through trusted publishing (environment `pypi`);
   - after a successful upload, creates a GitHub release for the tag with the sdist and wheel
     attached and that version's `CHANGELOG.md` section as its notes.

If the build or tests fail, nothing is published: fix the problem, delete the tag
(`git push --delete origin vX.Y.Z` and `git tag -d vX.Y.Z`) and tag again. PyPI never accepts
the same version twice, so once the upload has succeeded, ship any fix as a new version.

## After the release

Bump the requirement in the ha-sabatark integration's `manifest.json` to `sabatark==X.Y.Z` and
release the integration.
