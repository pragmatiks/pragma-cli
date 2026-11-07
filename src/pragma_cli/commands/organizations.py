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
from pragma_sdk.models.api import MemberRole, Organization, OrganizationMember
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.provider_output import fetch_with_spinner
from pragma_cli.errors import require_auth
from pragma_cli.helpers import OutputFormat, format_optional_value, output_data


app = typer.Typer(help="Organization management commands")
members_app = typer.Typer(help="Organization member commands")
app.add_typer(members_app, name="members")

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


def print_members_table(members: list[OrganizationMember]) -> None:
    """Print organization members as a table with User ID, Identifier and Role columns.

    Args:
        members: Members of the caller's organization.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("User ID")
    table.add_column("Identifier")
    table.add_column("Role")

    for member in members:
        table.add_row(escape(member.user_id), format_optional_value(member.identifier), member.role.value)

    console.print(table)


@members_app.command("list")
def list_members(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List the members of your organization.

    Examples:
        pragma organizations members list
        pragma organizations members list -o json
    """
    client = get_client()
    require_auth(client)

    members = fetch_with_spinner("Fetching members...", client.list_organization_members)

    if output != OutputFormat.TABLE:
        output_data([member.model_dump(mode="json") for member in members], output)
        return

    if not members:
        console.print("[dim]No members found.[/dim]")
        return

    print_members_table(members)


@members_app.command("update")
def update_member(
    user_id: Annotated[str, typer.Argument(help="User ID of the member")],
    role: Annotated[MemberRole, typer.Option("--role", help="New role of the member")],
) -> None:
    """Update a member's role. Only an organization admin can update roles.

    Examples:
        pragma organizations members update user_123 --role admin
    """
    client = get_client()
    require_auth(client)

    member = fetch_with_spinner("Updating member...", lambda: client.update_organization_member_role(user_id, role))

    console.print(f"[green]✓[/green] Updated {escape(member.user_id)}: {member.role.value}")


@members_app.command("delete")
def delete_member(
    user_id: Annotated[str, typer.Argument(help="User ID of the member")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Delete a member's membership. Deleting your own membership leaves the organization.

    Run ``pragma auth whoami`` to see your own user ID.

    Examples:
        pragma organizations members delete user_123
        pragma organizations members delete user_123 --yes

    \f

    Args:
        user_id: User ID of the member whose membership is deleted.
        yes: Skip the confirmation prompt.

    Raises:
        typer.Exit: When the confirmation is declined.
    """
    client = get_client()
    require_auth(client)

    caller_user_id = fetch_with_spinner("Fetching your account...", client.get_me).user_id

    if not yes and not typer.confirm(f"Delete the membership of {user_id}?"):
        console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit()

    fetch_with_spinner("Deleting membership...", lambda: client.delete_organization_member(user_id))

    if user_id == caller_user_id:
        console.print(
            "[green]✓[/green] You left the organization. Run pragma auth login to choose another organization."
        )
        return

    console.print(f"[green]✓[/green] Deleted the membership of {escape(user_id)}.")
