"""API key commands for the caller's organization: create, list and revoke."""

from __future__ import annotations

from typing import Annotated

import typer
from pragma_sdk.models.api import ApiKey
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.provider_output import fetch_with_spinner
from pragma_cli.errors import error_console, require_auth
from pragma_cli.helpers import OutputFormat, format_optional_value, output_data


app = typer.Typer(help="API key management commands")

console = Console()

STORE_SECRET_WARNING = "Store this secret now: Pragmatiks never shows it again."
"""Warning printed on stderr after an API key is created, since its secret is returned only once."""


@app.command("create")
def create_api_key(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Name of the API key, unique in the organization")],
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Create an API key for your organization and print its secret once.

    Only an organization admin signed in as a user can create API keys. In
    table format stdout carries only the secret, so a shell can capture it;
    ``-o json`` prints the whole record.

    Examples:
        pragma api-keys create ci-runner
        SECRET=$(pragma api-keys create ci-runner)
        pragma api-keys create ci-runner -o json

    \f

    Args:
        context: Typer context carrying the top-level resolved context name.
        name: Name of the API key.
        output: Output format.
    """
    client = get_client()
    require_auth(client)

    created = fetch_with_spinner("Creating API key...", lambda: client.create_api_key(name))

    secret = created.secret.get_secret_value()

    context_name = context.obj["context"]
    error_console.print(
        f"[green]✓[/green] Created API key {escape(created.name)} ({escape(created.id)}) "
        f"in context '{escape(context_name)}'"
    )

    if output == OutputFormat.TABLE:
        typer.echo(secret)
    else:
        record = created.model_dump(mode="json", include={"id", "name", "created_at"}) | {"secret": secret}
        output_data(record, output)

    error_console.print(f"[yellow]{STORE_SECRET_WARNING}[/yellow]")


def print_api_keys_table(api_keys: list[ApiKey]) -> None:
    """Print API keys as a table with ID, Name, Created and Last used columns.

    The ID column is never cut, at any terminal width, since ``revoke`` takes the whole ID;
    the other columns fold onto more lines instead.

    Args:
        api_keys: Active API keys of the caller's organization.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", no_wrap=True, min_width=max(len(api_key.id) for api_key in api_keys))
    table.add_column("Name", overflow="fold")
    table.add_column("Created", overflow="fold")
    table.add_column("Last used", overflow="fold")

    for api_key in api_keys:
        last_used = api_key.last_used_at.isoformat() if api_key.last_used_at else None
        table.add_row(
            escape(api_key.id),
            escape(api_key.name),
            api_key.created_at.isoformat(),
            format_optional_value(last_used),
        )

    console.print(table)


@app.command("list")
def list_api_keys(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List your organization's active API keys, without their secrets.

    Examples:
        pragma api-keys list
        pragma api-keys list -o json

    \f

    Args:
        output: Output format.
    """
    client = get_client()
    require_auth(client)

    api_keys = fetch_with_spinner("Fetching API keys...", client.list_api_keys)

    if output != OutputFormat.TABLE:
        output_data([api_key.model_dump(mode="json") for api_key in api_keys], output)
        return

    if not api_keys:
        console.print("[dim]No API keys found.[/dim]")
        return

    print_api_keys_table(api_keys)


@app.command("revoke")
def revoke_api_key(
    api_key_id: Annotated[str, typer.Argument(help="ID of the API key")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Revoke an API key, so it no longer authenticates. Its name stays taken.

    Only an organization admin signed in as a user can revoke API keys.

    Examples:
        pragma api-keys revoke ak_123
        pragma api-keys revoke ak_123 --yes

    \f

    Args:
        api_key_id: ID of the API key.
        yes: Skip the confirmation prompt.

    Raises:
        typer.Exit: When the confirmation is declined.
    """
    client = get_client()
    require_auth(client)

    if not yes and not typer.confirm(f"Revoke the API key {api_key_id}?"):
        error_console.print("[dim]Cancelled.[/dim]")
        raise typer.Exit()

    fetch_with_spinner("Revoking API key...", lambda: client.revoke_api_key(api_key_id))

    console.print(f"[green]✓[/green] Revoked {escape(api_key_id)}.")
