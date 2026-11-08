"""Project context resolution for project-scoped resource commands."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

import click
import typer
from rich.markup import escape

from pragma_cli.config import MalformedConfigError, get_current_context
from pragma_cli.errors import error_console
from pragma_cli.exit_codes import INPUT_ERROR_EXIT_CODE


_MISSING_PROJECT_MESSAGE = (
    "No project context set. Pass --project, set PRAGMA_PROJECT, or run 'pragma projects use <project-id>'."
)


class ProjectSource(StrEnum):
    """Where the effective project of an invocation comes from, valued by its display label."""

    FLAG = "--project flag"
    ENVIRONMENT = "PRAGMA_PROJECT env var"
    CONTEXT = "context default"


@dataclass(frozen=True)
class EffectiveProject:
    """The project commands target, and the source it was resolved from.

    Attributes:
        project: Project ID.
        source: Where the project ID comes from.
    """

    project: str
    source: ProjectSource


def format_effective_project(effective: EffectiveProject | None) -> str:
    """Format the effective project with its source for status output.

    Args:
        effective: Effective project, or ``None`` when none is configured.

    Returns:
        ``<project> (source: <label>)``, or ``none set``.
    """
    if effective is None:
        return "none set"
    return f"{effective.project} (source: {effective.source})"


def format_context_default(context_default: str | None) -> str:
    """Format a context's default project for messages.

    Args:
        context_default: The context's persisted project, or ``None``.

    Returns:
        The project in single quotes, or ``none set``.
    """
    return f"'{context_default}'" if context_default else "none set"


def is_project_override(context_default: str | None, effective: EffectiveProject | None) -> bool:
    """Tell whether a flag or env var project differs from the context default.

    Args:
        context_default: The context's persisted project, or ``None``.
        effective: Effective project, or ``None`` when none is configured.

    Returns:
        ``True`` when commands target a project other than the context default.
    """
    return effective is not None and effective.source != ProjectSource.CONTEXT and effective.project != context_default


def build_project_override_warning(
    context_name: str, context_default: str | None, effective: EffectiveProject | None
) -> str | None:
    """Build the status-command warning for a project that overrides the context default.

    Args:
        context_name: Name of the active context.
        context_default: The context's persisted project, or ``None``.
        effective: Effective project, or ``None`` when none is configured.

    Returns:
        The escaped warning markup, or ``None`` when commands use the context default.
    """
    if effective is None or not is_project_override(context_default, effective):
        return None
    return (
        f"[yellow]Warning:[/yellow] context '{escape(context_name)}' default project is "
        f"{escape(format_context_default(context_default))}; "
        f"commands use '{escape(effective.project)}' from {escape(effective.source)}."
    )


def build_project_override_notice(
    context_name: str, context_default: str | None, effective: EffectiveProject | None
) -> str | None:
    """Build the resource-command notice for a project that overrides the context default.

    Args:
        context_name: Name of the active context.
        context_default: The context's persisted project, or ``None``.
        effective: Effective project, or ``None`` when none is configured.

    Returns:
        The escaped notice markup, or ``None`` when commands use the context default.
    """
    if effective is None or not is_project_override(context_default, effective):
        return None
    return (
        f"Using project '{escape(effective.project)}' (source: {escape(effective.source)}; "
        f"context '{escape(context_name)}' default is {escape(format_context_default(context_default))})"
    )


def print_project_override_warning(
    context_name: str, context_default: str | None, effective: EffectiveProject | None
) -> None:
    """Print the override warning on stderr when the effective project overrides the context default.

    Args:
        context_name: Name of the active context.
        context_default: The context's persisted project, or ``None``.
        effective: Effective project, or ``None`` when none is configured.
    """
    warning = build_project_override_warning(context_name, context_default, effective)
    if warning is not None:
        error_console.print(warning, highlight=False, soft_wrap=True)


def print_project_override_notice(typer_ctx: typer.Context | click.Context | None) -> None:
    """Print ``Using project ...`` on stderr when a flag or env var overrides the context default.

    Reads the configuration only when a flag or env var sets a project.

    Args:
        typer_ctx: Active Typer or Click context.

    Raises:
        UnknownContextError: If an override is set and the context is not in the configuration.
        MalformedConfigError: If an override is set and the config file cannot be parsed.
    """  # noqa: DOC502
    override = read_project_override(typer_ctx)
    if override is None:
        return

    context_name, context_config = get_current_context(extract_context_name(typer_ctx))
    notice = build_project_override_notice(context_name, context_config.project, override)
    if notice is not None:
        error_console.print(notice, highlight=False, soft_wrap=True)


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
    effective = resolve_effective_project(typer_ctx)

    if effective is None:
        error_console.print(f"[red]Error:[/red] {_MISSING_PROJECT_MESSAGE}")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    return effective.project


def resolve_optional_project(typer_ctx: typer.Context | click.Context | None) -> str | None:
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
        effective = resolve_effective_project(typer_ctx)
    except (ValueError, OSError, MalformedConfigError, typer.Exit):
        return None
    return effective.project if effective is not None else None


def resolve_effective_project(typer_ctx: typer.Context | click.Context | None) -> EffectiveProject | None:
    """Resolve the effective project and its source, or ``None`` when none is configured.

    Precedence:
        1. Global ``--project`` flag (from ``ctx.obj`` when the root
           callback has run, otherwise ``root_context.params`` during shell
           completion where the callback never fires).
        2. ``PRAGMA_PROJECT`` environment variable.
        3. Persistent default on the current CLI context.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        Effective project with its source, or ``None`` if nothing is configured.

    Raises:
        UnknownContextError: If no project is given and the context is not
            in the configuration.
        MalformedConfigError: If no project is given and the config file
            cannot be parsed.
        OSError: If no project is given and the config file cannot be read.
    """  # noqa: DOC502
    override = read_project_override(typer_ctx)
    if override is not None:
        return override

    _, context_config = get_current_context(extract_context_name(typer_ctx))
    if not context_config.project:
        return None
    return EffectiveProject(project=context_config.project, source=ProjectSource.CONTEXT)


def read_project_override(typer_ctx: typer.Context | click.Context | None) -> EffectiveProject | None:
    """Read the project a ``--project`` flag or ``PRAGMA_PROJECT`` sets, flag first.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        The overriding project with its source, or ``None`` when neither is set.
    """
    root_context = typer_ctx.find_root() if typer_ctx is not None else None
    root_object = root_context.obj if root_context is not None and isinstance(root_context.obj, dict) else {}

    flag_project = root_object.get("project")
    if not flag_project and root_context is not None:
        flag_project = root_context.params.get("project")
    if flag_project:
        return EffectiveProject(project=flag_project, source=ProjectSource.FLAG)

    env_project = os.getenv("PRAGMA_PROJECT")
    if env_project:
        return EffectiveProject(project=env_project, source=ProjectSource.ENVIRONMENT)

    return None


def extract_context_name(typer_ctx: typer.Context | click.Context | None) -> str | None:
    """Extract the context name the root callback or the ``--context`` parameter selected.

    Args:
        typer_ctx: Active Typer or Click context.

    Returns:
        The selected context name, or ``None`` for the configured current context.
    """
    root_context = typer_ctx.find_root() if typer_ctx is not None else None
    if root_context is None:
        return None

    root_object = root_context.obj if isinstance(root_context.obj, dict) else {}
    context_name = root_object.get("context")
    if context_name is None:
        context_name = root_context.params.get("context")
    return context_name
