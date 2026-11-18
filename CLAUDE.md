# pragma-cli

The `pragma` command-line interface for Pragmatiks (`pragmatiks-cli` on PyPI): resources, providers, projects, organizations and accounts, built on Typer, Rich and the SDK client.

Delivery: pragma-kit `project-instructions.md`. Engineering rules: the `pragma-kit:engineering-principles` skill; load it before planning, writing or reviewing a change. Tasks, toolchain and patterns: `docs/development.md`. Plugins: `docs/plugins.md`. Releases: `docs/releasing.md`.

## Gotchas

- **Command contract.** `docs/commands.md` holds every command's arguments, output, errors, exit codes, configuration and authentication. Read the part for a command before changing it, and update it in the same commit.
- **Install footprint.** End users install this CLI, so a new top-level dependency adds directly to their install. It never depends on a plugin: `pragma lint` reaches only repositories that declare `pragmatiks-lint[check]` (`docs/plugins.md`).
- **Plugins.** Packages add top-level commands through the `pragma.commands` entry-point group; a broken plugin logs a warning and is skipped, so the host never crashes (`docs/plugins.md`).
- **Local SDK.** For an unreleased pragma-sdk, set an absolute `PRAGMA_SDK_DIRECTORY` in the worktree's own `mise.local.toml`, never the main checkout's (mise loads it into every worktree), and never change `pyproject.toml` or `uv.lock` for it (`docs/development.md`).
- **Releases are CI's.** Never run `cz bump`, `uv build` or `uv publish` locally, or hand-edit the version or changelog; a merge runs the release pipeline (`docs/releasing.md`).
- **Toolchain.** mise is the only toolchain source. A `mise.toml` change ships with `mise run lock:toolchain` output, and `[tool.uv] required-version` must equal its uv pin; the `Toolchain` workflow fails either drift. Fresh clone: `mise trust`, then `mise run install`.
