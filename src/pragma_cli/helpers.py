"""CLI helper functions for parsing resource identifiers and API errors, and for output formatting."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml
from rich.markup import escape


if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx


class OutputFormat(StrEnum):
    """Output format options for CLI commands."""

    TABLE = "table"
    JSON = "json"
    YAML = "yaml"


def output_data(
    data: list[dict[str, Any]] | dict[str, Any],
    format: OutputFormat,
    table_renderer: Callable[..., None] | None = None,
) -> None:
    """Output data in the specified format.

    Args:
        data: Data to output (list of dicts or single dict).
        format: Output format (table, json, yaml).
        table_renderer: Function to render table output. Required for TABLE format.
    """
    if format == OutputFormat.TABLE:
        if table_renderer:
            table_renderer(data)
    elif format == OutputFormat.JSON:
        print(json.dumps(data, indent=2, default=str))
    elif format == OutputFormat.YAML:
        print(yaml.dump(data, default_flow_style=False, sort_keys=False))


def parse_resource_id(resource_id: str) -> tuple[str, str, str]:
    """Parse resource identifier into provider, resource type, and name.

    Args:
        resource_id: Resource identifier in format 'org/provider/resource/name'.

    Returns:
        Tuple of (provider, resource, name) where provider is 'org/provider'.

    Raises:
        ValueError: If resource_id does not have exactly 4 non-empty segments.
    """
    parts = resource_id.split("/")

    if len(parts) != 4 or not all(parts):
        raise ValueError(f"Invalid resource ID: {resource_id}. Expected 'org/provider/resource/name'.")

    provider = f"{parts[0]}/{parts[1]}"
    resource = parts[2]
    name = parts[3]

    return provider, resource, name


def parse_api_error_message(response: httpx.Response) -> str | None:
    """Read the human-readable message from an API error body.

    Args:
        response: The API's error response.

    Returns:
        A string ``detail``, ``detail.message``, or a top-level ``message``,
        or ``None`` when the body carries none of them.
    """
    try:
        body = response.json()
    except ValueError:
        return None

    if not isinstance(body, dict):
        return None

    detail = body.get("detail")

    if isinstance(detail, str):
        return detail

    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]

    message = body.get("message")
    return message if isinstance(message, str) else None


def format_optional_value(value: str | None) -> str:
    """Format an API-supplied table or panel value, with a dim dash when it is missing.

    Args:
        value: Value from the API, or ``None`` or empty when it has none.

    Returns:
        The value with Rich markup escaped, or ``[dim]-[/dim]``.
    """
    return escape(value) if value else "[dim]-[/dim]"
