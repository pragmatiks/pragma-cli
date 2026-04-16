"""Organization LLM settings commands."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from pragma_sdk import PerformanceProfile
from rich.console import Console
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Organization LLM settings")
console = Console()


def _get_organization_id() -> str:
    """Resolve the current user's organization ID from the API.

    Returns:
        Organization ID string.

    Raises:
        typer.Exit: If not authenticated or organization cannot be resolved.
    """
    client = get_client()

    try:
        user_info = client.get_me()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            console.print("[red]Error:[/red] Not authenticated. Run 'pragma auth login' first.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] Failed to resolve organization: {e.response.text}")
        raise typer.Exit(1) from e

    return user_info.organization_id


@app.command()
def show(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """Show current LLM settings for your organization.

    Displays the selected provider and performance profile.

    Examples:
        pragma settings show
        pragma settings show -o json
    """  # noqa: DOC501
    organization_id = _get_organization_id()
    client = get_client()

    try:
        settings = client.get_organization_settings(organization_id)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1) from e

    if output == OutputFormat.TABLE:
        console.print(f"[bold]Provider:[/bold]  {settings.provider}")
        console.print(f"[bold]Profile:[/bold]   {settings.performance_profile}")
    else:
        data = {
            "provider": settings.provider,
            "performance_profile": settings.performance_profile,
            "organization_id": settings.organization_id,
            "updated_at": settings.updated_at.isoformat(),
        }
        output_data(data, output)


@app.command("set-profile")
def set_profile(
    provider: Annotated[
        str,
        typer.Option("--provider", "-p", help="LLM provider slug (e.g. anthropic, openai, google)"),
    ],
    profile: Annotated[
        PerformanceProfile,
        typer.Option("--profile", help="Performance profile (fast, balanced, reasoning)"),
    ],
) -> None:
    """Update the LLM provider and performance profile for your organization.

    Both --provider and --profile are required. Valid profiles are: fast, balanced, reasoning.

    Examples:
        pragma settings set-profile --provider anthropic --profile balanced
        pragma settings set-profile -p openai --profile fast
    """  # noqa: DOC501
    organization_id = _get_organization_id()
    client = get_client()

    try:
        updated = client.update_organization_settings(
            organization_id,
            provider=provider,
            performance_profile=profile,
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1) from e

    console.print("[green]Updated LLM settings:[/green]")
    console.print(f"  [bold]Provider:[/bold]  {updated.provider}")
    console.print(f"  [bold]Profile:[/bold]   {updated.performance_profile}")


def _print_llm_providers_table(providers: list[dict]) -> None:
    """Print LLM providers in a formatted table.

    Args:
        providers: List of provider dictionaries to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Connected")
    table.add_column("Tiers")

    for p in providers:
        connected = "[green]\u2713[/green]" if p.get("connected") else "[red]\u2717[/red]"
        tiers = ", ".join(p.get("tiers_available", []))
        table.add_row(p.get("label", p.get("slug", "")), connected, tiers or "[dim]-[/dim]")

    console.print(table)


@app.command()
def providers(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
) -> None:
    """List available LLM providers for your organization.

    Shows each provider's connection status and available model tiers.

    Examples:
        pragma settings providers
        pragma settings providers -o json
    """  # noqa: DOC501
    organization_id = _get_organization_id()
    client = get_client()

    try:
        llm_providers = client.list_llm_providers(organization_id)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1) from e

    if not llm_providers:
        console.print("[dim]No LLM providers available.[/dim]")
        return

    data = [
        {
            "slug": p.slug,
            "label": p.label,
            "connected": p.connected,
            "is_platform_default": p.is_platform_default,
            "tiers_available": [t.value for t in p.tiers_available],
        }
        for p in llm_providers
    ]

    output_data(data, output, table_renderer=_print_llm_providers_table)
