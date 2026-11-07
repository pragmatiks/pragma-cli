"""Output helpers the provider commands and the publish flow share."""

from __future__ import annotations

from typing import Any

from pragma_sdk import (
    ProviderVersion,
    VersionStatus,
)
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn

from pragma_cli.errors import (
    error_console,
)


console = Console()


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


VERSION_STATUS_COLORS = {
    VersionStatus.PUBLISHED: "green",
    VersionStatus.FAILED: "red",
}
"""Rich color of each settled version status; every unsettled status shows yellow."""


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


def fetch_with_spinner(description: str, fetch_fn) -> Any:
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
