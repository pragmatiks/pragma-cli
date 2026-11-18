# Releasing

How `pragmatiks-cli` is versioned and published.

Package: `pragmatiks-cli` on [PyPI](https://pypi.org/project/pragmatiks-cli/)

Releases are CI-owned. People change `main` only through pull requests; the only direct push is the bump commit from the pragma-ci-bot GitHub App, which may bypass the `main` ruleset and whose key lives only in the `release` environment. Each merge to `main`, or a manual dispatch from `main`, runs `.github/workflows/publish.yaml`; runs queue and execute one at a time:

1. `bump` (no secrets) runs `cz bump` on the latest `main` (version and `CHANGELOG.md` from conventional commits, commit marked `[skip ci]`) and hands the bump commit and `v{version}` tag on as a git bundle.
2. `push` (`release` environment) pushes the bump commit and tag to `main` as pragma-ci-bot. If `main` moved since the bump, it pushes nothing, dispatches a new `publish.yaml` run on `main` (which bumps from the latest `main`) and ends without failure.
3. `build` builds the commit that was pushed, `publish` uploads it to PyPI through trusted publishing (`pypi` environment), `dispatch` triggers the pragma-providers re-publish, and `github-release` creates the GitHub release.

A merge whose commits carry no bump type (`chore`, `docs`, …) releases nothing. The update-sdk workflow merges with `GITHUB_TOKEN`, which starts no push-triggered run, so after it merges an SDK bump it dispatches `publish.yaml` on `main` itself; a bump that crosses an SDK major version it opens but leaves for review, and the human merge starts Publish. The GitHub release is created last and marks a release complete: when `bump` finds nothing to bump but the current version's tag has no GitHub release, the run builds, publishes and releases that tag's commit without a push. So after a failure past `push`, any later run (a re-run, a manual dispatch or the next merge) finishes the release.

Never run `cz bump`, `uv build` or `uv publish` locally, and never hand-edit the version or changelog.

**Tag format**: `v{version}` (e.g., `v11.0.0`)
