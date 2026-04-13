"""Project context resolution for project-scoped resource commands."""

from __future__ import annotations

import os

import click
import typer

from pragma_cli.config import ConfigDirSymlinkError, MalformedConfigError, get_current_context


_MISSING_PROJECT_MESSAGE = (
    "No project context set. Pass --project, set PRAGMA_PROJECT, or run 'pragma projects use <slug>'."
)


def _resolve_project_slug(typer_ctx: typer.Context | click.Context | None) -> str | None:
    """Resolve the active project slug without emitting errors.

    Precedence:
        1. Global ``--project`` flag (from ``ctx.obj`` when the root
           callback has run, otherwise ``root_ctx.params`` during shell
           completion where the callback never fires).
        2. ``PRAGMA_PROJECT`` environment variable.
        3. Persistent default on the current CLI context.

    All configuration errors (malformed file, missing context, OS
    failures on the config directory) are swallowed so that shell
    completion callbacks never print anything to stderr. Defense in
    depth: ``typer.Exit`` is also caught in case a future refactor
    reintroduces it through a shared helper.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project slug, or ``None`` if nothing is configured.
    """
    root_ctx = typer_ctx.find_root() if typer_ctx is not None else None
    root_obj = root_ctx.obj if root_ctx is not None and isinstance(root_ctx.obj, dict) else {}

    project = root_obj.get("project")
    if project:
        return project

    if root_ctx is not None:
        param_project = root_ctx.params.get("project")
        if param_project:
            return param_project

    env_project = os.getenv("PRAGMA_PROJECT")
    if env_project:
        return env_project

    context_name = root_obj.get("context")
    if context_name is None and root_ctx is not None:
        context_name = root_ctx.params.get("context")

    try:
        _, context_config = get_current_context(context_name)
    except (ValueError, OSError, MalformedConfigError, ConfigDirSymlinkError, typer.Exit):
        return None

    return context_config.project or None


def resolve_project(typer_ctx: typer.Context | click.Context | None) -> str:
    """Resolve the active project slug from CLI flag, env var, or config.

    Intended for real command execution: emits a CLI error and exits
    with code 2 when no project can be resolved.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project slug.

    Raises:
        typer.Exit: If no project context is configured.
    """
    project = _resolve_project_slug(typer_ctx)
    if project is not None:
        return project

    typer.echo(f"Error: {_MISSING_PROJECT_MESSAGE}", err=True)
    raise typer.Exit(2)


def resolve_project_or_none(typer_ctx: typer.Context | click.Context | None) -> str | None:
    """Resolve the active project slug without side effects.

    Intended for shell completion callbacks: never prints to stderr,
    never raises ``typer.Exit``. Returns ``None`` when no project can
    be resolved so callers can exit completion cleanly.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Resolved project slug, or ``None`` if nothing is configured.
    """
    return _resolve_project_slug(typer_ctx)
