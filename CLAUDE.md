# CLAUDE.md

## Project

**pragma-cli**: Command-line interface for Pragmatiks. Provides `pragma` command for managing resources, providers, and workflows.

## Shared rules

Delivery lives in pragma-kit `project-instructions.md`. Evidence-based development, the solution preference order, the new-dependency proposal, the 13 engineering principles and the reviewer and developer protocols live in the pragma-kit plugin's `pragma-kit:engineering-principles` skill: load it before planning, writing or reviewing a change here. This file holds only what is specific to this repository.

## Architecture

```
pragma-cli/
└── src/pragma_cli/
    ├── main.py            # Typer app entry point
    ├── commands/          # Command modules
    └── utils/             # Helpers (output, config)
```

## Dependencies

- `typer` - CLI framework
- `pragmatiks-sdk` - API client
- `rich` - Terminal formatting
- `copier` - Project scaffolding
- `pyyaml` - YAML parsing

This is a CLI end users install: a new top-level dependency adds directly to their install footprint.

## Entry Point

```bash
pragma <command> [options]
```

Defined in `pyproject.toml`:
```toml
[project.scripts]
pragma = "pragma_cli.main:app"
```

## Commands

The behavior contract of every command (arguments, output, errors, exit codes, configuration and authentication) lives in `docs/commands.md`. Read the part for a command before changing it, and update it in the same commit.

## Plugin Authoring

`pragma` discovers and mounts top-level subcommands at startup via the `pragma.commands`
Python entry-point group. To extend the CLI with a new command, publish a package that
exposes a Typer app and registers it under the group:

```toml
# in your plugin package's pyproject.toml
[project.entry-points."pragma.commands"]
mycommand = "my_package.cli:app"
```

`my_package.cli:app` must be a `typer.Typer` instance. After installation, `pragma mycommand`
becomes available; broken plugins log a warning and are skipped, so the host never crashes.

### Distribution

| Plugin type | Channel | Auth |
| -- | -- | -- |
| Public | PyPI | none |
| Private | `[tool.uv.sources] my-plugin = { git = "...", tag = "v..." }` | git credentials |

For a public plugin like `pragmatiks-lint`, consumers install via `uv add pragmatiks-lint`.
For an internal plugin (e.g. future `pragmatiks-console`), consumers declare a git source
in their pyproject.toml. No central index is required; auth piggybacks on existing GitHub
SSH/HTTPS credentials.

## Development

Always use mise tasks (`mise tasks` lists them all):

| Command | Purpose |
|---------|---------|
| `mise run install` | Install the locked mise toolchain, the locked environment into `.venv`, and the pre-commit and commit-msg hooks |
| `mise run format` | Format with ruff |
| `mise run check` | Lint + type check against `.venv`, whatever `VIRTUAL_ENV` the shell carries |
| `mise run lock:toolchain` | Regenerate `mise.lock` and `.mise/locks/` for macOS arm64 and Linux x64 without bumping a tool; runs only on those hosts and only with mise at the `min_version` in `mise.toml` |

### Toolchain

mise is the only toolchain source, on the Mac, in Linux cloud sessions and in CI. On a fresh clone run `mise trust`, then `mise run install`.

- `mise.toml` pins python, uv, pre-commit, commitizen and yq; `mise.lock` and `.mise/locks/` lock them; `locked = true` in `[settings]` makes every mise install fail on a tool the lockfile does not record.
- `mise.toml` points uv at mise's Python (`UV_PYTHON`, `UV_PYTHON_PREFERENCE`), so `.venv` always runs the pinned interpreter.
- `[tool.uv] required-version` in `pyproject.toml` equals the uv pin in `mise.toml`; uv refuses to run at any other version, and the `Toolchain` workflow fails a pull request where they differ.
- Changing a tool version in `mise.toml` means running `mise run lock:toolchain` and committing the lock; the `Toolchain` workflow fails a pull request whose lock is stale.
- The mise version is pinned twice and moves in one sweep across every Pragmatiks repo: `min_version` in `mise.toml`, and every `jdx/mise-action` `version:` under `.github/workflows`.

### Local SDK checkout

To work against an unreleased pragma-sdk, set an absolute `PRAGMA_SDK_DIRECTORY` in the worktree's own gitignored `mise.local.toml` and run `mise trust` once:

```toml
[env]
PRAGMA_SDK_DIRECTORY = "/absolute/path/to/pragma-sdk"
```

- `mise run check` validates the path (absolute, contains `src/pragma_sdk`), prints it, and type-checks against the local SDK checkout layered over the locked environment.
- Run the CLI against it with `uv run --with-editable "$PRAGMA_SDK_DIRECTORY" pragma …`.
- `pyproject.toml` and `uv.lock` never change; never `uv add --editable` a sibling checkout.
- Code that uses names only in an unreleased SDK passes `mise run check` with the variable set, but the pre-commit ty hook rejects it until that SDK is released, because committed code must work against the registry SDK.
- Never set it in the main checkout's `mise.local.toml`: mise loads parent directories' config into every worktree under `.claude/worktrees`.

## Patterns

- Typer for command definitions
- Rich console for formatted output
- SDK client for all API operations
- YAML for resource definitions

## Publishing to PyPI

Package: `pragmatiks-cli` on [PyPI](https://pypi.org/project/pragmatiks-cli/)

Releases are CI-owned. People change `main` only through pull requests; the only direct push is the bump commit from the pragma-ci-bot GitHub App, which may bypass the `main` ruleset and whose key lives only in the `release` environment. Each merge to `main`, or a manual dispatch from `main`, runs `.github/workflows/publish.yaml`; runs queue and execute one at a time:

1. `bump` (no secrets) runs `cz bump` on the latest `main` (version and `CHANGELOG.md` from conventional commits, commit marked `[skip ci]`) and hands the bump commit and `v{version}` tag on as a git bundle.
2. `push` (`release` environment) pushes the bump commit and tag to `main` as pragma-ci-bot. If `main` moved since the bump, it pushes nothing, dispatches a new `publish.yaml` run on `main` (which bumps from the latest `main`) and ends without failure.
3. `build` builds the commit that was pushed, `publish` uploads it to PyPI through trusted publishing (`pypi` environment), `dispatch` triggers the pragma-providers re-publish, and `github-release` creates the GitHub release.

A merge whose commits carry no bump type (`chore`, `docs`, …) releases nothing. The update-sdk workflow merges with `GITHUB_TOKEN`, which starts no push-triggered run, so after it merges an SDK bump it dispatches `publish.yaml` on `main` itself; a bump that crosses an SDK major version it opens but leaves for review, and the human merge starts Publish. The GitHub release is created last and marks a release complete: when `bump` finds nothing to bump but the current version's tag has no GitHub release, the run builds, publishes and releases that tag's commit without a push. So after a failure past `push`, any later run (a re-run, a manual dispatch or the next merge) finishes the release.

Never run `cz bump`, `uv build` or `uv publish` locally, and never hand-edit the version or changelog.

**Tag format**: `v{version}` (e.g., `v11.0.0`)
