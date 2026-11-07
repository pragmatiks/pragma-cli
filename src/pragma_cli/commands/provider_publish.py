"""The ``pragma providers publish`` flow.

Builds or takes a wheel, renames it, declares its version, uploads it and
watches its admission, reporting each failure with its next step.
"""

from __future__ import annotations

import json
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
import httpx
import typer
from pragma_sdk import (
    SETTLED_VERSION_STATUSES,
    DeclaredProviderVersion,
    PragmaClient,
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
from rich.markup import escape
from rich.progress import Progress, TaskID

from pragma_cli import get_client
from pragma_cli.commands.provider_output import (
    VERSION_STATUS_LABELS,
    build_spinner_progress,
    build_version_record,
    console,
    fetch_with_spinner,
    format_version_status,
)
from pragma_cli.errors import (
    check_bootstrap_error,
    error_console,
    format_api_error,
    format_known_request_failure,
    format_os_error_reason,
    format_request_failure,
    is_request_unsent,
    print_unreadable_response,
    require_auth,
)
from pragma_cli.exit_codes import (
    ADMISSION_UNCONFIRMED_EXIT_CODE,
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
)
from pragma_cli.helpers import OutputFormat, output_data, parse_api_error_message


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


def read_changelog(path: Path | None) -> str | None:
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
        return fetch_with_spinner(
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
        return fetch_with_spinner(
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
        return fetch_with_spinner(
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
        return fetch_with_spinner(
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
    organization = fetch_with_spinner("Reading your organization...", client.get_current_organization)

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

    changelog_text = read_changelog(changelog)

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
