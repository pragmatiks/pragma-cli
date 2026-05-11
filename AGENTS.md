# AGENTS.md

## Scope

These instructions apply to the whole repository.

This repository is Codex-ready, but the existing Claude Code setup remains authoritative for Claude-specific behavior. Keep `CLAUDE.md`, `.claude/` local settings rules, and Claude hooks untouched unless the task explicitly asks for Claude Code changes.

Do not add personal Codex configuration, MCP auth files, local hooks, machine-specific paths, or secrets to this repository.

## Project

`pragma-cli` is the Python CLI for Pragmatiks. It exposes the `pragma` command from `pragma_cli.main:app` and uses:

- Typer for command definitions.
- Rich for terminal output.
- `pragmatiks-sdk` for API access.
- YAML/TOML parsing for resources, config, and provider metadata.

Source lives under `src/pragma_cli/`. Command implementations live under `src/pragma_cli/commands/`.

## Required Tooling

- Python 3.13 or newer. CI installs Python 3.13.
- `uv` for environment management, dependency sync, and builds.
- `task` for the project command shortcuts.

## Commands

Install dependencies:

```bash
task install
```

Format:

```bash
task format
```

Check lint and types:

```bash
task check
```

Test / CLI smoke checks that do not require authentication:

```bash
task test
uv run pragma --help
uv run pragma config current-context
```

Run `task test` when changes touch plugin discovery. For other behavior, run `task check` plus targeted CLI smoke
checks or command-specific checks that match the change.

Build distributions:

```bash
uv build
```

Release/version commands:

```bash
uv run cz bump
uv build
uv publish
```

Only publish when explicitly asked and when PyPI credentials are available. The GitHub `Publish CLI` workflow also bumps with Commitizen, builds, creates a GitHub release, publishes to PyPI, and triggers the providers publish workflow after changes land on `main`.

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

## Validation Expectations

Before handing off code changes, run the narrowest reliable validation for the change. For most changes in this repo, that means:

```bash
task check
task test
uv run pragma --help
```

For packaging or release changes, also run:

```bash
uv build
```

If a command cannot be run because credentials, network access, or external services are unavailable, report that explicitly with the command attempted and the observed failure.

## Code Style

- Follow the existing Typer command-module pattern.
- Prefer SDK client calls over direct HTTP.
- Keep Rich output consistent with nearby commands.
- Use Google-style docstrings where public or nontrivial functions need them.
- Ruff is configured for Python 3.13, 120-character lines, import sorting, pyupgrade, pydocstyle, and Ruff docstring checks.
- The pre-commit policy forbids ordinary explanatory `#` comments in `src/`; extract clearer code instead. `TODO`, `FIXME`, `BUG`, `HACK`, `type:`, `noqa`, `fmt:`, and `pragma:` comments are allowed where appropriate.
- Test files, if added later, should use standalone pytest-style functions. Do not use `unittest` or `unittest.mock`; use pytest fixtures and `pytest-mock` conventions.

## Local Config And Secrets

The CLI reads local user config outside the repository:

- Default config directory: `~/.config/pragma/`.
- `XDG_CONFIG_HOME` changes the config root to `$XDG_CONFIG_HOME/pragma/`.
- Config file: `config`.
- Credentials file: `credentials`.
- Lock file: `config.lock`.

Relevant environment variables include:

- `PRAGMA_CONTEXT`
- `PRAGMA_PROJECT`
- `PRAGMA_AUTH_TOKEN`
- `PRAGMA_AUTH_TOKEN_<CONTEXT>`
- `PRAGMA_AUTH_CALLBACK_PORT`
- `PRAGMA_AUTH_CALLBACK_PATH`
- `PRAGMA_PROVIDER_TEMPLATE`

Never commit local config, credentials, tokens, `.env`, `.venv`, build outputs, caches, `.claude/settings.local.json`, or `.claude/scheduled_tasks.lock`.

## Worktree Expectations

- Start by checking `git status --short --branch`; Codex worktrees may be detached or have user changes.
- Do not revert or overwrite changes you did not make.
- Keep edits scoped to the issue.
- Avoid writing outside the repository except for normal tool caches and virtual environments.
- Do not add generated artifacts such as `dist/`, `.ruff_cache/`, `.pytest_cache/`, or `.venv/`.

## Linear Workflow

When working from a Linear issue:

- Treat the issue description and checklist as the source of truth.
- If Linear tooling is configured, move the issue to an active state when starting, post concise progress comments for meaningful blockers, and leave a completion comment with validation results.
- If you discover follow-up work, create or propose a linked Linear issue instead of expanding scope silently.
- Keep parent issue context intact and mention related issue identifiers in summaries when useful.
- Work autonomously through implementation and validation unless the issue requires a product decision or secret access.
