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
pragma resources get gcp/storage my-bucket
```

## Commands

### Resources

| Command | Description |
|---------|-------------|
| `pragma resources list` | List resources with optional filters |
| `pragma resources types` | List available resource types |
| `pragma resources get <type> [name]` | Get resource(s) by type |
| `pragma resources describe <type> <name>` | Show detailed resource info |
| `pragma resources apply <file>` | Apply resources from YAML |
| `pragma resources delete <type> <name>` | Delete a resource |
| `pragma resources tags list/add/remove` | Manage resource tags |

### Providers

| Command | Description |
|---------|-------------|
| `pragma providers list` | List deployed providers |
| `pragma providers init <name>` | Initialize a new provider project |
| `pragma providers update` | Update project from template |
| `pragma providers push [--deploy]` | Build and push (optionally deploy) |
| `pragma providers deploy <id> [version]` | Deploy a specific version |
| `pragma providers status <id>` | Check deployment status |
| `pragma providers builds <id>` | List build history |
| `pragma providers delete <id> [--cascade]` | Delete a provider |

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
| `pragma ops dead-letter retry <id> [--all]` | Retry failed event(s) |
| `pragma ops dead-letter delete <id> [--all]` | Delete failed event(s) |

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PRAGMA_CONTEXT` | Override current context |
| `PRAGMA_AUTH_TOKEN` | Authentication token |
| `PRAGMA_AUTH_TOKEN_<CONTEXT>` | Context-specific token |

## License

MIT
