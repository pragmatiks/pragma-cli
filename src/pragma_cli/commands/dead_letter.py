"""Dead letter event management commands.

Commands for listing, inspecting, retrying, and deleting dead letter events
from the Pragmatiks platform. Dead letter events are failed resource
operations that exceeded retry attempts.
"""

from __future__ import annotations

import json
from typing import Annotated

import httpx
import typer
from rich import print
from rich.console import Console
from rich.table import Table

from pragma_cli import get_client


app = typer.Typer(help="Dead letter event management commands")

console = Console()


def truncate(text: str, max_length: int = 50) -> str:
    """Truncate text to max_length, adding ellipsis if needed.

    Args:
        text: Text to truncate.
        max_length: Maximum length including ellipsis.

    Returns:
        Truncated text with ellipsis if exceeded max_length.
    """
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


@app.command("list")
def list_events(
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="Filter by provider name"),
    ] = None,
):
    """List dead letter events.

    Displays a table of all dead letter events, optionally filtered by provider.
    The error_message column is truncated to 50 characters for readability.

    Example:
        pragma ops dead-letter list
        pragma ops dead-letter list --provider postgres
    """
    client = get_client()
    events = client.list_dead_letter_events(provider=provider)

    if not events:
        print("[dim]No dead letter events found.[/dim]")
        return

    table = Table()
    table.add_column("Event ID", style="cyan")
    table.add_column("Provider", style="green")
    table.add_column("Resource Type")
    table.add_column("Resource Name")
    table.add_column("Error Message", style="red")
    table.add_column("Failed At")

    for event in events:
        table.add_row(
            event.get("id", ""),
            event.get("provider", ""),
            event.get("resource_type", ""),
            event.get("resource_name", ""),
            truncate(event.get("error_message", ""), 50),
            event.get("failed_at", ""),
        )

    console.print(table)


@app.command()
def show(
    event_id: Annotated[str, typer.Argument(help="Dead letter event ID")],
):
    """Show detailed information about a dead letter event.

    Displays the full event data as formatted JSON.

    Example:
        pragma ops dead-letter show evt_123abc

    Raises:
        typer.Exit: If event not found (code 1).
    """  # noqa: DOC501
    client = get_client()
    try:
        event = client.get_dead_letter_event(event_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print(f"[red]Event not found:[/red] {event_id}")
            raise typer.Exit(1)
        raise

    print(json.dumps(event, indent=2, default=str))


@app.command()
def retry(
    event_id: Annotated[
        str | None,
        typer.Argument(help="Dead letter event ID to retry"),
    ] = None,
    all_events: Annotated[
        bool,
        typer.Option("--all", help="Retry all dead letter events"),
    ] = False,
):
    """Retry dead letter event(s).

    Retries a single event by ID, or all events with --all flag.
    The --all flag requires confirmation.

    Example:
        pragma ops dead-letter retry evt_123abc
        pragma ops dead-letter retry --all

    Raises:
        typer.Exit: If event not found (code 1) or user cancels (code 0).
    """  # noqa: DOC501
    client = get_client()

    if all_events:
        events = client.list_dead_letter_events()
        count = len(events)

        if count == 0:
            print("[dim]No dead letter events to retry.[/dim]")
            return

        if not typer.confirm(f"Retry {count} event(s)?"):
            raise typer.Exit(0)

        retried = client.retry_all_dead_letter_events()
        print(f"[green]Retried {retried} event(s)[/green]")
    elif event_id:
        try:
            client.retry_dead_letter_event(event_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"[red]Event not found:[/red] {event_id}")
                raise typer.Exit(1)
            raise

        print(f"[green]Retried event:[/green] {event_id}")
    else:
        print("[red]Error:[/red] Provide an event_id or use --all")
        raise typer.Exit(1)


@app.command()
def delete(
    event_id: Annotated[
        str | None,
        typer.Argument(help="Dead letter event ID to delete"),
    ] = None,
    all_events: Annotated[
        bool,
        typer.Option("--all", help="Delete all dead letter events"),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option("--provider", "-p", help="Delete all events for this provider"),
    ] = None,
):
    """Delete dead letter event(s).

    Deletes a single event by ID, all events for a provider, or all events.
    The --all and --provider flags require confirmation.

    Example:
        pragma ops dead-letter delete evt_123abc
        pragma ops dead-letter delete --all
        pragma ops dead-letter delete --provider postgres

    Raises:
        typer.Exit: If event not found (code 1) or user cancels (code 0).
    """  # noqa: DOC501
    client = get_client()

    if all_events:
        events = client.list_dead_letter_events()
        count = len(events)

        if count == 0:
            print("[dim]No dead letter events to delete.[/dim]")
            return

        if not typer.confirm(f"Delete {count} event(s)?"):
            raise typer.Exit(0)

        deleted = client.delete_dead_letter_events(all=True)
        print(f"[green]Deleted {deleted} event(s)[/green]")
    elif provider:
        events = client.list_dead_letter_events(provider=provider)
        count = len(events)

        if count == 0:
            print(f"[dim]No dead letter events found for provider '{provider}'.[/dim]")
            return

        if not typer.confirm(f"Delete {count} event(s) for provider '{provider}'?"):
            raise typer.Exit(0)

        deleted = client.delete_dead_letter_events(provider=provider)
        print(f"[green]Deleted {deleted} event(s) for provider '{provider}'[/green]")
    elif event_id:
        try:
            client.delete_dead_letter_event(event_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                print(f"[red]Event not found:[/red] {event_id}")
                raise typer.Exit(1)
            raise

        print(f"[green]Deleted event:[/green] {event_id}")
    else:
        print("[red]Error:[/red] Provide an event_id, --provider, or --all")
        raise typer.Exit(1)
