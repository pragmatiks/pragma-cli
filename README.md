<p align="center">
  <img src="assets/wordmark.png" alt="Pragmatiks" width="800">
</p>

# Pragma CLI

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/pragmatiks/pragma-cli)
[![PyPI version](https://img.shields.io/pypi/v/pragmatiks-cli.svg)](https://pypi.org/project/pragmatiks-cli/)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**[Documentation](https://docs.pragmatiks.io/cli/overview)** | **[SDK](https://github.com/pragmatiks/pragma-sdk)** | **[Providers](https://github.com/pragmatiks/pragma-providers)**

Command-line interface for managing Pragmatiks resources.

## Installation

```bash
pip install pragmatiks-cli
```

Enable shell completion:

```bash
pragma --install-completion
```

## Quick Start

```bash
# Authenticate
pragma auth login

# Apply a resource from YAML
pragma resources apply bucket.yaml

# Check status
pragma resources get pragmatiks/gcp/secret/my-secret
```

## Commands

Exit codes and output streams are listed in `pragma --help`: errors and warnings go to stderr, command output (including `-o json`) to stdout.

### Resources

| Command | Description |
|---------|-------------|
| `pragma resources list` | List resources with optional filters |
| `pragma resources schemas` | List available resource schemas |
| `pragma resources get <org/provider/resource[/name]>` | Get resource(s) by type or full ID |
| `pragma resources describe <org/provider/resource/name>` | Show detailed resource info |
| `pragma resources apply <file>` | Apply resources from YAML |
| `pragma resources delete <org/provider/resource/name> \| -f <file>` | Delete a resource |
| `pragma resources deactivate <org/provider/resource/name> \| -f <file>` | Deactivate a resource |
| `pragma resources tags list/add/remove` | Manage resource tags |

### Providers

| Command | Description |
|---------|-------------|
| `pragma providers list` | List deployed providers |
| `pragma providers init <name>` | Initialize a new provider project |
| `pragma providers update [project-directory]` | Update project from template |
| `pragma providers publish [project-directory \| --wheel <path>] [--changelog <file>]` | Build the wheel via `uv build` into `<project-directory>/dist` (or take a prebuilt one), upload it, and wait until your organization's provider host admits it (`published`) or refuses it (`failed`) |
| `pragma providers versions <name>` | List a provider's versions with their status; a version still being admitted shows `admitting` |
| `pragma providers deploy <name> [--version <v>]` | Restart an installed provider at its installed version; `--version` must name a published version and does not change the installed one (use `upgrade` or `downgrade`) |
| `pragma providers status <name> [-o json\|yaml]` | Check deployment status |
| `pragma providers delete <name> [--yes]` | Delete a provider from the catalog (admins of the owning organization only); a provider that is still installed is refused |

### Configuration

| Command | Description |
|---------|-------------|
| `pragma config current-context` | Show current context |
| `pragma config get-contexts` | List available contexts |
| `pragma config use-context <name>` | Switch context |
| `pragma config set-context <name> --api-url <url>` | Create/update context |
| `pragma config delete-context <name>` | Delete context |

### Authentication

| Command | Description |
|---------|-------------|
| `pragma auth login` | Authenticate (opens browser) |
| `pragma auth whoami` | Show current user |
| `pragma auth logout` | Clear credentials |

### Operations

| Command | Description |
|---------|-------------|
| `pragma ops dead-letter list` | List failed events |
| `pragma ops dead-letter show <id>` | Show event details |
| `pragma ops dead-letter retry <id> \| --all` | Retry failed event(s) |
| `pragma ops dead-letter delete <id> \| --all \| --provider <name>` | Delete failed event(s) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PRAGMA_CONTEXT` | Override current context |
| `PRAGMA_AUTH_TOKEN` | Authentication token |
| `PRAGMA_AUTH_TOKEN_<CONTEXT>` | Context-specific token |

## License

MIT
