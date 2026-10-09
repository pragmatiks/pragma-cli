"""Organization management commands.

Customer-realm self-scoped commands for the caller's own organization.
Cross-tenant administration (listing every organization, reading
another tenant's record, triggering tenant cleanup) lives under the
console realm and is exposed by the separate ``pragma-console`` CLI;
those endpoints are deliberately not reachable from this CLI.
"""

from __future__ import annotations

from typing import Annotated, Any

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
    "active": "green",
    "ready": "green",
    "bootstrapping": "yellow",
    "deactivating": "yellow",
    "pending": "yellow",
    "failed": "red",
    "deleted": "red",
}


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

    Calls ``GET /organizations/me`` and renders the result. Replaces
    the previous cross-tenant listing commands, which moved to the
    console realm.

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


@app.command("status")
def show_status(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Show the bootstrap status of the caller's own organization.

    Calls ``GET /organizations/me/status``. The endpoint collapses the
    internal lifecycle into three public states: ``bootstrapping``,
    ``ready``, and ``failed``. Polling is safe while the organization
    is bootstrapping.

    Examples:
        pragma organizations status
        pragma organizations status -o json
    """
    client = get_client()
    require_auth(client)

    response: dict[str, Any] = client._request("GET", "/organizations/me/status")

    status_value = str(response.get("status", "unknown"))

    if output == OutputFormat.TABLE:
        console.print()
        console.print("[bold]Organization Status[/bold]")
        console.print()
        console.print(f"  Status: {_format_status(status_value)}")
        return

    output_data({"status": status_value}, output)
