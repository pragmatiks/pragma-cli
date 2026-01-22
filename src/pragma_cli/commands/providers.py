"""Provider management commands.

Commands for scaffolding, building, and deploying Pragmatiks providers to the platform.
"""

import io
import os
import tarfile
import time
import tomllib
from pathlib import Path
from typing import Annotated

import copier
import httpx
import typer
from pragma_sdk import (
    BuildInfo,
    BuildStatus,
    DeploymentStatus,
    PragmaClient,
    ProviderDeleteResult,
    ProviderInfo,
    PushResult,
)
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_provider_ids, completion_provider_versions
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

BUILD_POLL_INTERVAL = 2.0
BUILD_TIMEOUT = 600


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


@app.command("list")
def list_providers(
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List all deployed providers.

    Shows providers with their deployment status. Displays:
    - Provider ID
    - Deployed version
    - Status (running/stopped)
    - Last deployed timestamp

    Examples:
        pragma providers list
        pragma providers list -o json

    Raises:
        typer.Exit: If authentication is missing or API call fails.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Fetching providers...", total=None)
            providers = client.list_providers()
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if not providers:
        console.print("[dim]No providers found.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_providers_table(providers)
    else:
        # Convert Pydantic models to dicts for JSON/YAML output
        data = [p.model_dump(mode="json") for p in providers]
        output_data(data, output)


def _print_providers_table(providers: list[ProviderInfo]) -> None:
    """Print providers in a formatted table.

    Args:
        providers: List of ProviderInfo to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider ID")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Last Deployed")

    for provider in providers:
        status = _format_deployment_status(provider.deployment_status)
        version = provider.current_version or "[dim]never deployed[/dim]"
        updated = provider.updated_at.strftime("%Y-%m-%d %H:%M:%S") if provider.updated_at else "[dim]-[/dim]"

        table.add_row(provider.provider_id, version, status, updated)

    console.print(table)


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
        # Use HEAD when running from local path (development) to pick up uncommitted changes
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
        raise typer.Exit(1)

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
    typer.echo("When ready to deploy:")
    typer.echo("  pragma providers push")


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
        raise typer.Exit(1)

    typer.echo("")
    typer.echo("Provider project updated successfully.")


@app.command()
def push(
    package: Annotated[
        str | None,
        typer.Option("--package", "-p", help="Provider package name (auto-detected if not specified)"),
    ] = None,
    directory: Annotated[
        Path,
        typer.Option("--directory", "-d", help="Provider source directory"),
    ] = Path("."),
    deploy: Annotated[
        bool,
        typer.Option("--deploy", help="Deploy after successful build"),
    ] = False,
    logs: Annotated[
        bool,
        typer.Option("--logs", help="Stream build logs"),
    ] = False,
    wait: Annotated[
        bool,
        typer.Option("--wait/--no-wait", help="Wait for build to complete"),
    ] = True,
):
    """Build and push provider code to the platform.

    Creates a tarball of the provider source code and uploads it to the
    Pragmatiks platform for building. The platform uses BuildKit to create
    a container image.

    Build only:
        pragma providers push
        -> Uploads code and waits for build

    Build and deploy:
        pragma providers push --deploy
        -> Uploads code, builds, and deploys

    Async build:
        pragma providers push --no-wait
        -> Uploads code and returns immediately

    With logs:
        pragma providers push --logs
        -> Shows build output in real-time

    Example:
        pragma providers push
        pragma providers push --deploy
        pragma providers push --logs --deploy

    Raises:
        typer.Exit: If provider detection fails or build fails.
    """
    provider_name = package or detect_provider_package()

    if not provider_name:
        console.print("[red]Error:[/red] Could not detect provider package.")
        console.print("Run from a provider directory or specify --package")
        raise typer.Exit(1)

    provider_id = provider_name.replace("_", "-").removesuffix("-provider")

    if not directory.exists():
        console.print(f"[red]Error:[/red] Directory not found: {directory}")
        raise typer.Exit(1)

    console.print(f"[bold]Pushing provider:[/bold] {provider_id}")
    console.print(f"[dim]Source directory:[/dim] {directory.absolute()}")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Creating tarball...", total=None)
        tarball = create_tarball(directory)

    console.print(f"[green]Created tarball:[/green] {len(tarball) / 1024:.1f} KB")

    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        push_result = _upload_code(client, provider_id, tarball)

        if not wait:
            console.print()
            console.print("[dim]Build running in background. Check status with:[/dim]")
            console.print(f"  pragma providers builds {provider_id} {push_result.version}")
            return

        _wait_for_build(client, provider_id, push_result.version, logs)

        if deploy:
            console.print()
            _deploy_provider(client, provider_id, push_result.version)
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _upload_code(client: PragmaClient, provider_id: str, tarball: bytes) -> PushResult:
    """Upload provider code tarball to the platform.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        tarball: Gzipped tarball bytes of provider source.

    Returns:
        PushResult with build details including version.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Uploading code...", total=None)
        push_result = client.push_provider(provider_id, tarball)

    console.print(f"[green]Build started:[/green] {push_result.version}")
    return push_result


def _wait_for_build(
    client: PragmaClient,
    provider_id: str,
    version: str,
    logs: bool,
) -> BuildInfo:
    """Wait for build to complete, optionally streaming logs.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        version: CalVer version string.
        logs: Whether to stream build logs.

    Returns:
        Final BuildInfo.

    Raises:
        typer.Exit: On build failure or timeout.
    """
    if logs:
        _stream_build_logs(client, provider_id, version)
    else:
        build_result = _poll_build_status(client, provider_id, version)

        if build_result.status == BuildStatus.FAILED:
            console.print(f"[red]Build failed:[/red] {build_result.error_message}")
            raise typer.Exit(1)

        console.print(f"[green]Build successful:[/green] {build_result.version}")

    final_build = client.get_build_status(provider_id, version)

    if final_build.status != BuildStatus.SUCCESS:
        console.print(f"[red]Build failed:[/red] {final_build.error_message}")
        raise typer.Exit(1)

    return final_build


def _poll_build_status(client: PragmaClient, provider_id: str, version: str) -> BuildInfo:
    """Poll build status until completion or timeout.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        version: CalVer version string.

    Returns:
        Final BuildInfo.

    Raises:
        typer.Exit: If build times out.
    """
    start_time = time.time()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Building...", total=None)

        while True:
            build_result = client.get_build_status(provider_id, version)

            if build_result.status in (BuildStatus.SUCCESS, BuildStatus.FAILED):
                return build_result

            elapsed = time.time() - start_time
            if elapsed > BUILD_TIMEOUT:
                console.print("[red]Error:[/red] Build timed out")
                raise typer.Exit(1)

            progress.update(task, description=f"Building... ({build_result.status.value})")
            time.sleep(BUILD_POLL_INTERVAL)


def _stream_build_logs(client: PragmaClient, provider_id: str, version: str) -> None:
    """Stream build logs to console.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        version: CalVer version string.
    """
    console.print()
    console.print("[bold]Build logs:[/bold]")
    console.print("-" * 40)

    try:
        with client.stream_build_logs(provider_id, version) as response:
            for line in response.iter_lines():
                console.print(line)
    except httpx.HTTPError as e:
        console.print(f"[yellow]Warning:[/yellow] Could not stream logs: {e}")
        console.print("[dim]Falling back to polling...[/dim]")
        _poll_build_status(client, provider_id, version)

    console.print("-" * 40)


def _deploy_provider(client: PragmaClient, provider_id: str, version: str | None = None) -> None:
    """Deploy the provider to a specific version.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        version: Version to deploy (CalVer format). If None, deploys latest.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Deploying...", total=None)
        deploy_result = client.deploy_provider(provider_id, version)

    console.print(f"[green]Deployment started:[/green] {provider_id}")
    console.print(f"[dim]Status:[/dim] {deploy_result.status.value}")


@app.command()
def deploy(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID to deploy (e.g., 'postgres', 'my-provider')",
            autocompletion=completion_provider_ids,
        ),
    ],
    version: Annotated[
        str | None,
        typer.Argument(
            help="Version to deploy (e.g., 20250115.120000). Defaults to latest.",
            autocompletion=completion_provider_versions,
        ),
    ] = None,
):
    """Deploy a provider to a specific version.

    Deploys the provider to Kubernetes. If no version is specified, deploys
    the latest successful build. Use 'pragma providers builds' to see available versions.

    Deploy latest:
        pragma providers deploy postgres

    Deploy specific version:
        pragma providers deploy postgres 20250115.120000

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

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        _deploy_provider(client, provider_id, version)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


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


@app.command()
def delete(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID to delete (e.g., 'postgres', 'my-provider')",
            autocompletion=completion_provider_ids,
        ),
    ],
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Delete all resources for this provider"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation prompt"),
    ] = False,
):
    """Delete a provider and all associated resources.

    Removes the provider deployment, resource definitions, and pending events
    from the platform. By default, fails if the provider has any resources.

    Without --cascade:
        pragma providers delete my-provider
        -> Fails if provider has resources

    With --cascade:
        pragma providers delete my-provider --cascade
        -> Deletes provider and all its resources

    Skip confirmation:
        pragma providers delete my-provider --force
        pragma providers delete my-provider --cascade --force

    Example:
        pragma providers delete postgres
        pragma providers delete postgres --cascade
        pragma providers delete postgres --cascade --force

    Raises:
        typer.Exit: If deletion fails or user cancels.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    console.print(f"[bold]Provider:[/bold] {provider_id}")
    if cascade:
        console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")
    console.print()

    if not force:
        action = "DELETE provider and all its resources" if cascade else "DELETE provider"
        confirm = typer.confirm(f"Are you sure you want to {action}?")
        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Deleting provider...", total=None)
            result = client.delete_provider(provider_id, cascade=cascade)

        _print_delete_result(result)

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            detail = e.response.json().get("detail", "Provider has resources")
            console.print(f"[red]Error:[/red] {detail}")
            console.print("[dim]Use --cascade to delete all resources with the provider.[/dim]")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _print_delete_result(result: ProviderDeleteResult) -> None:
    """Print a summary of the deletion result.

    Args:
        result: ProviderDeleteResult from the API.
    """
    console.print()
    console.print(f"[green]Provider deleted:[/green] {result.provider_id}")

    if result.deployment_deleted:
        console.print("[dim]Deployment stopped[/dim]")

    if result.resources_deleted > 0:
        console.print(f"[dim]Resources deleted: {result.resources_deleted}[/dim]")


@app.command()
def status(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID to check status (e.g., 'postgres', 'my-provider')",
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
        pragma providers status postgres
        pragma providers status my-provider
        pragma providers status postgres -o json

    Raises:
        typer.Exit: If deployment not found or status check fails.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        result = client.get_deployment_status(provider_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Deployment not found for provider: {provider_id}")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)

    if output == OutputFormat.TABLE:
        _print_deployment_status(provider_id, result)
    else:
        # Convert Pydantic model to dict for JSON/YAML output
        data = result.model_dump(mode="json")
        data["provider_id"] = provider_id  # Include provider_id in output
        output_data(data, output)


def _print_deployment_status(provider_id: str, result) -> None:
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

    table.add_row("Status", f"[{status_color}]{result.status.value}[/{status_color}]")

    healthy_display = "[green]yes[/green]" if result.healthy else "[red]no[/red]"
    table.add_row("Healthy", healthy_display)

    if result.updated_at:
        table.add_row("Updated", result.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

    console.print(table)


@app.command()
def builds(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID to list builds for (e.g., 'postgres', 'my-provider')",
            autocompletion=completion_provider_ids,
        ),
    ],
):
    """List build history for a provider.

    Shows the last 10 builds ordered by creation time (newest first).
    Useful for selecting versions for rollback and verifying build status.

    Example:
        pragma providers builds postgres
        pragma providers builds my-provider

    Raises:
        typer.Exit: If request fails.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Fetching builds...", total=None)
            build_list = client.list_builds(provider_id)

        if not build_list:
            console.print(f"[dim]No builds found for provider:[/dim] {provider_id}")
            raise typer.Exit(0)

        console.print(f"[bold]Builds for provider:[/bold] {provider_id}")
        console.print()

        table = Table(show_header=True, header_style="bold")
        table.add_column("Version")
        table.add_column("Status")
        table.add_column("Created")
        table.add_column("Error")

        for build in build_list:
            status_color = _get_build_status_color(build.status)
            error_display = (
                build.error_message[:50] + "..."
                if build.error_message and len(build.error_message) > 50
                else (build.error_message or "-")
            )
            table.add_row(
                build.version,
                f"[{status_color}]{build.status.value}[/{status_color}]",
                build.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                error_display,
            )

        console.print(table)

    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _get_build_status_color(status: BuildStatus) -> str:
    """Get the color for a build status.

    Args:
        status: Build status enum value.

    Returns:
        Rich color name for the status.
    """
    return {
        BuildStatus.PENDING: "yellow",
        BuildStatus.BUILDING: "blue",
        BuildStatus.SUCCESS: "green",
        BuildStatus.FAILED: "red",
    }.get(status, "white")
