"""Project context resolution for project-scoped resource commands."""

from __future__ import annotations

import os

import click
import typer

from pragma_cli.config import get_current_context


_MISSING_PROJECT_MESSAGE = (
    "No project context set. Pass --project, set PRAGMA_PROJECT, or run 'pragma projects use <slug>'."
)


def resolve_project(typer_ctx: typer.Context | click.Context | None) -> str:
    """Resolve the active project slug from CLI flag, env var, or config.

    Precedence:
        1. Global ``--project`` flag.
        2. ``PRAGMA_PROJECT`` environment variable.
        3. Persistent default on the current CLI context.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project slug.

    Raises:
        typer.Exit: If no project context is configured.
    """
    root_ctx = typer_ctx.find_root() if typer_ctx is not None else None
    root_obj = root_ctx.obj if root_ctx is not None and isinstance(root_ctx.obj, dict) else {}

    project = root_obj.get("project")
    if project:
        return project

    env_project = os.getenv("PRAGMA_PROJECT")
    if env_project:
        return env_project

    context_name = root_obj.get("context")
    _, context_config = get_current_context(context_name)
    if context_config.project:
        return context_config.project

    typer.echo(f"Error: {_MISSING_PROJECT_MESSAGE}", err=True)
    raise typer.Exit(2)
