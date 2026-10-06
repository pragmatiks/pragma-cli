"""Provider management commands.

Unified commands for registering, installing, deploying, and managing
Pragmatiks providers.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import copier
import httpx
import typer
import yaml
from pragma_sdk import (
    DeploymentResult,
    DeploymentStatus,
    PragmaClient,
    ProviderVersion,
    VersionStatus,
)
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.bootstrap_errors import check_bootstrap_error
from pragma_cli.commands.completions import completion_provider_ids
from pragma_cli.helpers import OutputFormat, output_data, parse_api_error_message


app = typer.Typer(help="Provider management commands")
console = Console()

DEFAULT_TEMPLATE_URL = "gh:pragmatiks/pragma-providers"
TEMPLATE_PATH_ENV = "PRAGMA_PROVIDER_TEMPLATE"

ADMISSION_POLL_INTERVAL_SECONDS = 2.0
ADMISSION_MINIMUM_WATCH_SECONDS = 150.0
"""Shortest time the publish watch waits from the upload, covering the provider host taking the version."""
ADMISSION_DEADLINE_MARGIN_SECONDS = 30.0
"""Time the publish watch keeps waiting past a version's ``operation_deadline_at``, for settlement and clock skew."""
UPLOAD_FAILED_MESSAGE = "Could not upload the wheel. Publish the version again."

VERSION_STATUS_DISPLAY = {
    VersionStatus.PENDING: "[yellow]admitting[/yellow]",
    VersionStatus.PUBLISHED: "[green]published[/green]",
    VersionStatus.FAILED: "[red]failed[/red]",
}
"""Rich display of each version status; ``pending`` reads ``admitting``, the word every surface uses."""


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


def _build_wheel(project_dir: Path) -> Path:
    """Build the provider wheel with ``uv build`` and return its path.

    Args:
        project_dir: Provider project directory containing pyproject.toml.

    Returns:
        Path to the freshly built wheel in ``dist/``.

    Raises:
        typer.Exit: If the build fails or produces no wheel.
    """
    console.print("[dim]Building wheel with 'uv build'...[/dim]")

    result = subprocess.run(["uv", "build", "--wheel"], cwd=project_dir, capture_output=True, text=True)

    if result.returncode != 0:
        console.print(f"[red]Error:[/red] uv build failed:\n{result.stderr}")
        raise typer.Exit(1)

    wheels = sorted((project_dir / "dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)

    if not wheels:
        console.print("[red]Error:[/red] uv build produced no wheel in dist/.")
        raise typer.Exit(1)

    return wheels[-1].resolve()


def _read_changelog(path: Path | None) -> str | None:
    """Read changelog text from a file path, returning ``None`` when not supplied.

    Args:
        path: Path to a UTF-8 text file, or ``None``.

    Returns:
        The file's text content, or ``None`` if no path was given.

    Raises:
        typer.Exit: If the file is missing or cannot be read.
    """
    if path is None:
        return None

    if not path.exists():
        console.print(f"[red]Error:[/red] Changelog file not found: {path}")
        raise typer.Exit(1)

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        console.print(f"[red]Error:[/red] Could not read changelog '{path}': {e}")
        raise typer.Exit(1) from e


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
    """Format the API's message for an HTTP error, escaped for Rich markup.

    Args:
        error: The HTTP status error from httpx.

    Returns:
        The API's message, the raw response text, or the error itself,
        with Rich markup escaped so bracketed text prints verbatim.
    """
    message = parse_api_error_message(error.response) or error.response.text or str(error)
    return escape(message)


def format_upload_refusal(error: httpx.HTTPStatusError) -> str:
    """Format why the API refused or could not take an uploaded wheel.

    Args:
        error: The HTTP status error the upload raised.

    Returns:
        The upload-failed message for a 503 whose body carries no message,
        or else the API's message escaped for Rich markup.
    """
    if error.response.status_code == 503 and parse_api_error_message(error.response) is None:
        return UPLOAD_FAILED_MESSAGE

    return _format_api_error(error)


def format_transport_failure(error: httpx.TransportError) -> str:
    """Format what interrupted an exchange with the API, in plain words.

    Args:
        error: The transport error the request raised.

    Returns:
        A lowercase phrase naming the cause, such as ``the API did not
        answer in time``, without a trailing period. A cause without a
        phrase of its own, such as a proxy failure, is named by its error
        type: ``the connection to the API failed (ProxyError)``.
    """
    match error:
        case httpx.ConnectError() | httpx.ConnectTimeout():
            return "the API could not be reached"
        case httpx.WriteTimeout():
            return "sending to the API timed out"
        case httpx.ReadTimeout():
            return "the API did not answer in time"
        case httpx.TimeoutException():
            return "the connection to the API timed out"
        case httpx.NetworkError():
            return "the connection to the API broke off"
        case httpx.ProtocolError():
            return "the connection closed before the API answered"
        case _:
            return f"the connection to the API failed ({type(error).__name__})"


def format_upload_interruption(error: httpx.TransportError) -> str:
    """Format why an upload broke off after the connection to the API was made.

    Args:
        error: The transport error the upload raised.

    Returns:
        The upload-failed message naming what interrupted the upload.
    """
    return f"Could not upload the wheel: {format_transport_failure(error)}. Publish the version again."


def report_lookup_failure(error: httpx.HTTPStatusError, subject: str) -> NoReturn:
    """Print why reading a provider or version failed and exit.

    Args:
        error: The HTTP status error the read raised.
        subject: What was looked up, such as ``Provider 'acme/x'``; a 404
            prints it as not found in the store.

    Raises:
        typer.Exit: Always, with code 1.
    """
    check_bootstrap_error(error)

    if error.response.status_code == 404:
        console.print(f"[red]Error:[/red] {escape(subject)} not found in the store.")
        raise typer.Exit(1) from error

    console.print(f"[red]Error:[/red] {_format_api_error(error)}")
    raise typer.Exit(1) from error


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


def format_wheel_size(size_bytes: int) -> str:
    """Format a wheel's size in decimal megabytes, or kilobytes below one megabyte.

    Args:
        size_bytes: Size of the wheel file in bytes.

    Returns:
        Size such as ``"4.1 MB"`` or ``"86.3 KB"``.
    """
    if size_bytes < 1_000_000:
        return f"{size_bytes / 1_000:.1f} KB"

    return f"{size_bytes / 1_000_000:.1f} MB"


def format_resource_type_count(count: int) -> str:
    """Format the number of resource types a version declares.

    Args:
        count: Number of resource types.

    Returns:
        Count with a singular or plural noun, such as ``"2 resource types"``.
    """
    noun = "resource type" if count == 1 else "resource types"
    return f"{count} {noun}"


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
    typer.echo("  uv sync")
    typer.echo("")
    typer.echo(f"Edit src/{package_name}/resources/ to add your resources.")
    typer.echo("")
    typer.echo("To update this project when the template changes:")
    typer.echo("  copier update")
    typer.echo("")
    typer.echo("When ready to publish a version:")
    typer.echo("  pragma providers publish")


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


def upload_wheel(client: PragmaClient, wheel_path: Path, changelog: str | None) -> ProviderVersion:
    """Upload a wheel and return the version the API accepted for admission.

    Args:
        client: Authenticated SDK client.
        wheel_path: Path to the built ``.whl``.
        changelog: Release notes for the version, or ``None``.

    Returns:
        The ``pending`` version the organization's provider host now admits.

    Raises:
        typer.Exit: If the upload is interrupted, the API cannot take it, or
            the API refuses the wheel; an interruption prints what broke the
            upload off and a refusal prints the API's reason.
        httpx.ConnectError: If the API cannot be reached at all.
        httpx.ConnectTimeout: If connecting to the API times out.
    """
    try:
        return _fetch_with_spinner(
            f"Uploading {wheel_path.name}...",
            lambda: client.publish_provider_version(wheel_path, changelog=changelog),
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise
    except httpx.TransportError as e:
        console.print(f"[red]Error:[/red] {format_upload_interruption(e)}")
        raise typer.Exit(1) from e
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        console.print(f"[red]Error:[/red] {format_upload_refusal(e)}")
        raise typer.Exit(1) from e


def compute_watch_end(version: ProviderVersion, accepted_at: float) -> float:
    """Compute when the publish watch stops waiting for a version, on the monotonic clock.

    Args:
        version: The version as last read.
        accepted_at: ``time.monotonic()`` reading taken when the API accepted
            the upload.

    Returns:
        ``accepted_at`` plus ``ADMISSION_MINIMUM_WATCH_SECONDS``, or, once the
        provider host has taken the version, its ``operation_deadline_at``
        plus ``ADMISSION_DEADLINE_MARGIN_SECONDS`` when that is later. A
        deadline without a timezone is read as UTC.
    """
    minimum_end = accepted_at + ADMISSION_MINIMUM_WATCH_SECONDS
    deadline = version.operation_deadline_at

    if deadline is None:
        return minimum_end

    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)

    seconds_to_deadline = (deadline - datetime.now(UTC)).total_seconds()
    deadline_end = time.monotonic() + seconds_to_deadline + ADMISSION_DEADLINE_MARGIN_SECONDS
    return max(minimum_end, deadline_end)


def watch_admission(client: PragmaClient, pending_version: ProviderVersion, accepted_at: float) -> ProviderVersion:
    """Poll a pending version until admission ends it or the watch bound passes.

    Reads the version every ``ADMISSION_POLL_INTERVAL_SECONDS``. The bound
    follows the version's own admission deadline once the provider host has
    taken it (see :func:`compute_watch_end`), so the watch outlasts any
    deadline the API sets.

    Args:
        client: Authenticated SDK client.
        pending_version: The version the publish returned.
        accepted_at: ``time.monotonic()`` reading taken when the API accepted
            the upload.

    Returns:
        The version as last read: ``published``, ``failed``, or still
        ``pending`` when the watch bound passed first.

    Raises:
        httpx.HTTPStatusError: If reading the version fails.
        httpx.TransportError: If the API cannot be reached.
    """  # noqa: DOC502
    version = pending_version

    while version.status == VersionStatus.PENDING:
        remaining_seconds = compute_watch_end(version, accepted_at) - time.monotonic()

        if remaining_seconds <= 0:
            break

        time.sleep(min(ADMISSION_POLL_INTERVAL_SECONDS, remaining_seconds))
        version = client.get_provider_version(version.canonical, version.version)

    return version


def print_admission_outcome(version: ProviderVersion) -> None:
    """Print how admission of a version ended, exiting non-zero unless it published.

    Args:
        version: The version as the watch last read it.

    Raises:
        typer.Exit: If the version failed admission or is still pending.
    """
    match version.status:
        case VersionStatus.PUBLISHED:
            resource_types = format_resource_type_count(len(version.schemas or []))
            console.print(
                f"[green]Published[/green] {version.canonical} {version.version} — "
                f"Python {version.python_version}, SDK {version.sdk_version}, {resource_types}"
            )
        case VersionStatus.FAILED:
            console.print(f"[red]Error:[/red] {escape(version.error_message or '')}")
            raise typer.Exit(1)
        case VersionStatus.PENDING:
            report_unfinished_admission(version)


def report_watch_failure(reason: str, version: ProviderVersion) -> NoReturn:
    """Print why the publish watch stopped reading a version, with how to follow the version, then exit.

    Args:
        reason: Why the watch stopped, already escaped for Rich markup.
        version: The version being watched.

    Raises:
        typer.Exit: Always, with code 1.
    """
    console.print(f"[red]Error:[/red] {reason}")
    console.print(
        f"Check admission of {version.canonical} {version.version} with: pragma providers versions {version.canonical}"
    )
    raise typer.Exit(1)


def report_unfinished_admission(version: ProviderVersion) -> NoReturn:
    """Print that admission of a version has not finished and how to follow it, then exit.

    Args:
        version: The version still being admitted.

    Raises:
        typer.Exit: Always, with code 1.
    """
    console.print(
        f"[yellow]Admission of {version.canonical} {version.version} has not finished. "
        f"Check it with: pragma providers versions {version.canonical}[/yellow]"
    )
    raise typer.Exit(1)


@app.command()
def publish(
    project_dir: Annotated[
        Path,
        typer.Argument(help="Provider project directory"),
    ] = Path("."),
    wheel: Annotated[
        Path | None,
        typer.Option("--wheel", help="Prebuilt .whl to upload (skips 'uv build')"),
    ] = None,
    changelog: Annotated[
        Path | None,
        typer.Option("--changelog", help="Path to a UTF-8 text file with release notes"),
    ] = None,
):
    """Publish a new provider version and wait for its admission.

    Builds the wheel with 'uv build --wheel' (or takes a prebuilt one via
    '--wheel') and uploads it. The wheel alone identifies the version:
    its 'pragma.provider' entry point names the provider, its metadata
    carries the version, and the publishing organization comes from the
    authenticated user. Your organization's provider host then admits the
    version; the command waits for that and exits non-zero when admission
    fails or has not finished shortly after its admission deadline. A failed
    version can be published again.

    Examples:
        pragma providers publish
        pragma providers publish ./my-provider --changelog NOTES.md
        pragma providers publish --wheel dist/my_provider-1.0.0-py3-none-any.whl
    """  # noqa: DOC501
    wheel_path = wheel.resolve() if wheel else _build_wheel(project_dir)

    if not wheel_path.exists():
        console.print(f"[red]Error:[/red] Wheel not found: {wheel_path}")
        raise typer.Exit(1)

    changelog_text = _read_changelog(changelog)

    client = get_client()
    _require_auth(client)

    pending_version = upload_wheel(client, wheel_path, changelog_text)
    accepted_at = time.monotonic()

    console.print(f"[bold]Publishing[/bold] {pending_version.canonical} {pending_version.version}")
    console.print(f"Uploaded {format_wheel_size(wheel_path.stat().st_size)}")
    console.print("Admitting on your organization's provider host")

    try:
        version = _fetch_with_spinner(
            "Waiting for admission...",
            lambda: watch_admission(client, pending_version, accepted_at),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        report_watch_failure(_format_api_error(e), pending_version)
    except httpx.TransportError as e:
        report_watch_failure(f"Stopped waiting for admission: {format_transport_failure(e)}.", pending_version)

    print_admission_outcome(version)


def _merge_install_config(
    config_flags: list[str] | None,
    config_file_path: str | None,
) -> dict[str, str] | None:
    """Merge config from --config-file and --config flags.

    File values are loaded first, then individual flags override.
    Returns None if no config is provided.
    """  # noqa: DOC201, DOC501
    result: dict[str, str] = {}

    if config_file_path is not None:
        path = Path(config_file_path)

        if not path.exists():
            console.print(f"[red]Error:[/red] Config file not found: {config_file_path}")
            raise typer.Exit(1)

        try:
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            console.print(f"[red]Error:[/red] Failed to parse config file '{config_file_path}': {e}")
            raise typer.Exit(1) from e
        except (OSError, UnicodeError) as e:
            console.print(f"[red]Error:[/red] Could not read config file '{config_file_path}': {e}")
            raise typer.Exit(1) from e

        if data is None:
            console.print(f"[red]Error:[/red] Config file is empty: {config_file_path}")
            raise typer.Exit(1)

        if not isinstance(data, dict):
            console.print(f"[red]Error:[/red] Config file must contain a YAML mapping, got {type(data).__name__}")
            raise typer.Exit(1)

        for key, value in data.items():
            if isinstance(value, bool):
                result[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                result[str(key)] = str(value)
            else:
                console.print(
                    f"[red]Error:[/red] Config key '{key}' has unsupported type {type(value).__name__}. "
                    "Only strings, numbers, and booleans are allowed."
                )
                raise typer.Exit(1)

    if config_flags is not None:
        for entry in config_flags:
            if "=" not in entry:
                console.print(f"[red]Error:[/red] Invalid config format '{entry}'. Expected KEY=VALUE.")
                raise typer.Exit(1)

            key, _, value = entry.partition("=")

            if not key:
                console.print(f"[red]Error:[/red] Config key cannot be empty in '{entry}'.")
                raise typer.Exit(1)

            result[key] = value

    return result if result else None


def fetch_install_preview(client: PragmaClient, name: str, version: str | None) -> tuple[str, str]:
    """Fetch the label and version an install confirms before it runs.

    With an explicit version, reads that version in any status, so a version
    the caller's organization is still admitting, or one that failed, reaches
    the install request and its refusal rather than stopping at the catalog.
    Without one, reads the catalog entry and previews its latest version.

    Args:
        client: Authenticated SDK client.
        name: Provider name (``org/name``).
        version: Version to install, or ``None`` for the default.

    Returns:
        Tuple of (label, version to show); the label is the catalog display
        name, or ``name`` when ``version`` is given or the catalog has none.

    Raises:
        typer.Exit: If the provider or version is not found or the request fails.
    """  # noqa: DOC502
    try:
        if version is None:
            provider = _fetch_with_spinner(f"Fetching provider '{name}'...", lambda: client.get_provider(name))
            label = provider.display_name or name
            latest_version = provider.latest_version or "latest"
            return label, latest_version

        provider_version = _fetch_with_spinner(
            f"Fetching {name} {version}...",
            lambda: client.get_provider_version(name, version),
        )
        return name, provider_version.version
    except httpx.HTTPStatusError as e:
        subject = f"Provider '{name}'" if version is None else f"Version {version} of '{name}'"
        report_lookup_failure(e, subject)


@app.command()
def install(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str | None, typer.Option("--version", "-v", help="Version to install (default: latest)")] = None,
    upgrade_policy: Annotated[
        str,
        typer.Option("--upgrade-policy", help="Upgrade policy (manual, auto-minor, auto-patch)"),
    ] = "manual",
    config: Annotated[
        list[str] | None,
        typer.Option("--config", "-c", help="Configuration key=value pair (repeatable)"),
    ] = None,
    config_file: Annotated[
        str | None,
        typer.Option("--config-file", help="Path to YAML file with configuration key-value pairs"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Install a provider from the store.

    Examples:
        pragma providers install pragmatiks/qdrant
        pragma providers install pragmatiks/postgres --version 1.2.0
        pragma providers install pragmatiks/redis --upgrade-policy auto-minor
        pragma providers install pragmatiks/qdrant --config SOME_KEY=some_value
        pragma providers install pragmatiks/qdrant --config-file config.yaml --config OVERRIDE_KEY=value
        pragma providers install pragmatiks/qdrant -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    merged_config = _merge_install_config(config, config_file)

    display, install_version = fetch_install_preview(client, name, version)

    provider_label = name if display == name else f"{display} ({name})"
    console.print(f"[bold]Provider:[/bold] {provider_label}")
    console.print(f"[bold]Version:[/bold]  {install_version}")

    if merged_config:
        console.print("[bold]Config:[/bold]")
        for key, value in sorted(merged_config.items()):
            console.print(f"  {key} = {value}", markup=False)

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
                upgrade_policy=upgrade_policy,
                config=merged_config,
            ),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
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
        check_bootstrap_error(e)

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
        confirm = typer.confirm(f"Upgrade {name} to v{target}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Upgrading provider...",
            lambda: client.upgrade_provider(name, target_version=version),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Upgraded:[/green] {name} -> v{result.installed_version}")


@app.command()
def downgrade(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str, typer.Option("--version", "-v", help="Target version to downgrade to")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Downgrade an installed provider to a previous version.

    Requires an explicit target version. Migrations run sequentially
    through each intermediate version in reverse order.

    Examples:
        pragma providers downgrade pragmatiks/qdrant --version 1.0.0
        pragma providers downgrade pragmatiks/postgres -v 1.2.0 -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Downgrading:[/bold] {name} -> v{version}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Downgrade {name} to v{version}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Downgrading provider...",
            lambda: client.downgrade_provider(name, target_version=version),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 422:
            console.print(f"[red]Error:[/red] {_format_api_error(e)}")
            console.print("[dim]The version chain between current and target may be broken.[/dim]")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Downgraded:[/green] {name} -> v{result.installed_version}")


@app.command("list")
def list_providers(
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
        _list_installations(client, output)
        return

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    try:
        result = _fetch_with_spinner(
            "Fetching providers...",
            lambda: client.list_providers(
                query=query,
                scope=scope,
                tags=tag_list,
                limit=limit,
                offset=offset,
            ),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
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


def _list_installations(client: PragmaClient, output: OutputFormat) -> None:
    """List provider installations for the current tenant.

    Args:
        client: SDK client instance.
        output: Output format for display.
    """  # noqa: DOC501
    _require_auth(client)

    try:
        providers = _fetch_with_spinner(
            "Fetching installed providers...",
            lambda: client.list_installations(),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
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
        provider = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        report_lookup_failure(e, f"Provider '{name}'")

    try:
        versions = _fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        versions = []

    if output == OutputFormat.TABLE:
        _print_provider_info(provider, versions)
    else:
        data = _provider_detail_to_dict(provider, versions)
        output_data(data, output)


@app.command()
def versions(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List the versions of a provider with their status.

    A version your organization's provider host is still admitting shows
    as "admitting"; a failed version shows why it failed. Versions that
    are not published are visible only to the organization that published
    them.

    Examples:
        pragma providers versions acme/pipeline
        pragma providers versions acme/pipeline -o json
    """  # noqa: DOC501
    client = get_client()

    try:
        provider_versions = _fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        report_lookup_failure(e, f"Provider '{name}'")

    if output != OutputFormat.TABLE:
        data = [provider_version.model_dump(mode="json") for provider_version in provider_versions]
        output_data(data, output)
        return

    if not provider_versions:
        console.print("[dim]No versions found.[/dim]")
        return

    print_versions_table(provider_versions)


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
        check_bootstrap_error(e)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
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
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    try:
        result = client.get_deployment_status(provider_id)
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Deployment not found for provider: {provider_id}")
            raise typer.Exit(1) from e

        raise
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
    """Delete a provider from the catalog (admins of the owning organization only).

    Removes the provider and all its versions from the catalog. A provider
    installed in at least one organization is refused; it can be deleted
    once every installation is uninstalled.

    Examples:
        pragma providers delete myorg/my-provider
        pragma providers delete myorg/my-provider --yes

    Raises:
        typer.Exit: If deletion fails or user cancels.
    """
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Provider:[/bold] {name}")
    console.print("[yellow]Warning:[/yellow] This will permanently delete the provider from the catalog.")
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
        check_bootstrap_error(e)
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
    table.add_column("Author")
    table.add_column("Latest Version")
    table.add_column("Installs", justify="right")
    table.add_column("Tags")

    for provider in result.items:
        tags_display = ", ".join(getattr(provider, "tags", []) or [])
        install_count = getattr(provider, "install_count", 0) or 0
        author = getattr(provider, "author", None)
        author_display = getattr(author, "display_name", None) or "[dim]-[/dim]"

        table.add_row(
            provider.canonical,
            getattr(provider, "display_name", None) or "[dim]-[/dim]",
            author_display,
            getattr(provider, "latest_version", None) or "[dim]-[/dim]",
            str(install_count),
            tags_display or "[dim]-[/dim]",
        )

    console.print(table)

    total = getattr(result, "total", 0)
    offset = getattr(result, "offset", 0)
    showing_end = min(offset + len(result.items), total)
    console.print(f"[dim]Showing {offset + 1}-{showing_end} of {total} providers[/dim]")


def _print_provider_info(provider, versions: list | None = None) -> None:
    """Print detailed provider information in a panel with version table.

    Args:
        provider: Provider metadata object.
        versions: List of provider version objects.
    """
    versions = versions or []

    author = getattr(provider, "author", None)
    author_display = getattr(author, "display_name", None) or "[dim]-[/dim]"
    tags = ", ".join(getattr(provider, "tags", []) or []) or "[dim]-[/dim]"
    install_count = getattr(provider, "install_count", 0) or 0
    description = getattr(provider, "description", None) or "[dim]No description[/dim]"
    created_at = getattr(provider, "created_at", None)
    updated_at = getattr(provider, "updated_at", None)

    info_lines = [
        f"[bold]Name:[/bold]         {provider.canonical}",
        f"[bold]Display Name:[/bold] {getattr(provider, 'display_name', None) or provider.canonical}",
        f"[bold]Author:[/bold]       {author_display}",
        f"[bold]Description:[/bold]  {description}",
        f"[bold]Tags:[/bold]         {tags}",
        f"[bold]Installs:[/bold]     {install_count}",
    ]

    if created_at:
        info_lines.append(f"[bold]Created:[/bold]      {str(created_at)[:19]}")

    if updated_at:
        info_lines.append(f"[bold]Updated:[/bold]      {str(updated_at)[:19]}")

    panel = Panel("\n".join(info_lines), title=provider.canonical, border_style="blue")
    console.print(panel)

    if versions:
        console.print()
        print_versions_table(versions)


def print_versions_table(versions: list[ProviderVersion]) -> None:
    """Print provider versions with their admission status in a table.

    Args:
        versions: Provider versions to list.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Published")
    table.add_column("Message")

    for provider_version in versions:
        published_at = provider_version.published_at
        published = published_at.strftime("%Y-%m-%d %H:%M:%S") if published_at else "[dim]-[/dim]"
        error_message = provider_version.error_message
        message = escape(error_message) if error_message else "[dim]-[/dim]"

        table.add_row(
            provider_version.version,
            VERSION_STATUS_DISPLAY[provider_version.status],
            published,
            message,
        )

    console.print(table)


def _print_installed_table(providers) -> None:
    """Print installed providers in a formatted table.

    Args:
        providers: List of installed provider summaries.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Version")
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
            p.canonical,
            p.installed_version,
            getattr(p, "upgrade_policy", None) or "[dim]-[/dim]",
            installed_at,
            upgrade_display,
        )

    console.print(table)


def _serialize_datetime(obj: object, attr: str) -> str | None:
    val = getattr(obj, attr, None)
    return val.isoformat() if val else None


def _author_to_dict(author) -> dict | None:
    """Convert a ProviderAuthor model to a plain dict for JSON/YAML output.

    Args:
        author: ProviderAuthor object or None.

    Returns:
        Dictionary representation, or None if no author.
    """
    if author is None:
        return None

    return {
        "kind": getattr(author, "kind", None),
        "organization_id": getattr(author, "organization_id", None),
        "display_name": getattr(author, "display_name", None),
    }


def _provider_summary_to_dict(provider) -> dict:
    """Convert a store provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Store provider summary object.

    Returns:
        Dictionary representation.
    """
    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": _author_to_dict(getattr(provider, "author", None)),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
    }


def _provider_detail_to_dict(provider, versions: list[ProviderVersion] | None = None) -> dict:
    """Convert a provider and its versions to a plain dict for JSON/YAML output.

    Args:
        provider: Provider metadata object.
        versions: List of provider version objects.

    Returns:
        Dictionary representation.
    """
    versions = versions or []

    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": _author_to_dict(getattr(provider, "author", None)),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
        "readme": getattr(provider, "readme", None),
        "created_at": _serialize_datetime(provider, "created_at"),
        "updated_at": _serialize_datetime(provider, "updated_at"),
        "versions": [provider_version.model_dump(mode="json") for provider_version in versions],
    }


def _installed_provider_to_dict(provider) -> dict:
    """Convert an installed provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Installed provider summary object.

    Returns:
        Dictionary representation.
    """
    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "installed_version": provider.installed_version,
        "upgrade_policy": getattr(provider, "upgrade_policy", None),
        "installed_at": _serialize_datetime(provider, "installed_at"),
        "latest_version": getattr(provider, "latest_version", None),
        "upgrade_available": getattr(provider, "upgrade_available", False),
    }
