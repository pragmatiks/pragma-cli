# pragma-cli

**Command-line interface for Pragmatiks** - Manage resources with powerful auto-completion.

```bash
# Install
pip install pragma-cli

# Quick start
pragma-cli resources list-groups
pragma-cli resources get Database.apps.pragmatiks.io/v1
pragma-cli resources apply manifest.yaml
```

## Features

- 🎯 **Declarative commands** - Apply, get, delete resources with familiar patterns
- ( **Smart auto-completion** - Dynamic completions for groups, kinds, versions, and resource names
- =� **YAML manifest support** - Multi-document files with declarative resource management
- <� **Type-safe** - Full type hints with modern Python patterns
- =' **Configurable** - Environment variables for API endpoint and timeouts

## Installation

```bash
# From PyPI (when published)
pip install pragma-cli

# From source
cd packages/cli
pip install -e .

# Enable shell completion
pragma-cli --install-completion
```

## Quick Start

### List resources

```bash
# Discover available resource types
pragma-cli resources list-groups
pragma-cli resources list-kinds apps.pragmatiks.io
pragma-cli resources list-versions apps.pragmatiks.io Database

# Get resources
pragma-cli resources get Database.apps.pragmatiks.io/v1
pragma-cli resources get Database.apps.pragmatiks.io/v1 my-database
```

### Apply resources

```yaml
# database.yaml
group: apps.pragmatiks.io
version: v1
kind: Database
metadata:
  name: my-postgres
spec:
  engine: postgresql
  replicas: 3
```

```bash
# Apply from file
pragma-cli resources apply database.yaml

# Apply multiple files
pragma-cli resources apply *.yaml
```

### Delete resources

```bash
pragma-cli resources delete Database.apps.pragmatiks.io/v1 my-database
```

## Configuration

Set environment variables to configure the CLI:

```bash
export PRAGMA_API_URL=https://api.pragma.example.com
export PRAGMA_CLIENT_TIMEOUT=30
```

## Commands

### Resource Discovery

| Command | Description |
|---------|-------------|
| `list-groups` | List all resource groups |
| `list-kinds [GROUP]` | List kinds in group(s) |
| `list-versions [GROUP] [KIND]` | List versions for kind(s) |

### Resource Management

| Command | Description |
|---------|-------------|
| `get RESOURCE_ID [NAME]` | Get resource(s) |
| `apply FILE...` | Apply resources from YAML file(s) |
| `delete RESOURCE_ID NAME` | Delete a resource |
| `unregister RESOURCE_ID` | Unregister a resource definition |

**Resource ID Format**: `KIND.GROUP/VERSION` (e.g., `Database.apps.pragmatiks.io/v1`)

## Auto-Completion

Install shell completion for intelligent command-line assistance:

```bash
# Bash
pragma-cli --install-completion bash
source ~/.bashrc

# Zsh
pragma-cli --install-completion zsh
source ~/.zshrc

# Fish
pragma-cli --install-completion fish
```

Once enabled, tab completion provides:
- Available groups, kinds, and versions
- Existing resource names
- Partial matching for faster navigation

```bash
pragma-cli resources get Data<TAB>
# � Database.apps.pragmatiks.io/v1

pragma-cli resources get Database.apps.pragmatiks.io/v1 my-<TAB>
# � my-postgres
# � my-redis
```

## YAML Manifests

### Single Resource

```yaml
group: apps.pragmatiks.io
version: v1
kind: Database
metadata:
  name: my-database
  labels:
    environment: production
spec:
  engine: postgresql
  version: "15"
  replicas: 3
```

### Multiple Resources

Use `---` to separate multiple resources in one file:

```yaml
---
group: apps.pragmatiks.io
version: v1
kind: Database
metadata:
  name: prod-db
spec:
  replicas: 3
---
group: apps.pragmatiks.io
version: v1
kind: Database
metadata:
  name: staging-db
spec:
  replicas: 1
```

## Development

```bash
# Setup
cd packages/cli
uv sync --dev

# Format code
task cli:format

# Run tests
task cli:test

# Type check
task cli:check
```

## Architecture

The CLI is a thin layer over the Python SDK, delegating all business logic to the API:

```
pragma-cli � pragmatiks-sdk � pragma-api
```

This design ensures:
- **Separation of concerns** - CLI handles UX, SDK handles HTTP, API handles business logic
- **Testability** - Easy to mock and test CLI commands
- **Consistency** - Same behavior across all clients (CLI, SDK, direct API)

## Requirements

- Python 3.13+
- Access to Pragmatiks API

## License

See repository root for license information.

## Links

- **Documentation**: `/packages/cli/CLAUDE.md`
- **Python SDK**: `/packages/python-sdk`
- **API Server**: `/packages/api`
