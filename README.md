<p align="center">
  <img src="https://raw.githubusercontent.com/pragmatiks/pragmatiks/main/assets/pragmatiks_brand/wordmark/tech_readout.png" alt="Pragmatiks" width="800">
</p>

# Pragmatiks CLI

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/pragmatiks/cli)
[![PyPI version](https://img.shields.io/pypi/v/pragmatiks-cli.svg)](https://pypi.org/project/pragmatiks-cli/)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

**[Documentation](https://docs.pragmatiks.io/cli/overview)** | **[SDK](https://github.com/pragmatiks/sdk)** | **[Providers](https://github.com/pragmatiks/providers)**

Command-line interface for managing Pragmatiks resources.

## Quick Start

```bash
# Authenticate
pragma auth login

# Apply a resource
pragma resources apply bucket.yaml

# Check status
pragma resources get gcp/storage my-bucket
```

## Installation

```bash
pip install pragmatiks-cli
```

Or with uv:

```bash
uv add pragmatiks-cli
```

Enable shell completion for intelligent command-line assistance:

```bash
pragma --install-completion
```

## Features

- **Declarative Resources** - Apply, get, and delete resources with YAML manifests
- **Smart Completion** - Tab completion for providers, resources, and names
- **Provider Development** - Initialize, sync, and deploy custom providers
- **Multi-document Support** - Apply multiple resources from a single YAML file

## Resource Management

### Apply Resources

```yaml
# bucket.yaml
provider: gcp
resource: storage
name: my-bucket
config:
  location: US
  storage_class: STANDARD
```

```bash
# Apply from file
pragma resources apply bucket.yaml

# Apply multiple files
pragma resources apply *.yaml

# Apply with pending flag to execute immediately
pragma resources apply --pending bucket.yaml
```

### List and Get Resources

```bash
# List all resources
pragma resources list

# Filter by provider
pragma resources list --provider gcp

# Filter by resource type
pragma resources list --resource storage

# Get specific resource
pragma resources get gcp/storage my-bucket
```

### Delete Resources

```bash
pragma resources delete gcp/storage my-bucket
```

## Provider Development

Build and deploy custom providers:

```bash
# Initialize a new provider project
pragma provider init mycompany

# Sync resource schemas with the platform
pragma provider sync

# Build and deploy
pragma provider push --deploy
```

## Authentication

```bash
# Login (opens browser)
pragma auth login

# Check current user
pragma auth whoami

# Logout
pragma auth logout
```

## Configuration

Set environment variables to configure the CLI:

```bash
export PRAGMA_API_URL=https://api.pragmatiks.io
export PRAGMA_AUTH_TOKEN=sk_...
```

## Command Reference

### Resources

| Command | Description |
|---------|-------------|
| `pragma resources list` | List resources with optional filters |
| `pragma resources get <provider/resource> <name>` | Get a specific resource |
| `pragma resources apply <file>` | Apply resources from YAML |
| `pragma resources delete <provider/resource> <name>` | Delete a resource |

### Providers

| Command | Description |
|---------|-------------|
| `pragma provider init <name>` | Initialize a new provider project |
| `pragma provider sync` | Sync resource schemas with platform |
| `pragma provider push` | Build and push provider image |
| `pragma provider push --deploy` | Build, push, and deploy |

### Authentication

| Command | Description |
|---------|-------------|
| `pragma auth login` | Authenticate with the platform |
| `pragma auth whoami` | Show current user |
| `pragma auth logout` | Clear credentials |

### Operations

| Command | Description |
|---------|-------------|
| `pragma ops dead-letter list` | List failed events |
| `pragma ops dead-letter retry <id>` | Retry a failed event |

## Development

```bash
# Run tests
task cli:test

# Format code
task cli:format

# Type check and lint
task cli:check
```

## License

MIT
