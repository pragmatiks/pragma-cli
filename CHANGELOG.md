# Changelog

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
