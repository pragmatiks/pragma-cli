# Pragma CLI Context

## Overview
`pragma-cli` is the command-line interface for the Pragmatiks platform, built with **Typer** and **Rich**. It enables users to manage resources, providers, authentication, and configuration contexts.

## Architecture

*   **Entry Point**: `src/pragma_cli/main.py` defines the main `typer.Typer` app.
*   **Commands**: Subcommands are organized in `src/pragma_cli/commands/`.
*   **SDK Usage**: Uses `pragmatiks-sdk` for all API interactions.
*   **Configuration**: Manages contexts and credentials (stored in `~/.config/pragma/`).

## Development

### Key Commands
Run these from the `pragma-cli` directory or via `task cli:<command>` from the root.

*   `task test`: Run tests with `pytest` and coverage.
*   `task format`: Format code with `ruff`.
*   `task check`: Run linting and static analysis.

### Dependencies
*   **Runtime**: `typer`, `rich`, `pragmatiks-sdk`, `copier` (for scaffolding).
*   **Dev**: `pytest`, `pytest-mock`, `ruff`.

## Conventions
*   **Output**: Use `rich.console` for all user-facing output.
*   **Error Handling**: Catch SDK exceptions and present friendly error messages to the CLI user.
*   **Arguments**: Use `typer.Option` and `typer.Argument` with type hints.
