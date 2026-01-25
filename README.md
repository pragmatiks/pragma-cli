<p align="center">
  <img src="assets/wordmark.png" alt="Pragma-OS" width="800">
</p>

# Pragma CLI

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/pragmatiks/pragma-cli)
[![PyPI version](https://img.shields.io/pypi/v/pragmatiks-cli.svg)](https://pypi.org/project/pragmatiks-cli/)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

**[Documentation](https://docs.pragmatiks.io/cli/overview)** | **[SDK](https://github.com/pragmatiks/pragma-sdk)** | **[Providers](https://github.com/pragmatiks/pragma-providers)**

Command-line interface for managing pragma-os resources.

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

# Apply without deploying (keep as draft)
pragma resources apply --draft bucket.yaml
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
pragma providers init mycompany

# Update project from latest template
pragma providers update

# Build and push (without deploying)
pragma providers push

# Build, push, and deploy
pragma providers push --deploy

# Stream build logs
pragma providers push --logs --deploy
```

### Managing Deployed Providers

```bash
# List all deployed providers
pragma providers list

# Check deployment status
pragma providers status mycompany

# View build history
pragma providers builds mycompany

# Deploy a specific version
pragma providers deploy mycompany 20250115.120000

# Delete a provider (fails if resources exist)
pragma providers delete mycompany

# Delete provider and all its resources
pragma providers delete mycompany --cascade
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

The CLI uses **contexts** to manage connections to different environments (production, staging, local).

### Context Management

```bash
# Show current context
pragma config current-context

# List available contexts
pragma config get-contexts

# Switch context
pragma config use-context staging

# Create a new context
pragma config set-context staging --api-url https://api.staging.pragmatiks.io

# Delete a context
pragma config delete-context old-context
```

Configuration is stored in `~/.config/pragma/config`.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `PRAGMA_CONTEXT` | Override the current context |
| `PRAGMA_AUTH_TOKEN` | Authentication token (overrides stored credentials) |
| `PRAGMA_AUTH_TOKEN_<CONTEXT>` | Context-specific token (e.g., `PRAGMA_AUTH_TOKEN_PRODUCTION`) |

**Token Discovery Precedence:**
1. `--token` flag (explicit override)
2. `PRAGMA_AUTH_TOKEN_<CONTEXT>` environment variable
3. `PRAGMA_AUTH_TOKEN` environment variable
4. `~/.config/pragma/credentials` file (from `pragma auth login`)

Example:
```bash
# Use a specific context
PRAGMA_CONTEXT=staging pragma resources list

# Use a token directly
PRAGMA_AUTH_TOKEN=sk_... pragma resources list
```

## Command Reference

### Resources

| Command | Description |
|---------|-------------|
| `pragma resources list` | List resources with optional filters |
| `pragma resources types` | List available resource types from providers |
| `pragma resources get <provider/resource> [name]` | Get resource(s) by type, optionally by name |
| `pragma resources describe <provider/resource> <name>` | Show detailed resource info (config, outputs, deps) |
| `pragma resources apply <file>` | Apply resources from YAML |
| `pragma resources delete <provider/resource> <name>` | Delete a resource |
| `pragma resources tags list <provider/resource> <name>` | List tags for a resource |
| `pragma resources tags add <provider/resource> <name> --tag <tag>` | Add tags to a resource |
| `pragma resources tags remove <provider/resource> <name> --tag <tag>` | Remove tags from a resource |

### Providers

| Command | Description |
|---------|-------------|
| `pragma providers list` | List all deployed providers |
| `pragma providers init <name>` | Initialize a new provider project |
| `pragma providers update [dir]` | Update provider project from template |
| `pragma providers push` | Build and push provider image |
| `pragma providers push --deploy` | Build, push, and deploy |
| `pragma providers deploy <provider-id> [version]` | Deploy a provider to a specific version |
| `pragma providers status <provider-id>` | Check deployment status |
| `pragma providers builds <provider-id>` | List build history |
| `pragma providers delete <provider-id>` | Delete a provider (use --cascade for resources) |

### Configuration

| Command | Description |
|---------|-------------|
| `pragma config current-context` | Show current context |
| `pragma config get-contexts` | List available contexts |
| `pragma config use-context <name>` | Switch to a different context |
| `pragma config set-context <name> --api-url <url>` | Create or update a context |
| `pragma config delete-context <name>` | Delete a context |

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
| `pragma ops dead-letter show <id>` | Show detailed event information |
| `pragma ops dead-letter retry <id>` | Retry a failed event |
| `pragma ops dead-letter retry --all` | Retry all failed events |
| `pragma ops dead-letter delete <id>` | Delete a failed event |
| `pragma ops dead-letter delete --all` | Delete all failed events |

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
