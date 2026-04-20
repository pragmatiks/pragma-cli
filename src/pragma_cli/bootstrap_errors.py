"""Reactive handling of organization-bootstrap 503 responses.

When the API returns HTTP 503 with a structured body indicating the
organization is still being provisioned (or provisioning failed), the
CLI surfaces a clean user-facing message instead of raw JSON.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer
from rich.console import Console


if TYPE_CHECKING:
    import httpx


_console = Console(stderr=True)

_BOOTSTRAPPING = "organization_bootstrapping"
_BOOTSTRAP_FAILED = "organization_bootstrap_failed"


def check_bootstrap_error(error: httpx.HTTPStatusError) -> None:
    """Handle 503 organization-bootstrap responses with a generic message.

    Inspects the response body for a structured organization-bootstrap
    signal. If matched, prints the user-facing message to stderr and
    exits with status 1. Otherwise returns without side effects so the
    caller can continue with its own error handling.

    Args:
        error: HTTP status error raised by the SDK.

    Raises:
        typer.Exit: If the response indicates an organization bootstrap
            condition. Exit code 1.
    """
    if error.response.status_code != 503:
        return

    try:
        body = error.response.json()
    except ValueError:
        return

    if not isinstance(body, dict):
        return

    kind = body.get("error")

    if kind == _BOOTSTRAPPING:
        _console.print("[yellow]Your workspace is still being set up. Please try again in a moment.[/yellow]")
        raise typer.Exit(1)

    if kind == _BOOTSTRAP_FAILED:
        _console.print("[red]Your workspace setup failed. Please contact support.[/red]")
        raise typer.Exit(1)
