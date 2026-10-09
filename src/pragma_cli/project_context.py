"""Project context resolution for project-scoped resource commands."""

from __future__ import annotations

import os

import click
import typer

from pragma_cli.config import MalformedConfigError, get_current_context
from pragma_cli.errors import error_console
from pragma_cli.exit_codes import INPUT_ERROR_EXIT_CODE


_MISSING_PROJECT_MESSAGE = (
    "No project context set. Pass --project, set PRAGMA_PROJECT, or run 'pragma projects use <project-id>'."
)


def resolve_project(typer_ctx: typer.Context | click.Context | None) -> str:
    """Resolve the active project ID from CLI flag, env var, or config.

    Intended for real command execution: emits a CLI error and exits
    with ``INPUT_ERROR_EXIT_CODE`` when no project can be resolved.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project ID.

    Raises:
        UnknownContextError: If no project is given and the context the root
            callback resolved is not in the configuration.
        MalformedConfigError: If no project is given and the config file
            cannot be parsed.
        typer.Exit: If no project context is configured.
    """  # noqa: DOC502
    project = resolve_project_id(typer_ctx)

    if project is None:
        error_console.print(f"[red]Error:[/red] {_MISSING_PROJECT_MESSAGE}")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    return project


def resolve_project_or_none(typer_ctx: typer.Context | click.Context | None) -> str | None:
    """Resolve the active project ID without side effects.

    Intended for shell completion callbacks: never prints to stderr,
    never raises ``typer.Exit``. Returns ``None`` when no project can
    be resolved, or the configuration cannot be read, so callers can
    exit completion cleanly.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project ID, or ``None`` if nothing is configured.
    """
    try:
        return resolve_project_id(typer_ctx)
    except (ValueError, OSError, MalformedConfigError, typer.Exit):
        return None


def resolve_project_id(typer_ctx: typer.Context | click.Context | None) -> str | None:
    """Resolve the active project ID, or ``None`` when none is configured.

    Precedence:
        1. Global ``--project`` flag (from ``ctx.obj`` when the root
           callback has run, otherwise ``root_context.params`` during shell
           completion where the callback never fires).
        2. ``PRAGMA_PROJECT`` environment variable.
        3. Persistent default on the current CLI context.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project ID, or ``None`` if nothing is configured.

    Raises:
        UnknownContextError: If no project is given and the context is not
            in the configuration.
        MalformedConfigError: If no project is given and the config file
            cannot be parsed.
        OSError: If no project is given and the config file cannot be read.
    """  # noqa: DOC502
    root_context = typer_ctx.find_root() if typer_ctx is not None else None
    root_object = root_context.obj if root_context is not None and isinstance(root_context.obj, dict) else {}

    project = root_object.get("project")
    if project:
        return project

    if root_context is not None:
        param_project = root_context.params.get("project")
        if param_project:
            return param_project

    env_project = os.getenv("PRAGMA_PROJECT")
    if env_project:
        return env_project

    context_name = root_object.get("context")
    if context_name is None and root_context is not None:
        context_name = root_context.params.get("context")

    _, context_config = get_current_context(context_name)
    return context_config.project or None
