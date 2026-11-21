"""Upload of the ``@path`` files of ``pragma resources apply``'s ``pragma/file`` resources, before anything is applied.

Holds the rules a ``@path`` reference must pass, the planned upload, the
streamed upload into the project and the report of an upload that failed.
"""

from __future__ import annotations

import contextlib
import functools
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, NoReturn

import httpx
import typer
from pragma_sdk import FileUploadNotStoredError, FileUploadPendingError, UploadError
from pydantic import ValidationError
from rich import print
from rich.markup import escape

from pragma_cli.commands.provider_output import fetch_with_spinner
from pragma_cli.errors import (
    check_bootstrap_error,
    error_console,
    format_os_error_reason,
    format_request_failure,
    format_storage_request_failure,
    print_api_error,
    print_unreadable_response,
)
from pragma_cli.exit_codes import (
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
    UNCONFIRMED_EXIT_CODE,
    compute_http_exit_code,
)


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from pragma_sdk import FileMetadata, ProjectResources


PRAGMA_FILE_URI_PREFIX = "pragma://files/"
"""Prefix of a Pragmatiks file reference, followed by the file's name."""

UPLOAD_CONTENT_TYPE_KEY = "content_type"
"""Key of a ``pragma/file`` config holding the upload's media type, which leaves the applied config."""

FILE_SIZE_UNITS = (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024))
"""Binary units a file size is shown in, largest first, each with its size in bytes."""

FILE_UPLOAD_FAILURE_ERRORS = (
    UploadError,
    FileUploadNotStoredError,
    FileUploadPendingError,
    httpx.HTTPError,
    OSError,
    ValidationError,
)
"""Errors the SDK ends a planned file upload with, each reported by ``report_file_upload_failure``."""


@dataclass(frozen=True)
class PlannedFileUpload:
    """Planned upload of a ``pragma/file`` resource's ``@path`` file, checked but not read.

    Attributes:
        name: Name of the file in the project.
        reference: The ``@path`` reference, without the ``@``.
        base_dir: Manifest directory the reference resolves within.
        size: Size of the file in bytes when it was planned.
        identity: ``(st_dev, st_ino)`` of the file when it was planned.
        content_type: Media type the file is uploaded with.
    """

    name: str
    reference: str
    base_dir: Path
    size: int
    identity: tuple[int, int]
    content_type: str


@dataclass(frozen=True)
class OpenedFileReference:
    """A ``@path`` reference opened under :func:`open_file_reference`'s rules.

    Attributes:
        path: Absolute, realpath-resolved path inside the manifest
            directory, suitable for display in error messages.
        file: The file, open for binary reading from the descriptor the
            rules checked.
        status: The descriptor's ``fstat``.
    """

    path: Path
    file: BinaryIO
    status: os.stat_result


@dataclass(frozen=True)
class FileUploadRecovery:
    """What a failed file upload tells its user to run next.

    Attributes:
        api_url: The API's URL, which tells a request to the API from the
            ``PUT`` to storage.
        apply_again_command: The ``pragma resources apply`` command that
            runs this batch again, quoted for a POSIX shell.
    """

    api_url: str
    apply_again_command: str


def describe_file_type(mode: int) -> str:
    """Describe the file type of a stat ``st_mode`` for error messages.

    Args:
        mode: ``st_mode`` field from a stat result.

    Returns:
        Lowercase short label such as ``"FIFO"``, ``"socket"``,
        ``"directory"``, ``"character device"``, ``"block device"``,
        or ``"unknown"`` when the type does not match a known kind.
    """
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "unknown"


def resolve_file_reference(path_str: str, base_dir: Path) -> Path:
    """Resolve a ``@path`` reference inside the manifest directory, refusing one that escapes it.

    Applies rules 1 to 4 of :func:`open_file_reference`.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Returns:
        The absolute, realpath-resolved path inside ``base_dir``.

    Raises:
        ValueError: If the path is absolute, contains ``..``, does not
            exist, or resolves outside ``base_dir`` (directly or via
            symlink).
    """
    raw_path = Path(path_str)

    if raw_path.is_absolute():
        raise ValueError(
            f"@path reference {path_str!r} is absolute; only paths relative to the manifest directory are allowed."
        )

    if ".." in raw_path.parts:
        raise ValueError(f"@path reference {path_str!r} contains '..'; parent traversal is not allowed.")

    base_real = Path(os.path.realpath(base_dir))
    candidate = base_real / raw_path

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise ValueError(f"File not found: {candidate}") from e
    except OSError as e:
        raise ValueError(f"Cannot resolve @path reference {path_str!r}: {e}") from e

    try:
        resolved.relative_to(base_real)
    except ValueError as e:
        raise ValueError(
            f"@path reference {path_str!r} resolves outside the manifest directory ({base_real}); "
            "refusing to read for security reasons."
        ) from e

    return resolved


@contextlib.contextmanager
def open_file_reference(path_str: str, base_dir: Path) -> Generator[OpenedFileReference]:
    """Open a ``@path`` reference through one descriptor, refusing anything outside the manifest directory.

    Security model: ``@path`` references in manifests are a data-flow
    from a potentially untrusted YAML author to the local filesystem.
    An attacker who can influence a manifest could otherwise exfiltrate
    local secrets (``~/.ssh/id_rsa``, ``~/.aws/credentials``, etc.) by
    pointing ``@path`` at them, and the CLI would send their bytes. To
    block that class of attack, this opener enforces six rules:

    1. Absolute paths are rejected.
    2. Raw ``..`` segments in the path string are rejected.
    3. The resolved path must land inside ``base_dir``.
    4. Symlinks are followed before the containment check so symlink
       escapes (``base_dir/link`` -> ``/etc/passwd``) are rejected.
    5. The file is opened with ``O_NOFOLLOW`` and validated via
       ``fstat`` on the same descriptor, so the type check and the
       yielded status describe the exact file the descriptor reads,
       closing the stat-to-open race where the file is swapped between
       the check and the open.
    6. Only regular files are accepted: FIFOs, sockets, devices, and
       directories would hang or mis-upload.

    ``~`` expansion is intentionally NOT performed: a tilde should not
    map to a real path during manifest loading. The file is closed on
    exit, whatever the body raises.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Yields:
        The resolved path, the open file and its ``fstat``.

    Raises:
        ValueError: If the path is absolute, contains ``..``, resolves
            outside ``base_dir`` (directly or via symlink), is not a
            regular file, or cannot be opened.
    """
    resolved = resolve_file_reference(path_str, base_dir)

    try:
        descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise ValueError(f"Cannot open @path reference {path_str!r}: {e}") from e

    try:
        file = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise

    with file:
        try:
            file_status = os.fstat(file.fileno())
        except OSError as e:
            raise ValueError(f"Cannot stat @path reference {path_str!r}: {e}") from e

        if not stat.S_ISREG(file_status.st_mode):
            raise ValueError(
                f"@path reference {path_str!r} is not a regular file "
                f"(detected {describe_file_type(file_status.st_mode)}); "
                "FIFOs, sockets, devices, and directories are refused."
            )

        yield OpenedFileReference(resolved, file, file_status)


def check_file_reference(path_str: str, base_dir: Path) -> os.stat_result:
    """Check a ``@path`` reference under :func:`open_file_reference`'s rules without reading its content.

    The file is closed before this returns, so a large batch holds no
    open files.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Returns:
        The file's ``fstat``, taken on the descriptor the rules checked.

    Raises:
        ValueError: As :func:`open_file_reference` describes.
    """  # noqa: DOC502
    with open_file_reference(path_str, base_dir) as opened:
        return opened.status


@contextlib.contextmanager
def open_planned_upload(upload: PlannedFileUpload) -> Generator[BinaryIO]:
    """Reopen the file of a planned upload, refusing one replaced since it was planned.

    The file is reopened under :func:`open_file_reference`'s rules, and
    its device and inode must equal those the planner recorded, so a file
    swapped between planning and upload is never sent. The file is closed
    on exit.

    Args:
        upload: The planned upload.

    Yields:
        The file, open for binary reading from the descriptor the rules
        checked.

    Raises:
        ValueError: As :func:`open_file_reference` describes, or if the
            file was replaced after it was planned.
    """  # noqa: DOC502
    with open_file_reference(upload.reference, upload.base_dir) as opened:
        if (opened.status.st_dev, opened.status.st_ino) != upload.identity:
            raise ValueError(f"{upload.reference} was replaced after it was planned.")

        yield opened.file


def plan_file_upload(resource: dict, base_dir: Path) -> tuple[dict, PlannedFileUpload]:
    """Plan the upload of a pragma/file resource's ``@path`` content without reading it.

    Callers must ensure the resource has ``config.content`` starting
    with ``@``. :func:`check_file_reference` refuses anything that
    would escape ``base_dir`` (absolute paths, ``..`` traversal, symlink
    escapes) or is not a regular file, and the planned upload records the
    file's size and identity, not its bytes. The file uploads under the
    resource's name, so ``content`` becomes the reference
    ``pragma://files/<name>`` in the applied config; ``content_type``
    becomes the upload's media type and leaves the applied config. No
    network calls are made.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Tuple of (resource dict with ``content`` replaced by the file's
        reference and ``content_type`` removed, planned upload).

    Raises:
        ValueError: If the resource is missing required fields, or the
            file cannot be found, escapes the manifest directory, or is
            not a regular file.
    """
    config = resource["config"]
    content = config["content"]

    content_type = config.get("content_type")
    if not content_type:
        raise ValueError("content_type is required for pragma/file resources with @path syntax")

    name = resource.get("name")
    if not name:
        raise ValueError("Resource name is required for pragma/file resources")

    reference = content[1:]
    file_status = check_file_reference(reference, base_dir)

    applied_config = {key: value for key, value in config.items() if key != UPLOAD_CONTENT_TYPE_KEY}
    applied_config["content"] = f"{PRAGMA_FILE_URI_PREFIX}{name}"
    applied_resource = resource.copy()
    applied_resource["config"] = applied_config

    return applied_resource, PlannedFileUpload(
        name=name,
        reference=reference,
        base_dir=base_dir,
        size=file_status.st_size,
        identity=(file_status.st_dev, file_status.st_ino),
        content_type=content_type,
    )


def upload_planned_files(
    project: ProjectResources,
    uploads: Sequence[tuple[str, PlannedFileUpload]],
    recovery: FileUploadRecovery,
) -> list[str]:
    """Upload the file of every planned ``pragma/file`` resource, in plan order, before anything is applied.

    Each file is streamed from disk behind an ``Uploading <name> (<size>)``
    spinner on stderr, and ``Uploaded <name> (<size>)`` prints on stdout
    once the file is stored.

    Args:
        project: Project-scoped SDK handle the files are uploaded into.
        uploads: ``(resource_id, upload)`` pairs of the plan's
            ``pragma/file`` resources, in plan order.
        recovery: What a failed upload tells its user to run next.

    Returns:
        Resource IDs whose files are stored, in plan order.

    Raises:
        typer.Exit: On the first upload that fails, as
            :func:`report_file_upload_failure` describes.
    """  # noqa: DOC502
    uploaded: list[str] = []

    for resource_id, upload in uploads:
        stored = upload_planned_file(project, upload, f"Error uploading {upload.name} for {resource_id}", recovery)
        uploaded.append(resource_id)
        print(f"Uploaded {escape(upload.name)} ({format_file_size(stored.size)})")

    return uploaded


def upload_planned_file(
    project: ProjectResources, upload: PlannedFileUpload, heading: str, recovery: FileUploadRecovery
) -> FileMetadata:
    """Reopen a planned upload's file and stream it into the project behind a spinner, returning once it is stored.

    Args:
        project: Project-scoped SDK handle the file is uploaded into.
        upload: The planned upload.
        heading: What failed, should the upload fail, such as ``Error
            uploading report.pdf for pragma/file/report.pdf``.
        recovery: What a failed upload tells its user to run next.

    Returns:
        The stored file's metadata.

    Raises:
        typer.Exit: If the file no longer passes the ``@path`` rules, was
            replaced after it was planned, or the upload fails, as
            :func:`report_file_upload_failure` describes.
    """  # noqa: DOC502
    try:
        with open_planned_upload(upload) as file:
            return fetch_with_spinner(
                f"Uploading {upload.name} ({format_file_size(upload.size)})",
                functools.partial(project.upload_file, upload.name, file, upload.content_type),
            )
    except (ValueError, *FILE_UPLOAD_FAILURE_ERRORS) as e:
        report_file_upload_failure(e, heading, recovery)


def report_file_upload_failure(error: Exception, heading: str, recovery: FileUploadRecovery) -> NoReturn:
    """Print why a planned file upload failed and the command to run next, then exit before any resource is applied.

    Never prints the signed upload address. A 503 saying the caller's
    organization is not set up yet prints the set-up message instead.
    Ends with ``No resource was applied.`` and the apply command that
    keeps waiting for a pending upload, or that uploads and applies again.

    Args:
        error: One of ``FILE_UPLOAD_FAILURE_ERRORS``, or the ``ValueError``
            of a file that no longer passes the ``@path`` rules or was
            replaced after it was planned.
        heading: What failed, such as ``Error uploading report.pdf for
            pragma/file/report.pdf``.
        recovery: What the failure tells its user to run next.

    Raises:
        typer.Exit: With ``UNCONFIRMED_EXIT_CODE`` for an upload still
            pending, ``INPUT_ERROR_EXIT_CODE`` for a local file error, the
            code ``compute_http_exit_code`` gives an API error's status, and
            ``FAILURE_EXIT_CODE`` otherwise, an API answer the CLI cannot
            read included, printed without its values.
    """
    next_step = "To apply again, run"

    match error:
        case httpx.HTTPStatusError():
            check_bootstrap_error(error)
            print_api_error(error, heading)
            exit_code = compute_http_exit_code(error.response.status_code)
        case httpx.RequestError():
            reason = format_file_upload_request_failure(error, recovery.api_url)
            error_console.print(f"[red]{escape(heading)}:[/red] {escape(reason)}.")
            exit_code = FAILURE_EXIT_CODE
        case FileUploadPendingError():
            error_console.print(f"[yellow]{escape(heading)}:[/yellow] {escape(str(error))}")
            next_step = "To keep waiting for it, run"
            exit_code = UNCONFIRMED_EXIT_CODE
        case OSError():
            error_console.print(f"[red]{escape(heading)}:[/red] {escape(format_os_error_reason(error))}.")
            exit_code = INPUT_ERROR_EXIT_CODE
        case ValidationError():
            print_unreadable_response(error)
            exit_code = FAILURE_EXIT_CODE
        case ValueError():
            error_console.print(f"[red]{escape(heading)}:[/red] {escape(str(error))}")
            exit_code = INPUT_ERROR_EXIT_CODE
        case _:
            error_console.print(f"[red]{escape(heading)}:[/red] {escape(str(error))}")
            exit_code = FAILURE_EXIT_CODE

    error_console.print("No resource was applied.")
    error_console.print(f"{next_step}: {escape(recovery.apply_again_command)}", soft_wrap=True)
    raise typer.Exit(exit_code) from error


def format_file_upload_request_failure(error: httpx.RequestError, api_url: str) -> str:
    """Format what kept a file upload's request from getting an answer, in plain words.

    The file's ``PUT`` goes to storage and the declaration and the
    progress reads to the API, told apart by the request's host.

    Args:
        error: The request error the upload raised.
        api_url: The API's URL.

    Returns:
        :func:`~pragma_cli.errors.format_request_failure`'s phrase for a
        request to the API, else
        :func:`~pragma_cli.errors.format_storage_request_failure`'s, which
        never carries the signed upload address. Not escaped for Rich
        markup.
    """
    if error.request.url.host == httpx.URL(api_url).host:
        return format_request_failure(error)

    return format_storage_request_failure(error)


def format_file_size(size: int) -> str:
    """Format a file's size in binary units.

    Args:
        size: Size in bytes.

    Returns:
        Size such as ``"4.8 GiB"``, ``"86.3 KiB"`` or ``"512 B"``.
    """
    for unit, factor in FILE_SIZE_UNITS:
        if size >= factor:
            return f"{size / factor:.1f} {unit}"

    return f"{size} B"
