"""CLI helper functions for parsing resource identifiers and output formatting."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import yaml


if TYPE_CHECKING:
    from collections.abc import Callable


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
        resource_id: Resource identifier in format 'provider/resource/name'.

    Returns:
        Tuple of (provider, resource, name).

    Raises:
        ValueError: If resource_id format is invalid.
    """
    parts = resource_id.split("/")

    if len(parts) != 3:
        raise ValueError(f"Invalid resource ID: {resource_id}. Expected 'provider/resource/name'.")

    return parts[0], parts[1], parts[2]
