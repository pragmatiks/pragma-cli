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
- `pragma providers publish [project-dir] [--wheel <path>] [--changelog <file>]` - Publish a new provider version: builds the wheel with `uv build --wheel` unless `--wheel` points at a prebuilt one, uploads it to `POST /providers/publish` (202, version `pending`), then polls `GET /providers/{name}/versions/{version}` every 2 s until the organization's provider host admits it (`published`) or fails it (`failed`, prints the version's `error_message`, exit 1). The wheel alone identifies the version: its `pragma.provider` entry point names the provider and its metadata carries the version; nothing is read from pyproject.toml. The watch lasts at least 150 s from the upload and, once the host has taken the version, until the version's `operation_deadline_at` plus 30 s. A watch timeout exits 1 with "has not finished" and points to `pragma providers versions`; an HTTP error while polling prints the API's message and the same pointer; a transport error while polling prints what stopped the watch (API unreachable, timeout, dropped connection) and the same pointer. Refusals print the API's message with Rich markup escaped; a 503 whose body carries no message prints the upload-failed message; an upload interrupted after connecting prints what broke it off (timeout, dropped connection); a connection failure goes to the global handler
- `pragma providers versions <name> [-o json|yaml]` - List a provider's versions with status (`pending` shows `admitting`) and the failure message of failed versions
- `pragma providers deploy <id> [version]` - Deploy a specific version
- `pragma providers status <id>` - Check deployment status
- `pragma providers delete <id> [--yes]` - Delete a provider from the catalog (admins of the owning organization only)

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

## Evidence-based development

Fact-check with live lookups before writing code. The cost of a query is far less than a debugging cycle on stale assumptions.

**Use internal knowledge for**: programming skill, language fluency, algorithms, design patterns, general engineering judgment, code comprehension.

**Always look up**: library API details, framework feature lists, version-specific behavior, current best practice, recent changes — anything where being wrong costs time.

If you find yourself thinking "I'm pretty sure this library does X" or "the API was Y last time I used it" — STOP and query. Training data is months to years out of date; library APIs change.

### Lookup routing

- **exa-search** (skill, `~/.claude/skills/exa-search`) — live web search via the Exa Search API over raw HTTP. Use for library / framework / SDK docs, release notes, blog posts, GitHub issues — any external fact-check. Requires `EXA_API_KEY` in the environment.
- **exa-contents** (skill, `~/.claude/skills/exa-contents`) — extract text, highlights, or summaries from known URLs via the Exa Contents API over raw HTTP. Invoke the `exa-contents` skill when you already have the URL and need its content. Requires `EXA_API_KEY` in the environment.
- **claude-mem** (`mcp__plugin_claude-mem_mcp-search__smart_search`, `mcp__plugin_claude-mem_mcp-search__search`, `mcp__plugin_claude-mem_mcp-search__get_observations`) — search prior session memory. Use when working in an area that has prior session decisions. Cite observation IDs.

## Solution preference order

Before writing custom code, work through these in order:

1. **Reuse what is already in the project.** Check `pyproject.toml` and `uv.lock` for an existing dependency that solves the problem. Grep the codebase for prior patterns. The cheapest correct answer is already on disk.
2. **Adopt an established external library.** Look for popular, state-of-the-art, actively maintained libraries. Verify GitHub stars / last release / open critical issues / maintainer reputation. A boring widely-used library beats a custom implementation.
3. **Custom code, only as a last resort.** Only after 1 and 2 fail should you write it from scratch.

Prefer the simplest solution that meets the requirement. Avoid abstractions for hypothetical future needs.

## New dependency proposal (BLOCKING)

If your work requires adding a new top-level dependency, STOP before installing it.

1. **Research candidates.** For each viable candidate, record: name, version, license, maintainer (individual / org / foundation) and their track record, last release date and release frequency, popularity signals (GitHub stars, downloads, ecosystem use), known issues affecting us (security advisories, deprecated APIs), fit and trade-offs, at least one realistic alternative considered.
2. **Present findings to the user** with a one-sentence recommendation. Do NOT install the dependency or write code that uses it.
3. **Wait for approval.** Install only after explicit user approval (`uv add` for runtime, `uv add --dev` for tooling). If rejected, revisit the solution preference order.

This applies to any new top-level dependency. It does NOT apply to transitive dependencies pulled in by existing direct deps. Also remember: this is a CLI consumed by end users — a new top-level dependency directly bloats their install footprint.

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

Prefer the simplest implementation that works. Inline the obvious; abstract on the second caller — duplication is a smell, not a feature.

**Programmatic check**:
- Python: `ruff C901` (cyclomatic complexity threshold).
- TS: `eslint-plugin-sonarjs/cognitive-complexity`.

**Reviewer hint**: extract-method PR? Verify there are at least two callers in the diff or repo.

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

The code must be self-explanatory. Do not write comments — none at all, WHY comments included (amended 2026-08-31; they metastasized). Docstrings are documentation, not comments; this rule does not restrict them.

- Google-style docstrings are **required** on every module, class, function, and method (not only the `pragma-sdk` public surface). Describe purpose, arguments, returns, and raised exceptions; no noise padding.
- Docstrings address the **caller**: contract, constraints, how to use. Not implementation narration, not change history, not a log of what the function used to do. A workaround, hidden constraint, or subtle invariant worth recording goes in the owning function's docstring (a `Note:` section when it needs a home), or the code gets restructured until it needs no explanation.
- Tool directives that must sit on the flagged line (`# noqa`, `eslint-disable`, `# type: ignore`) stay, bare — the justification lives in the docstring, not in appended prose.

Forbidden: ALL non-docstring comments — WHY comments, block comments restating what the code does, section dividers, commented-out code, "added for X" / "used by Y" trail comments, planning comments (`# TODO: refactor later`).

**Programmatic check**:
- `pra-no-block-comments` semgrep rule: flags multi-line `#` blocks in Python and `/* ... */` blocks in TS that are not docstrings.
- `pra-no-todo-comments` semgrep rule: flags `# TODO` / `// TODO` / `/* TODO */`.
- Existing custom script for comment ban (to migrate to semgrep). Needs extension: flag single-line comments too (WHY ban).

**Reviewer hint**: every non-directive comment in the diff is a finding. Rationale that matters moves to the nearest docstring; rationale that does not matter dies. Docstrings that read like implementation walkthroughs or changelogs are findings too.

### 11. Semantic Names — No Abbreviations

Identifiers use full words. No `k8s`, `cfg`, `db`, `req`, `res`, `ctx`, `tmp`, `pkg`, `svc`, `mgr`, `repo`, `usr`, `pwd`, `idx`, `cnt`, `msg`, `err`, etc. Use `kubernetes`, `config`, `database`, `request`, `response`, `context`, `temporary`, `package`, `service`, `manager`, `repository`, `user`, `password`, `index`, `count`, `message`, `error`.

**Allowlist** (industry-standard exceptions):
- `id`, `url`, `uri`, `api`, `cli`, `sdk`, `os`, `io`, `ip`, `tls`, `ssl`, `jwt`, `json`, `yaml`, `html`, `css`, `dom`, `ast`, `gpu`, `cpu`, `ram`, `vm`.
- React-specific: `props`, `ref`, `e` (event handler param).
- Python-specific: `cls`, `self`, `kwargs`, `args`.

**Vocabulary**: one concept, one term, at every layer (CLI, SDK, HTTP, service, errors, log markers, states, UI copy). New name for concept that already has a term = violation. Before coining a word, grep for it: already carries a meaning here → pick another; absent from codebase while an established phrasing exists → use the established phrasing. Established terms:

| Concept | Term | Banned synonyms / misuse |
|---|---|---|
| Delete the row (runs provider teardown first when provisioned), ends `DELETED` | `delete` | `remove` |
| Run provider teardown, keep the row, back to `DRAFT` | `deactivate` | — |
| Resource provider (package reconciling external infrastructure) | `provider` | generic dependency-injection suffix (`SupervisorProvider`); use `Accessor` for the callable type, `current_<thing>` for the argument |
| Platform function provider code calls back into (`apply_resource`) | `callback` | `handler` |
| Function the platform invokes to process an event or message: provider lifecycle methods (`on_create` … `on_observe`) and runtime-side message handlers | `handler` | `callback` |
| Worker converging resources toward desired state | `Reconciler` | `StuckReconciler`, qualifiers describing one behavior |
| Moving a dependent to `WAITING` | "set to waiting" (`set_dependent_waiting`) | `park` |

Provider event `on_delete` stays (provider deletes external object).

**User-facing surfaces** (HTTP API, OpenAPI, SDK, CLI, web UI, docs):
- API mirrors CLI: route noun + verb match CLI command (`pragma providers publish` ↔ `POST /providers/publish`), and reverse. Mismatch = finding, not REST-purist path.
- No internal infrastructure terms (Kubernetes, pod, namespace, NATS, JetStream, SurrealDB internals) or vendor names (Clerk) in route summaries, descriptions, schema field docs, error strings, UI copy. OpenAPI spec feeds public docs verbatim — fix at source.
- Failure reasons written to a resource's terminal `FAILED` state: no blame ("provider failed to…"), no internal jargon (queue, outbox, dispatch, event id), end with shared `RECOVERY_GUIDANCE` constant — reuse existing reason constants, never per-path prose. Other failures (publication, install) end with the recovery wording their decision doc defines.

**Programmatic check**:
- `eslint-plugin-unicorn/prevent-abbreviations` (TS) — direct fit, allowlist config.
- `pra-no-abbreviations` semgrep rule (Python) — regex matching forbidden short identifiers, allowlist.
- `packages/api/scripts/check_openapi_spec.py` (`task api:check`) — flags infrastructure terms in generated OpenAPI spec. Kubernetes, pod, namespace, and vendor names not in term list yet — reviewer scans for them.

**Reviewer hint**: `db`, `cfg`, `k8s` in any new code = blocker. Banned synonym from vocabulary table on any surface = blocker.

### 12. Compound Names Violate SRP

If a function or method name contains `and`, `or`, `then`, or describes multiple actions, it violates SRP and must be split. Same applies to class names and module names. Examples to forbid: `validate_and_save_user`, `fetch_or_create_session`, `build_and_publish_wheel`.

**Programmatic check**:
- `pra-srp-and-or-name` semgrep rule (cross-language).

**Reviewer hint**: blocker — propose the split inline.

### 13. Leading Underscores Are Deliberate, Not Habitual

Default every attribute, method, function, and variable to a plain public name. Encapsulation comes from module boundaries and clear naming, not reflexive underscore prefixes. A leading underscore is allowed only where it does real work:

- A backing field paired with a same-named public property or accessor (`self._jetstream` behind a `jetstream` property).
- Language-mandated names: dunders (`__init__`, `__enter__`, etc.), `self`, `cls`, the throwaway `_` in unpacking.

Everything else is public. If no same-named public accessor mediates the identifier, drop the underscore. Applies to Python; TS/React keep their own conventions.

**Programmatic check**:
- `pra-no-underscore-members` semgrep rule (Python), warning severity: flags identifiers matching `^_[a-z]` in assignments, `def`, and attribute targets. A match paired with a same-named property in the same class is allowlisted; every other match needs reviewer judgment.

**Reviewer hint**: a new `_name` with no same-named public accessor is a blocker. Propose the plain public name inline.

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
5. **Evidence-check the diff.** If the diff cites library behavior, version-specific features, or external API shapes you cannot fully verify from the code alone, query exa to confirm. Flag claims grounded in stale training data.
6. **Dependency scrutiny.** If the diff adds a new top-level dependency, confirm the PR description includes the new-dependency proposal (research, alternatives, maintainer signals). Missing proposal = blocker. Spot-check the proposal's claims via exa. Confirm no existing project dependency could have solved the problem.

A reviewer who fails to invoke programmatic tooling but only eyeballs the diff is incomplete and should be re-run.

### Developer protocol

Every developer dispatch must:

1. Read this `## Engineering Principles` section before starting.
2. Run `pragmatiks-lint check` locally before opening a PR.
3. Resolve all 🚨 blockers from the lint pack. ⚠️ findings: address or justify in PR body.
4. State principle compliance in the callback to the supervisor.
