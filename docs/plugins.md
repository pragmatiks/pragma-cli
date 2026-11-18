# Plugin authoring

How a package adds a top-level `pragma` command, and how plugins are distributed.

## Entry point

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

## Distribution

| Plugin type | Channel | Auth |
| -- | -- | -- |
| Public | PyPI | none |
| Private | `[tool.uv.sources] my-plugin = { git = "...", tag = "v..." }` | git credentials |

For a public plugin like `pragmatiks-lint`, consumers install via `uv add pragmatiks-lint[check]`.
`pragmatiks-cli` itself never depends on a plugin: `pragma lint` and its semgrep install reach only
repositories that declare `pragmatiks-lint[check]` (this repository's `dev` group, the hub's dependencies),
not the end-user `pragma` install.
For an internal plugin (e.g. future `pragmatiks-console`), consumers declare a git source
in their pyproject.toml. No central index is required; auth piggybacks on existing GitHub
SSH/HTTPS credentials.
