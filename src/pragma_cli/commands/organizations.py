"""Organization management commands.

Customer-realm self-scoped commands for the caller's own organization.
Cross-tenant administration (listing every organization, reading
another tenant's record) lives under the console realm and is exposed
by the separate ``pragma-console`` CLI; those endpoints are
deliberately not reachable from this CLI.
"""

from __future__ import annotations

from typing import Annotated

import typer
from pragma_sdk.models.api import Organization
from rich.console import Console
from rich.markup import escape

from pragma_cli import get_client
from pragma_cli.errors import require_auth
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Organization management commands")

console = Console()


_STATUS_COLORS: dict[str, str] = {
    "ready": "green",
    "bootstrapping": "yellow",
    "deleting": "yellow",
    "bootstrap_failed": "red",
    "deleted": "red",
}

DELETION_STATUS_LINES: dict[str, tuple[str, str]] = {
    "deleting": (
        "Deleting {slug}. Waiting on platform teardown. Contact support if this does not finish.",
        "Deleting your organization. Waiting on platform teardown. Contact support if this does not finish.",
    ),
    "deleted": (
        "Organization {slug} has been deleted.",
        "Your organization has been deleted.",
    ),
}
"""Line printed under the ``deleting`` and ``deleted`` statuses: the form naming the slug, then the form for no slug."""


def _format_status(status: str) -> str:
    """Wrap a lifecycle status string in Rich colour markup.

    Args:
        status: Lifecycle status returned by the API.

    Returns:
        Status string wrapped in Rich color markup.
    """
    color = _STATUS_COLORS.get(status.lower(), "white")
    return f"[{color}]{escape(status)}[/{color}]"


def _print_organization_panel(organization: Organization) -> None:
    """Render the caller's organization as a labelled key/value list.

    Args:
        organization: The fetched organization record.
    """
    console.print()
    console.print("[bold]Organization[/bold]")
    console.print()
    console.print(f"  ID:      [cyan]{escape(organization.organization_id)}[/cyan]")
    console.print(f"  Name:    [cyan]{escape(organization.name)}[/cyan]")
    console.print(f"  Slug:    [cyan]{escape(organization.slug)}[/cyan]")
    console.print(f"  Status:  {_format_status(organization.status.value)}")
    console.print(f"  Created: [dim]{organization.created_at.isoformat()}[/dim]")
    console.print(f"  Updated: [dim]{organization.updated_at.isoformat()}[/dim]")


@app.command("me")
def show_me(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Show the caller's own organization.

    Prints the organization's ID, name, slug, status and timestamps.

    Examples:
        pragma organizations me
        pragma organizations me -o json
    """
    client = get_client()
    require_auth(client)

    response = client._request("GET", "/organizations/me")

    organization = Organization.model_validate(response)

    if output == OutputFormat.TABLE:
        _print_organization_panel(organization)
        return

    output_data(organization.model_dump(mode="json"), output)


def print_organization_status(status: str, slug: str | None) -> None:
    """Render the organization status, with the deletion line under ``deleting`` and ``deleted``.

    Args:
        status: Stored organization status returned by the API.
        slug: The organization's slug, or ``None`` when the API has no
            organization record yet; the deletion line then names "your
            organization".
    """
    console.print()
    console.print("[bold]Organization Status[/bold]")
    console.print()
    console.print(f"  Status: {_format_status(status)}")

    deletion_lines = DELETION_STATUS_LINES.get(status)

    if deletion_lines is None:
        return

    named_line, unnamed_line = deletion_lines
    console.print()
    console.print(named_line.format(slug=escape(slug)) if slug else unnamed_line)


@app.command("status")
def show_status(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Show the status of the caller's own organization.

    Prints the stored status: bootstrapping, ready, bootstrap_failed,
    deleting or deleted. While the organization is being deleted, and once
    it is deleted, a line under the status says so. JSON and YAML output is
    the API's answer as returned. Polling is safe in every status, and the
    command exits 0 in every status.

    Examples:
        pragma organizations status
        pragma organizations status -o json
    """
    client = get_client()
    require_auth(client)

    organization_status = client.get_organization_status()

    if output == OutputFormat.TABLE:
        print_organization_status(organization_status.status, organization_status.slug)
        return

    output_data(organization_status.model_dump(mode="json"), output)
