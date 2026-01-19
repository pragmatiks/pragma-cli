# CLAUDE.md

## Project

**pragma-cli**: Command-line interface for Pragmatiks. Provides `pragma` command for managing resources, providers, and workflows.

## Architecture

```
pragma-cli/
├── src/pragma_cli/
│   ├── main.py            # Typer app entry point
│   ├── commands/          # Command modules
│   └── utils/             # Helpers (output, config)
└── tests/
```

## Dependencies

- `typer` - CLI framework
- `pragmatiks-sdk` - API client
- `rich` - Terminal formatting
- `copier` - Project scaffolding
- `pyyaml` - YAML parsing

## Development

Always use `task` commands:

| Command | Purpose |
|---------|---------|
| `task test` | Run pytest |
| `task format` | Format with ruff |
| `task check` | Lint + type check |

## Entry Point

```bash
pragma <command> [options]
```

Defined in `pyproject.toml`:
```toml
[project.scripts]
pragma = "pragma_cli.main:app"
```

## Patterns

- Typer for command definitions
- Rich console for formatted output
- SDK client for all API operations
- YAML for resource definitions

## Testing

- Mock SDK calls, don't hit real API
- Test command output formatting
- Test error handling paths

## Publishing to PyPI

Package: `pragmatiks-cli` on [PyPI](https://pypi.org/project/pragmatiks-cli/)

**Versioning** (commitizen):
```bash
cz bump              # Bump version based on conventional commits
cz bump --patch      # Force patch bump
cz bump --minor      # Force minor bump
```

**Publishing**:
```bash
uv build             # Build wheel and sdist
uv publish           # Publish to PyPI (requires PYPI_TOKEN)
```

**Version files**: `pyproject.toml` (version field updated by commitizen)

**Tag format**: `v{version}` (e.g., `v0.12.2`)

## Related Repositories

- `../pragma-sdk/` - SDK (this CLI depends on it)
- `../pragma-os/` - API server
