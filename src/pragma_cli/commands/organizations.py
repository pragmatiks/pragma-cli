"""Organization management commands."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.bootstrap_errors import check_bootstrap_error
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Organization management commands")

console = Console()


def _format_status_color(status: str) -> str:
    """Format organization status with color markup.

    Returns:
        Status string wrapped in Rich color markup.
    """
    status_colors = {
        "active": "green",
        "deactivating": "yellow",
        "deleted": "red",
    }
    color = status_colors.get(status.lower(), "white")
    return f"[{color}]{status}[/{color}]"


def _print_organizations_table(organizations: list[dict]) -> None:
    """Print organizations in a formatted table.

    Args:
        organizations: List of organization dictionaries to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Organization ID")
    table.add_column("Name")
    table.add_column("Slug")
    table.add_column("Status")

    for org in organizations:
        table.add_row(
            org.get("organization_id", ""),
            org.get("name", ""),
            org.get("slug", ""),
            _format_status_color(org.get("status", "")),
        )

    console.print(table)


@app.command("list")
def list_organizations(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List all organizations.

    Displays a table of organizations accessible to the current user.

    Examples:
        pragma organizations list
        pragma organizations list -o json
    """  # noqa: DOC501
    client = get_client()

    organizations = client.list_organizations()

    if not organizations:
        console.print("[dim]No organizations found.[/dim]")
        return

    output_data(
        [org.model_dump(mode="json") for org in organizations],
        output,
        table_renderer=_print_organizations_table,
    )


@app.command()
def cleanup(
    organization_id: Annotated[str, typer.Argument(help="Organization ID to clean up")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Trigger cleanup for an organization.

    Initiates resource teardown and deprovisioning for all resources
    within the organization. This is a destructive operation.

    Examples:
        pragma organizations cleanup org_abc123
        pragma organizations cleanup org_abc123 --yes

    Raises:
        typer.Exit: If organization not found or user cancels.
    """  # noqa: DOC501
    client = get_client()

    if not yes:
        confirm = typer.confirm(f"Clean up organization '{organization_id}'? This will tear down all resources.")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        client.cleanup_organization(organization_id)
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Organization not found: {organization_id}")
            raise typer.Exit(1) from e

        raise

    console.print(f"[green]Cleanup initiated for organization:[/green] {organization_id}")
