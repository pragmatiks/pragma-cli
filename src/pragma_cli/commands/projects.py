"""Project management commands."""

from __future__ import annotations

from typing import Annotated

import typer
from pragma_sdk import (
    CreateProjectRequest,
    DeleteProjectRequest,
    Project,
    ProjectHasResourcesError,
    UpdateProjectRequest,
)
from rich.console import Console
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.config import ContextConfig, load_config, update_config
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Project management commands")

console = Console()


def _print_projects_table(projects: list[dict]) -> None:
    """Render projects in a table.

    Args:
        projects: Project payloads to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Slug")
    table.add_column("Name")
    table.add_column("Organization ID")
    table.add_column("Private")
    table.add_column("Updated")

    for project in projects:
        table.add_row(
            project["slug"],
            project["name"],
            project["organization_id"],
            "yes" if project["is_private"] else "no",
            project["updated_at"],
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

    table.add_row("ID", project["id"])
    table.add_row("Slug", project["slug"])
    table.add_row("Name", project["name"])
    table.add_row("Organization ID", project["organization_id"])
    table.add_row("Private", "yes" if project["is_private"] else "no")
    table.add_row("Created", project["created_at"])
    table.add_row("Updated", project["updated_at"])

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
    """
    config = load_config()
    context_name = _active_context_name(ctx)
    context_config = config.contexts[context_name]
    return context_name, context_config


@app.command("list")
def list_projects(
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """List projects visible to the current caller."""
    projects = get_client().list_projects()

    if not projects:
        console.print("[dim]No projects found.[/dim]")
        return

    output_data([_project_payload(project) for project in projects], output, table_renderer=_print_projects_table)


@app.command("get")
def get_project(
    slug: Annotated[str, typer.Argument(help="Project slug")],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
) -> None:
    """Get a project by slug."""
    project = get_client().get_project(slug)
    output_data([_project_payload(project)], output, table_renderer=_print_project_detail)


@app.command("create")
def create_project(
    slug: Annotated[str, typer.Argument(help="Project slug")],
    name: Annotated[str, typer.Option("--name", help="Human-readable project name")],
) -> None:
    """Create a project."""
    project = get_client().create_project(CreateProjectRequest(name=name, slug=slug))
    console.print(f"[green]Created project:[/green] {project.slug}")


@app.command("update")
def update_project(
    slug: Annotated[str, typer.Argument(help="Project slug")],
    name: Annotated[str, typer.Option("--name", help="Human-readable project name")],
) -> None:
    """Update project metadata."""
    project = get_client().update_project(slug, UpdateProjectRequest(name=name))
    console.print(f"[green]Updated project:[/green] {project.slug}")


def _print_orphan_warning(slug: str) -> None:
    """Warn the caller that ``--orphan-resources`` leaves real infrastructure running.

    Args:
        slug: Slug of the project about to be deleted.
    """
    console.print(
        f"[yellow]Warning:[/yellow] --orphan-resources will delete project [bold]{slug}[/bold] from Pragma only."
    )
    console.print(
        "[dim]The underlying infrastructure (kubernetes pods, Supabase projects, "
        "GCP resources, etc.) will keep running[/dim]"
    )
    console.print(
        "[dim]without Pragma managing it. You are exiting tracking, not cleaning up — billing will continue.[/dim]"
    )
    console.print()


def _print_project_has_resources(error: ProjectHasResourcesError, *, orphan_already_requested: bool) -> None:
    """Render the ``ProjectHasResourcesError`` as a user-friendly CLI message.

    Args:
        error: Typed 409 raised by the SDK when a project still holds resources.
        orphan_already_requested: Whether the caller already passed
            ``--orphan-resources``. Suppresses the flag suggestion when True.
    """
    console.print(
        f"[red]Error:[/red] Project [bold]{error.project_id}[/bold] still contains {error.resource_count} resource(s)."
    )

    if error.resources:
        sample_size = len(error.resources)
        if sample_size < error.resource_count:
            console.print(f"[dim]Showing {sample_size} of {error.resource_count}:[/dim]")
        else:
            console.print("[dim]Resources:[/dim]")

        for resource_id in error.resources:
            console.print(f"  [cyan]{resource_id}[/cyan]")

    console.print()

    if orphan_already_requested:
        console.print(
            "[dim]The server refused the request even though --orphan-resources was set. "
            "Delete the resources first with[/dim] "
            "[bold]pragma resources delete <type> <name>[/bold][dim].[/dim]"
        )
        return

    console.print("[dim]Choose one of:[/dim]")
    console.print("  [dim]1. Delete the resources first with[/dim] [bold]pragma resources delete <type> <name>[/bold]")
    console.print(
        "  [dim]2. Re-run with[/dim] [bold]--orphan-resources[/bold] "
        "[dim]to leave the resources running without Pragma tracking[/dim]"
    )


def _resolve_confirmation(yes: bool, confirm: str | None) -> str:
    """Resolve the typed confirmation value from flags or an interactive prompt.

    Args:
        yes: Whether the caller passed ``--yes`` to skip interactive confirmation.
        confirm: Value passed via ``--confirm``, required when ``yes`` is set.

    Returns:
        Typed confirmation string the caller supplied.

    Raises:
        typer.Exit: If ``--yes``/``--confirm`` are combined incorrectly.
    """
    if yes:
        if confirm is None:
            console.print("[red]Error:[/red] --confirm <slug> is required with --yes.")
            raise typer.Exit(2)
        return confirm

    if confirm is not None:
        console.print("[red]Error:[/red] --confirm can only be used together with --yes.")
        raise typer.Exit(2)

    return typer.prompt("Type the project slug to confirm deletion: ")


@app.command("delete")
def delete_project(
    slug: Annotated[str, typer.Argument(help="Project slug")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the interactive confirmation prompt")] = False,
    confirm: Annotated[str | None, typer.Option("--confirm", help="Typed confirmation value")] = None,
    orphan_resources: Annotated[
        bool,
        typer.Option(
            "--orphan-resources",
            help="Delete the project but leave its resources running. "
            "The infrastructure will keep billing — you are exiting Pragma tracking, not cleaning up.",
        ),
    ] = False,
) -> None:
    """Delete a project with typed confirmation.

    By default the server refuses to delete a project that still contains
    resources. Pass ``--orphan-resources`` to remove Pragma's tracking
    without touching the underlying infrastructure.

    Raises:
        typer.Exit: If confirmation flags are invalid, confirmation does not
            match, or the server refuses the delete because resources remain.
    """
    if orphan_resources and not yes:
        _print_orphan_warning(slug)

    confirmation = _resolve_confirmation(yes, confirm)

    if confirmation != slug:
        console.print("[red]Error:[/red] Confirmation did not match project slug.")
        raise typer.Exit(1)

    try:
        get_client().delete_project(
            slug,
            DeleteProjectRequest(confirmation=confirmation, orphan_resources=orphan_resources),
        )
    except ProjectHasResourcesError as error:
        _print_project_has_resources(error, orphan_already_requested=orphan_resources)
        raise typer.Exit(1) from error

    if orphan_resources:
        console.print(f"[green]Deleted project tracking:[/green] {slug}")
        console.print("[dim]Resources were not touched and continue to run outside Pragma.[/dim]")
    else:
        console.print(f"[green]Deleted project:[/green] {slug}")


@app.command("use")
def use_project(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(help="Project slug")],
) -> None:
    """Persist the default project on the current CLI context.

    Honors the global ``--context``/``-c`` flag so that
    ``pragma -c staging projects use <slug>`` writes to the staging
    context instead of the persistent current context.

    Raises:
        typer.Exit: If the active context does not exist in the config.
    """
    context_name = _active_context_name(ctx)

    with update_config() as config:
        if context_name not in config.contexts:
            console.print(f"[red]Error:[/red] Context '{context_name}' not found in configuration.")
            raise typer.Exit(2)

        config.contexts[context_name].project = slug

    console.print(f"[green]Current project for context '{context_name}':[/green] {slug}")


@app.command("current")
def current_project(ctx: typer.Context) -> None:
    """Show the current default project for the active context.

    Honors the global ``--context``/``-c`` flag so the reported project
    reflects the context the rest of the CLI is operating on.
    """
    _, context_config = _current_context_config(ctx)
    console.print(context_config.project or "none set")
