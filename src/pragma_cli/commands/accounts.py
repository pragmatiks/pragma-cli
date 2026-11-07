"""Account commands for the signed-in user's own Pragmatiks account."""

from __future__ import annotations

from typing import Annotated

import typer
from pragma_sdk import PragmaClient
from pragma_sdk.config import load_credentials
from rich.console import Console

from pragma_cli import get_client
from pragma_cli.credentials import clear_credentials, extract_client_token
from pragma_cli.errors import error_console, require_auth


app = typer.Typer(help="Account commands")

console = Console()

ACCOUNT_DELETION_WARNING = (
    "This permanently deletes your Pragmatiks account. You leave every organization you belong to. It cannot be undone."
)


def is_stored_credential(client: PragmaClient, context_name: str) -> bool:
    """Tell whether the client authenticates with the context's stored credential.

    Args:
        client: The client the command used.
        context_name: Context whose credentials file entry is compared.

    Returns:
        True when the client's token is the one ``pragma auth login`` stored
        for the context, False for a ``--token`` or environment token.
    """
    token = extract_client_token(client)
    return token is not None and token == load_credentials(context_name)


@app.command("delete")
def delete_account(
    context: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
) -> None:
    """Delete your Pragmatiks account and every membership it holds.

    On success the current context's stored credential is cleared; a token
    given with ``--token`` or an environment variable is left alone.

    Examples:
        pragma accounts delete
        pragma accounts delete --yes

    \f

    Args:
        context: Typer context carrying the top-level resolved context name.
        yes: Skip the confirmation prompt.

    Raises:
        typer.Exit: When the confirmation is declined.
    """
    client = get_client()
    require_auth(client)

    if not yes:
        error_console.print(ACCOUNT_DELETION_WARNING)
        if not typer.confirm("Delete your account?"):
            error_console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit()

    context_name = context.obj["context"]
    clears_credential = is_stored_credential(client, context_name)

    client.delete_account()

    if clears_credential:
        clear_credentials(context_name)

    console.print("[green]✓[/green] Account deleted.")
