"""Provider management commands.

Unified commands for publishing, installing, and managing Pragmatiks
providers.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
    PragmaClient,
    ProviderInstallation,
    ProviderInstallationEvent,
    ProviderVersion,
    ProviderVersionDeclaration,
    RenamedWheelWriteError,
    VersionStatus,
    WheelExpansionLimitError,
    WheelUploadAddress,
    WheelUploadError,
    WheelUploadOutcome,
    read_wheel_declaration,
    rename_provider_wheel,
)
from pydantic import ValidationError
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
    format_known_request_failure,
    format_os_error_reason,
    format_request_failure,
    is_request_unsent,
    print_api_error,
    print_unreadable_response,
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

DECLARATION_UNAVAILABLE_MESSAGE = "Pragmatiks cannot accept the publish right now. Try again later."
"""Message for a 503 refusing a declaration whose body carries no message of its own."""

UNUSABLE_WHEEL_ERRORS = (ValueError, zipfile.BadZipFile, WheelExpansionLimitError)
"""Errors ``rename_provider_wheel`` and ``read_wheel_declaration`` raise for a wheel that cannot be published.

The file is not a readable wheel, or it expands past the size limit Pragmatiks sets.
"""

UPLOAD_FAILURE_ERRORS = (WheelUploadError, httpx.RequestError, OSError)
"""Errors ``upload_provider_wheel`` raises when the wheel did not reach storage, or its outcome is unknown.

Storage refused the upload, got no readable answer to it, or the wheel could not be read.
"""

RENAMED_WHEEL_DIRECTORY_PREFIX = "pragma-publish-"
"""Prefix of the renamed wheel directory, the temporary directory the renamed copy of the wheel is written to.

Lets a leftover one be recognized.
"""

WHEEL_READ_FAILURE_HINT = "Check the disk holding the wheel, then publish again."
"""Next step printed when the built wheel fails to read with an operating system error."""

TEMPORARY_DIRECTORY_HINT = "Free space in the temporary directory or point TMPDIR elsewhere, then publish again."
"""Next step printed when the renamed copy of the wheel cannot be written."""

UPLOADED_WHEEL_LABEL = "wheel uploaded, waiting for Pragmatiks to record it"
"""Label of an ``awaiting_wheel`` version whose wheel this publish uploaded, until a read shows it ``staged``."""

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

AWAITING_WHEEL_HINT = (
    "If your publish stopped before its upload finished, run 'pragma providers publish' again. "
    "A finished upload shows as wheel received once Pragmatiks records it."
)
"""Message the versions table shows for an ``awaiting_wheel`` version, which has no message of its own."""

VERSION_STATUS_COLORS = {
    VersionStatus.PUBLISHED: "green",
    VersionStatus.FAILED: "red",
}
"""Rich color of each settled version status; every unsettled status shows yellow."""

INSTALLATION_STATE_COLORS = {
    "ready": "green",
    "failed": "red",
    "deleting": "dim",
}
"""Rich color of each installation state outside an operation; ``pending`` and ``processing`` show yellow."""

INSTALLATION_STATE_LABELS = {
    "pending": "Queued",
    "processing": "Starting",
    "ready": "Running",
    "failed": "Failed",
    "deleting": "Uninstalling",
    "uninstalled": "Uninstalled",
}
"""Label the tables show for each installation state, matching the web console; json and yaml keep the raw state."""

INSTALLATION_OPERATION_LABELS = {"start": "restart"}
"""Word the tables show for an operation whose type reads differently on the web console; other types show as is."""


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


def format_declaration_refusal(error: httpx.HTTPStatusError) -> str:
    """Format why the API refused or could not take a declaration.

    Args:
        error: The HTTP status error the declaration raised.

    Returns:
        ``DECLARATION_UNAVAILABLE_MESSAGE`` for a 503 whose body carries no
        message, or else ``format_api_error``'s text, escaped for Rich
        markup.
    """
    if error.response.status_code == 503 and parse_api_error_message(error.response) is None:
        return DECLARATION_UNAVAILABLE_MESSAGE

    return escape(format_api_error(error))


def format_declaration_interruption(error: httpx.TransportError) -> str:
    """Format why a declaration broke off after the connection to the API was made.

    Args:
        error: The transport error the declaration raised.

    Returns:
        A message naming what interrupted the publish, escaped for Rich
        markup.
    """
    return f"Could not finish the publish: {escape(format_request_failure(error))}."


def format_storage_request_failure(error: httpx.RequestError) -> str:
    """Format what kept a wheel upload from getting a readable answer from storage, in plain words.

    Never includes the error's own text, so no part of the signed upload
    address can reach the terminal.

    Args:
        error: The request error the upload raised.

    Returns:
        :func:`~pragma_cli.errors.format_known_request_failure`'s phrase for
        ``storage``, such as ``storage did not answer in time``,
        ``storage's answer could not be read`` for an answer that could not
        be decoded, or ``the connection to storage broke off while sending``
        for a connection that broke while the wheel was being sent. A cause
        without a phrase of its own gives ``the connection to storage failed
        (<ErrorType>)``.
    """
    if isinstance(error, httpx.DecodingError):
        return "storage's answer could not be read"

    if isinstance(error, httpx.WriteError):
        return "the connection to storage broke off while sending"

    if (phrase := format_known_request_failure(error, "storage")) is not None:
        return phrase

    return f"the connection to storage failed ({type(error).__name__})"


def format_upload_failure(error: WheelUploadError | httpx.RequestError | OSError) -> str:
    """Format why a wheel upload did not store the wheel, or why its outcome is unknown.

    Never includes the signed upload address.

    Args:
        error: One of ``UPLOAD_FAILURE_ERRORS``.

    Returns:
        Storage's refusal as the SDK words it, an expired address and
        storage's request id included; that the wheel could not be read for
        an OS error; that the outcome is unknown when storage may have
        received the wheel but no answer arrived (no answer in time, the
        connection broke off or closed before an answer, or an undecodable
        answer); else that the wheel could not be uploaded, with the
        cause. Not escaped for Rich markup.
    """
    match error:
        case WheelUploadError():
            return str(error)
        case OSError():
            return f"Could not read the wheel to upload it: {format_os_error_reason(error)}."
        case (
            httpx.ReadTimeout()
            | httpx.ReadError()
            | httpx.CloseError()
            | httpx.RemoteProtocolError()
            | httpx.DecodingError()
        ):
            return f"The upload's outcome is unknown ({format_storage_request_failure(error)})."
        case _:
            return f"Could not upload the wheel: {format_storage_request_failure(error)}."


def format_version_status(status: VersionStatus, label: str) -> str:
    """Format a version status's label for the terminal, colored by the status.

    Args:
        status: The version's status.
        label: The status's plain-words label, not escaped for Rich markup.

    Returns:
        The label in Rich markup: green when published, red when failed,
        yellow while unsettled.
    """
    color = VERSION_STATUS_COLORS.get(status, "yellow")
    return f"[{color}]{escape(label)}[/{color}]"


def format_watched_status_label(declared_version: DeclaredProviderVersion, status: VersionStatus) -> str:
    """Format the plain-words label of a status the publish watch shows.

    Args:
        declared_version: The version as this publish declared it; the
            watch starts only after the upload its ``wheel_upload`` asked
            for left the wheel in storage.
        status: The watched version's status.

    Returns:
        ``UPLOADED_WHEEL_LABEL`` for ``awaiting_wheel`` when the declaration
        returned an upload address, so this publish uploaded the wheel,
        else the label from ``VERSION_STATUS_LABELS``. Not escaped for Rich
        markup.
    """
    if declared_version.wheel_upload is not None and status == VersionStatus.AWAITING_WHEEL:
        return UPLOADED_WHEEL_LABEL

    return VERSION_STATUS_LABELS[status]


def format_spinner_description(label: str) -> str:
    """Format a version status label as the description beside the publish watch's spinner.

    Args:
        label: The status's plain-words label, not escaped for Rich markup.

    Returns:
        The capitalized label followed by an ellipsis, escaped for Rich
        markup.
    """
    capitalized_label = label[:1].upper() + label[1:]
    return f"{escape(capitalized_label)}..."


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


def report_unusable_wheel(error: Exception) -> NoReturn:
    """Print why a wheel cannot be published, with what to do about it, and exit.

    A wheel expanding past the size limit prints the SDK's message; any
    other error prints its message followed by a request to rebuild the
    wheel.

    Args:
        error: One of ``UNUSABLE_WHEEL_ERRORS``.

    Raises:
        typer.Exit: Always, with ``INPUT_ERROR_EXIT_CODE``.
    """
    if isinstance(error, WheelExpansionLimitError):
        message = str(error)
    else:
        message = f"{str(error).rstrip('.')}. Rebuild the wheel and publish it again."

    error_console.print(f"[red]Error:[/red] {escape(message)}")
    raise typer.Exit(INPUT_ERROR_EXIT_CODE) from error


def report_wheel_error(error: Exception, report_read_failure: Callable[[str, Exception], NoReturn]) -> NoReturn:
    """Print why a wheel could not be renamed or read, and exit.

    Args:
        error: One of ``UNUSABLE_WHEEL_ERRORS``.
        report_read_failure: Reports the operating system error the SDK
            chained as ``error``'s ``__cause__``, such as
            :func:`report_wheel_read_failure`; any other error is reported by
            :func:`report_unusable_wheel`.

    Raises:
        typer.Exit: Always, with the exit code of the reporter it calls.
    """  # noqa: DOC502
    if isinstance(error.__cause__, OSError):
        report_read_failure(format_os_error_reason(error.__cause__), error)

    report_unusable_wheel(error)


def report_wheel_read_failure(reason: str, error: Exception) -> NoReturn:
    """Print that the built wheel could not be read from disk, with what to do about it, and exit.

    Args:
        reason: What the operating system reported, without the path it
            names, such as ``Input/output error``.
        error: The error reading the wheel raised.

    Raises:
        typer.Exit: Always, with ``INPUT_ERROR_EXIT_CODE``.
    """
    error_console.print(f"[red]Error:[/red] Could not read the wheel: {escape(reason)}. {WHEEL_READ_FAILURE_HINT}")
    raise typer.Exit(INPUT_ERROR_EXIT_CODE) from error


def report_renamed_copy_failure(reason: str, error: Exception) -> NoReturn:
    """Print that the renamed copy of the wheel could not be written, with what to do about it, and exit.

    Args:
        reason: What the operating system reported, without the path it
            names, such as ``No space left on device``.
        error: The error writing the copy, creating the renamed wheel
            directory, or reading the copy back, raised.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    error_console.print(
        f"[red]Error:[/red] Could not write the renamed copy of the wheel: {escape(reason)}. {TEMPORARY_DIRECTORY_HINT}"
    )
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def remove_renamed_wheel_directory(directory: Path) -> None:
    """Remove the renamed wheel directory.

    A directory that cannot be removed prints a warning on stderr naming it,
    and is left for the user to delete.

    Args:
        directory: Path :func:`renamed_wheel_directory` yielded.
    """
    try:
        shutil.rmtree(directory)
    except OSError:
        error_console.print(
            f"[yellow]Warning:[/yellow] Could not remove the temporary directory {escape(str(directory))}; "
            "delete it yourself.",
            soft_wrap=True,
        )


@contextmanager
def renamed_wheel_directory() -> Iterator[Path]:
    """Scope the renamed wheel directory, the temporary directory the renamed copy of the wheel is written to.

    The directory is made on entry in the system's temporary directory,
    which ``TMPDIR`` names, under ``RENAMED_WHEEL_DIRECTORY_PREFIX``, before
    the CLI knows whether the wheel needs a renamed copy, so a temporary
    directory that cannot be written fails a wheel needing none too. It is
    removed on exit; a directory that cannot be removed never fails a
    publish whose declaration and upload succeeded (see
    :func:`remove_renamed_wheel_directory`).

    Yields:
        Path to the directory.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the directory cannot be
            created (see :func:`report_renamed_copy_failure`).
    """  # noqa: DOC502
    try:
        directory = Path(tempfile.mkdtemp(prefix=RENAMED_WHEEL_DIRECTORY_PREFIX))
    except OSError as e:
        report_renamed_copy_failure(format_os_error_reason(e), e)

    try:
        yield directory
    finally:
        remove_renamed_wheel_directory(directory)


def rename_wheel(wheel_path: Path, organization_slug: str, destination_directory: Path) -> Path:
    """Rename a built wheel to the organization's distribution name, behind a spinner on stderr.

    Args:
        wheel_path: Path to the built ``.whl``, left unchanged.
        organization_slug: Slug of the publishing organization.
        destination_directory: The renamed wheel directory the renamed copy
            is written to.

    Returns:
        Path to the renamed copy, or ``wheel_path`` when no copy was written
        because the wheel already carries the distribution name or names no
        single provider.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if reading the open wheel
            fails with an operating system error, which
            ``rename_provider_wheel`` raises as the ``__cause__`` of a
            :class:`zipfile.BadZipFile` (see
            :func:`report_wheel_read_failure`), or if the wheel cannot be
            published for any other reason (see ``UNUSABLE_WHEEL_ERRORS``);
            or with ``FAILURE_EXIT_CODE`` if the renamed copy cannot be
            written.
        OSError: If ``wheel_path`` cannot be opened.
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Renaming {wheel_path.name}...",
            lambda: rename_provider_wheel(wheel_path, organization_slug, destination_directory),
        )
    except UNUSABLE_WHEEL_ERRORS as e:
        report_wheel_error(e, report_wheel_read_failure)
    except RenamedWheelWriteError as e:
        report_renamed_copy_failure(e.reason, e)


def print_renamed_wheel(wheel_path: Path, renamed_wheel_path: Path) -> None:
    """Print the renamed copy's filename on stderr when renaming changed it.

    A copy keeps the built wheel's filename when renaming changed only its
    ``.dist-info`` directory or ``METADATA``; nothing is printed for it.

    Args:
        wheel_path: Path to the built ``.whl``.
        renamed_wheel_path: Path to the renamed copy :func:`rename_wheel` wrote.
    """
    if renamed_wheel_path.name != wheel_path.name:
        error_console.print(f"Renamed to {escape(renamed_wheel_path.name)}")


def read_declaration(
    wheel_path: Path,
    changelog: str | None,
    report_read_failure: Callable[[str, Exception], NoReturn],
) -> ProviderVersionDeclaration:
    """Read what a wheel declares about itself, behind a spinner on stderr.

    Args:
        wheel_path: Path to the ``.whl`` to declare: the built wheel, or the
            renamed copy :func:`rename_wheel` wrote.
        changelog: Release notes for the version, or ``None``.
        report_read_failure: Reports an operating system error opening or
            reading ``wheel_path``: :func:`report_wheel_read_failure` for the
            built wheel, :func:`report_renamed_copy_failure` for the renamed
            copy.

    Returns:
        The wheel's declaration.

    Raises:
        typer.Exit: With the exit code of ``report_read_failure`` if opening
            or reading the wheel fails with an operating system error, which
            ``read_wheel_declaration`` raises as is or as the ``__cause__``
            of a :class:`zipfile.BadZipFile`; or with
            ``INPUT_ERROR_EXIT_CODE`` if the wheel cannot be published for
            any other reason (see :func:`report_unusable_wheel`).
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Reading {wheel_path.name}...",
            lambda: read_wheel_declaration(wheel_path, changelog=changelog),
        )
    except OSError as e:
        report_read_failure(format_os_error_reason(e), e)
    except UNUSABLE_WHEEL_ERRORS as e:
        report_wheel_error(e, report_read_failure)


def print_publish_again_hint(republish_command: str) -> None:
    """Print how to publish the same wheel again after a publish stopped before its watch.

    Args:
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.
    """
    error_console.print(f"To publish it again, run: {escape(republish_command)}", soft_wrap=True)


def report_declaration_failure(error: httpx.TransportError | httpx.HTTPStatusError, republish_command: str) -> NoReturn:
    """Print why a declaration failed, and exit.

    Args:
        error: The error the declaration raised.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the request broke off after
            the API received some of it, which prints what broke it off and
            the command that publishes the wheel again, or if the API
            refused it or could not take it, which prints the API's reason
            or the set-up message for an organization that is not ready,
            and, for any other ``5xx``, a proxy's included, the command that
            publishes the wheel again.
        httpx.TransportError: If the API received none of the request, as
            ``errors.is_request_unsent`` tells: the request is invalid, or
            connecting failed or timed out.
    """  # noqa: DOC502
    if isinstance(error, httpx.HTTPStatusError):
        check_bootstrap_error(error)
        error_console.print(f"[red]Error:[/red] {format_declaration_refusal(error)}")

        if error.response.is_server_error:
            print_publish_again_hint(republish_command)

        raise typer.Exit(FAILURE_EXIT_CODE) from error

    if is_request_unsent(error):
        raise error

    error_console.print(f"[red]Error:[/red] {format_declaration_interruption(error)}")
    print_publish_again_hint(republish_command)
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def report_unreadable_declaration_response(
    error: ValidationError | json.JSONDecodeError | httpx.DecodingError | UnicodeDecodeError,
    republish_command: str,
) -> NoReturn:
    """Print that the API answered the declaration with data the CLI cannot read, and exit.

    Prints which fields are invalid and why, never their values, so no part
    of a signed upload address reaches the terminal, or why the body could
    not be decoded. Ends with the command that publishes the wheel again.

    Args:
        error: The error reading the declaration's answer raised: an invalid
            model, a body that is not JSON, or a body that could not be
            decoded, its JSON text not valid UTF-8 included.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    print_unreadable_response(error)
    print_publish_again_hint(republish_command)
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def declare_version(
    client: PragmaClient,
    declaration: ProviderVersionDeclaration,
    republish_command: str,
) -> DeclaredProviderVersion:
    """Declare the version a wheel describes.

    Args:
        client: Authenticated SDK client.
        declaration: The wheel's declaration.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Returns:
        The declared version: ``awaiting_wheel`` for a new declaration, or
        the version in flight, in whatever status it is in, for a
        declaration identical to it.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the API refuses the
            declaration, a 404 included, cannot take it, or the request
            breaks off (see :func:`report_declaration_failure`), or if its
            answer cannot be read (see
            :func:`report_unreadable_declaration_response`).
        httpx.TransportError: If the API received none of the request.
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Declaring {declaration.wheel_filename}...",
            lambda: client.declare_provider_version(declaration),
        )
    except (httpx.TransportError, httpx.HTTPStatusError) as e:
        report_declaration_failure(e, republish_command)
    except (ValidationError, json.JSONDecodeError, httpx.DecodingError, UnicodeDecodeError) as e:
        report_unreadable_declaration_response(e, republish_command)


def print_declared_version(declared_version: DeclaredProviderVersion) -> None:
    """Print the declared version and its operation id on stderr.

    Args:
        declared_version: The version as this publish declared it.
    """
    error_console.print(
        f"[bold]Publishing[/bold] {escape(declared_version.canonical)} {escape(declared_version.version)}"
    )
    error_console.print(f"[dim]Operation {escape(declared_version.operation_id)}[/dim]")


def print_upload_outcome(outcome: WheelUploadOutcome, wheel_size: int) -> None:
    """Print on stderr how this publish's upload left the wheel in storage.

    Args:
        outcome: What the upload returned.
        wheel_size: Size of the wheel in bytes.
    """
    match outcome:
        case WheelUploadOutcome.ALREADY_STORED:
            error_console.print("Wheel already uploaded")
        case WheelUploadOutcome.STORED:
            error_console.print(f"Uploaded {format_wheel_size(wheel_size)}")


def report_upload_failure(error: WheelUploadError | httpx.RequestError | OSError, republish_command: str) -> NoReturn:
    """Print why a wheel upload failed or why its outcome is unknown, and exit.

    Ends with the command that publishes the wheel again.

    Args:
        error: One of ``UPLOAD_FAILURE_ERRORS``.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    error_console.print(f"[red]Error:[/red] {escape(format_upload_failure(error))}")
    print_publish_again_hint(republish_command)
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def upload_declared_wheel(
    client: PragmaClient,
    wheel_upload: WheelUploadAddress,
    wheel_path: Path,
    republish_command: str,
) -> WheelUploadOutcome:
    """Upload the wheel of a declared version to the address its declaration returned.

    Args:
        client: Authenticated SDK client.
        wheel_upload: The declared version's ``wheel_upload``.
        wheel_path: Path to the declared, renamed ``.whl``.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Returns:
        Whether this upload stored the wheel or storage already held it.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if storage refused the
            upload, its address expired included, the upload got no
            readable answer from storage, or the wheel could not be read
            (see :func:`report_upload_failure`).
    """  # noqa: DOC502
    try:
        return _fetch_with_spinner(
            f"Uploading {wheel_path.name}...",
            lambda: client.upload_provider_wheel(wheel_upload, wheel_path),
        )
    except UPLOAD_FAILURE_ERRORS as e:
        report_upload_failure(e, republish_command)


def publish_wheel(
    client: PragmaClient,
    wheel_path: Path,
    changelog: str | None,
    republish_command: str,
) -> DeclaredProviderVersion:
    """Declare a built wheel under the caller's organization's distribution name, and upload it when asked.

    Reads the caller's organization first, and uploads the wheel only when
    the declaration returns an upload address; without one, prints that the
    wheel was already received and a ``Status:`` line for the declared
    status. Prints each step on stderr.

    Args:
        client: Authenticated SDK client.
        wheel_path: Path to the built ``.whl``, left unchanged.
        changelog: Release notes for the version, or ``None``.
        republish_command: Command that publishes the same wheel again, from ``build_republish_command``.

    Returns:
        The version as this publish declared it; its ``wheel_upload`` is set
        when this publish uploaded the wheel.

    Raises:
        typer.Exit: If the renamed copy cannot be written or read back, the
            wheel cannot be published, or the declaration or the upload
            fails (see :func:`renamed_wheel_directory`,
            :func:`rename_wheel`, :func:`read_declaration`,
            :func:`declare_version` and :func:`upload_declared_wheel`).
        httpx.HTTPStatusError: If the API refuses to name the caller's
            organization.
        httpx.TransportError: If reading the caller's organization fails in
            transit, or the API received none of the declaration.
        OSError: If the wheel cannot be opened.
    """  # noqa: DOC502
    organization = _fetch_with_spinner("Reading your organization...", client.get_current_organization)

    with renamed_wheel_directory() as directory:
        declared_wheel_path = rename_wheel(wheel_path, organization.slug, directory)

        if declared_wheel_path == wheel_path:
            declaration = read_declaration(wheel_path, changelog, report_wheel_read_failure)
        else:
            print_renamed_wheel(wheel_path, declared_wheel_path)
            declaration = read_declaration(declared_wheel_path, changelog, report_renamed_copy_failure)

        declared_version = declare_version(client, declaration, republish_command)
        print_declared_version(declared_version)

        if declared_version.wheel_upload is None:
            error_console.print("Wheel already received")
            print_version_status(declared_version.status, VERSION_STATUS_LABELS[declared_version.status])
        else:
            outcome = upload_declared_wheel(
                client, declared_version.wheel_upload, declared_wheel_path, republish_command
            )
            print_upload_outcome(outcome, declaration.wheel_size)

    return declared_version


def is_replaced_publish(version: ProviderVersion, declared_version: DeclaredProviderVersion) -> bool:
    """Tell whether another publish of the same version replaced the one being watched.

    A replacing publish declares the version under its own operation, so the
    version's ``operation_id`` no longer matches the one it carried when the
    watch started.

    Args:
        version: The version as last read.
        declared_version: The version the watch started from, as this
            publish declared it.

    Returns:
        ``True`` if the two versions' ``operation_id`` differ.
    """
    return version.operation_id != declared_version.operation_id


def compute_watch_end(version: ProviderVersion, watch_started_at: float) -> float:
    """Compute when the publish watch stops waiting for a version, on the monotonic clock.

    Args:
        version: The version as last read.
        watch_started_at: ``time.monotonic()`` reading taken when the upload
            finished, or the declaration when no upload was needed.

    Returns:
        ``watch_started_at`` plus ``ADMISSION_MINIMUM_WATCH_SECONDS``, or,
        once the provider host has taken the version, its
        ``operation_deadline_at`` plus ``ADMISSION_DEADLINE_MARGIN_SECONDS``
        when that is later. A deadline without a timezone is read as UTC.
    """
    minimum_end = watch_started_at + ADMISSION_MINIMUM_WATCH_SECONDS
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
    declared_version: DeclaredProviderVersion,
    watch_started_at: float,
    show_status: Callable[[VersionStatus], object],
) -> ProviderVersion:
    """Poll a version until it settles, another publish replaces it, or the watch bound passes.

    Reads the version every ``ADMISSION_POLL_INTERVAL_SECONDS``. The bound
    follows the version's own admission deadline once the provider host has
    taken it (see :func:`compute_watch_end`), so the watch outlasts any
    deadline the API sets.

    Args:
        client: Authenticated SDK client.
        declared_version: The version as this publish declared it, which
            :func:`is_replaced_publish` compares each read against.
        watch_started_at: ``time.monotonic()`` reading taken when the upload
            finished, or the declaration when no upload was needed.
        show_status: Called with the version's status each time a read
            shows a status different from the previous read, the first read
            being compared with ``declared_version``; never called for
            ``declared_version`` itself, nor for a read showing another
            publish replaced this one.

    Returns:
        The version as last read: ``published``, ``failed``, replaced, or
        still unsettled when the watch bound passed first.

    Raises:
        httpx.HTTPStatusError: If reading the version fails.
        httpx.TransportError: If the API cannot be reached.
    """  # noqa: DOC502
    version = declared_version

    while version.status not in SETTLED_VERSION_STATUSES:
        remaining_seconds = compute_watch_end(version, watch_started_at) - time.monotonic()

        if remaining_seconds <= 0:
            break

        time.sleep(min(ADMISSION_POLL_INTERVAL_SECONDS, remaining_seconds))
        previous_status = version.status
        version = client.get_provider_version(version.canonical, version.version)

        if is_replaced_publish(version, declared_version):
            break

        if version.status != previous_status:
            show_status(version.status)

    return version


def print_version_status(status: VersionStatus, label: str) -> None:
    """Print a version status as a ``Status:`` line on stderr.

    Args:
        status: The version's status.
        label: The status's plain-words label, not escaped for Rich markup.
    """
    error_console.print(f"Status: {format_version_status(status, label)}")


def show_status_change(
    progress: Progress,
    task: TaskID,
    declared_version: DeclaredProviderVersion,
    status: VersionStatus,
) -> None:
    """Show a status the publish watch reached: a ``Status:`` line on stderr and its label beside the spinner.

    Args:
        progress: The spinner the publish watch runs behind.
        task: The spinner's task.
        declared_version: The version as this publish declared it.
        status: The version's new status.
    """
    label = format_watched_status_label(declared_version, status)
    print_version_status(status, label)
    progress.update(task, description=format_spinner_description(label))


def watch_admission_with_progress(
    client: PragmaClient,
    declared_version: DeclaredProviderVersion,
    watch_started_at: float,
) -> ProviderVersion:
    """Run :func:`watch_admission` behind a spinner on stderr, printing each status the version reaches.

    The spinner starts at the declared status's label from
    :func:`format_watched_status_label`.

    Args:
        client: Authenticated SDK client.
        declared_version: The version as this publish declared it.
        watch_started_at: ``time.monotonic()`` reading taken when the upload
            finished, or the declaration when no upload was needed.

    Returns:
        The version as :func:`watch_admission` last read it.

    Raises:
        httpx.HTTPStatusError: If reading the version fails.
        httpx.TransportError: If the API cannot be reached.
    """  # noqa: DOC502
    starting_label = format_watched_status_label(declared_version, declared_version.status)

    with build_spinner_progress() as progress:
        task = progress.add_task(format_spinner_description(starting_label), total=None)
        show_status = partial(show_status_change, progress, task, declared_version)
        return watch_admission(client, declared_version, watch_started_at, show_status)


def build_version_record(version: ProviderVersion) -> dict[str, Any]:
    """Build the record ``pragma providers versions -o json|yaml`` prints for one version.

    Args:
        version: Any provider version, a :class:`DeclaredProviderVersion`
            included.

    Returns:
        The version's :class:`ProviderVersion` fields in JSON mode, never
        ``wheel_upload``, so the signed upload address is never printed.
    """
    listed_fields = set(ProviderVersion.model_fields) | set(ProviderVersion.model_computed_fields)
    return version.model_dump(mode="json", include=listed_fields)


def build_publish_document(version: ProviderVersion, declared_version: DeclaredProviderVersion) -> dict[str, Any]:
    """Build the document ``pragma providers publish -o json|yaml`` prints once the watch ends.

    Args:
        version: The version to report, any :class:`ProviderVersion`, a
            :class:`DeclaredProviderVersion` included; its ``wheel_upload``
            is dropped.
        declared_version: The version as this publish declared it.

    Returns:
        A dict with ``operation_id``, this publish's own operation id, and
        ``version``, the record :func:`build_version_record` builds, as
        ``pragma providers versions -o json`` prints it. Another publish
        replaced this one when ``version.operation_id`` differs from
        ``operation_id``.
    """
    return {
        "operation_id": declared_version.operation_id,
        "version": build_version_record(version),
    }


def format_published_version(version: ProviderVersion) -> str:
    """Format the ``Published`` line for a version admission published.

    Args:
        version: The published version.

    Returns:
        The line in Rich markup, its values escaped.
    """
    resource_types = format_resource_type_count(len(version.schemas or []))
    return (
        f"[green]Published[/green] {escape(version.canonical)} {escape(version.version)} — "
        f"Python {escape(str(version.python_version))}, SDK {escape(str(version.sdk_version))}, "
        f"{resource_types}"
    )


def report_unpublished_version(
    version: ProviderVersion,
    declared_version: DeclaredProviderVersion,
    republish_command: str,
) -> None:
    """Print why admission did not publish the version, and exit; return when it published.

    Args:
        version: The version as the watch last read it.
        declared_version: The version as this publish declared it.
        republish_command: Command that keeps waiting for the version, from ``build_republish_command``.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if another publish replaced
            this one or the version failed admission, or with
            ``ADMISSION_UNCONFIRMED_EXIT_CODE`` if it has not settled.
    """
    if is_replaced_publish(version, declared_version):
        report_replaced_publish(version)

    match version.status:
        case VersionStatus.PUBLISHED:
            return
        case VersionStatus.FAILED:
            error_console.print(f"[red]Error:[/red] {escape(version.error_message or '')}")
            raise typer.Exit(FAILURE_EXIT_CODE)
        case _:
            status_label = format_watched_status_label(declared_version, version.status)
            report_unfinished_admission(version, status_label, republish_command)


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


def format_watch_failure_reason(error: Exception) -> str:
    """Format why the publish watch stopped reading a version.

    Args:
        error: What reading the version raised.

    Returns:
        A one-line reason the watch stopped, not escaped for Rich markup.
    """
    if isinstance(error, httpx.HTTPStatusError):
        return format_api_error(error)

    if isinstance(error, httpx.TransportError):
        return format_request_failure(error)

    return str(error) or type(error).__name__


def report_watch_failure(reason: str, version: ProviderVersion, republish_command: str) -> NoReturn:
    """Print why the publish watch stopped reading a version, with how to follow the version, then exit.

    Args:
        reason: Why the watch stopped, not escaped for Rich markup.
        version: The version being watched.
        republish_command: Command that keeps waiting for the version, from ``build_republish_command``.

    Raises:
        typer.Exit: Always, with ``ADMISSION_UNCONFIRMED_EXIT_CODE``.
    """
    canonical = escape(version.canonical)
    error_console.print(f"[red]Error:[/red] Stopped waiting for admission: {escape(reason.rstrip('.'))}.")
    error_console.print(
        f"Check admission of {canonical} {escape(version.version)} with: pragma providers versions {canonical}"
    )
    print_keep_waiting_hint(republish_command)
    raise typer.Exit(ADMISSION_UNCONFIRMED_EXIT_CODE)


def report_unfinished_admission(version: ProviderVersion, status_label: str, republish_command: str) -> NoReturn:
    """Print that a version has not settled, what it waits on, and how to follow it, then exit.

    Args:
        version: The version as the watch last read it, still unsettled.
        status_label: Plain-words label of the version's status, from
            :func:`format_watched_status_label`, not escaped for Rich
            markup.
        republish_command: Command that keeps waiting for the version, from ``build_republish_command``.

    Raises:
        typer.Exit: Always, with ``ADMISSION_UNCONFIRMED_EXIT_CODE``.
    """
    canonical = escape(version.canonical)
    error_console.print(
        f"[yellow]Admission of {canonical} {escape(version.version)} has not finished: "
        f"{escape(status_label)}. "
        f"Check it with: pragma providers versions {canonical}[/yellow]"
    )
    print_keep_waiting_hint(republish_command)
    raise typer.Exit(ADMISSION_UNCONFIRMED_EXIT_CODE)


def print_keep_waiting_hint(republish_command: str) -> None:
    """Print how to keep waiting for admission of the version just published.

    Args:
        republish_command: Command that keeps waiting for the version, from ``build_republish_command``.
    """
    error_console.print(f"To keep waiting, run: {escape(republish_command)}", soft_wrap=True)


def build_republish_command(wheel_path: Path, changelog: Path | None) -> str:
    """Build the publish command that sends the same declaration again.

    The same declaration makes publish watch the version in flight instead
    of replacing it: the wheel and the changelog are both part of it, so a
    rebuilt wheel or a missing changelog would replace the version. The
    renamed copy is the same only when renamed by the same Python and zlib,
    so the command is meant for the environment that ran this publish.

    Args:
        wheel_path: The built wheel this publish read, not its renamed copy.
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
            help="Prebuilt .whl to publish instead of building one; not combined with PROJECT_DIRECTORY",
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
    output: Annotated[
        OutputFormat,
        typer.Option(
            "--output",
            "-o",
            help="Output format; json and yaml print the operation id and version record on stdout",
        ),
    ] = OutputFormat.TABLE,
):
    """Publish a new provider version and wait for its admission.

    Builds the wheel with 'uv build --wheel' into PROJECT_DIRECTORY/dist/
    (the current directory by default), or takes a prebuilt one via
    '--wheel', renames a copy under your organization's distribution name
    (the original file is left unchanged), declares the version it
    describes, and uploads the copy unless Pragmatiks already received it.
    The wheel alone identifies the version: its 'pragma.provider' entry
    point names the provider, its metadata carries the version, and the
    publishing organization comes from the authenticated user. Your
    organization's provider host then admits the version; the command waits
    for that and exits non-zero when admission fails, another publish of
    the version replaces this one, or the version has not settled shortly
    after its admission deadline. A failed version can be published again.

    Exit code 3: the wheel was uploaded, or was already received, but
    admission is not confirmed: the watch ended with the version still
    unsettled, or could not read the version. Publishing the same wheel
    again with '--wheel', and the same '--changelog' if one was given, from
    the same environment, keeps waiting for it.

    With '-o json' or '-o yaml', once the watch ends, stdout carries one
    document and nothing else: 'operation_id', this publish's own
    operation id, and 'version', the version as last read, shaped like a
    'pragma providers versions -o json' record (the declared version when
    a read fails or none was read). It is printed on exit 0 (published),
    exit 1 when admission failed ('version.error_message' says why) or
    another publish replaced this one ('version.operation_id' differs from
    'operation_id'), and exit 3, where 'version.status' may be the
    declared status. Every other exit leaves stdout empty. With json and
    yaml, all progress, errors and the 'Published' line go to stderr; in
    table format the 'Published' line stays on stdout.

    Examples:
        pragma providers publish
        pragma providers publish ./my-provider --changelog NOTES.md
        pragma providers publish --wheel dist/my_provider-1.0.0-py3-none-any.whl
        pragma providers publish --wheel dist/my_provider-1.0.0-py3-none-any.whl -o json

    \f

    Exits through ``typer.Exit`` with ``FAILURE_EXIT_CODE`` when admission
    failed or another publish replaced this one, and with
    ``ADMISSION_UNCONFIRMED_EXIT_CODE`` when admission is not confirmed.

    Raises:
        click.UsageError: If both PROJECT_DIRECTORY and --wheel are given.
    """
    if wheel is not None and project_directory is not None:
        raise click.UsageError("Pass either PROJECT_DIRECTORY or --wheel, not both.")

    changelog_text = _read_changelog(changelog)

    client = get_client()
    require_auth(client)

    wheel_path = wheel or run_wheel_build(project_directory or Path("."))
    republish_command = build_republish_command(wheel_path, changelog)

    declared_version = publish_wheel(client, wheel_path, changelog_text, republish_command)
    watch_started_at = time.monotonic()

    try:
        last_read_version = watch_admission_with_progress(client, declared_version, watch_started_at)
    except Exception as e:
        reason = format_watch_failure_reason(e)
        try:
            output_data(build_publish_document(declared_version, declared_version), output)
        finally:
            report_watch_failure(reason, declared_version, republish_command)

    output_data(build_publish_document(last_read_version, declared_version), output)

    report_unpublished_version(last_read_version, declared_version, republish_command)

    published_console = console if output == OutputFormat.TABLE else error_console
    published_console.print(format_published_version(last_read_version))


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
            config=merged_config,
        ),
    )

    console.print(f"[green]Installed:[/green] {escape(name)} {format_target_version(result.operation_version)}")
    print_history_pointer(name)


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

    console.print(f"[green]Upgraded:[/green] {escape(name)} -> {format_target_version(result.operation_version)}")
    print_history_pointer(name)


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

    console.print(f"[green]Downgraded:[/green] {escape(name)} -> {format_target_version(result.operation_version)}")
    print_history_pointer(name)


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
        output_data([build_installation_record(installation) for installation in providers], output)
        return

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    print_installed_table(providers)


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
        data = [build_version_record(provider_version) for provider_version in provider_versions]
        output_data(data, output)
        return

    if not provider_versions:
        console.print("[dim]No versions found.[/dim]")
        return

    print_versions_table(provider_versions)


@app.command()
def history(
    name: CompletedProviderNameArgument,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show the history of an installed provider, newest first.

    Lists each transition of the installation: when it happened, the
    states it moved between, the operation and version behind it, its
    message and, for a failure, its cause. The history is kept for 30 days,
    also after the provider is uninstalled.

    Examples:
        pragma providers history acme/pipeline
        pragma providers history acme/pipeline -o json
    """
    client = get_client()
    require_auth(client)

    events = _fetch_with_spinner(
        "Fetching history...",
        lambda: client.list_provider_history(name),
    )

    if output != OutputFormat.TABLE:
        output_data([event.model_dump(mode="json") for event in events], output)
        return

    if not events:
        console.print("[dim]No history found.[/dim]")
        return

    print_history_table(events)


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
        status_label = VERSION_STATUS_LABELS[provider_version.status]

        table.add_row(
            escape(provider_version.version),
            format_version_status(provider_version.status, status_label),
            published,
            format_version_message(provider_version),
        )

    console.print(table)


def format_target_version(version: str | None) -> str:
    """Format the version an operation targets, such as ``v3.0.0``.

    Args:
        version: The target version, or ``None`` when the API sent none.

    Returns:
        The version prefixed with ``v`` with Rich markup escaped, or a dim dash.
    """
    if not version:
        return "[dim]-[/dim]"

    return escape(f"v{version}")


def print_history_pointer(name: str) -> None:
    """Print the command that follows an installation operation started for a provider.

    Args:
        name: The provider's namespaced name.
    """
    console.print(f"[dim]Follow it with: pragma providers history {escape(name)}[/dim]")


def format_installation_state_label(state: str) -> str:
    """Format an installation state as the label the tables show, such as ``Running`` for ``ready``.

    Args:
        state: An installation state, or ``uninstalled`` from a history entry.

    Returns:
        The state's label with Rich markup escaped, or the state itself when
        it has no label.
    """
    return escape(INSTALLATION_STATE_LABELS.get(state, state))


def format_installation_state(state: str | None) -> str:
    """Format an installation's state for the terminal, colored by the state.

    Args:
        state: The installation's lifecycle state, or ``None`` when the API
            sent none.

    Returns:
        The state's label in Rich markup: green when running, red when
        failed, dim while uninstalling, yellow while an operation runs, or a
        dim dash.
    """
    if not state:
        return "[dim]-[/dim]"

    color = INSTALLATION_STATE_COLORS.get(state, "yellow")
    return f"[{color}]{format_installation_state_label(state)}[/{color}]"


def format_installation_operation(operation_type: str | None, operation_version: str | None) -> str:
    """Format an operation and the version it targets, such as ``upgrade v3.0.0`` or ``restart v3.0.0``.

    Args:
        operation_type: The operation's type, or ``None`` when there is none.
        operation_version: The version the operation targets, or ``None``.

    Returns:
        The operation and its version with Rich markup escaped, or a dim
        dash when there is no operation.
    """
    if not operation_type:
        return "[dim]-[/dim]"

    operation_label = escape(INSTALLATION_OPERATION_LABELS.get(operation_type, operation_type))
    if not operation_version:
        return operation_label

    return f"{operation_label} {format_target_version(operation_version)}"


def format_history_transition(event: ProviderInstallationEvent) -> str:
    """Format the states a history entry moved between, such as ``Queued -> Starting``.

    Args:
        event: The history entry.

    Returns:
        ``<from> -> <to>`` as state labels with Rich markup escaped, or the
        entered state's label alone when the entry has no previous state.
    """
    to_label = format_installation_state_label(event.to_state)
    if not event.from_state:
        return to_label

    return f"{format_installation_state_label(event.from_state)} -> {to_label}"


def print_installed_table(installations: list[ProviderInstallation]) -> None:
    """Print installed providers with their state and latest operation in a table.

    Args:
        installations: Installed providers to list.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("State")
    table.add_column("Version")
    table.add_column("Operation")
    table.add_column("Message")

    for installation in installations:
        table.add_row(
            escape(installation.canonical),
            format_installation_state(installation.lifecycle_state),
            format_target_version(installation.installed_version),
            format_installation_operation(installation.operation_type, installation.operation_version),
            format_optional_value(installation.failure_message),
        )

    console.print(table)


def print_history_table(events: list[ProviderInstallationEvent]) -> None:
    """Print an installation's history in a table, in the order given.

    Args:
        events: History entries, newest first.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Time")
    table.add_column("Transition")
    table.add_column("Operation")
    table.add_column("Version")
    table.add_column("Message")
    table.add_column("Cause")

    for event in events:
        table.add_row(
            event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            format_history_transition(event),
            format_installation_operation(event.operation_type, None),
            format_target_version(event.version),
            format_optional_value(event.message),
            format_optional_value(event.cause),
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
        "versions": [build_version_record(provider_version) for provider_version in versions],
    }


def build_installation_record(installation: ProviderInstallation) -> dict:
    """Build the JSON or YAML record of an installed provider.

    Args:
        installation: The installed provider.

    Returns:
        The installation's identity, served version, state, latest
        operation and its target version, failure message, and installation
        time in ISO 8601.
    """
    return installation.model_dump(mode="json", exclude={"config", "created_at", "updated_at"})
