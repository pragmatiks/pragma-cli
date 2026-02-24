"""Provider Store commands for browsing, installing, and managing store providers."""

from __future__ import annotations

from typing import Annotated

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Provider Store commands")
console = Console()


TRUST_TIER_STYLES = {
    "official": "green",
    "verified": "blue",
    "community": "dim",
}


def _format_trust_tier(tier: str) -> str:
    color = TRUST_TIER_STYLES.get(tier, "white")
    return f"[{color}]{tier}[/{color}]"


def _format_api_error(error: httpx.HTTPStatusError) -> str:
    try:
        detail = error.response.json().get("detail", error.response.text)
    except Exception:
        return error.response.text or str(error)

    if isinstance(detail, str):
        return detail

    return detail.get("message", str(error))


def _fetch_with_spinner(description: str, fetch_fn):
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description, total=None)
        return fetch_fn()


@app.command("list")
def list_providers(
    trust_tier: Annotated[
        str | None,
        typer.Option("--trust-tier", help="Filter by trust tier (official, verified, community)"),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Filter by tags (comma-separated)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of results"),
    ] = 20,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Offset for pagination"),
    ] = 0,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """List available providers in the store.

    Browse the provider catalog with optional filters for trust tier and tags.

    Examples:
        pragma store list
        pragma store list --trust-tier official
        pragma store list --tags ml,vector --limit 10
        pragma store list -o json
    """  # noqa: DOC501
    client = get_client()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    try:
        result = _fetch_with_spinner(
            "Fetching providers...",
            lambda: client.list_store_providers(
                trust_tier=trust_tier,
                tags=tag_list,
                limit=limit,
                offset=offset,
            ),
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    if not result.items:
        console.print("[dim]No providers found.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_store_list_table(result)
    else:
        data = [_provider_summary_to_dict(p) for p in result.items]
        output_data(data, output)


@app.command("search")
def search_providers(
    query: Annotated[str, typer.Argument(help="Search query")],
    trust_tier: Annotated[
        str | None,
        typer.Option("--trust-tier", help="Filter by trust tier (official, verified, community)"),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Filter by tags (comma-separated)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of results"),
    ] = 20,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Offset for pagination"),
    ] = 0,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Search for providers in the store.

    Examples:
        pragma store search postgres
        pragma store search "vector database" --trust-tier official
        pragma store search ml --tags embeddings -o json
    """  # noqa: DOC501
    client = get_client()
    tag_list = [t.strip() for t in tags.split(",")] if tags else None

    try:
        result = _fetch_with_spinner(
            f"Searching for '{query}'...",
            lambda: client.list_store_providers(
                q=query,
                trust_tier=trust_tier,
                tags=tag_list,
                limit=limit,
                offset=offset,
            ),
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    if not result.items:
        console.print(f"[dim]No providers found matching '{query}'.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_store_list_table(result)
    else:
        data = [_provider_summary_to_dict(p) for p in result.items]
        output_data(data, output)


@app.command("info")
def info_provider(
    name: Annotated[str, typer.Argument(help="Provider name")],
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Show detailed information about a store provider.

    Displays provider metadata and version history.

    Examples:
        pragma store info qdrant
        pragma store info postgres -o json
    """  # noqa: DOC501
    client = get_client()

    try:
        detail = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_store_provider(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    if output == OutputFormat.TABLE:
        _print_provider_info(detail)
    else:
        data = _provider_detail_to_dict(detail)
        output_data(data, output)


@app.command("install")
def install_provider(
    name: Annotated[str, typer.Argument(help="Provider name to install")],
    version: Annotated[str | None, typer.Argument(help="Version to install (default: latest)")] = None,
    tier: Annotated[
        str,
        typer.Option("--tier", help="Resource tier (free, standard, performance)"),
    ] = "standard",
    upgrade_policy: Annotated[
        str,
        typer.Option("--upgrade-policy", help="Upgrade policy (auto, manual)"),
    ] = "manual",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Install a provider from the store.

    Examples:
        pragma store install qdrant
        pragma store install postgres 1.2.0
        pragma store install redis --tier performance --upgrade-policy auto
        pragma store install qdrant -y
    """  # noqa: DOC501
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        detail = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_store_provider(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    provider = detail.provider
    display = getattr(provider, "display_name", None) or name
    install_version = version or getattr(provider, "latest_version", "latest")

    console.print(f"[bold]Provider:[/bold] {display} ({name})")
    console.print(f"[bold]Version:[/bold]  {install_version}")
    console.print(f"[bold]Tier:[/bold]     {tier}")
    console.print()

    if not yes:
        confirm = typer.confirm("Install this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Installing provider...",
            lambda: client.install_store_provider(
                name,
                version=version,
                resource_tier=tier,
                upgrade_policy=upgrade_policy,
            ),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already installed.")
            raise typer.Exit(1)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    console.print(f"[green]Installed:[/green] {name} v{result.installed_version}")


@app.command("uninstall")
def uninstall_provider(
    name: Annotated[str, typer.Argument(help="Provider name to uninstall")],
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Delete all resources created by this provider"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
):
    """Uninstall a provider.

    Examples:
        pragma store uninstall qdrant
        pragma store uninstall postgres --cascade
        pragma store uninstall redis --force
    """  # noqa: DOC501
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    console.print(f"[bold]Provider:[/bold] {name}")

    if cascade:
        console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")

    console.print()

    if not force:
        action = "UNINSTALL provider and delete all its resources" if cascade else "UNINSTALL provider"
        confirm = typer.confirm(f"Are you sure you want to {action}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        _fetch_with_spinner(
            "Uninstalling provider...",
            lambda: client.uninstall_store_provider(name, cascade=cascade),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1)
        if e.response.status_code == 409:
            console.print(f"[red]Error:[/red] Provider '{name}' has active resources.")
            console.print("[dim]Use --cascade to delete all resources with the provider.[/dim]")
            raise typer.Exit(1)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    console.print(f"[green]Uninstalled:[/green] {name}")


@app.command("upgrade")
def upgrade_provider(
    name: Annotated[str, typer.Argument(help="Provider name to upgrade")],
    version: Annotated[str | None, typer.Argument(help="Target version (default: latest)")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Upgrade an installed provider to a newer version.

    Examples:
        pragma store upgrade qdrant
        pragma store upgrade postgres 2.0.0
        pragma store upgrade redis -y
    """  # noqa: DOC501
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    target = version or "latest"
    console.print(f"[bold]Upgrading:[/bold] {name} -> {target}")
    console.print()

    if not yes:
        confirm = typer.confirm("Proceed with upgrade?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Upgrading provider...",
            lambda: client.upgrade_store_provider(name, version=version),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1)
        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already on the requested version.")
            raise typer.Exit(1)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    console.print(f"[green]Upgraded:[/green] {name} -> v{result.installed_version}")


@app.command("installed")
def list_installed(
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """List installed providers.

    Shows all providers installed from the store with their version and upgrade status.

    Examples:
        pragma store installed
        pragma store installed -o json
    """  # noqa: DOC501
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        providers = _fetch_with_spinner(
            "Fetching installed providers...",
            lambda: client.list_installed_providers(),
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_installed_table(providers)
    else:
        data = [_installed_provider_to_dict(p) for p in providers]
        output_data(data, output)


def _provider_summary_to_dict(provider) -> dict:
    return {
        "name": provider.name,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": getattr(getattr(provider, "author", None), "org_name", None),
        "trust_tier": getattr(provider, "trust_tier", None),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
    }


def _provider_detail_to_dict(detail) -> dict:
    provider = detail.provider
    versions = detail.versions or []

    return {
        "name": provider.name,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": getattr(getattr(provider, "author", None), "org_name", None),
        "trust_tier": getattr(provider, "trust_tier", None),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
        "readme": getattr(provider, "readme", None),
        "created_at": str(getattr(provider, "created_at", None)),
        "updated_at": str(getattr(provider, "updated_at", None)),
        "versions": [
            {
                "version": v.version,
                "status": getattr(v, "status", None),
                "runtime_version": getattr(v, "runtime_version", None),
                "published_at": str(getattr(v, "published_at", None)),
                "changelog": getattr(v, "changelog", None),
            }
            for v in versions
        ],
    }


def _installed_provider_to_dict(provider) -> dict:
    return {
        "store_provider_name": provider.store_provider_name,
        "installed_version": provider.installed_version,
        "upgrade_policy": getattr(provider, "upgrade_policy", None),
        "resource_tier": getattr(provider, "resource_tier", None),
        "installed_at": str(getattr(provider, "installed_at", None)),
        "latest_version": getattr(provider, "latest_version", None),
        "upgrade_available": getattr(provider, "upgrade_available", False),
    }


def _print_store_list_table(result) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Display Name")
    table.add_column("Trust Tier")
    table.add_column("Latest Version")
    table.add_column("Installs", justify="right")
    table.add_column("Tags")

    for provider in result.items:
        tags_display = ", ".join(getattr(provider, "tags", []) or [])
        install_count = getattr(provider, "install_count", 0) or 0

        table.add_row(
            provider.name,
            getattr(provider, "display_name", None) or "[dim]-[/dim]",
            _format_trust_tier(getattr(provider, "trust_tier", "community")),
            getattr(provider, "latest_version", None) or "[dim]-[/dim]",
            str(install_count),
            tags_display or "[dim]-[/dim]",
        )

    console.print(table)

    total = getattr(result, "total", 0)
    offset = getattr(result, "offset", 0)
    showing_end = min(offset + len(result.items), total)
    console.print(f"[dim]Showing {offset + 1}-{showing_end} of {total} providers[/dim]")


def _print_provider_info(detail) -> None:
    provider = detail.provider
    versions = detail.versions or []

    trust_tier = getattr(provider, "trust_tier", "community")
    author = getattr(getattr(provider, "author", None), "org_name", None) or "[dim]-[/dim]"
    tags = ", ".join(getattr(provider, "tags", []) or []) or "[dim]-[/dim]"
    install_count = getattr(provider, "install_count", 0) or 0
    description = getattr(provider, "description", None) or "[dim]No description[/dim]"
    created_at = getattr(provider, "created_at", None)
    updated_at = getattr(provider, "updated_at", None)

    info_lines = [
        f"[bold]Name:[/bold]         {provider.name}",
        f"[bold]Display Name:[/bold] {getattr(provider, 'display_name', None) or provider.name}",
        f"[bold]Author:[/bold]       {author}",
        f"[bold]Trust Tier:[/bold]   {_format_trust_tier(trust_tier)}",
        f"[bold]Description:[/bold]  {description}",
        f"[bold]Tags:[/bold]         {tags}",
        f"[bold]Installs:[/bold]     {install_count}",
    ]

    if created_at:
        info_lines.append(f"[bold]Created:[/bold]      {str(created_at)[:19]}")

    if updated_at:
        info_lines.append(f"[bold]Updated:[/bold]      {str(updated_at)[:19]}")

    panel = Panel("\n".join(info_lines), title=provider.name, border_style="blue")
    console.print(panel)

    if versions:
        console.print()

        version_table = Table(show_header=True, header_style="bold")
        version_table.add_column("Version")
        version_table.add_column("Status")
        version_table.add_column("Runtime Version")
        version_table.add_column("Published")

        for v in versions:
            status = getattr(v, "status", None) or "-"
            status_display = _format_version_status(status)
            runtime = getattr(v, "runtime_version", None) or "[dim]-[/dim]"
            published = str(getattr(v, "published_at", None) or "-")[:19]

            version_table.add_row(
                v.version,
                status_display,
                runtime,
                published,
            )

        console.print(version_table)


def _format_version_status(status: str) -> str:
    status_colors = {
        "published": "green",
        "building": "yellow",
        "failed": "red",
        "yanked": "dim",
    }
    color = status_colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _print_installed_table(providers) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Version")
    table.add_column("Tier")
    table.add_column("Upgrade Policy")
    table.add_column("Installed At")
    table.add_column("Upgrade Available")

    for p in providers:
        installed_at = str(getattr(p, "installed_at", None) or "-")[:19]
        upgrade_available = getattr(p, "upgrade_available", False)
        latest = getattr(p, "latest_version", None)

        if upgrade_available and latest:
            upgrade_display = f"[green]yes[/green] ({latest})"
        else:
            upgrade_display = "[dim]-[/dim]"

        table.add_row(
            p.store_provider_name,
            p.installed_version,
            getattr(p, "resource_tier", None) or "[dim]-[/dim]",
            getattr(p, "upgrade_policy", None) or "[dim]-[/dim]",
            installed_at,
            upgrade_display,
        )

    console.print(table)
