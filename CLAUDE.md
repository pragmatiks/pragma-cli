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
- `pragma resources get <org/provider/resource[/name]>` - Get resource(s) by type or full ID
- `pragma resources describe <org/provider/resource/name>` - Show detailed resource info
- `pragma resources apply <file>... | -f <file>...` - Apply resources from YAML. Files given both positionally and with `-f` are a usage error. Every document is planned first; invalid documents are listed and the command exits with an input error before anything is applied. A schema read the API refuses (any status but 404, which skips schema validation for that provider), a schema that is not a valid JSON schema, or a broken network goes to the global handler, failure, before anything is applied
- `pragma resources delete|deactivate <org/provider/resource/name> | -f <file>...` - Delete or deactivate a resource. With `-f`, every file is parsed and checked first; a file that is not UTF-8 or valid YAML, or a document without a provider, resource and name or declaring a `project_id` other than the active project, is listed and the command exits with an input error before any resource changes. A resource ID together with `-f` is a usage error
- `pragma resources tags list/add/remove` - Manage resource tags
- A resource ID that is not `org/provider/resource/name` with no empty segment (`get` also takes `org/provider/resource`) is a usage error before any request

### Providers
- `pragma providers list [--installed] [-o json|yaml]` - List providers in the store; `--installed` lists installed providers with columns Provider, State (labelled as on the web: Queued, Starting, Running, Failed, Uninstalling), Version (served, a dash before the first ready), Operation (latest operation and its target version) and Message (the failure message); JSON/YAML records carry `installed_version`, the raw `lifecycle_state`, `operation_type`, `operation_version` and `failure_message`
- `pragma providers init <name>` - Initialize a new provider project
- `pragma providers update [project-directory]` - Update project from template
- `pragma providers publish [project-directory | --wheel <path>] [--changelog <file>] [-o json|yaml]` - Publish a new provider version: builds the wheel with `uv build --wheel --out-dir <project-directory>/dist` (so a uv workspace member's wheel lands in its own `dist/`; a project directory that does not exist or has no `pyproject.toml`, a missing `uv`, or a failed build is an input error) unless `--wheel` points at a prebuilt one (passing both is a usage error). All its progress goes to stderr and, in table format (the default), only the final `Published <name> <version> — Python …, SDK …, <n> resource types` line goes to stdout. Once the wheel is built it reads the caller's organization behind a spinner (`GET /organizations/me`, refusals go to the global handler), renames a copy of the wheel behind a `Renaming <filename>...` spinner to the organization's distribution name `<organization>-<provider>` in a `pragma-publish-` temporary directory kept until the upload ends (`pragma_sdk.rename_provider_wheel`; the original file is left unchanged; a wheel already carrying that name, or naming no single provider, is used as is) and prints `Renamed to <filename>` when the filename changed, reads the version's declaration from the renamed copy behind a `Reading <filename>...` spinner (a wheel that cannot be renamed or read, a wheel expanding past the size limit included, exits 2; a built wheel whose reading fails with an operating system error once open prints "Could not read the wheel: <reason>. Check the disk holding the wheel, then publish again.", input error; a renamed copy, or its temporary directory, that cannot be written, or a renamed copy that cannot be read back (an operating system error opening or reading it), prints "Could not write the renamed copy of the wheel: <reason>. Free space in the temporary directory or point TMPDIR elsewhere, then publish again.", failure; the temporary directory is made before the CLI knows whether a copy is needed, so one that cannot be created fails a wheel needing no copy too; a temporary directory that cannot be removed prints "Warning: Could not remove the temporary directory <path>; delete it yourself." and never fails the publish). The organization is read before the wheel, so an unreachable API is reported before an unreadable wheel. It then declares the renamed copy with `POST /providers/publish` (201 new, 200 when the identical declaration is already in flight) and prints `Publishing <name> <version>` and the operation id dimmed. When the declaration returns a `wheel_upload` address (only while the version is `awaiting_wheel`) it PUTs the renamed copy straight to that signed storage address and prints `Uploaded <size>`, or `Wheel already uploaded` when storage already holds it; the watch spinner then starts at `wheel uploaded, waiting for Pragmatiks to record it`, the label it keeps while reads still show `awaiting_wheel`. Without an address it prints `Wheel already received` and a `Status: <label>` line for the declared status, and the spinner starts there. It then polls `GET /providers/{name}/versions/{version}` every 2 s from the declared version, showing the status label beside a spinner and printing a `Status: <label>` line each time a read shows a different status from the previous one, until the organization's provider host admits it (`published`) or fails it (`failed`, prints the version's `error_message`, failure). The wheel alone identifies the version: its `pragma.provider` entry point names the provider and its metadata carries the version; nothing is read from pyproject.toml. The watch lasts at least 150 s from the finished upload (from the declaration when no upload was needed) and, once the version carries an `operation_deadline_at`, until that deadline plus 30 s. A version whose `operation_id` no longer matches the declared one was replaced by another publish: it prints that and the `pragma providers versions` pointer, failure. A watch timeout exits 3 with "Admission of <name> <version> has not finished", the status label (`wheel uploaded, waiting for Pragmatiks to record it` for an `awaiting_wheel` version this publish uploaded) and the `pragma providers versions` pointer; an HTTP error while polling prints the API's message and the same pointer, exit 3; a transport error while polling prints what stopped the watch (API unreachable, timeout, dropped connection) and the same pointer, exit 3; any other failure while polling also exits 3. Every exit 3 ends with `To keep waiting, run: pragma providers publish --wheel <path>` naming the built wheel this publish read, not its renamed copy, plus `--changelog <file>` when one was given, shell-quoted: the API treats the renamed wheel and the changelog as the declaration, so a rebuilt wheel, a dropped changelog, or a rename by another Python or zlib (which can give different bytes) would replace the version in flight. A refused declaration is a failure, a 404 included: it prints the API's message with Rich markup escaped (a 401 prints the login-required message); a 503 whose body carries no message prints "Pragmatiks cannot accept the publish right now. Try again later."; every 5xx, a proxy's 502 or 504 included, other than a 503 for an organization that is still being set up or whose setup failed, ends with `To publish it again, run: pragma providers publish --wheel <path> [--changelog <file>]`. A declaration interrupted after connecting prints what broke it off (timeout, dropped connection) and ends with `To publish it again, run: pragma providers publish --wheel <path> [--changelog <file>]`; a declaration whose connection failed, or an invalid one, goes to the global handler. A declaration answer the CLI cannot read prints which fields are invalid, never their values (a whole answer that is not an object prints what is wrong without a field; an upload address that is not an http(s) URL with a host, or upload headers that cannot be sent, is such an answer), or why a body that is not JSON or could not be decoded was refused, and ends with the same publish-again line, failure. A failed upload is a failure, never prints the signed address or the error's own text, and ends with `To publish it again, run: pragma providers publish --wheel <path> [--changelog <file>]`: an expired address prints the SDK's "The wheel upload address expired before storage received the whole wheel (HTTP <status> <code>, storage request <id>).", any other storage refusal the SDK's "Storage refused the wheel upload (HTTP <status> <code>, storage request <id>)." (in both, the code and the request id only when storage sent them), an upload that got no answer although storage may have received the wheel (no answer in time, the connection broke off or closed before an answer, an answer that could not be decoded) "The upload's outcome is unknown (<cause>).", any other upload that got no answer from storage (storage unreachable, sending or the connection timed out, the connection broke off while sending, an invalid request, or any other cause named by its error type) "Could not upload the wheel: <cause>.", and a wheel that cannot be read for the upload "Could not read the wheel to upload it: <reason>."
  - With `-o json|yaml` the `Published` line goes to stderr too, and once the watch ends stdout carries exactly one document, `{"operation_id": <this publish's operation id>, "version": <the version as last read, a `providers versions -o json` record>}`, on exit 0, exit 1 for a failed or replaced version (replaced when `version.operation_id` differs from `operation_id`), and exit 3; `version` is the declared version when a read fails or none was read, without its `wheel_upload`, so the signed upload address is never printed, and on exit 3 `version.status` may be the declared status. Every exit before the watch leaves stdout empty.
- `pragma providers versions <name> [-o json|yaml]` - List a provider's versions (columns Version, Status, Published, Package, Wheel SHA-256 shortened to 12 hex characters, the digest of the stored wheel after its rename, not of the built file, Message; `info` shows the same table) with a status label (`awaiting_wheel` waiting for the wheel, its message column, when the version has no message, hinting to run publish again if the publish stopped before its upload finished and that a finished upload shows as wheel received once Pragmatiks records it; `staged` wheel received; `queued` waiting for the provider host; `admitting` admitting; `promoting` publishing to the registry; `published`; `failed`) and the failure message of failed versions
- `pragma providers info <name> [-o json|yaml]` - Show a provider; only a 404 on its versions shows no versions, any other error goes to the global handler
- Every command taking a namespaced provider name (`org/name`) rejects one that is not `org/name` with a usage error before any request
- `pragma providers install|upgrade|downgrade|uninstall <name>` - Queue the operation and print `Install queued:`, `Upgrade queued:` or `Downgrade queued:` with its target version (`operation_version`), or `Uninstall queued:`, then a dim `Follow it with: pragma providers history <name>` pointer
- `pragma providers history <name> [-o json|yaml]` - Show an installed provider's history from `GET /providers/installed/{org}/{name}/history`, newest first, with columns Time, Transition (state labels, `Uninstalled` once removed), Operation (`restart` for a host start, as on the web), Version, Message and Cause; kept 30 days, also after an uninstall; no history prints "No history found." in table format
- `pragma providers downgrade <name> --version <v>` - A 422 prints the API's message, then a hint that the version chain may be broken
- `pragma providers delete <name> [--yes]` - Delete a provider from the catalog (admins of the owning organization only)

### Exit codes and output streams

The exit-code table lives in `pragma_cli.exit_codes` and prints in `pragma --help`; read it there. This file names outcomes by that table: failure 1, input error 2 (usage errors included), not found 4. A command's own help names only its command-specific codes (`providers publish`: 3).

Errors and warnings go to stderr; command output, including `-o json`, stays on stdout. With `-o json` or `-o yaml` an empty result prints an empty list; the "No ... found." line is for the table format only. Error reporting lives in `pragma_cli.errors`; the global handler in `main.py` reports what a command does not handle: an API error (status mapped by `compute_http_exit_code`; a 401 prints the login-required message), a request error (failure, with a hint to check the API URL when the API could not be reached), an invalid request such as a token with a line break (failure, printed without the request content), an API URL that is not an `http://`/`https://` URL with a host (input error, naming the context and its URL; checked when the client is built), an API response the CLI cannot read: an invalid model, a body that is not JSON, or a resource schema that is not a valid JSON schema (failure), an unknown context, malformed config or invalid local input (input error), an OS error naming a file (input error), and any other OS error (failure). A broken stdout pipe (`... | head -1`) exits 1 without printing. `errors.report_not_found` prints a command's own 404 message. Missing or conflicting arguments are Click usage errors.

The context (`--context`/`PRAGMA_CONTEXT`) is resolved when a command first needs the API client, so `pragma config` commands work while it names a context that does not exist yet (`PRAGMA_CONTEXT=new pragma config set-context new --api-url ...`).

### Configuration
- `pragma config current-context` - Show current context
- `pragma config get-contexts` - List available contexts
- `pragma config use-context <name>` - Switch context; an unknown context (here, in `config delete-context`, `auth login`, `projects use`, or via `--context`/`PRAGMA_CONTEXT` for a command that calls the API) prints "Error: Context '<name>' not found. Available contexts: ..." on stderr, input error
- `pragma config set-context <name> --api-url <url>` - Create/update context; an API URL that is not an `http://`/`https://` URL with a host is a usage error
- `pragma config delete-context <name>` - Delete context; the current context is refused with a pointer to `pragma config use-context`, input error

### Authentication
- `pragma auth login` - Authenticate (opens browser)
- `pragma auth whoami` - Show current user; no token or an expired one is a status report on stdout (success); a token the API rejects or an unreachable API is an error on stderr
- `pragma auth token` - Print the bearer token; without a valid one prints the login-required message on stderr, failure
- `pragma auth logout` - Clear credentials

### Organizations
- `pragma organizations me [-o json|yaml]` - Show the caller's own organization: ID, name, slug, status and timestamps
- `pragma organizations status [-o json|yaml]` - Show the organization's status (`bootstrapping`, `ready`, `bootstrap_failed`, `deleting`, `deleted`); `deleting` and `deleted` add a line saying so, JSON and YAML are the API's answer as returned, and the command exits 0 in every status

### Operations
- `pragma ops dead-letter list` - List failed events
- `pragma ops dead-letter show <id>` - Show event details as plain JSON; an unknown event prints "Event '<id>' not found." and exits not found
- `pragma ops dead-letter retry <id> | --all` - Retry failed event(s); an id together with `--all` is a usage error
- `pragma ops dead-letter delete <id> | --all | --provider <name>` - Delete failed event(s); an id together with `--all` or `--provider`, or `--all` together with `--provider`, is a usage error

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

Always use mise tasks (`mise tasks` lists them all):

| Command | Purpose |
|---------|---------|
| `mise run install` | Install the locked mise toolchain, the locked environment into `.venv`, and the pre-commit and commit-msg hooks |
| `mise run format` | Format with ruff |
| `mise run check` | Lint + type check against `.venv`, whatever `VIRTUAL_ENV` the shell carries |
| `mise run lock:toolchain` | Regenerate `mise.lock` and `.mise/locks/` for macOS arm64 and Linux x64 without bumping a tool; runs only on those hosts and only with mise at the `min_version` in `mise.toml` |

### Toolchain

mise is the only toolchain source, on the Mac, in Linux cloud sessions and in CI. On a fresh clone run `mise trust`, then `mise run install`.

- `mise.toml` pins python, uv, pre-commit, commitizen and yq; `mise.lock` and `.mise/locks/` lock them; `locked = true` in `[settings]` makes every mise install fail on a tool the lockfile does not record.
- `mise.toml` points uv at mise's Python (`UV_PYTHON`, `UV_PYTHON_PREFERENCE`), so `.venv` always runs the pinned interpreter.
- `[tool.uv] required-version` in `pyproject.toml` equals the uv pin in `mise.toml`; uv refuses to run at any other version, and the `Toolchain` workflow fails a pull request where they differ.
- Changing a tool version in `mise.toml` means running `mise run lock:toolchain` and committing the lock; the `Toolchain` workflow fails a pull request whose lock is stale.
- The mise version is pinned twice and moves in one sweep across every Pragmatiks repo: `min_version` in `mise.toml`, and every `jdx/mise-action` `version:` under `.github/workflows`.

### Local SDK checkout

To work against an unreleased pragma-sdk, set an absolute `PRAGMA_SDK_DIRECTORY` in the worktree's own gitignored `mise.local.toml` and run `mise trust` once:

```toml
[env]
PRAGMA_SDK_DIRECTORY = "/absolute/path/to/pragma-sdk"
```

- `mise run check` validates the path (absolute, contains `src/pragma_sdk`), prints it, and type-checks against the local SDK checkout layered over the locked environment.
- Run the CLI against it with `uv run --with-editable "$PRAGMA_SDK_DIRECTORY" pragma …`.
- `pyproject.toml` and `uv.lock` never change; never `uv add --editable` a sibling checkout.
- Code that uses names only in an unreleased SDK passes `mise run check` with the variable set, but the pre-commit ty hook rejects it until that SDK is released, because committed code must work against the registry SDK.
- Never set it in the main checkout's `mise.local.toml`: mise loads parent directories' config into every worktree under `.claude/worktrees`.

## Patterns

- Typer for command definitions
- Rich console for formatted output
- SDK client for all API operations
- YAML for resource definitions

## Delivery

Canonical: pragma-kit `project-instructions.md` (Delivery, Cloud threads). In short:

- **Land it** by default: commit, push, open the pull request, and rebase-merge it once CI and review are clean.
- **PR it** (push, open the pull request, stop) only when the work departs from the agreed plan: an architecture change, a new pattern, a new library, or a changed design decision. Name the departure and wait for the founder. The founder saying "PR it" or "land it" sets the mode for that task.
- Branch from the latest `origin/main` as `<feat|fix|chore>/PRA-<n>_<snake_case_summary>`; the branch is deleted on merge.
- Commits carry the founder's git identity, with no `Co-Authored-By`, `Claude-Session` or other Claude trailers.

## Evidence-based development

Fact-check with live lookups before writing code. The cost of a query is far less than a debugging cycle on stale assumptions.

**Use internal knowledge for**: programming skill, language fluency, algorithms, design patterns, general engineering judgment, code comprehension.

**Always look up**: library API details, framework feature lists, version-specific behavior, current best practice, recent changes — anything where being wrong costs time.

If you find yourself thinking "I'm pretty sure this library does X" or "the API was Y last time I used it" — STOP and query. Training data is months to years out of date; library APIs change.

### Lookup routing

- **WebSearch** — live web search. Use for library / framework / SDK docs, release notes, blog posts, GitHub issues — any external fact-check.
- **WebFetch** (or the `web-fetch` agent) — read a known URL when you already have it and need its content.
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

Releases are CI-owned. People change `main` only through pull requests; the only direct push is the bump commit from the pragma-ci-bot GitHub App, which may bypass the `main` ruleset and whose key lives only in the `release` environment. Each merge to `main`, or a manual dispatch from `main`, runs `.github/workflows/publish.yaml`; runs queue and execute one at a time:

1. `bump` (no secrets) runs `cz bump` on the latest `main` (version and `CHANGELOG.md` from conventional commits, commit marked `[skip ci]`) and hands the bump commit and `v{version}` tag on as a git bundle.
2. `push` (`release` environment) pushes the bump commit and tag to `main` as pragma-ci-bot. If `main` moved since the bump, it pushes nothing, dispatches a new `publish.yaml` run on `main` (which bumps from the latest `main`) and ends without failure.
3. `build` builds the commit that was pushed, `publish` uploads it to PyPI through trusted publishing (`pypi` environment), `dispatch` triggers the pragma-providers re-publish, and `github-release` creates the GitHub release.

A merge whose commits carry no bump type (`chore`, `docs`, …) releases nothing. Merges made by the update-sdk workflow use `GITHUB_TOKEN`, which starts no workflow run, so they ship with the next merge or a manual dispatch of `publish.yaml`. The GitHub release is created last and marks a release complete: when `bump` finds nothing to bump but the current version's tag has no GitHub release, the run builds, publishes and releases that tag's commit without a push. So after a failure past `push`, any later run (a re-run, a manual dispatch or the next merge) finishes the release.

Never run `cz bump`, `uv build` or `uv publish` locally, and never hand-edit the version or changelog.

**Tag format**: `v{version}` (e.g., `v11.0.0`)

## Engineering Principles

Canonical engineering rules for all Pragmatiks code in this repository. Workers (developers and reviewers) must follow these in every dispatch. Reviewers must check each PR against this list and produce one finding per violation.

### Scope

Applies to all code in this repository. Some principles only apply to one language or stack — flagged where relevant.

This section is an embedded copy of the pragma-os repo's `docs/engineering-principles.md`, the canonical source. The same text is embedded in every Pragmatiks subrepo's `CLAUDE.md`. When a principle changes, every embed must be updated in lockstep and the corresponding `pragmatiks-lint` / `@pragmatiks/lint` rule versions bumped.

### Enforcement layers

| Layer | What | Where |
|---|---|---|
| 1. Style + standard smells | `ruff` (Python), `eslint` (TS) with curated rule set | per-repo `mise run check` / `pnpm lint` |
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

Code must self-explain. No comments — none, WHY comments included (amended 2026-08-31; they metastasized). Docstrings = documentation, not comments — rule not restrict them. Requirements:

- Google-style docstrings **required** on every module, class, function, method (not only `pragma-sdk` public surface). Describe purpose, arguments, returns, raised exceptions; no noise padding.
- Docstrings address the **caller**: contract, constraints, how to use. Not implementation narration, not change history, not a log of what the function used to do. A workaround, hidden constraint, or subtle invariant worth recording goes in the owning function's docstring (a `Note:` section when it needs a home), or the code gets restructured until it needs no explanation.
- Tool directives that must sit on the flagged line (`# noqa`, `eslint-disable`, `# type: ignore`) stay, bare — justification lives in the docstring, not appended prose.

Forbidden: ALL non-docstring comments — WHY comments, block comments restating code, section dividers, commented-out code, "added for X" / "used by Y" trail comments, planning comments (`# TODO: refactor later`).

**Programmatic check**:
- `check-comments` pre-commit hook (`python -m pragmatiks_lint.comments` from `pragmatiks-lint`, pinned via `additional_dependencies`): flags every comment in every Python file, trailing included; only a line-1 shebang and exact bare tool directives pass (`# noqa`, `# ruff: noqa`, `# type: ignore`, `# ty: ignore`, each with optional codes; `# fmt: off|on|skip`); a directive with prose fails.
- `pra-no-block-comments-js` semgrep rule: flags every `/* ... */` comment in TS/JS except JSDoc `/** ... */`.
- `pra-no-todo-comments-js` semgrep rule: flags a `//` or `/* */` comment in TS/JS that opens with `TODO`, `FIXME` or `XXX` (any case), except `TODO(PRA-n)`.

**Reviewer hint**: every non-directive comment in diff = finding. Rationale that matters moves to the nearest docstring; rationale that does not matter dies. Docstrings reading like implementation walkthroughs or changelogs = finding too.

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
- API mirrors CLI: route noun + verb match CLI command (`pragma providers publish` ↔ `POST /providers/publish`), and reverse. Mismatch = finding, not REST-purist path. A sub-route under a command's noun belongs to that command.
- No internal infrastructure terms (Kubernetes, pod, namespace, NATS, JetStream, SurrealDB internals) or vendor names (Clerk) in route summaries, descriptions, schema field docs, error strings, UI copy. OpenAPI spec feeds public docs verbatim — fix at source.
- Failure reasons written to a resource's terminal `FAILED` state: no blame ("provider failed to…"), no internal jargon (queue, outbox, dispatch, event id), end with shared `RECOVERY_GUIDANCE` constant — reuse existing reason constants, never per-path prose. Other failures (publication, install) end with the recovery wording their decision doc defines.

**Programmatic check**:
- `eslint-plugin-unicorn/prevent-abbreviations` (TS) — direct fit, allowlist config.
- `pra-no-abbreviations-python` semgrep rule (Python) — regex matching forbidden short identifiers, allowlist.
- `packages/api/scripts/check_openapi_spec.py` (`mise run api:check`) — flags infrastructure terms in generated OpenAPI spec. Kubernetes, pod, namespace, and vendor names not in term list yet — reviewer scans for them.

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

1. Run `pragma lint check <paths>` (programmatic findings) before reading the diff.
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
2. Run `pragma lint check <paths>` locally before opening a PR.
3. Resolve all 🚨 blockers from the lint pack. ⚠️ findings: address or justify in PR body.
4. State principle compliance in the callback to the supervisor.
