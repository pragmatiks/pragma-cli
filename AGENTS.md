# AGENTS.md

## Scope

These instructions apply to the whole repository.

This repository is Codex-ready, but the existing Claude Code setup remains authoritative for Claude-specific behavior. Keep `CLAUDE.md`, `.claude/` local settings rules, and Claude hooks untouched unless the task explicitly asks for Claude Code changes.

Do not add personal Codex configuration, MCP auth files, local hooks, machine-specific paths, or secrets to this repository.

## Project

`pragma-cli` is the Python CLI for Pragmatiks. It exposes the `pragma` command from `pragma_cli.main:app` and uses:

- Typer for command definitions.
- Rich for terminal output.
- `pragmatiks-sdk` for API access.
- YAML/TOML parsing for resources, config, and provider metadata.

Source lives under `src/pragma_cli/`. Command implementations live under `src/pragma_cli/commands/`.

## Required Tooling

- Python 3.13 or newer. CI installs Python 3.13.
- `uv` for environment management, dependency sync, and builds.
- `task` for the project command shortcuts.

## Commands

Install dependencies:

```bash
task install
```

Format:

```bash
task format
```

Check lint and types:

```bash
task check
```

Test / CLI smoke checks that do not require authentication:

```bash
task test
uv run pragma --help
uv run pragma config current-context
```

Run `task test` when changes touch plugin discovery. For other behavior, run `task check` plus targeted CLI smoke
checks or command-specific checks that match the change.

Build distributions:

```bash
uv build
```

Release/version commands:

```bash
uv run cz bump
uv build
uv publish
```

Only publish when explicitly asked and when PyPI credentials are available. The GitHub `Publish CLI` workflow also bumps with Commitizen, builds, creates a GitHub release, publishes to PyPI, and triggers the providers publish workflow after changes land on `main`.

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

## Validation Expectations

Before handing off code changes, run the narrowest reliable validation for the change. For most changes in this repo, that means:

```bash
task check
task test
uv run pragma --help
```

For packaging or release changes, also run:

```bash
uv build
```

If a command cannot be run because credentials, network access, or external services are unavailable, report that explicitly with the command attempted and the observed failure.

## Code Style

- Follow the existing Typer command-module pattern.
- Prefer SDK client calls over direct HTTP.
- Keep Rich output consistent with nearby commands.
- Use Google-style docstrings where public or nontrivial functions need them.
- Ruff is configured for Python 3.13, 120-character lines, import sorting, pyupgrade, pydocstyle, and Ruff docstring checks.
- The pre-commit policy forbids ordinary explanatory `#` comments in `src/`; extract clearer code instead. `TODO`, `FIXME`, `BUG`, `HACK`, `type:`, `noqa`, `fmt:`, and `pragma:` comments are allowed where appropriate.
- Test files, if added later, should use standalone pytest-style functions. Do not use `unittest` or `unittest.mock`; use pytest fixtures and `pytest-mock` conventions.

## Local Config And Secrets

The CLI reads local user config outside the repository:

- Default config directory: `~/.config/pragma/`.
- `XDG_CONFIG_HOME` changes the config root to `$XDG_CONFIG_HOME/pragma/`.
- Config file: `config`.
- Credentials file: `credentials`.
- Lock file: `config.lock`.

Relevant environment variables include:

- `PRAGMA_CONTEXT`
- `PRAGMA_PROJECT`
- `PRAGMA_AUTH_TOKEN`
- `PRAGMA_AUTH_TOKEN_<CONTEXT>`
- `PRAGMA_AUTH_CALLBACK_PORT`
- `PRAGMA_AUTH_CALLBACK_PATH`
- `PRAGMA_PROVIDER_TEMPLATE`

Never commit local config, credentials, tokens, `.env`, `.venv`, build outputs, caches, `.claude/settings.local.json`, or `.claude/scheduled_tasks.lock`.

## Worktree Expectations

- Start by checking `git status --short --branch`; Codex worktrees may be detached or have user changes.
- Do not revert or overwrite changes you did not make.
- Keep edits scoped to the issue.
- Avoid writing outside the repository except for normal tool caches and virtual environments.
- Do not add generated artifacts such as `dist/`, `.ruff_cache/`, `.pytest_cache/`, or `.venv/`.

## Linear Workflow

When working from a Linear issue:

- Treat the issue description and checklist as the source of truth.
- If Linear tooling is configured, move the issue to an active state when starting, post concise progress comments for meaningful blockers, and leave a completion comment with validation results.
- If you discover follow-up work, create or propose a linked Linear issue instead of expanding scope silently.
- Keep parent issue context intact and mention related issue identifiers in summaries when useful.
- Work autonomously through implementation and validation unless the issue requires a product decision or secret access.

## Engineering Principles

Canonical engineering rules for all Pragmatiks code in this repository. Workers (developers and reviewers) must follow these in every dispatch. Reviewers must check each PR against this list and produce one finding per violation.

### Scope

Applies to all code in this repository. Some principles only apply to one language or stack — flagged where relevant.

This section is the ground truth for engineering principles in this repository. The same text is embedded in every Pragmatiks subrepo's `AGENTS.md` and `CLAUDE.md`. When a principle changes, every embed must be updated in lockstep and the corresponding `pragmatiks-lint` / `@pragmatiks/lint` rule versions bumped.

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

When the user types `$graphify`, invoke the graphify pipeline before doing anything else.

Rules:
- ALWAYS read graphify-out/GRAPH_REPORT.md before reading any source files, running grep/glob searches, or answering codebase questions. The graph is your primary map of the codebase.
- IF graphify-out/wiki/index.md EXISTS, navigate it instead of reading raw files
- For cross-module "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`, or `graphify explain "<concept>"` over grep — these traverse the graph's EXTRACTED + INFERRED edges instead of scanning files
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
