# Development

Tasks, toolchain, working against a local SDK checkout, and code patterns.

## Tasks

Always use mise tasks (`mise tasks` lists them all):

| Command | Purpose |
|---------|---------|
| `mise run install` | Write the `.miserc.toml` mise ceiling, install the locked mise toolchain, the locked environment into `.venv`, and the pre-commit and commit-msg hooks |
| `mise run format` | Format with ruff |
| `mise run check` | Lint + type check against `.venv`, whatever `VIRTUAL_ENV` the shell carries |
| `mise run lock:toolchain` | Regenerate `mise.lock` and `.mise/locks/` for macOS arm64 and Linux x64 without bumping a tool; runs only on those hosts and only with mise at the `min_version` in `mise.toml` |
| `mise run check:mise-pins` | Fail when a `jdx/mise-action` step's `version:` or `sha256:` differs from `min_version` and its runner's mise binary checksum |
| `mise run check:toolchain-scripts` | Fail when a shared toolchain script under `scripts/tasks/` or the git hook `scripts/git_hooks/hook.sh` differs from the public repositories' copies (same-named branch, else `main`) |

## Toolchain

mise is the only toolchain source, on the Mac, in Linux cloud sessions and in CI. On a fresh clone run `mise trust`, then `mise run install`.

- `mise.toml` pins python, uv, pre-commit, commitizen and yq; `mise.lock` and `.mise/locks/` lock them; `locked = true` in `[settings]` makes every mise install fail on a tool the lockfile does not record.
- `mise.toml` points uv at mise's Python (`UV_PYTHON`, `UV_PYTHON_PREFERENCE`), so `.venv` always runs the pinned interpreter.
- `[tool.uv] required-version` in `pyproject.toml` equals the uv pin in `mise.toml`; uv refuses to run at any other version, and the `Toolchain` workflow fails a pull request where they differ.
- Changing a tool version in `mise.toml` means running `mise run lock:toolchain` and committing the lock; the `Toolchain` workflow fails a pull request whose lock is stale.
- The mise version is pinned in three places and moves in one sweep across every Pragmatiks repo: `min_version` in `mise.toml`, and every `jdx/mise-action` `version:` and `sha256:` (the plain mise binary's checksum for the step's runner) under `.github/workflows`; the `Toolchain` workflow fails a step whose pins do not match.
- `scripts/tasks/` holds the shared toolchain scripts and `scripts/git_hooks/hook.sh` the installed git hook, both byte-identical across every Pragmatiks repo; `mise run check:toolchain-scripts` fails on any difference (hub `docs/runbooks/local-dev-setup.md`, Shared toolchain scripts).

## Local SDK checkout

To work against an unreleased pragma-sdk, set an absolute `PRAGMA_SDK_DIRECTORY` in the checkout's own gitignored `mise.local.toml` and run `mise trust` once:

```toml
[env]
PRAGMA_SDK_DIRECTORY = "/absolute/path/to/pragma-sdk"
```

- `mise run check` validates the path (absolute, contains `src/pragma_sdk`), prints it, and type-checks against the local SDK checkout layered over the locked environment.
- Run the CLI against it with `uv run --with-editable "$PRAGMA_SDK_DIRECTORY" pragma …`.
- `pyproject.toml` and `uv.lock` never change; never `uv add --editable` a sibling checkout.
- Code that uses names only in an unreleased SDK passes `mise run check` with the variable set, but the pre-commit ty hook rejects it until that SDK is released, because committed code must work against the registry SDK.

## Patterns

- Typer for command definitions
- Rich console for formatted output
- SDK client for all API operations
- YAML for resource definitions
