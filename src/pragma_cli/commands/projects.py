"""Project management commands."""

from __future__ import annotations

import re
from typing import Annotated

import click
import typer
from pragma_sdk import (
    CreateProjectRequest,
    DeleteProjectRequest,
    Project,
    ProjectHasResourcesError,
    UpdateProjectRequest,
)
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.config import ContextConfig, load_config, select_context, update_config
from pragma_cli.errors import error_console
from pragma_cli.exit_codes import FAILURE_EXIT_CODE, INPUT_ERROR_EXIT_CODE
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Project management commands")

console = Console()

_MAX_DISPLAYED_RESOURCES = 20
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_display(value: str) -> str:
    """Make a server-supplied string safe to render via Rich.

    Escapes Rich markup syntax (so ``[red]...[/red]`` from the API prints
    literally instead of colouring later output) and strips control
    characters that could corrupt column alignment or inject ANSI
    escapes into the admin's terminal.

    Args:
        value: Untrusted string, typically a resource or project
            identifier returned by the API.

    Returns:
        A string safe to pass to ``console.print`` inside an f-string.
    """
    return escape(_CONTROL_CHARS_RE.sub("", value))


def _print_projects_table(projects: list[dict]) -> None:
    """Render projects in a table.

    Args:
        projects: Project payloads to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Organization ID")
    table.add_column("Updated")

    for project in projects:
        table.add_row(
            escape(project["project_id"]),
            escape(project["name"]),
            escape(project["organization_id"]),
            escape(project["updated_at"]),
        )

    console.print(table)


def _print_project_detail(projects: list[dict]) -> None:
    """Render a single project as key-value rows.

    Args:
        projects: Single-item list containing the target project.
    """
    project = projects[0]

    table = Table(show_header=False, box=None)
    table.add_column("Field", style="bold")
    table.add_column("Value")

    table.add_row("ID", escape(project["project_id"]))
    table.add_row("Name", escape(project["name"]))
    table.add_row("Organization ID", escape(project["organization_id"]))
    table.add_row("Created", escape(project["created_at"]))
    table.add_row("Updated", escape(project["updated_at"]))

    console.print(table)


def _project_payload(project: Project) -> dict:
    """Convert a project model to JSON-safe CLI output data.

    Args:
        project: Project model from the SDK.

    Returns:
        JSON-serializable project payload.
    """
    return project.model_dump(mode="json")


def _active_context_name(ctx: typer.Context) -> str:
    """Return the resolved context name for the active CLI invocation.

    Honors the global ``--context``/``-c`` flag by reading the value the
    root callback stored on ``ctx.obj``. Falls back to the persistent
    current context when the root callback has not run (e.g. completion).

    Args:
        ctx: Active Typer context for the invoked command.

    Returns:
        Context name to operate on.
    """
    root_obj = ctx.find_root().obj if ctx is not None else None
    if isinstance(root_obj, dict):
        context_name = root_obj.get("context")
        if context_name:
            return context_name

    return load_config().current_context


def _current_context_config(ctx: typer.Context) -> tuple[str, ContextConfig]:
    """Return the active context and its config object.

    Args:
        ctx: Active Typer context used to resolve the target context name.

    Returns:
        Tuple of context name and mutable context config.

    Raises:
        UnknownContextError: If the context is not in the configuration.
    """  # noqa: DOC502
    context_name = _active_context_name(ctx)
    return context_name, select_context(load_config(), context_name)


@app.command("list")
def list_projects(
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """List projects visible to the current caller."""
    projects = get_client().list_projects()

    if not projects and output == OutputFormat.TABLE:
        console.print("[dim]No projects found.[/dim]")
        return

    output_data([_project_payload(project) for project in projects], output, table_renderer=_print_projects_table)


@app.command("get")
def get_project(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Get a project by ID."""
    project = get_client().get_project(project_id)
    output_data([_project_payload(project)], output, table_renderer=_print_project_detail)


@app.command("create")
def create_project(
    name: Annotated[str, typer.Argument(help="Human-readable project name")],
) -> None:
    """Create a project."""
    project = get_client().create_project(CreateProjectRequest(name=name))
    console.print(f"[green]Created project:[/green] {escape(project.name)} ({escape(project.project_id)})")


@app.command("update")
def update_project(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    name: Annotated[str, typer.Option("--name", help="Human-readable project name")],
) -> None:
    """Update project metadata."""
    project = get_client().update_project(project_id, UpdateProjectRequest(name=name))
    console.print(f"[green]Updated project:[/green] {escape(project.name)} ({escape(project.project_id)})")


def _print_orphan_warning(name: str) -> None:
    """Warn the caller that ``--orphan-resources`` leaves real infrastructure running.

    Args:
        name: Name of the project about to be deleted.
    """
    error_console.print(
        f"[yellow]Warning:[/yellow] --orphan-resources will delete project [bold]{escape(name)}[/bold] "
        "from Pragmatiks only."
    )
    error_console.print(
        "[dim]The underlying infrastructure (kubernetes pods, Supabase projects, "
        "GCP resources, etc.) will keep running[/dim]"
    )
    error_console.print(
        "[dim]without Pragmatiks managing it. You are exiting tracking, not cleaning up — billing will continue.[/dim]"
    )
    error_console.print()


def _print_project_has_resources(error: ProjectHasResourcesError, *, orphan_already_requested: bool) -> None:
    """Render the ``ProjectHasResourcesError`` as a user-friendly CLI message.

    Server-supplied fields (``project_id``, each entry in ``resources``) are
    sanitized before rendering so that crafted identifiers cannot inject
    Rich markup or ANSI escapes into the admin's terminal. The displayed
    sample is also capped locally at :data:`_MAX_DISPLAYED_RESOURCES` as
    defense-in-depth against an uncapped server response.

    Args:
        error: Typed 409 raised by the SDK when a project still holds resources.
        orphan_already_requested: Whether the caller already passed
            ``--orphan-resources``. Suppresses the flag suggestion when True.
    """
    safe_project_id = _sanitize_display(error.project_id)
    error_console.print(
        f"[red]Error:[/red] Project [bold]{safe_project_id}[/bold] still contains {error.resource_count} resource(s)."
    )

    if error.resources:
        display_resources = error.resources[:_MAX_DISPLAYED_RESOURCES]
        sample_size = len(display_resources)
        truncated = sample_size < len(error.resources) or sample_size < error.resource_count
        if truncated:
            error_console.print(f"[dim]Showing {sample_size} of {error.resource_count}:[/dim]")
        else:
            error_console.print("[dim]Resources:[/dim]")

        for resource_id in display_resources:
            error_console.print(f"  [cyan]{_sanitize_display(resource_id)}[/cyan]")

    error_console.print()

    if orphan_already_requested:
        error_console.print(
            "[dim]The server refused the request even though --orphan-resources was set. "
            "Delete the resources first with[/dim] "
            "[bold]pragma resources delete <org/provider/resource/name>[/bold][dim].[/dim]"
        )
        return

    error_console.print("[dim]Choose one of:[/dim]")
    error_console.print(
        "  [dim]1. Delete the resources first with[/dim] "
        "[bold]pragma resources delete <org/provider/resource/name>[/bold]"
    )
    error_console.print(
        "  [dim]2. Re-run with[/dim] [bold]--orphan-resources[/bold] "
        "[dim]to leave the resources running without Pragmatiks tracking[/dim]"
    )


def check_confirmation_flags(yes: bool, confirm: str | None) -> None:
    """Check that ``--yes`` and ``--confirm`` are given together or not at all.

    Args:
        yes: Whether the caller passed ``--yes`` to skip interactive confirmation.
        confirm: Value passed via ``--confirm``, required when ``yes`` is set.

    Raises:
        click.UsageError: If only one of ``--yes`` and ``--confirm`` is given.
    """
    if yes and confirm is None:
        raise click.UsageError("--confirm <name> is required with --yes.")

    if not yes and confirm is not None:
        raise click.UsageError("--confirm can only be used together with --yes.")


@app.command("delete")
def delete_project(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the interactive confirmation prompt")] = False,
    confirm: Annotated[str | None, typer.Option("--confirm", help="Typed confirmation value")] = None,
    orphan_resources: Annotated[
        bool,
        typer.Option(
            "--orphan-resources",
            help="Delete the project but leave its resources running. "
            "The infrastructure will keep billing — you are exiting Pragmatiks tracking, not cleaning up.",
        ),
    ] = False,
) -> None:
    """Delete a project with typed confirmation of its name.

    By default the server refuses to delete a project that still contains
    resources. Pass ``--orphan-resources`` to remove Pragmatiks' tracking
    without touching the underlying infrastructure.

    \f

    Raises:
        typer.Exit: If confirmation does not match, or the server refuses the
            delete because resources remain.
        click.UsageError: If ``--yes`` and ``--confirm`` are combined incorrectly.
    """  # noqa: DOC502
    check_confirmation_flags(yes, confirm)

    client = get_client()
    project = client.get_project(project_id)

    if orphan_resources and not yes:
        _print_orphan_warning(project.name)

    confirmation = confirm if confirm is not None else typer.prompt("Type the project name to confirm deletion: ")

    if confirmation != project.name:
        error_console.print(
            f"[red]Error:[/red] Confirmation did not match the project name '{escape(project.name)}'. "
            "Type the name exactly, or pass --yes --confirm <name>."
        )
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    try:
        client.delete_project(
            project_id,
            DeleteProjectRequest(confirmation=confirmation, orphan_resources=orphan_resources),
        )
    except ProjectHasResourcesError as error:
        _print_project_has_resources(error, orphan_already_requested=orphan_resources)
        raise typer.Exit(FAILURE_EXIT_CODE) from error

    if orphan_resources:
        console.print(f"[green]Deleted project tracking:[/green] {escape(project.name)}")
        console.print("[dim]Resources were not touched and continue to run outside Pragmatiks.[/dim]")
    else:
        console.print(f"[green]Deleted project:[/green] {escape(project.name)}")


@app.command("use")
def use_project(
    ctx: typer.Context,
    project_id: Annotated[str, typer.Argument(help="Project ID")],
) -> None:
    """Persist the default project on the current CLI context.

    Honors the global ``--context``/``-c`` flag so that
    ``pragma -c staging projects use <project-id>`` writes to the staging
    context instead of the persistent current context.

    \f

    Raises:
        UnknownContextError: If the active context is not in the configuration.
    """  # noqa: DOC502
    context_name = _active_context_name(ctx)

    with update_config() as config:
        select_context(config, context_name).project = project_id

    console.print(f"[green]Current project for context '{escape(context_name)}':[/green] {escape(project_id)}")


@app.command("current")
def current_project(ctx: typer.Context) -> None:
    """Show the current default project for the active context.

    Honors the global ``--context``/``-c`` flag so the reported project
    reflects the context the rest of the CLI is operating on.
    """
    _, context_config = _current_context_config(ctx)
    console.print(escape(context_config.project or "none set"))
