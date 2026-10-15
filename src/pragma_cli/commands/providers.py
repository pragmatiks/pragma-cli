"""Provider management commands.

Unified commands for registering, installing, deploying, and managing
Pragmatiks providers.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, Any, NoReturn

import click
import copier
import httpx
import typer
import yaml
from pragma_sdk import (
    SETTLED_VERSION_STATUSES,
    DeclaredProviderVersion,
    DeploymentResult,
    DeploymentStatus,
    PragmaClient,
    ProviderVersion,
    ProviderVersionDeclaration,
    UpgradePolicy,
    VersionStatus,
    read_wheel_declaration,
)
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_provider_ids
from pragma_cli.errors import (
    check_bootstrap_error,
    error_console,
    format_api_error,
    format_request_failure,
    is_request_unsent,
    print_api_error,
    report_not_found,
    require_auth,
)
from pragma_cli.exit_codes import (
    ADMISSION_UNCONFIRMED_EXIT_CODE,
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
)
from pragma_cli.helpers import OutputFormat, format_optional_value, output_data, parse_api_error_message


app = typer.Typer(help="Provider management commands")
console = Console()

DEFAULT_TEMPLATE_URL = "gh:pragmatiks/pragma-providers"
TEMPLATE_PATH_ENV = "PRAGMA_PROVIDER_TEMPLATE"

ADMISSION_POLL_INTERVAL_SECONDS = 2.0
ADMISSION_MINIMUM_WATCH_SECONDS = 150.0
"""Shortest time the publish watch waits from the upload, or from the declaration when no upload was needed."""
ADMISSION_DEADLINE_MARGIN_SECONDS = 30.0
"""Time the publish watch keeps waiting past a version's ``operation_deadline_at``, for settlement and clock skew."""

PUBLISH_UNAVAILABLE_MESSAGE = "Pragmatiks cannot accept the publish right now. Try again later."
"""Message for a 503 refusing a declaration or an upload whose body carries no message of its own."""

UNREADABLE_WHEEL_ERRORS = (ValueError, zipfile.BadZipFile)
"""Errors ``pragma_sdk.read_wheel_declaration`` raises when the file is not a readable wheel."""

VERSION_STATUS_LABELS = {
    VersionStatus.AWAITING_WHEEL: "waiting for the wheel",
    VersionStatus.STAGED: "wheel received",
    VersionStatus.QUEUED: "waiting for the provider host",
    VersionStatus.ADMITTING: "admitting",
    VersionStatus.PROMOTING: "publishing to the registry",
    VersionStatus.PUBLISHED: "published",
    VersionStatus.FAILED: "failed",
}
"""Plain-words label of each version status, naming what an unsettled version waits on."""

AWAITING_WHEEL_HINT = "If your publish stopped, run 'pragma providers publish' again."
"""Message the versions table shows for an ``awaiting_wheel`` version, which has no message of its own."""

VERSION_STATUS_COLORS = {
    VersionStatus.PUBLISHED: "green",
    VersionStatus.FAILED: "red",
}
"""Rich color of each settled version status; every unsettled status shows yellow."""


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


def run_wheel_build(project_directory: Path) -> Path:
    """Build the provider wheel with ``uv build`` into ``project_directory/dist/``.

    Args:
        project_directory: Provider project directory containing pyproject.toml.

    Returns:
        Path to the newest wheel in ``project_directory/dist/``.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the directory has no
            pyproject.toml, uv is not installed, or the build fails or
            produces no wheel.
    """
    if not (project_directory / "pyproject.toml").is_file():
        error_console.print(
            f"[red]Error:[/red] {escape(str(project_directory))} has no pyproject.toml; "
            "pass a provider project directory or --wheel <path>."
        )
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    error_console.print("[dim]Building wheel with 'uv build'...[/dim]")

    wheel_output_directory = (project_directory / "dist").resolve()

    try:
        result = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheel_output_directory)],
            cwd=project_directory,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        error_console.print("[red]Error:[/red] uv is not installed; install it or pass --wheel <path>.")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e

    if result.returncode != 0:
        error_console.print(f"[red]Error:[/red] uv build failed:\n{escape(result.stderr)}")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    wheels = sorted(wheel_output_directory.glob("*.whl"), key=lambda path: path.stat().st_mtime)

    if not wheels:
        error_console.print(f"[red]Error:[/red] uv build produced no wheel in {escape(str(wheel_output_directory))}.")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    return wheels[-1]


def _read_changelog(path: Path | None) -> str | None:
    """Read changelog text from a file path, returning ``None`` when not supplied.

    Args:
        path: Path to an existing UTF-8 text file, or ``None``.

    Returns:
        The file's text content, or ``None`` if no path was given.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the file cannot be read.
    """
    if path is None:
        return None

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        error_console.print(f"[red]Error:[/red] Could not read changelog '{escape(str(path))}': {escape(str(e))}")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e


def validate_provider_name(context: typer.Context, name: str) -> str:
    """Check a provider name is namespaced as ``org/name``, for use as a Typer argument callback.

    Args:
        context: Click context of the command being parsed.
        name: Provider name as typed.

    Returns:
        The name unchanged.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if the
            name is not ``org/name``.
    """
    if context.resilient_parsing:
        return name

    organization, _, provider = name.partition("/")

    if not organization or not provider or "/" in provider:
        raise typer.BadParameter(f"must be 'org/name', got '{name}'.")

    return name


ProviderNameArgument = Annotated[
    str,
    typer.Argument(help="Provider name (org/name format)", callback=validate_provider_name),
]
"""Positional ``org/name`` provider argument, checked by ``validate_provider_name``."""

CompletedProviderNameArgument = Annotated[
    str,
    typer.Argument(
        help="Provider name (org/name format)",
        autocompletion=completion_provider_ids,
        callback=validate_provider_name,
    ),
]
"""``ProviderNameArgument`` that shell completion fills from the provider catalog."""


def build_spinner_progress() -> Progress:
    """Build a transient spinner display on stderr.

    Returns:
        A Rich progress display with a spinner and a task description,
        drawn on stderr and cleared once it stops.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=error_console,
        transient=True,
    )


def _fetch_with_spinner(description: str, fetch_fn) -> Any:
    """Execute a function with a spinner progress indicator on stderr, keeping stdout for output.

    Args:
        description: Text to display next to the spinner, printed verbatim
            with Rich markup escaped.
        fetch_fn: Zero-argument callable to execute.

    Returns:
        Result from fetch_fn.
    """
    with build_spinner_progress() as progress:
        progress.add_task(escape(description), total=None)
        return fetch_fn()


def format_publish_refusal(error: httpx.HTTPStatusError) -> str:
    """Format why the API refused or could not take a declaration or an upload.

    Args:
        error: The HTTP status error the declaration or the upload raised.

    Returns:
        ``PUBLISH_UNAVAILABLE_MESSAGE`` for a 503 whose body carries no
        message, or else ``format_api_error``'s text, escaped for Rich
        markup.
    """
    if error.response.status_code == 503 and parse_api_error_message(error.response) is None:
        return PUBLISH_UNAVAILABLE_MESSAGE

    return escape(format_api_error(error))


def format_publish_interruption(error: httpx.TransportError) -> str:
    """Format why a declaration or an upload broke off after the connection to the API was made.

    Args:
        error: The transport error the declaration or the upload raised.

    Returns:
        A message naming what interrupted the publish and telling the user
        to publish again, escaped for Rich markup.
    """
    return (
        f"Could not finish the publish: {escape(format_request_failure(error))}. Run 'pragma providers publish' again."
    )


def format_version_status(status: VersionStatus) -> str:
    """Format a version status for the terminal.

    Args:
        status: The version's status.

    Returns:
        The status label from ``VERSION_STATUS_LABELS`` in Rich markup:
        green when published, red when failed, yellow while unsettled.
    """
    color = VERSION_STATUS_COLORS.get(status, "yellow")
    return f"[{color}]{escape(VERSION_STATUS_LABELS[status])}[/{color}]"


def format_version_message(version: ProviderVersion) -> str:
    """Format the message column of a version row.

    Args:
        version: The version to show.

    Returns:
        The version's own message, ``AWAITING_WHEEL_HINT`` for an
        ``awaiting_wheel`` version without one, or a dim dash, with Rich
        markup escaped.
    """
    if not version.error_message and version.status == VersionStatus.AWAITING_WHEEL:
        return escape(AWAITING_WHEEL_HINT)

    return format_optional_value(version.error_message)


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

    \f

    Raises:
        typer.Exit: If directory already exists or template copy fails.
    """
    project_directory = output_dir or Path(f"./{name}-provider")

    if project_directory.exists():
        error_console.print(f"[red]Error:[/red] Directory {escape(str(project_directory))} already exists.")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    template_source = get_template_source()

    data = {"name": name}

    if description:
        data["description"] = description

    if author_name:
        data["author_name"] = author_name

    if author_email:
        data["author_email"] = author_email

    typer.echo(f"Creating provider project: {project_directory}")
    typer.echo(f"  Template: {template_source}")
    typer.echo("")

    try:
        vcs_ref = "HEAD" if not template_source.startswith("gh:") else None
        copier.run_copy(
            src_path=template_source,
            dst_path=project_directory,
            data=data,
            defaults=defaults,
            unsafe=True,
            vcs_ref=vcs_ref,
        )
    except Exception as e:
        error_console.print(f"[red]Error:[/red] Could not create the provider project: {escape(str(e))}")
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    package_name = name.lower().replace("-", "_").replace(" ", "_") + "_provider"

    typer.echo("")
    typer.echo(f"Created provider project: {project_directory}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {project_directory}")
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
    project_directory: Annotated[
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

    \f

    Raises:
        typer.Exit: If directory is not a Copier project or update fails.
    """
    answers_file = project_directory / ".copier-answers.yml"

    if not answers_file.exists():
        error_console.print(
            f"[red]Error:[/red] {escape(str(project_directory))} is not a Copier-generated project "
            "(missing .copier-answers.yml)."
        )
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    typer.echo(f"Updating provider project: {project_directory}")
    typer.echo("")

    try:
        copier.run_update(dst_path=project_directory, unsafe=True)
    except Exception as e:
        error_console.print(f"[red]Error:[/red] Could not update the provider project: {escape(str(e))}")
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    typer.echo("")
    typer.echo("Provider project updated successfully.")


def read_declaration(wheel_path: Path, changelog: str | None) -> ProviderVersionDeclaration:
    """Read what a built wheel declares about itself, before anything is sent to the API.

    Args:
        wheel_path: Path to the built ``.whl``.
        changelog: Release notes for the version, or ``None``.

    Returns:
        The wheel's declaration.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the file is not a zip
            archive or has no single top-level ``.dist-info`` directory with
            a ``METADATA`` file in it (see ``UNREADABLE_WHEEL_ERRORS``).
    """
    try:
        return read_wheel_declaration(wheel_path, changelog=changelog)
    except UNREADABLE_WHEEL_ERRORS as e:
        error_console.print(f"[red]Error:[/red] {escape(str(e))}. Rebuild the wheel and publish it again.")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e


def report_publish_failure(error: httpx.TransportError | httpx.HTTPStatusError) -> NoReturn:
    """Print why a declaration or an upload failed, and exit.

    Args:
        error: The error the declaration or the upload raised.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the request broke off after
            the API received some of it, which prints what broke it off, or
            if the API refused it or could not take it, which prints the
            API's reason or the set-up message for an organization that is
            not ready.
        httpx.TransportError: If the API received none of the request, as
            ``errors.is_request_unsent`` tells: the request is invalid, or
            connecting failed or timed out.
    """  # noqa: DOC502
    if isinstance(error, httpx.HTTPStatusError):
        check_bootstrap_error(error)
        error_console.print(f"[red]Error:[/red] {format_publish_refusal(error)}")
        raise typer.Exit(FAILURE_EXIT_CODE) from error

    if is_request_unsent(error):
        raise error

    error_console.print(f"[red]Error:[/red] {format_publish_interruption(error)}")
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def declare_version(client: PragmaClient, declaration: ProviderVersionDeclaration) -> DeclaredProviderVersion:
    """Declare the version a wheel describes.

    Args:
        client: Authenticated SDK client.
        declaration: The wheel's declaration.

    Returns:
        The declared version: ``awaiting_wheel`` for a new declaration, or
        the version in flight, in whatever status it is in, for a
        declaration identical to it.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the API refuses the
            declaration, a 404 included, cannot take it, or the request
            breaks off (see :func:`report_publish_failure`).
        httpx.TransportError: If the API received none of the request.
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Declaring {declaration.wheel_filename}...",
            lambda: client.declare_provider_version(declaration),
        )
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        report_publish_failure(e)


def upload_declared_wheel(client: PragmaClient, operation_id: str, wheel_path: Path) -> ProviderVersion:
    """Upload the wheel of a declared version.

    Args:
        client: Authenticated SDK client.
        operation_id: The declared version's ``operation_id``.
        wheel_path: Path to the declared ``.whl``.

    Returns:
        The version after the upload, ``staged`` or further along.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the API refuses the upload,
            a 409 for a publish another one replaced or that has finished
            included, cannot take it, or the request breaks off (see
            :func:`report_publish_failure`).
        httpx.TransportError: If the API received none of the request.
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Uploading {wheel_path.name}...",
            lambda: client.upload_provider_wheel(operation_id, wheel_path),
        )
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        report_publish_failure(e)


def is_replaced_publish(version: ProviderVersion, watched_version: ProviderVersion) -> bool:
    """Tell whether another publish of the same version replaced the one being watched.

    A replacing publish records its own wheel, or none until its upload, so
    the version's ``wheel_sha256`` no longer matches the one it carried when
    the watch started. A republish of the same wheel with only a new
    changelog goes unnoticed.

    Args:
        version: The version as last read.
        watched_version: The version the watch started from, which already
            holds this publish's wheel.

    Returns:
        ``True`` if the two versions' ``wheel_sha256`` differ.
    """
    return version.wheel_sha256 != watched_version.wheel_sha256


def compute_watch_end(version: ProviderVersion, accepted_at: float) -> float:
    """Compute when the publish watch stops waiting for a version, on the monotonic clock.

    Args:
        version: The version as last read.
        accepted_at: ``time.monotonic()`` reading taken when the API accepted
            the upload, or the declaration when no upload was needed.

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


def watch_admission(
    client: PragmaClient,
    watched_version: ProviderVersion,
    accepted_at: float,
    show_status: Callable[[VersionStatus], object],
) -> ProviderVersion:
    """Poll a version until it settles, another publish replaces it, or the watch bound passes.

    Reads the version every ``ADMISSION_POLL_INTERVAL_SECONDS``. The bound
    follows the version's own admission deadline once the provider host has
    taken it (see :func:`compute_watch_end`), so the watch outlasts any
    deadline the API sets.

    Args:
        client: Authenticated SDK client.
        watched_version: The version after the upload, or after the
            declaration when the wheel was already received; the baseline
            :func:`is_replaced_publish` compares against.
        accepted_at: ``time.monotonic()`` reading taken when the API accepted
            the upload, or the declaration when no upload was needed.
        show_status: Called with the version's status each time it changes,
            starting with the status of ``watched_version``; not called for
            a read showing another publish replaced this one.

    Returns:
        The version as last read: ``published``, ``failed``, replaced, or
        still unsettled when the watch bound passed first.

    Raises:
        httpx.HTTPStatusError: If reading the version fails.
        httpx.TransportError: If the API cannot be reached.
    """  # noqa: DOC502
    version = watched_version
    show_status(version.status)

    while version.status not in SETTLED_VERSION_STATUSES:
        remaining_seconds = compute_watch_end(version, accepted_at) - time.monotonic()

        if remaining_seconds <= 0:
            break

        time.sleep(min(ADMISSION_POLL_INTERVAL_SECONDS, remaining_seconds))
        previous_status = version.status
        version = client.get_provider_version(version.canonical, version.version)

        if is_replaced_publish(version, watched_version):
            break

        if version.status != previous_status:
            show_status(version.status)

    return version


def show_version_status(progress: Progress, task: TaskID, status: VersionStatus) -> None:
    """Print a version status as a line on stderr and show its label next to the spinner.

    Args:
        progress: The spinner the publish watch runs behind.
        task: The spinner's task.
        status: The version's new status.
    """
    label = VERSION_STATUS_LABELS[status]
    capitalized_label = label[:1].upper() + label[1:]
    progress.console.print(f"Status: {format_version_status(status)}")
    progress.update(task, description=f"{escape(capitalized_label)}...")


def watch_admission_with_progress(
    client: PragmaClient,
    watched_version: ProviderVersion,
    accepted_at: float,
) -> ProviderVersion:
    """Run :func:`watch_admission` behind a spinner on stderr, printing each status the version reaches.

    Args:
        client: Authenticated SDK client.
        watched_version: The version after the upload, or after the
            declaration when the wheel was already received.
        accepted_at: ``time.monotonic()`` reading taken when the API accepted
            the upload, or the declaration when no upload was needed.

    Returns:
        The version as :func:`watch_admission` last read it.

    Raises:
        httpx.HTTPStatusError: If reading the version fails.
        httpx.TransportError: If the API cannot be reached.
    """  # noqa: DOC502
    with build_spinner_progress() as progress:
        task = progress.add_task("", total=None)
        return watch_admission(client, watched_version, accepted_at, partial(show_version_status, progress, task))


def print_admission_outcome(version: ProviderVersion, watched_version: ProviderVersion, resume_command: str) -> None:
    """Print how the publish of a version ended, exiting non-zero unless it published.

    Args:
        version: The version as the watch last read it.
        watched_version: The version the watch started from.
        resume_command: Command that keeps waiting for the version, from ``build_resume_command``.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if another publish replaced
            this one or the version failed admission, or with
            ``ADMISSION_UNCONFIRMED_EXIT_CODE`` if it has not settled.
    """
    if is_replaced_publish(version, watched_version):
        report_replaced_publish(version)

    match version.status:
        case VersionStatus.PUBLISHED:
            resource_types = format_resource_type_count(len(version.schemas or []))
            console.print(
                f"[green]Published[/green] {escape(version.canonical)} {escape(version.version)} — "
                f"Python {escape(str(version.python_version))}, SDK {escape(str(version.sdk_version))}, "
                f"{resource_types}"
            )
        case VersionStatus.FAILED:
            error_console.print(f"[red]Error:[/red] {escape(version.error_message or '')}")
            raise typer.Exit(FAILURE_EXIT_CODE)
        case _:
            report_unfinished_admission(version, resume_command)


def report_replaced_publish(version: ProviderVersion) -> NoReturn:
    """Print that another publish of the same version replaced this one, and exit.

    Args:
        version: The version as the watch last read it.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    canonical = escape(version.canonical)
    error_console.print(
        f"[red]Error:[/red] {canonical} {escape(version.version)} was replaced by another publish of this version."
    )
    error_console.print(f"Check it with: pragma providers versions {canonical}")
    raise typer.Exit(FAILURE_EXIT_CODE)


def report_watch_failure(reason: str, version: ProviderVersion, resume_command: str) -> NoReturn:
    """Print why the publish watch stopped reading a version, with how to follow the version, then exit.

    Args:
        reason: Why the watch stopped, not escaped for Rich markup.
        version: The version being watched.
        resume_command: Command that keeps waiting for the version, from ``build_resume_command``.

    Raises:
        typer.Exit: Always, with ``ADMISSION_UNCONFIRMED_EXIT_CODE``.
    """
    canonical = escape(version.canonical)
    error_console.print(f"[red]Error:[/red] Stopped waiting for admission: {escape(reason.rstrip('.'))}.")
    error_console.print(
        f"Check admission of {canonical} {escape(version.version)} with: pragma providers versions {canonical}"
    )
    print_resume_hint(resume_command)
    raise typer.Exit(ADMISSION_UNCONFIRMED_EXIT_CODE)


def report_unfinished_admission(version: ProviderVersion, resume_command: str) -> NoReturn:
    """Print that a version has not settled, what it waits on, and how to follow it, then exit.

    Args:
        version: The version as the watch last read it, still unsettled.
        resume_command: Command that keeps waiting for the version, from ``build_resume_command``.

    Raises:
        typer.Exit: Always, with ``ADMISSION_UNCONFIRMED_EXIT_CODE``.
    """
    canonical = escape(version.canonical)
    error_console.print(
        f"[yellow]Admission of {canonical} {escape(version.version)} has not finished: "
        f"{escape(VERSION_STATUS_LABELS[version.status])}. "
        f"Check it with: pragma providers versions {canonical}[/yellow]"
    )
    print_resume_hint(resume_command)
    raise typer.Exit(ADMISSION_UNCONFIRMED_EXIT_CODE)


def print_resume_hint(resume_command: str) -> None:
    """Print how to keep waiting for admission of the version just published.

    Args:
        resume_command: Command that keeps waiting for the version, from ``build_resume_command``.
    """
    error_console.print(f"To keep waiting, run: {escape(resume_command)}", soft_wrap=True)


def build_resume_command(wheel_path: Path, changelog: Path | None) -> str:
    """Build the publish command that sends the same declaration again.

    The same declaration makes publish watch the version in flight instead
    of replacing it: the wheel and the changelog are both part of it, so a
    rebuilt wheel or a missing changelog would replace the version.

    Args:
        wheel_path: The published wheel.
        changelog: The release notes file passed to this publish, if any.

    Returns:
        The command, quoted for a POSIX shell.
    """
    arguments = ["pragma", "providers", "publish", "--wheel", str(wheel_path)]
    if changelog is not None:
        arguments += ["--changelog", str(changelog)]

    return shlex.join(arguments)


@app.command()
def publish(
    project_directory: Annotated[
        Path | None,
        typer.Argument(
            help="Provider project directory (default: current directory); the wheel is built into its dist/",
            show_default=False,
            exists=True,
            file_okay=False,
        ),
    ] = None,
    wheel: Annotated[
        Path | None,
        typer.Option(
            "--wheel",
            help="Prebuilt .whl to upload instead of building one; not combined with PROJECT_DIRECTORY",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    changelog: Annotated[
        Path | None,
        typer.Option(
            "--changelog",
            help="Path to a UTF-8 text file with release notes",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
):
    """Publish a new provider version and wait for its admission.

    Builds the wheel with 'uv build --wheel' into PROJECT_DIRECTORY/dist/
    (the current directory by default), or takes a prebuilt one via '--wheel',
    declares the version it describes and uploads it, unless the same
    declaration is already in flight with its wheel received. The wheel
    alone identifies the version: its 'pragma.provider' entry point names
    the provider, its metadata carries the version, and the publishing
    organization comes from the authenticated user. Your organization's
    provider host then admits the version; the command waits for that and
    exits non-zero when admission fails, another publish of the version
    replaces this one, or the version has not settled shortly after its
    admission deadline. A failed version can be published again.

    Exit code 3: the upload was accepted, or the wheel was already received,
    but admission is not confirmed: the watch ended with the version still
    unsettled, or could not read the version. Publishing the same wheel
    again with '--wheel', and the same '--changelog' if one was given,
    keeps waiting for it.

    Examples:
        pragma providers publish
        pragma providers publish ./my-provider --changelog NOTES.md
        pragma providers publish --wheel dist/my_provider-1.0.0-py3-none-any.whl

    \f

    Raises:
        click.UsageError: If both PROJECT_DIRECTORY and --wheel are given.
    """
    if wheel is not None and project_directory is not None:
        raise click.UsageError("Pass either PROJECT_DIRECTORY or --wheel, not both.")

    changelog_text = _read_changelog(changelog)

    client = get_client()
    require_auth(client)

    wheel_path = wheel or run_wheel_build(project_directory or Path("."))
    declaration = read_declaration(wheel_path, changelog_text)

    declared_version = declare_version(client, declaration)
    error_console.print(
        f"[bold]Publishing[/bold] {escape(declared_version.canonical)} {escape(declared_version.version)}"
    )
    error_console.print(f"[dim]Operation {escape(declared_version.operation_id)}[/dim]")

    if declared_version.status == VersionStatus.AWAITING_WHEEL:
        watched_version = upload_declared_wheel(client, declared_version.operation_id, wheel_path)
        error_console.print(f"Uploaded {format_wheel_size(declaration.wheel_size)}")
    else:
        watched_version = declared_version
        error_console.print("Wheel already received")

    accepted_at = time.monotonic()
    resume_command = build_resume_command(wheel_path, changelog)

    try:
        last_read_version = watch_admission_with_progress(client, watched_version, accepted_at)
    except httpx.HTTPStatusError as e:
        report_watch_failure(format_api_error(e), watched_version, resume_command)
    except httpx.TransportError as e:
        report_watch_failure(format_request_failure(e), watched_version, resume_command)
    except Exception as e:
        report_watch_failure(str(e) or type(e).__name__, watched_version, resume_command)

    print_admission_outcome(last_read_version, watched_version, resume_command)


def _merge_install_config(
    config_flags: list[str] | None,
    config_file_path: Path | None,
) -> dict[str, str] | None:
    """Merge config from --config-file and --config flags.

    File values are loaded first, then individual flags override them.

    Args:
        config_flags: ``KEY=VALUE`` entries from repeated ``--config`` flags,
            or ``None``.
        config_file_path: Existing YAML file holding a mapping of strings,
            numbers and booleans, or ``None``.

    Returns:
        The merged config as strings, booleans written ``true`` or
        ``false``, or ``None`` if neither source gives any value.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the file cannot be read
            or parsed, is empty, is not a mapping, or holds a value of
            another type, or a flag is not ``KEY=VALUE`` with a non-empty key.
    """
    result: dict[str, str] = {}

    if config_file_path is not None:
        shown_path = escape(str(config_file_path))

        try:
            with config_file_path.open(encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except yaml.YAMLError as e:
            error_console.print(f"[red]Error:[/red] Failed to parse config file '{shown_path}': {escape(str(e))}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e
        except (OSError, UnicodeError) as e:
            error_console.print(f"[red]Error:[/red] Could not read config file '{shown_path}': {escape(str(e))}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e

        if data is None:
            error_console.print(f"[red]Error:[/red] Config file is empty: {shown_path}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE)

        if not isinstance(data, dict):
            error_console.print(f"[red]Error:[/red] Config file must contain a YAML mapping, got {type(data).__name__}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE)

        for key, value in data.items():
            if isinstance(value, bool):
                result[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                result[str(key)] = str(value)
            else:
                error_console.print(
                    f"[red]Error:[/red] Config key '{escape(str(key))}' has unsupported type {type(value).__name__}. "
                    "Only strings, numbers, and booleans are allowed."
                )
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    if config_flags is not None:
        for entry in config_flags:
            if "=" not in entry:
                error_console.print(f"[red]Error:[/red] Invalid config format '{escape(entry)}'. Expected KEY=VALUE.")
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

            key, _, value = entry.partition("=")

            if not key:
                error_console.print(f"[red]Error:[/red] Config key cannot be empty in '{escape(entry)}'.")
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

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
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the provider or version
            is not found.
        httpx.HTTPStatusError: If the read fails with any other status.
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
        report_not_found(e, f"{subject} not found in the store.")


@app.command()
def install(
    name: ProviderNameArgument,
    version: Annotated[str | None, typer.Option("--version", "-v", help="Version to install (default: latest)")] = None,
    upgrade_policy: Annotated[
        UpgradePolicy,
        typer.Option(
            "--upgrade-policy",
            help=(
                "auto: upgrade to each newer published version automatically; "
                "manual: stay on the installed version until you run 'pragma providers upgrade'"
            ),
        ),
    ] = UpgradePolicy.MANUAL,
    config: Annotated[
        list[str] | None,
        typer.Option("--config", "-c", help="Configuration key=value pair (repeatable)"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            help="Path to YAML file with configuration key-value pairs",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
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
        pragma providers install pragmatiks/redis --upgrade-policy auto
        pragma providers install pragmatiks/qdrant --config SOME_KEY=some_value
        pragma providers install pragmatiks/qdrant --config-file config.yaml --config OVERRIDE_KEY=value
        pragma providers install pragmatiks/qdrant -y
    """  # noqa: DOC501
    merged_config = _merge_install_config(config, config_file)

    client = get_client()
    require_auth(client)

    display, install_version = fetch_install_preview(client, name, version)

    provider_label = name if display == name else f"{display} ({name})"
    console.print(f"[bold]Provider:[/bold] {escape(provider_label)}")
    console.print(f"[bold]Version:[/bold]  {escape(install_version)}")

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

    result = _fetch_with_spinner(
        "Installing provider...",
        lambda: client.install_provider(
            name,
            version=version,
            upgrade_policy=upgrade_policy,
            config=merged_config,
        ),
    )

    console.print(f"[green]Installed:[/green] {escape(name)} v{escape(result.installed_version)}")


@app.command()
def uninstall(
    name: ProviderNameArgument,
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

    \f

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the provider still has
            resources and --cascade is not set, with ``NOT_FOUND_EXIT_CODE``
            if it is not installed, or with code 0 if the user cancels.
        httpx.HTTPStatusError: If the API refuses the uninstall for any
            other reason.
    """  # noqa: DOC502
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Provider:[/bold] {escape(name)}")

    if cascade:
        error_console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")

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
        if e.response.status_code == 409:
            error_console.print(f"[red]Error:[/red] Provider '{escape(name)}' has active resources.")
            error_console.print("[dim]Use --cascade to delete all resources with the provider.[/dim]")
            raise typer.Exit(FAILURE_EXIT_CODE) from e

        report_not_found(e, f"Provider '{name}' is not installed.")

    console.print(f"[green]Uninstalled:[/green] {escape(name)}")


@app.command()
def upgrade(
    name: ProviderNameArgument,
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
    require_auth(client)

    target = f"v{version}" if version else "latest"
    console.print(f"[bold]Upgrading:[/bold] {escape(name)} -> {escape(target)}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Upgrade {name} to {target}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    result = _fetch_with_spinner(
        "Upgrading provider...",
        lambda: client.upgrade_provider(name, target_version=version),
    )

    console.print(f"[green]Upgraded:[/green] {escape(name)} -> v{escape(result.installed_version)}")


@app.command()
def downgrade(
    name: ProviderNameArgument,
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

    \f

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` after printing the API's
            message and the version-chain hint on a 422, or with code 0 if
            the user cancels.
        httpx.HTTPStatusError: If the API refuses the downgrade with any
            other status.
    """
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Downgrading:[/bold] {escape(name)} -> v{escape(version)}")
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
        if e.response.status_code != 422:
            raise

        print_api_error(e)
        error_console.print("[dim]The version chain between current and target may be broken.[/dim]")
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    console.print(f"[green]Downgraded:[/green] {escape(name)} -> v{escape(result.installed_version)}")


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
    """
    client = get_client()

    if installed:
        _list_installations(client, output)
        return

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

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

    if output != OutputFormat.TABLE:
        output_data([_provider_summary_to_dict(p) for p in result.items], output)
        return

    if not result.items:
        if query:
            console.print(f"[dim]No providers found matching '{escape(query)}'.[/dim]")
        else:
            console.print("[dim]No providers found.[/dim]")
        return

    _print_store_list_table(result)


def _list_installations(client: PragmaClient, output: OutputFormat) -> None:
    """List provider installations for the current tenant.

    Args:
        client: SDK client instance.
        output: Output format for display; JSON and YAML print an empty
            list when nothing is installed.
    """
    require_auth(client)

    providers = _fetch_with_spinner(
        "Fetching installed providers...",
        lambda: client.list_installations(),
    )

    if output != OutputFormat.TABLE:
        output_data([_installed_provider_to_dict(p) for p in providers], output)
        return

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    _print_installed_table(providers)


@app.command()
def info(
    name: ProviderNameArgument,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Show detailed information about a provider.

    Displays provider metadata, install count, and version history.

    Examples:
        pragma providers info pragmatiks/qdrant
        pragma providers info pragmatiks/postgres -o json

    \f

    Raises:
        httpx.HTTPStatusError: If reading the provider's versions fails with
            any status but 404, which shows no versions.
    """
    client = get_client()

    try:
        provider = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Provider '{name}' not found in the store.")

    try:
        versions = _fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise

        versions = []

    if output != OutputFormat.TABLE:
        output_data(_provider_detail_to_dict(provider, versions), output)
        return

    _print_provider_info(provider, versions)


@app.command()
def versions(
    name: ProviderNameArgument,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List the versions of a provider with their status.

    A version still being published shows where it is, from "waiting for
    the wheel" to "publishing to the registry"; a failed version shows why
    it failed. Versions that are not published are visible only to the
    organization that published them.

    Examples:
        pragma providers versions acme/pipeline
        pragma providers versions acme/pipeline -o json
    """
    client = get_client()

    try:
        provider_versions = _fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Provider '{name}' not found in the store.")

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
    name: CompletedProviderNameArgument,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            "-v",
            help="Published version the API checks before restarting; it does not change the installed version",
        ),
    ] = None,
):
    """Restart an installed provider at its installed version.

    Restarts the provider on your organization's provider host, which
    reloads it at the version installed in your organization. --version
    must name a published version and never changes the installed
    version; use 'pragma providers upgrade' or 'pragma providers downgrade'
    to change it.

    Examples:
        pragma providers deploy pragmatiks/postgres
        pragma providers deploy pragmatiks/postgres --version 1.2.0
    """
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Restarting provider:[/bold] {escape(name)}")
    console.print()

    deploy_result = _fetch_with_spinner(
        "Restarting...",
        lambda: client.deploy_provider(name, version),
    )

    console.print(f"[green]Restart requested:[/green] {escape(name)}")
    console.print(f"[dim]Deployment:[/dim] {escape(deploy_result.deployment_name)}")
    console.print(f"[dim]Status:[/dim] {escape(deploy_result.status.value)}")

    if deploy_result.image:
        console.print(f"[dim]Image:[/dim] {escape(deploy_result.image)}")


@app.command()
def status(
    name: CompletedProviderNameArgument,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Check the deployment status of an installed provider.

    Shows the deployment name, status (pending, progressing, available or
    failed), ready and available replicas, and, when known, the deployed
    version, image, last update time and status message.

    Examples:
        pragma providers status pragmatiks/postgres
        pragma providers status pragmatiks/my-provider -o json
    """
    client = get_client()
    require_auth(client)

    result = _fetch_with_spinner(
        "Fetching deployment status...",
        lambda: client.get_deployment_status(name),
    )

    if output != OutputFormat.TABLE:
        output_data(result.model_dump(mode="json"), output)
        return

    _print_deployment_status(name, result)


@app.command()
def delete(
    name: CompletedProviderNameArgument,
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

    \f

    Raises:
        typer.Exit: If the user cancels.
    """
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Provider:[/bold] {escape(name)}")
    error_console.print("[yellow]Warning:[/yellow] This will permanently delete the provider from the catalog.")
    console.print()

    if not yes:
        confirm = typer.confirm("Are you sure you want to DELETE this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    _fetch_with_spinner(
        "Deleting provider...",
        lambda: client.delete_provider(name),
    )

    console.print(f"[green]✓[/green] Provider [bold]{escape(name)}[/bold] deleted successfully")


def _print_deployment_status(name: str, result: DeploymentResult) -> None:
    """Print deployment status in a formatted table.

    Args:
        name: Provider name (``org/name``).
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
    console.print(f"[bold]Provider:[/bold] {escape(name)}")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("Deployment", escape(result.deployment_name))
    table.add_row("Status", f"[{status_color}]{escape(result.status.value)}[/{status_color}]")
    table.add_row("Replicas", f"{result.ready_replicas}/{result.available_replicas}")

    if result.version:
        table.add_row("Version", escape(result.version))

    if result.image:
        table.add_row("Image", escape(result.image))

    if result.updated_at:
        table.add_row("Updated", result.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

    if result.message:
        table.add_row("Message", escape(result.message))

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

        table.add_row(
            escape(provider.canonical),
            format_optional_value(getattr(provider, "display_name", None)),
            format_optional_value(getattr(author, "display_name", None)),
            format_optional_value(getattr(provider, "latest_version", None)),
            str(install_count),
            format_optional_value(tags_display),
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
    tags = ", ".join(getattr(provider, "tags", []) or [])
    install_count = getattr(provider, "install_count", 0) or 0
    description = getattr(provider, "description", None)
    description_display = escape(description) if description else "[dim]No description[/dim]"
    created_at = getattr(provider, "created_at", None)
    updated_at = getattr(provider, "updated_at", None)

    info_lines = [
        f"[bold]Name:[/bold]         {escape(provider.canonical)}",
        f"[bold]Display Name:[/bold] {escape(getattr(provider, 'display_name', None) or provider.canonical)}",
        f"[bold]Author:[/bold]       {format_optional_value(getattr(author, 'display_name', None))}",
        f"[bold]Description:[/bold]  {description_display}",
        f"[bold]Tags:[/bold]         {format_optional_value(tags)}",
        f"[bold]Installs:[/bold]     {install_count}",
    ]

    if created_at:
        info_lines.append(f"[bold]Created:[/bold]      {str(created_at)[:19]}")

    if updated_at:
        info_lines.append(f"[bold]Updated:[/bold]      {str(updated_at)[:19]}")

    panel = Panel("\n".join(info_lines), title=escape(provider.canonical), border_style="blue")
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
        table.add_row(
            escape(provider_version.version),
            format_version_status(provider_version.status),
            published,
            format_version_message(provider_version),
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
            upgrade_display = f"[green]yes[/green] ({escape(latest)})"
        else:
            upgrade_display = "[dim]-[/dim]"

        table.add_row(
            escape(p.canonical),
            escape(p.installed_version),
            format_optional_value(getattr(p, "upgrade_policy", None)),
            escape(installed_at),
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
