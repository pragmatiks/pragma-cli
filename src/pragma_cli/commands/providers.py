"""Provider management commands.

Commands for scaffolding, syncing, and pushing Pragmatiks providers to the platform.
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
    BuildResult,
    BuildStatus,
    Config,
    DeploymentStatus,
    PragmaClient,
    ProviderDeleteResult,
    ProviderInfo,
    PushResult,
    Resource,
)
from pragma_sdk.provider import discover_resources
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client


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

DEFAULT_TEMPLATE_URL = "gh:pragmatiks/provider-template"
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

    local_template = Path(__file__).parents[5] / "templates" / "provider"
    if local_template.exists() and (local_template / "copier.yml").exists():
        return str(local_template)

    return DEFAULT_TEMPLATE_URL


@app.command("list")
def list_providers():
    """List all deployed providers.

    Shows providers with their deployment status. Displays:
    - Provider ID
    - Deployed version
    - Status (running/stopped)
    - Last deployed timestamp

    Example:
        pragma providers list

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

    _print_providers_table(providers)


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
        copier.run_copy(
            src_path=template_source,
            dst_path=project_dir,
            data=data,
            defaults=defaults,
            unsafe=True,
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
    typer.echo(f"Edit src/{package_name}/resources.py to add your resources.")
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
        console.print("[red]Error:[/red] Authentication required. Run 'pragma login' first.")
        raise typer.Exit(1)

    try:
        push_result = _upload_code(client, provider_id, tarball)

        if not wait:
            console.print()
            console.print("[dim]Build running in background. Check status with:[/dim]")
            console.print(f"  pragma providers status {provider_id} --job {push_result.job_name}")
            return

        build_result = _wait_for_build(client, provider_id, push_result.job_name, logs)

        if deploy:
            if not build_result.image:
                console.print("[red]Error:[/red] Build succeeded but no image was produced")
                raise typer.Exit(1)

            console.print()
            _deploy_provider(client, provider_id, build_result.image)
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
        PushResult with build job details.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Uploading code...", total=None)
        push_result = client.push_provider(provider_id, tarball)

    console.print(f"[green]Build started:[/green] {push_result.job_name}")
    return push_result


def _wait_for_build(
    client: PragmaClient,
    provider_id: str,
    job_name: str,
    logs: bool,
) -> BuildResult:
    """Wait for build to complete, optionally streaming logs.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        job_name: Build job name.
        logs: Whether to stream build logs.

    Returns:
        Final BuildResult.

    Raises:
        typer.Exit: On build failure or timeout.
    """
    if logs:
        _stream_build_logs(client, provider_id, job_name)
    else:
        build_result = _poll_build_status(client, provider_id, job_name)

        if build_result.status == BuildStatus.FAILED:
            console.print(f"[red]Build failed:[/red] {build_result.error_message}")
            raise typer.Exit(1)

        console.print(f"[green]Build successful:[/green] {build_result.image}")

    final_build = client.get_build_status(provider_id, job_name)

    if final_build.status != BuildStatus.SUCCESS:
        console.print(f"[red]Build failed:[/red] {final_build.error_message}")
        raise typer.Exit(1)

    return final_build


def _poll_build_status(client: PragmaClient, provider_id: str, job_name: str) -> BuildResult:
    """Poll build status until completion or timeout.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        job_name: Build job name.

    Returns:
        Final BuildResult.

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
            build_result = client.get_build_status(provider_id, job_name)

            if build_result.status in (BuildStatus.SUCCESS, BuildStatus.FAILED):
                return build_result

            elapsed = time.time() - start_time
            if elapsed > BUILD_TIMEOUT:
                console.print("[red]Error:[/red] Build timed out")
                raise typer.Exit(1)

            progress.update(task, description=f"Building... ({build_result.status.value})")
            time.sleep(BUILD_POLL_INTERVAL)


def _stream_build_logs(client: PragmaClient, provider_id: str, job_name: str) -> None:
    """Stream build logs to console.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        job_name: Build job name.
    """
    console.print()
    console.print("[bold]Build logs:[/bold]")
    console.print("-" * 40)

    try:
        with client.stream_build_logs(provider_id, job_name) as response:
            for line in response.iter_lines():
                console.print(line)
    except httpx.HTTPError as e:
        console.print(f"[yellow]Warning:[/yellow] Could not stream logs: {e}")
        console.print("[dim]Falling back to polling...[/dim]")
        _poll_build_status(client, provider_id, job_name)

    console.print("-" * 40)


def _deploy_provider(client: PragmaClient, provider_id: str, image: str) -> None:
    """Deploy the provider.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.
        image: Container image to deploy.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Deploying...", total=None)
        deploy_result = client.deploy_provider(provider_id, image)

    console.print(f"[green]Deployment started:[/green] {deploy_result.deployment_name}")
    console.print(f"[dim]Status:[/dim] {deploy_result.status.value}")


@app.command()
def deploy(
    image: Annotated[
        str,
        typer.Option("--image", "-i", help="Container image to deploy (required)"),
    ],
    package: Annotated[
        str | None,
        typer.Option("--package", "-p", help="Provider package name (auto-detected if not specified)"),
    ] = None,
):
    """Deploy a provider from a built container image.

    Deploys a provider image to Kubernetes. Use after 'pragma providers push'
    completes successfully, or to redeploy/rollback to a specific version.

    Example:
        pragma providers deploy --image europe-west4-docker.pkg.dev/project/repo/provider:tag
        pragma providers deploy -i provider:latest --package my_provider

    Raises:
        typer.Exit: If deployment fails.
    """
    provider_name = package or detect_provider_package()

    if not provider_name:
        console.print("[red]Error:[/red] Could not detect provider package.")
        console.print("Run from a provider directory or specify --package")
        raise typer.Exit(1)

    provider_id = provider_name.replace("_", "-").removesuffix("-provider")

    console.print(f"[bold]Deploying provider:[/bold] {provider_id}")
    console.print(f"[dim]Image:[/dim] {image}")
    console.print()

    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)

    try:
        _deploy_provider(client, provider_id, image)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


@app.command()
def sync(
    package: Annotated[
        str | None,
        typer.Option("--package", "-p", help="Provider package name (auto-detected if not specified)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be registered without making changes"),
    ] = False,
):
    """Sync resource type definitions to the Pragmatiks platform.

    Discovers resources in the provider package and registers their schemas
    with the API. This allows users to create instances of these resource types.

    The command introspects the provider code to extract:
    - Provider and resource names from @provider.resource() decorator
    - JSON schema from Pydantic Config classes

    Example:
        pragma providers sync
        pragma providers sync --package postgres_provider
        pragma providers sync --dry-run

    Raises:
        typer.Exit: If package not found or registration fails.
    """
    package_name = package or detect_provider_package()

    if not package_name:
        typer.echo("Error: Could not detect provider package.", err=True)
        typer.echo("Run from a provider directory or specify --package", err=True)
        raise typer.Exit(1)

    typer.echo(f"Discovering resources in {package_name}...")

    try:
        resources = discover_resources(package_name)
    except ImportError as e:
        typer.echo(f"Error importing package: {e}", err=True)
        raise typer.Exit(1)

    if not resources:
        typer.echo("No resources found. Ensure resources are decorated with @provider.resource().")
        raise typer.Exit(0)

    typer.echo(f"Found {len(resources)} resource(s):")
    typer.echo("")

    if dry_run:
        for (provider, resource_name), resource_class in resources.items():
            typer.echo(f"  {provider}/{resource_name} ({resource_class.__name__})")
        typer.echo("")
        typer.echo("Dry run - no changes made.")
        raise typer.Exit(0)

    client = get_client()

    for (provider, resource_name), resource_class in resources.items():
        try:
            config_class = get_config_class(resource_class)
            schema = config_class.model_json_schema()
        except ValueError as e:
            typer.echo(f"  {provider}/{resource_name}: skipped ({e})", err=True)
            continue

        try:
            client.register_resource(
                provider=provider,
                resource=resource_name,
                schema=schema,
            )
            typer.echo(f"  {provider}/{resource_name}: registered")
        except Exception as e:
            typer.echo(f"  {provider}/{resource_name}: failed ({e})", err=True)

    typer.echo("")
    typer.echo("Sync complete.")


def get_config_class(resource_class: type[Resource]) -> type[Config]:
    """Extract Config subclass from Resource's config field annotation.

    Args:
        resource_class: A Resource subclass.

    Returns:
        Config subclass type from the Resource's config field.

    Raises:
        ValueError: If Resource has no config field or wrong type.
    """
    annotations = resource_class.model_fields
    config_field = annotations.get("config")

    if config_field is None:
        raise ValueError(f"Resource {resource_class.__name__} has no config field")

    config_type = config_field.annotation

    if not isinstance(config_type, type) or not issubclass(config_type, Config):
        raise ValueError(f"Resource {resource_class.__name__} config field is not a Config subclass")

    return config_type


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
        typer.Argument(help="Provider ID to delete (e.g., 'postgres', 'my-provider')"),
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
        console.print("[red]Error:[/red] Authentication required. Run 'pragma login' first.")
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
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Deleted", justify="right")

    table.add_row("Builds Cancelled", str(result.builds_cancelled))
    table.add_row("Source Archives", str(result.source_archives_deleted))
    table.add_row("Deployment", "Yes" if result.deployment_deleted else "No (not found)")
    table.add_row("Resources", str(result.resources_deleted))
    table.add_row("Resource Definitions", str(result.resource_definitions_deleted))
    table.add_row("Outbox Events", str(result.outbox_events_deleted))
    table.add_row("Dead Letter Events", str(result.dead_letter_events_deleted))
    table.add_row("NATS Messages Purged", str(result.messages_purged))
    table.add_row("NATS Consumer", "Deleted" if result.consumer_deleted else "Not found")

    console.print(table)


@app.command()
def rollback(
    provider_id: Annotated[
        str,
        typer.Argument(help="Provider ID to rollback (e.g., 'postgres', 'my-provider')"),
    ],
    version: Annotated[
        str | None,
        typer.Option("--version", "-v", help="Target version to rollback to (default: previous successful build)"),
    ] = None,
):
    """Rollback a provider to a previous version.

    Redeploys a previously successful build. If no version is specified,
    rolls back to the previous successful version.

    Rollback to specific version:
        pragma providers rollback my-provider --version 20250114.120000

    Rollback to previous version:
        pragma providers rollback my-provider

    Example:
        pragma providers rollback postgres --version 20250114.120000
        pragma providers rollback postgres -v 20250114.120000
        pragma providers rollback postgres

    Raises:
        typer.Exit: If rollback fails or no suitable version found.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma login' first.")
        raise typer.Exit(1)

    try:
        target_version = version
        if target_version is None:
            target_version = _find_previous_version(client, provider_id)

        console.print(f"[bold]Rolling back provider:[/bold] {provider_id}")
        console.print(f"[dim]Target version:[/dim] {target_version}")
        console.print()

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Deploying previous version...", total=None)
            result = client.rollback_provider(provider_id, target_version)

        console.print(f"[green]Rollback initiated:[/green] {result.deployment_name}")
        console.print(f"[dim]Status:[/dim] {result.status.value}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            detail = e.response.json().get("detail", "Build not found")
            console.print(f"[red]Error:[/red] {detail}")
        elif e.response.status_code == 400:
            detail = e.response.json().get("detail", "Build not deployable")
            console.print(f"[red]Error:[/red] {detail}")
        else:
            console.print(f"[red]Error:[/red] {e.response.text}")
        raise typer.Exit(1)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _find_previous_version(client: PragmaClient, provider_id: str) -> str:
    """Find the previous successful version for rollback.

    Gets the build history and finds the second-most-recent successful build,
    which represents the version before the current deployment.

    Args:
        client: SDK client instance.
        provider_id: Provider identifier.

    Returns:
        CalVer version string of the previous successful build.

    Raises:
        ValueError: If no previous successful build exists.
    """
    builds = client.list_builds(provider_id)
    successful_builds = [b for b in builds if b.status == BuildStatus.SUCCESS]

    if len(successful_builds) < 2:
        raise ValueError(
            f"No previous successful build found for provider '{provider_id}'. "
            "Specify a version explicitly with --version."
        )

    return successful_builds[1].version


@app.command()
def status(
    provider_id: Annotated[
        str,
        typer.Argument(help="Provider ID to check status (e.g., 'postgres', 'my-provider')"),
    ],
):
    """Check the deployment status of a provider.

    Displays:
    - Deployment status (pending/progressing/available/failed)
    - Replica count (available/ready)
    - Current image version
    - Last updated timestamp

    Example:
        pragma providers status postgres
        pragma providers status my-provider

    Raises:
        typer.Exit: If deployment not found or status check fails.
    """
    client = get_client()

    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma login' first.")
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

    _print_deployment_status(provider_id, result)


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
    table.add_row("Replicas", f"{result.available_replicas} available / {result.ready_replicas} ready")

    if result.image:
        version = result.image.split(":")[-1] if ":" in result.image else "unknown"
        table.add_row("Version", version)
        table.add_row("Image", f"[dim]{result.image}[/dim]")

    if result.updated_at:
        table.add_row("Updated", result.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

    if result.message:
        table.add_row("Message", result.message)

    console.print(table)


@app.command()
def builds(
    provider_id: Annotated[
        str,
        typer.Argument(help="Provider ID to list builds for (e.g., 'postgres', 'my-provider')"),
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
        console.print("[red]Error:[/red] Authentication required. Run 'pragma login' first.")
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
