# Changelog

## v0.29.0 (2026-04-04)

### Feat

- cascade to providers after CLI publish

## v0.28.0 (2026-04-03)

### Feat

- pass icon_url from pyproject.toml to publish

## v0.27.6 (2026-04-03)

### Fix

- use org_slug from JWT instead of org name for provider namespace

## v0.27.5 (2026-03-25)

### Fix

- **ci**: pull --rebase before push and fix update-sdk auto-merge

## v0.27.4 (2026-03-19)

### Fix

- resolve E2E testing bugs (PRA-262, PRA-263, PRA-264) (#35)

## v0.27.3 (2026-03-19)

### Refactor

- simplify _apply_tags to use API PATCH semantics (#34)

## v0.27.2 (2026-03-18)

### Fix

- add error handling for get 404 and preserve lifecycle state in tags (#32)

## v0.27.1 (2026-03-18)

### Fix

- require org/provider/resource/name format for resource IDs (#30)

## v0.27.0 (2026-03-17)

### Feat

- add organizations command group with list and cleanup (#28)

## v0.26.0 (2026-03-16)

### Feat

- use provider/resource/name format for resource IDs (#27)

## v0.25.3 (2026-03-16)

### Fix

- use PyPI JSON API for version availability polling (#25)

## v0.25.2 (2026-03-16)

### Fix

- unset VIRTUAL_ENV in ty pre-commit hook (#24)

## v0.25.1 (2026-03-16)

### Fix

- add PyPI availability polling to update-sdk workflow (#23)

## v0.25.0 (2026-03-16)

### Feat

- add pragma resources deactivate command (#22)

## v0.24.0 (2026-03-16)

### Feat

- add --config and --config-file to providers install (#20)

## v0.23.1 (2026-03-08)

### Refactor

- update CLI for SDK 0.27.0 renames (PRA-253) (#18)

## v0.23.0 (2026-03-06)

### Feat

- auto-detect version and org in publish command

## v0.22.1 (2026-03-06)

### Refactor

- read provider name from [tool.pragma].provider (#17)

## v0.22.0 (2026-03-06)

### Feat

- add provider downgrade command (PRA-226) (#16)

## v0.21.1 (2026-03-04)

### Fix

- **deps**: update pragmatiks-sdk to v0.26.0
- **deps**: update pragmatiks-sdk to v0.25.0
- **deps**: update pragmatiks-sdk to v0.24.0

## v0.21.0 (2026-03-03)

### Feat

- add --reveal flag and schema-driven sensitive field display (PRA-227) (#15)

### Fix

- **deps**: update pragmatiks-sdk to v0.23.0

## v0.20.0 (2026-03-01)

### Feat

- show immutable indicator in resource describe output (PRA-225) (#14)

## v0.19.0 (2026-02-27)

### Feat

- send store metadata from pyproject.toml on publish (#13)

### Fix

- **deps**: update pragmatiks-sdk to v0.21.1

## v0.18.1 (2026-02-26)

### Refactor

- unify provider commands with org/name namespacing (#12)

## v0.18.0 (2026-02-24)

### Feat

- add store CLI commands and providers publish (#11)

### Fix

- **deps**: update pragmatiks-sdk to v0.20.0

## v0.17.0 (2026-02-23)

### Feat

- add WAITING and DELETING to CLI status display (#10)

### Fix

- **deps**: update pragmatiks-sdk to v0.19.0

## v0.16.1 (2026-02-07)

### Refactor

- add ty type checking to Taskfile check task

## v0.16.0 (2026-02-07)

### Feat

- support deleting resources from YAML files (#8)

## v0.15.1 (2026-01-31)

### Fix

- move httpx import to top of file

## v0.15.0 (2026-01-31)

### Feat

- add @path syntax support for pragma/file resources

### Fix

- **deps**: update pragmatiks-sdk to v0.18.0
- **deps**: update pragmatiks-sdk to v0.17.1

## v0.14.6 (2026-01-31)

### Fix

- **deps**: update pragmatiks-sdk to v0.17.0

## v0.14.5 (2026-01-29)

### Fix

- **ci**: trigger publish after SDK updates
- **deps**: update pragmatiks-sdk to v0.16.0

## v0.14.4 (2026-01-28)

### Fix

- **deps**: update pragmatiks-sdk to v0.15.6

## v0.14.3 (2026-01-25)

### Fix

- **deps**: update pragmatiks-sdk to v0.15.5

## v0.14.2 (2026-01-25)

### Fix

- **ci**: detect actual version change before publishing
- **deps**: update pragmatiks-sdk to v0.15.4

## v0.14.1 (2026-01-25)

### Fix

- **ci**: detect actual version change before publishing

## v0.14.0 (2026-01-25)

### Feat

- rename --pending to --draft and invert default behavior (#6)

## v0.13.0 (2026-01-23)

### Feat

- add pragma resources tags command (#5)

## v0.12.7 (2026-01-22)

### Fix

- update provider template URL to pragma-providers (#4)

## v0.12.6 (2026-01-20)

### Fix

- update to use ProviderStatus model from SDK

## v0.12.5 (2026-01-19)

### Fix

- auth login now uses current context by default

## v0.12.4 (2026-01-19)

### Fix

- add --auth-url flag and improve localhost auth URL handling

### Refactor

- use urlparse for precise localhost detection

## v0.12.3 (2026-01-19)

### Fix

- correct error messages to reference `pragma auth login`

## v0.12.2 (2026-01-18)

### Fix

- align code with pragma_sdk API models

## v0.12.1 (2026-01-18)

### Fix

- remove workspace-only uv sources for isolated rig worktree

## v0.12.0 (2026-01-16)

### Feat

- **cli**: add --output flag for structured JSON/YAML output

## v0.11.0 (2026-01-16)

### Feat

- Improve CLI UX with Rich tables and version flag

## v0.10.0 (2026-01-16)

### Feat

- Add describe command, resource types, and error message improvements

## v0.9.3 (2026-01-15)

### Refactor

- use BuildInfo for build status responses

## v0.9.2 (2026-01-15)

### Refactor

- hide internal details from provider output

## v0.9.1 (2026-01-15)

### Refactor

- simplify provider delete output

## v0.9.0 (2026-01-15)

### Feat

- add autocompletion for deploy command

## v0.8.1 (2026-01-15)

### Fix

- handle missing client in shell completions

## v0.8.0 (2026-01-15)

### Feat

- add provider autocomplete and update status display

## v0.7.0 (2026-01-15)

### Feat

- **cli**: simplify provider commands for declarative architecture

## v0.6.0 (2026-01-15)

### Feat

- **cli**: show user info in whoami command

## v0.5.3 (2026-01-15)

### Refactor

- **cli**: rename provider to providers for consistency

## v0.5.2 (2026-01-14)

### Fix

- **deps**: update pragmatiks-sdk to v0.6.0

## v0.5.1 (2026-01-14)

### Fix

- **deps**: update pragmatiks-sdk to v0.6.0

## v0.5.0 (2026-01-14)

### Feat

- **provider**: add list, rollback, status, and builds commands

### Fix

- **deps**: update pragmatiks-sdk to v0.5.0

## v0.4.1 (2026-01-14)

### Fix

- **ci**: use --no-sources for standalone SDK resolution

## v0.4.0 (2026-01-14)

### Feat

- add provider delete command with lifecycle cleanup

## v0.3.0 (2026-01-14)

### Feat

- add provider deploy command and use workspace SDK

## v0.2.3 (2026-01-13)

### Fix

- **provider**: pass context api_url to SDK client
- **deps**: update pragmatiks-sdk to v0.3.1

## v0.2.2 (2026-01-13)

### Fix

- add pypi environment for trusted publisher

## v0.2.1 (2026-01-13)

### Fix

- add module-name for uv_build to find pragma_cli

## v0.2.0 (2026-01-13)

### Feat

- add PyPI publishing and rename to pragmatiks-cli

### Fix

- **deps**: update pragmatiks-sdk to v0.2.1

## v0.1.0 (2025-01-13)

### Features

- Initial CLI package with resource management, provider scaffolding, and authentication
