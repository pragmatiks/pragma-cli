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

## Development

Always use `task` commands:

| Command | Purpose |
|---------|---------|
| `task format` | Format with ruff |
| `task check` | Lint + type check |
| `task test` | Run integration tests |

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

## Engineering Principles

Canonical engineering rules for all Pragmatiks code in this repository. Workers (developers and reviewers) must follow these in every dispatch. Reviewers must check each PR against this list and produce one finding per violation.

### Scope

Applies to all code in this repository. Some principles only apply to one language or stack — flagged where relevant.

This section is the ground truth for engineering principles in this repository. The same text is embedded in every Pragmatiks subrepo's `CLAUDE.md`. When a principle changes, every embed must be updated in lockstep and the corresponding `pragmatiks-lint` / `@pragmatiks/lint` rule versions bumped.

### Enforcement layers

| Layer | What | Where |
|---|---|---|
| 1. Style + standard smells | `ruff` (Python), `eslint` (TS) with curated rule set | per-repo `task check` / `pnpm lint` |
| 2. Complexity gating | `radon` / `xenon` (Python), `eslint-plugin-sonarjs/cognitive-complexity` (TS) | CI fail on regression |
| 3. Pragmatiks-specific rules | `semgrep` ruleset (cross-language) + custom scripts | shared via `pragmatiks-lint` (PyPI) and `@pragmatiks/lint` (npm) |

If a principle has a programmatic check, the reviewer relies on the tool. If the principle is judgment-based, the reviewer comments with `⚠️` severity.

---

### 1. YAGNI — You Aren't Gonna Need It

Do not add features, abstractions, or configuration for hypothetical future needs. No premature generalization, no speculative interfaces, no "we might need this later" code.

**Programmatic check**:
- Python: `vulture` flags unused functions and dead branches.
- TS: `knip` flags unused exports, files, and dependencies.

**Reviewer hint**: flag any new abstraction layer not justified by current callers.

### 2. KISS — Keep It Simple

Prefer the simplest implementation that works. Three similar lines beat a premature abstraction. Inline the obvious; abstract only when a third caller appears.

**Programmatic check**:
- Python: `ruff C901` (cyclomatic complexity threshold).
- TS: `eslint-plugin-sonarjs/cognitive-complexity`.

**Reviewer hint**: extract-method PR? Verify there are at least three callers in the diff or repo.

### 3. Boy Scout Rule

Leave the file better than you found it. Small adjacent cleanup (rename, move, dead-line removal) is welcome when touching a file. Do not pile in unrelated refactors.

**Programmatic check**: none — judgment.

**Reviewer hint**: if a PR touches no nearby messy code, no penalty. If it adds new mess, block.

### 4. Open–Closed Principle

Modules should be open for extension and closed for modification. New behavior added by adding code, not by modifying existing tested code paths.

**Programmatic check**: none — judgment.

**Reviewer hint**: if a PR modifies a stable public interface or stable internal contract to add a feature that could have been added via a new function/method, request an alternative.

### 5. Single Responsibility Principle

Each function, method, class, and module should have one reason to change. If you cannot describe what a unit does without saying "and" or "or", split it.

**Programmatic check**:
- Function names with `_and_`, `_or_`, `And`, `Or` flagged by `pra-srp-and-or-name` semgrep rule.
- Function size: `eslint max-lines-per-function`, `max-statements`, `max-depth`. Python: `ruff PLR0915` (too many statements), `PLR0912` (too many branches).
- Cognitive complexity from #2.

**Reviewer hint**: if a function name reads as compound, splitting is mandatory.

### 6. Always Use Dependency Injection

Pass dependencies in via constructor / function arguments. Do not instantiate concrete services inside business logic. Wire the graph at the application boundary (FastAPI lifespan, CLI entry point, Next.js server boundary, test harness).

**Programmatic check**:
- `pra-no-inline-instantiation` semgrep rule (heuristic): flags concrete-class instantiation inside non-boundary modules. False positives expected — allowlist module paths (`main.py`, `app.py`, `lifespan.py`, `entry.ts`, etc.).

**Reviewer hint**: a class that constructs an `httpx.AsyncClient` inside `__init__` is wrong; it should accept one as a constructor arg.

### 7. I/O Prefix Discipline

Function/method names starting with `get_`, `fetch_`, `retrieve_`, `load_`, `save_`, `read_`, `write_`, `query_` must perform I/O (network, disk, database, IPC). Pure-computation functions must use neutral names (`compute_*`, `build_*`, `derive_*`, `format_*`, `parse_*`).

**Programmatic check**:
- `pra-io-prefix-mismatch` semgrep rule: flags `get_*` / `fetch_*` / `retrieve_*` functions whose body contains no `await`, no httpx/requests/db client call, no file open. Heuristic; allowlist via decorator (`@no_io`) or function tag.

**Reviewer hint**: a `get_user_id_from_token(token: str) -> str` that just decodes a JWT must be renamed `parse_user_id_from_token` or `extract_user_id`.

### 8. Twelve-Factor App

Configuration via environment variables only. Read environment at the application boundary, never deep in business logic. No credentials, URLs, or behavior flags hard-coded. Stateless processes. Treat backing services (DB, cache, queue) as attached resources via URLs.

**Programmatic check**:
- `pra-env-read-deep` semgrep rule: flags `os.environ` / `os.getenv` / `process.env` reads outside designated boundary modules.
- `pra-no-hardcoded-secrets` semgrep rule: flags string literals matching common credential patterns (`sk-`, `AKIA`, etc.).

**Reviewer hint**: env reads should live in a settings module (Python: `Settings` Pydantic class; TS: a single `env.ts` boundary file).

### 9. Clean Code (default)

When unsure, follow Clean Code: meaningful names, small functions, single level of abstraction per function, no flag arguments, fewer arguments over more, prefer pure functions, fail fast at boundaries.

**Programmatic check**: combination of `ruff`, `eslint`, `eslint-plugin-sonarjs`, `eslint-plugin-unicorn`.

**Reviewer hint**: if a function takes a boolean flag that switches behavior, flag (split into two functions).

### 10. No Comments

The code must be self-explanatory. Do not write comments. Exceptions:

- Public docstrings on library APIs (`pragma-sdk` public surface).
- A single-line WHY comment for a non-obvious workaround, hidden constraint, or subtle invariant. Removing it would confuse a future reader.

Forbidden: block comments restating what the code does; section dividers; commented-out code; "added for X" / "used by Y" trail comments; multi-line docstrings on private internals; planning comments left in source (`# TODO: refactor later`).

**Programmatic check**:
- `pra-no-block-comments` semgrep rule: flags multi-line `#` blocks in Python and `/* ... */` blocks in TS that are not docstrings.
- `pra-no-todo-comments` semgrep rule: flags `# TODO` / `// TODO` / `/* TODO */`.
- Existing custom script for comment ban (to migrate to semgrep).

**Reviewer hint**: every comment in the diff must be justifiable as WHY. Otherwise: delete and rename code instead.

### 11. Semantic Names — No Abbreviations

Identifiers must use full words. No `k8s`, `cfg`, `db`, `req`, `res`, `ctx`, `tmp`, `pkg`, `svc`, `mgr`, `repo`, `usr`, `pwd`, `idx`, `cnt`, `msg`, `err`, etc. Use `kubernetes`, `config`, `database`, `request`, `response`, `context`, `temporary`, `package`, `service`, `manager`, `repository`, `user`, `password`, `index`, `count`, `message`, `error`.

**Allowlist** (industry-standard exceptions):
- `id`, `url`, `uri`, `api`, `cli`, `sdk`, `os`, `io`, `ip`, `tls`, `ssl`, `jwt`, `json`, `yaml`, `html`, `css`, `dom`, `ast`, `gpu`, `cpu`, `ram`, `vm`.
- React-specific: `props`, `ref`, `e` (event handler param).
- Python-specific: `cls`, `self`, `kwargs`, `args`.

**Programmatic check**:
- `eslint-plugin-unicorn/prevent-abbreviations` (TS) — direct fit, with allowlist config.
- `pra-no-abbreviations` semgrep rule (Python) — regex matching forbidden short identifiers, with allowlist.

**Reviewer hint**: `db`, `cfg`, `k8s` in any new code = blocker.

### 12. Compound Names Violate SRP

If a function or method name contains `and`, `or`, `then`, or describes multiple actions, it violates SRP and must be split. Same applies to class names and module names. Examples to forbid: `validate_and_save_user`, `fetch_or_create_session`, `build_and_publish_wheel`.

**Programmatic check**:
- `pra-srp-and-or-name` semgrep rule (cross-language).

**Reviewer hint**: blocker — propose the split inline.

---

### Reviewer protocol

Every reviewer dispatch must:

1. Run `pragmatiks-lint check` (programmatic findings) before reading the diff.
2. Read the diff.
3. For each principle, produce findings as:

   ```
   path:line: <emoji> <severity>: <principle #N> <problem>. <fix>.
   ```

   Severities: 🚨 blocker · ⚠️ important · 💡 nit.

4. Final verdict: `APPROVE` / `APPROVE_WITH_NITS` / `REQUEST_CHANGES`.

A reviewer who fails to invoke programmatic tooling but only eyeballs the diff is incomplete and should be re-run.

### Developer protocol

Every developer dispatch must:

1. Read this `## Engineering Principles` section before starting.
2. Run `pragmatiks-lint check` locally before opening a PR.
3. Resolve all 🚨 blockers from the lint pack. ⚠️ findings: address or justify in PR body.
4. State principle compliance in the callback to the supervisor.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- ALWAYS read graphify-out/GRAPH_REPORT.md before reading any source files, running grep/glob searches, or answering codebase questions. The graph is your primary map of the codebase.
- IF graphify-out/wiki/index.md EXISTS, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
