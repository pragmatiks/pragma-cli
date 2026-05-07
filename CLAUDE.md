# CLAUDE.md

## Project

**pragma-cli**: Command-line interface for Pragmatiks. Provides `pragma` command for managing resources, providers, and workflows.

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

### Resources
- `pragma resources list` - List resources with optional filters
- `pragma resources schemas` - List available resource schemas
- `pragma resources get <type> [name]` - Get resource(s) by type
- `pragma resources describe <type> <name>` - Show detailed resource info
- `pragma resources apply <file>` - Apply resources from YAML
- `pragma resources delete <type> <name>` - Delete a resource
- `pragma resources tags list/add/remove` - Manage resource tags

### Providers
- `pragma providers list` - List deployed providers
- `pragma providers init <name>` - Initialize a new provider project
- `pragma providers update` - Update project from template
- `pragma providers register --wheel-url <https url> --version <semver> --pyproject <path> [--sha256 <hex>] [--changelog <file>]` - Register an externally hosted wheel as a new provider version. The CLI does not build, upload, or host wheels — publishers run their own build + upload (`uv build` + `uv publish`, `twine`, `pypa/gh-action-pypi-publish`, ...) first, then call this command with the resulting URL. Reads provider identity (`provider`, `package`) and catalog metadata (`display_name`, `description`, `icon_url`, `tags`) from `[tool.pragma]` in the supplied pyproject.toml. SHA-256 is computed from the wheel URL when `--sha256` is omitted
- `pragma providers deploy <id> [version]` - Deploy a specific version
- `pragma providers status <id>` - Check deployment status
- `pragma providers delete <id> [--cascade]` - Delete a provider

### Configuration
- `pragma config current-context` - Show current context
- `pragma config get-contexts` - List available contexts
- `pragma config use-context <name>` - Switch context
- `pragma config set-context <name> --api-url <url>` - Create/update context
- `pragma config delete-context <name>` - Delete context

### Authentication
- `pragma auth login` - Authenticate (opens browser)
- `pragma auth whoami` - Show current user
- `pragma auth logout` - Clear credentials

### Operations
- `pragma ops dead-letter list` - List failed events
- `pragma ops dead-letter show <id>` - Show event details
- `pragma ops dead-letter retry <id> [--all]` - Retry failed event(s)
- `pragma ops dead-letter delete <id> [--all]` - Delete failed event(s)

## Development

Always use `task` commands:

| Command | Purpose |
|---------|---------|
| `task format` | Format with ruff |
| `task check` | Lint + type check |

## Patterns

- Typer for command definitions
- Rich console for formatted output
- SDK client for all API operations
- YAML for resource definitions

## Publishing to PyPI

Package: `pragmatiks-cli` on [PyPI](https://pypi.org/project/pragmatiks-cli/)

**Versioning** (commitizen):
```bash
cz bump              # Bump version based on conventional commits
```

**Publishing**:
```bash
uv build             # Build wheel and sdist
uv publish           # Publish to PyPI (requires PYPI_TOKEN)
```

**Tag format**: `v{version}` (e.g., `v0.12.2`)
