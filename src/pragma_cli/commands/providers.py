"""Provider management commands.

Unified commands for publishing, installing, deploying, and managing
Pragmatiks providers.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Annotated, Any

import copier
import httpx
import typer
from pragma_sdk import (
    DeploymentResult,
    DeploymentStatus,
    PragmaClient,
)
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_provider_ids
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Provider management commands")
console = Console()

TARBALL_EXCLUDES = {
    ".git",
    "__pycache__",
    ".venv",
    ".env",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.pyc",
    "*.pyo",
    "*.egg-info",
    "dist",
    "build",
    ".tox",
    ".nox",
}

DEFAULT_TEMPLATE_URL = "gh:pragmatiks/pragma-providers"
TEMPLATE_PATH_ENV = "PRAGMA_PROVIDER_TEMPLATE"

PUBLISH_POLL_INTERVAL = 2.0
PUBLISH_POLL_TIMEOUT = 600

TRUST_TIER_STYLES = {
    "official": "green",
    "verified": "blue",
    "community": "dim",
}


def create_tarball(source_dir: Path) -> bytes:
    """Create a gzipped tarball of the provider source directory.

    Excludes common development artifacts like .git, __pycache__, .venv, etc.

    Args:
        source_dir: Path to the provider source directory.

    Returns:
        Gzipped tarball bytes suitable for upload.
    """
    buffer = io.BytesIO()

    def exclude_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
        """Filter out excluded files and directories.

        Returns:
            The TarInfo object if included, None if excluded.
        """
        name = tarinfo.name
        parts = Path(name).parts

        for part in parts:
            if part in TARBALL_EXCLUDES:
                return None

            for pattern in TARBALL_EXCLUDES:
                if pattern.startswith("*") and part.endswith(pattern[1:]):
                    return None

        return tarinfo

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(source_dir, arcname=".", filter=exclude_filter)

    buffer.seek(0)
    return buffer.read()


def get_template_source() -> str:
    """Get the template source path or URL.

    Priority:
    1. PRAGMA_PROVIDER_TEMPLATE environment variable
    2. Local development path (if running from repo)
    3. Default GitHub URL

    Returns:
        Template path (local) or URL (GitHub).
    """
    if env_template := os.environ.get(TEMPLATE_PATH_ENV):
        return env_template

    local_template = Path(__file__).parents[4] / "pragma-providers"

    if local_template.exists() and (local_template / "copier.yml").exists():
        return str(local_template)

    return DEFAULT_TEMPLATE_URL


def detect_provider_package() -> str | None:
    """Detect provider package name from current directory.

    Returns:
        Package name with underscores if found, None otherwise.
    """
    pyproject = Path("pyproject.toml")

    if not pyproject.exists():
        return None

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    name = data.get("project", {}).get("name", "")

    if name and name.endswith("-provider"):
        return name.replace("-", "_")

    return None


def read_pragma_metadata(directory: Path) -> dict[str, str]:
    """Read provider store metadata from pyproject.toml [tool.pragma].

    Args:
        directory: Provider source directory containing pyproject.toml.

    Returns:
        Dict with optional keys: display_name, description, tags (JSON-encoded).
    """
    pyproject = directory / "pyproject.toml"

    if not pyproject.exists():
        return {}

    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    pragma_config = data.get("tool", {}).get("pragma", {})
    metadata: dict[str, str] = {}

    if display_name := pragma_config.get("display_name"):
        metadata["display_name"] = display_name

    if description := pragma_config.get("description"):
        metadata["description"] = description

    if tags := pragma_config.get("tags"):
        metadata["tags"] = json.dumps(tags)

    return metadata


def _require_auth(client: PragmaClient) -> None:
    """Verify the client is authenticated, exit with error if not.

    Args:
        client: SDK client instance.

    Raises:
        typer.Exit: If authentication is missing.
    """
    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)


def _fetch_with_spinner(description: str, fetch_fn) -> Any:
    """Execute a function with a spinner progress indicator.

    Args:
        description: Text to display next to the spinner.
        fetch_fn: Zero-argument callable to execute.

    Returns:
        Result from fetch_fn.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description, total=None)
        return fetch_fn()


def _format_api_error(error: httpx.HTTPStatusError) -> str:
    """Extract a human-readable message from an HTTP error response.

    Args:
        error: The HTTP status error from httpx.

    Returns:
        Formatted error message string.
    """
    try:
        detail = error.response.json().get("detail", error.response.text)
    except Exception:
        return error.response.text or str(error)

    if isinstance(detail, str):
        return detail

    if isinstance(detail, dict):
        return detail.get("message", str(error))

    return str(detail)


def _format_trust_tier(tier: str | None) -> str:
    """Format a trust tier with Rich color markup.

    Args:
        tier: Trust tier string or None.

    Returns:
        Formatted string with Rich markup.
    """
    if not tier:
        return "[dim]-[/dim]"

    color = TRUST_TIER_STYLES.get(tier, "white")
    return f"[{color}]{tier}[/{color}]"


def _format_deployment_status(status: DeploymentStatus | None) -> str:
    """Format deployment status with color coding.

    Args:
        status: Deployment status or None if not deployed.

    Returns:
        Formatted status string with Rich markup.
    """
    if status is None:
        return "[dim]not deployed[/dim]"

    match status:
        case DeploymentStatus.AVAILABLE:
            return "[green]running[/green]"
        case DeploymentStatus.PROGRESSING:
            return "[yellow]deploying[/yellow]"
        case DeploymentStatus.PENDING:
            return "[yellow]pending[/yellow]"
        case DeploymentStatus.FAILED:
            return "[red]failed[/red]"
        case _:
            return f"[dim]{status}[/dim]"


def _format_version_status(status: str) -> str:
    """Format a version build status with Rich color markup.

    Args:
        status: Version status string.

    Returns:
        Formatted string with Rich markup.
    """
    status_colors = {
        "published": "green",
        "building": "yellow",
        "failed": "red",
        "yanked": "dim",
    }
    color = status_colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Provider name (e.g., 'postgres', 'mycompany')")],
    output_dir: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output directory (default: ./{name}-provider)"),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="Provider description"),
    ] = None,
    author_name: Annotated[
        str | None,
        typer.Option("--author", help="Author name"),
    ] = None,
    author_email: Annotated[
        str | None,
        typer.Option("--email", help="Author email"),
    ] = None,
    defaults: Annotated[
        bool,
        typer.Option("--defaults", help="Accept all defaults without prompting"),
    ] = False,
):
    """Initialize a new provider project.

    Creates a complete provider project structure with:
    - pyproject.toml for packaging
    - README.md with documentation
    - src/{name}_provider/ with example resources
    - tests/ with example tests
    - mise.toml for tool management

    Example:
        pragma providers init mycompany
        pragma providers init postgres --output ./providers/postgres
        pragma providers init mycompany --defaults --description "My provider"

    Raises:
        typer.Exit: If directory already exists or template copy fails.
    """
    project_dir = output_dir or Path(f"./{name}-provider")

    if project_dir.exists():
        typer.echo(f"Error: Directory {project_dir} already exists", err=True)
        raise typer.Exit(1)

    template_source = get_template_source()

    data = {"name": name}

    if description:
        data["description"] = description

    if author_name:
        data["author_name"] = author_name

    if author_email:
        data["author_email"] = author_email

    typer.echo(f"Creating provider project: {project_dir}")
    typer.echo(f"  Template: {template_source}")
    typer.echo("")

    try:
        vcs_ref = "HEAD" if not template_source.startswith("gh:") else None
        copier.run_copy(
            src_path=template_source,
            dst_path=project_dir,
            data=data,
            defaults=defaults,
            unsafe=True,
            vcs_ref=vcs_ref,
        )
    except Exception as e:
        typer.echo(f"Error creating provider: {e}", err=True)
        raise typer.Exit(1) from e

    package_name = name.lower().replace("-", "_").replace(" ", "_") + "_provider"

    typer.echo("")
    typer.echo(f"Created provider project: {project_dir}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {project_dir}")
    typer.echo("  uv sync --dev")
    typer.echo("  uv run pytest tests/")
    typer.echo("")
    typer.echo(f"Edit src/{package_name}/resources/ to add your resources.")
    typer.echo("")
    typer.echo("To update this project when the template changes:")
    typer.echo("  copier update")
    typer.echo("")
    typer.echo("When ready to publish:")
    typer.echo("  pragma providers publish --version 0.1.0")


@app.command()
def update(
    project_dir: Annotated[
        Path,
        typer.Argument(help="Provider project directory"),
    ] = Path("."),
):
    """Update an existing provider project with latest template changes.

    Uses Copier's 3-way merge to preserve your customizations while
    incorporating template updates.

    Example:
        pragma providers update
        pragma providers update ./my-provider

    Raises:
        typer.Exit: If directory is not a Copier project or update fails.
    """
    answers_file = project_dir / ".copier-answers.yml"

    if not answers_file.exists():
        typer.echo(f"Error: {project_dir} is not a Copier-generated project", err=True)
        typer.echo("(missing .copier-answers.yml)", err=True)
        raise typer.Exit(1)

    typer.echo(f"Updating provider project: {project_dir}")
    typer.echo("")

    try:
        copier.run_update(dst_path=project_dir, unsafe=True)
    except Exception as e:
        typer.echo(f"Error updating provider: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo("")
    typer.echo("Provider project updated successfully.")


@app.command()
def publish(
    version: Annotated[
        str,
        typer.Option("--version", "-v", help="Semantic version for this release", show_default="required"),
    ],
    org: Annotated[
        str,
        typer.Option("--org", help="Organization namespace for the provider", show_default="required"),
    ],
    changelog: Annotated[
        str | None,
        typer.Option("--changelog", help="Changelog text for this version"),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Force publish even if source hash already exists"),
    ] = False,
    directory: Annotated[
        Path,
        typer.Option("--directory", "-d", help="Provider source directory"),
    ] = Path("."),
    package: Annotated[
        str | None,
        typer.Option("--package", "-p", help="Provider package name (auto-detected if not specified)"),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait for build to complete"),
    ] = True,
):
    """Publish a provider to the store.

    Creates a tarball of the provider source and publishes it to the
    Pragmatiks Provider Store with the specified semantic version.

    The provider name is read from pyproject.toml [project].name and
    namespaced under the specified organization.

    Examples:
        pragma providers publish --version 1.0.0 --org myorg
        pragma providers publish --version 1.1.0 --org myorg --changelog "Added new resources"
        pragma providers publish --version 2.0.0 --org myorg --force
        pragma providers publish --version 1.0.0 --org myorg --no-wait

    Raises:
        typer.Exit: If provider detection fails or publish fails.
    """
    provider_name = package or detect_provider_package()

    if not provider_name:
        console.print("[red]Error:[/red] Could not detect provider package.")
        console.print("Run from a provider directory or specify --package")
        raise typer.Exit(1)

    provider_id = provider_name.replace("_", "-").removesuffix("-provider")
    provider_id = f"{org}/{provider_id}"

    if not directory.exists():
        console.print(f"[red]Error:[/red] Directory not found: {directory}")
        raise typer.Exit(1)

    client = get_client()
    _require_auth(client)

    metadata = read_pragma_metadata(directory)

    console.print(f"[bold]Publishing provider:[/bold] {provider_id}")
    console.print(f"[dim]Version:[/dim] {version}")
    console.print(f"[dim]Source directory:[/dim] {directory.absolute()}")
    console.print()

    tarball = _fetch_with_spinner("Creating tarball...", lambda: create_tarball(directory))
    console.print(f"[green]Created tarball:[/green] {len(tarball) / 1024:.1f} KB")

    try:
        result = _fetch_with_spinner(
            "Publishing to store...",
            lambda: client.publish_provider(
                provider_id,
                tarball,
                version,
                changelog=changelog,
                force=force,
                **metadata,
            ),
        )

        published_version = result.version
        console.print(f"[green]Published:[/green] {result.provider_name} v{published_version}")

        if not wait:
            console.print()
            console.print("[dim]Build running in background.[/dim]")
            return

        _poll_publish_status(client, provider_id, published_version)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            console.print("[yellow]Warning:[/yellow] A version with this source hash already exists.")
            console.print("[dim]Use --force to publish anyway.[/dim]")
            raise typer.Exit(1) from e

        if e.response.status_code == 413:
            console.print("[red]Error:[/red] Tarball is too large.")
            console.print(f"[dim]Size: {len(tarball) / 1024:.1f} KB[/dim]")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1) from e
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise

        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


def _poll_publish_status(client: PragmaClient, provider_id: str, version: str) -> None:
    """Poll store build status until completion or timeout.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        version: Semantic version string.
    """  # noqa: DOC501
    start_time = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Building...", total=None)

        while True:
            build_status = client.get_publish_status(provider_id, version)
            current_status = getattr(build_status, "status", None)

            if current_status in ("published", "failed"):
                break

            elapsed = time.time() - start_time

            if elapsed > PUBLISH_POLL_TIMEOUT:
                console.print("[red]Error:[/red] Build timed out")
                raise typer.Exit(1)

            progress.update(task, description=f"Building... ({current_status})")
            time.sleep(PUBLISH_POLL_INTERVAL)

    if current_status == "published":
        console.print(f"[green]Build successful:[/green] {provider_id} v{version}")
    elif current_status == "failed":
        console.print(f"[red]Build failed:[/red] {provider_id} v{version}")
        raise typer.Exit(1)
    else:
        console.print(f"[dim]Build status:[/dim] {current_status}")


@app.command()
def install(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str | None, typer.Option("--version", "-v", help="Version to install (default: latest)")] = None,
    resource_tier: Annotated[
        str,
        typer.Option("--resource-tier", help="Resource tier (free, standard, performance)"),
    ] = "standard",
    upgrade_policy: Annotated[
        str,
        typer.Option("--upgrade-policy", help="Upgrade policy (manual, auto-minor, auto-patch)"),
    ] = "manual",
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Install a provider from the store.

    Examples:
        pragma providers install pragmatiks/qdrant
        pragma providers install pragmatiks/postgres --version 1.2.0
        pragma providers install pragmatiks/redis --resource-tier performance --upgrade-policy auto-minor
        pragma providers install pragmatiks/qdrant -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    try:
        detail = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    provider = detail.provider
    display = getattr(provider, "display_name", None) or name
    install_version = version or getattr(provider, "latest_version", "latest")

    console.print(f"[bold]Provider:[/bold] {display} ({name})")
    console.print(f"[bold]Version:[/bold]  {install_version}")
    console.print(f"[bold]Tier:[/bold]     {resource_tier}")
    console.print()

    if not yes:
        confirm = typer.confirm("Install this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Installing provider...",
            lambda: client.install_provider(
                name,
                version=version,
                resource_tier=resource_tier,
                upgrade_policy=upgrade_policy,
            ),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already installed.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Installed:[/green] {name} v{result.installed_version}")


@app.command()
def uninstall(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Delete all resources created by this provider"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Uninstall an installed provider.

    Examples:
        pragma providers uninstall pragmatiks/qdrant
        pragma providers uninstall pragmatiks/postgres --cascade
        pragma providers uninstall pragmatiks/redis --yes
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Provider:[/bold] {name}")

    if cascade:
        console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")

    console.print()

    if not yes:
        action = "UNINSTALL provider and delete all its resources" if cascade else "UNINSTALL provider"
        confirm = typer.confirm(f"Are you sure you want to {action}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        _fetch_with_spinner(
            "Uninstalling provider...",
            lambda: client.uninstall_provider(name, cascade=cascade),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1) from e

        if e.response.status_code == 409:
            console.print(f"[red]Error:[/red] Provider '{name}' has active resources.")
            console.print("[dim]Use --cascade to delete all resources with the provider.[/dim]")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Uninstalled:[/green] {name}")


@app.command()
def upgrade(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str | None, typer.Option("--version", "-v", help="Target version (default: latest)")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Upgrade an installed provider to a newer version.

    Examples:
        pragma providers upgrade pragmatiks/qdrant
        pragma providers upgrade pragmatiks/postgres --version 2.0.0
        pragma providers upgrade pragmatiks/redis -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

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
            lambda: client.upgrade_provider(name, target_version=version),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1) from e

        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already on the requested version.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Upgraded:[/green] {name} -> v{result.installed_version}")


@app.command("list")
def list_providers(
    trust_tier: Annotated[
        str | None,
        typer.Option("--trust-tier", help="Filter by trust tier (official, verified, community)"),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Filter by scope (public or tenant)"),
    ] = None,
    installed: Annotated[
        bool,
        typer.Option("--installed", help="Show installed providers only"),
    ] = False,
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search query"),
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
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List providers in the store or installed providers.

    Combines browsing and searching into a single command. Use --installed
    to show only installed providers, or --query to search the catalog.

    Examples:
        pragma providers list
        pragma providers list --installed
        pragma providers list --query postgres
        pragma providers list --scope public --tags ml,vector
        pragma providers list -o json
    """  # noqa: DOC501
    client = get_client()

    if installed:
        _list_installed_providers(client, output)
        return

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    try:
        result = _fetch_with_spinner(
            "Fetching providers...",
            lambda: client.list_providers(
                query=query,
                scope=scope,
                trust_tier=trust_tier,
                tags=tag_list,
                limit=limit,
                offset=offset,
            ),
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if not result.items:
        if query:
            console.print(f"[dim]No providers found matching '{query}'.[/dim]")
        else:
            console.print("[dim]No providers found.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_store_list_table(result)
    else:
        data = [_provider_summary_to_dict(p) for p in result.items]
        output_data(data, output)


def _list_installed_providers(client: PragmaClient, output: OutputFormat) -> None:
    """List installed providers for the current tenant.

    Args:
        client: SDK client instance.
        output: Output format for display.
    """  # noqa: DOC501
    _require_auth(client)

    try:
        providers = _fetch_with_spinner(
            "Fetching installed providers...",
            lambda: client.list_installed_providers(),
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_installed_table(providers)
    else:
        data = [_installed_provider_to_dict(p) for p in providers]
        output_data(data, output)


@app.command()
def info(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Show detailed information about a provider.

    Displays provider metadata, version history, and installation status.

    Examples:
        pragma providers info pragmatiks/qdrant
        pragma providers info pragmatiks/postgres -o json
    """  # noqa: DOC501
    client = get_client()

    try:
        detail = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if output == OutputFormat.TABLE:
        _print_provider_info(detail)
    else:
        data = _provider_detail_to_dict(detail)
        output_data(data, output)


@app.command()
def deploy(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    version: Annotated[
        str | None,
        typer.Option("--version", "-v", help="Version to deploy (default: latest)"),
    ] = None,
):
    """Deploy a provider to a specific version.

    Deploys the provider to Kubernetes. If no version is specified, deploys
    the latest successful build.

    Deploy latest:
        pragma providers deploy pragmatiks/postgres

    Deploy specific version:
        pragma providers deploy pragmatiks/postgres --version 1.2.0

    Raises:
        typer.Exit: If deployment fails.
    """
    console.print(f"[bold]Deploying provider:[/bold] {provider_id}")

    if version:
        console.print(f"[dim]Version:[/dim] {version}")
    else:
        console.print("[dim]Version:[/dim] latest")

    console.print()

    client = get_client()
    _require_auth(client)

    try:
        deploy_result = _fetch_with_spinner(
            "Deploying...",
            lambda: client.deploy_provider(provider_id, version),
        )
        console.print(f"[green]Deployment started:[/green] {provider_id}")
        console.print(f"[dim]Deployment:[/dim] {deploy_result.deployment_name}")
        console.print(f"[dim]Status:[/dim] {deploy_result.status.value}")
        console.print(f"[dim]Replicas:[/dim] {deploy_result.ready_replicas}/{deploy_result.available_replicas}")

        if deploy_result.image:
            console.print(f"[dim]Image:[/dim] {deploy_result.image}")
    except httpx.HTTPStatusError as e:
        console.print(_format_api_error(e))
        raise typer.Exit(1) from e
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise

        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def status(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Check the deployment status of a provider.

    Displays:
    - Deployment status (pending/progressing/available/failed)
    - Deployed version
    - Health status
    - Last updated timestamp

    Examples:
        pragma providers status pragmatiks/postgres
        pragma providers status pragmatiks/my-provider -o json

    Raises:
        typer.Exit: If deployment not found or status check fails.
    """
    client = get_client()
    _require_auth(client)

    try:
        result = client.get_deployment_status(provider_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Deployment not found for provider: {provider_id}")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")

        raise typer.Exit(1) from e
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e

    if output == OutputFormat.TABLE:
        _print_deployment_status(provider_id, result)
    else:
        data = result.model_dump(mode="json")
        data["provider_id"] = provider_id
        output_data(data, output)


@app.command()
def delete(
    name: Annotated[
        str,
        typer.Argument(
            help="Provider name (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Delete a provider from the store (author only).

    Removes the provider and all its versions from the store catalog.
    This does not uninstall the provider from tenants that have it installed.

    Examples:
        pragma providers delete myorg/my-provider
        pragma providers delete myorg/my-provider --yes

    Raises:
        typer.Exit: If deletion fails or user cancels.
    """
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Provider:[/bold] {name}")
    console.print("[yellow]Warning:[/yellow] This will permanently delete the provider from the store.")
    console.print()

    if not yes:
        confirm = typer.confirm("Are you sure you want to DELETE this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        _fetch_with_spinner(
            "Deleting provider...",
            lambda: client.delete_provider(name),
        )
        console.print(f"[green]✓[/green] Provider [bold]{name}[/bold] deleted successfully")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            try:
                detail = e.response.json().get("detail", "Provider has resources")
            except Exception:
                detail = "Provider has resources"
            console.print(f"[red]Error:[/red] {detail}")
        else:
            console.print(f"[red]Error:[/red] {_format_api_error(e)}")

        raise typer.Exit(1) from e
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise

        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


def _print_deployment_status(provider_id: str, result: DeploymentResult) -> None:
    """Print deployment status in a formatted table.

    Args:
        provider_id: Provider identifier.
        result: DeploymentResult from the API.
    """
    status_colors = {
        "pending": "yellow",
        "progressing": "cyan",
        "available": "green",
        "failed": "red",
    }
    status_color = status_colors.get(result.status.value, "white")

    console.print()
    console.print(f"[bold]Provider:[/bold] {provider_id}")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("Deployment", result.deployment_name)
    table.add_row("Status", f"[{status_color}]{result.status.value}[/{status_color}]")
    table.add_row("Replicas", f"{result.ready_replicas}/{result.available_replicas}")

    if result.version:
        table.add_row("Version", result.version)

    if result.image:
        table.add_row("Image", result.image)

    if result.updated_at:
        table.add_row("Updated", result.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

    if result.message:
        table.add_row("Message", result.message)

    console.print(table)


def _print_store_list_table(result) -> None:
    """Print store providers in a formatted table.

    Args:
        result: Paginated response of store provider summaries.
    """
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
    """Print detailed provider information in a panel with version table.

    Args:
        detail: Store provider detail response.
    """
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
            v_status = getattr(v, "status", None) or "-"
            status_display = _format_version_status(v_status)
            runtime = getattr(v, "runtime_version", None) or "[dim]-[/dim]"
            published = str(getattr(v, "published_at", None) or "-")[:19]

            version_table.add_row(
                v.version,
                status_display,
                runtime,
                published,
            )

        console.print(version_table)


def _print_installed_table(providers) -> None:
    """Print installed providers in a formatted table.

    Args:
        providers: List of installed provider summaries.
    """
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


def _serialize_datetime(obj: object, attr: str) -> str | None:
    val = getattr(obj, attr, None)
    return val.isoformat() if val else None


def _provider_summary_to_dict(provider) -> dict:
    """Convert a store provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Store provider summary object.

    Returns:
        Dictionary representation.
    """
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
    """Convert a store provider detail to a plain dict for JSON/YAML output.

    Args:
        detail: Store provider detail object.

    Returns:
        Dictionary representation.
    """
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
        "created_at": _serialize_datetime(provider, "created_at"),
        "updated_at": _serialize_datetime(provider, "updated_at"),
        "versions": [
            {
                "version": v.version,
                "status": getattr(v, "status", None),
                "runtime_version": getattr(v, "runtime_version", None),
                "published_at": _serialize_datetime(v, "published_at"),
                "changelog": getattr(v, "changelog", None),
            }
            for v in versions
        ],
    }


def _installed_provider_to_dict(provider) -> dict:
    """Convert an installed provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Installed provider summary object.

    Returns:
        Dictionary representation.
    """
    return {
        "store_provider_name": provider.store_provider_name,
        "installed_version": provider.installed_version,
        "upgrade_policy": getattr(provider, "upgrade_policy", None),
        "resource_tier": getattr(provider, "resource_tier", None),
        "installed_at": _serialize_datetime(provider, "installed_at"),
        "latest_version": getattr(provider, "latest_version", None),
        "upgrade_available": getattr(provider, "upgrade_available", False),
    }
