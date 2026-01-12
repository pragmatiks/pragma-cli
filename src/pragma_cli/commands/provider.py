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
from pragma_sdk import BuildResult, BuildStatus, Config, PragmaClient, PushResult, Resource
from pragma_sdk.provider import discover_resources
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn


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
        pragma provider init mycompany
        pragma provider init postgres --output ./providers/postgres
        pragma provider init mycompany --defaults --description "My provider"

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
    typer.echo("  pragma provider push")


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
        pragma provider update
        pragma provider update ./my-provider

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
        pragma provider push
        -> Uploads code and waits for build

    Build and deploy:
        pragma provider push --deploy
        -> Uploads code, builds, and deploys

    Async build:
        pragma provider push --no-wait
        -> Uploads code and returns immediately

    With logs:
        pragma provider push --logs
        -> Shows build output in real-time

    Example:
        pragma provider push
        pragma provider push --deploy
        pragma provider push --logs --deploy

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

    client = PragmaClient(require_auth=True)

    try:
        push_result = _upload_code(client, provider_id, tarball)

        if not wait:
            console.print()
            console.print("[dim]Build running in background. Check status with:[/dim]")
            console.print(f"  pragma provider status {provider_id} --job {push_result.job_name}")
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
    finally:
        client.close()


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
        pragma provider sync
        pragma provider sync --package postgres_provider
        pragma provider sync --dry-run

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

    client = PragmaClient()

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
