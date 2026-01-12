"""CLI helper functions for parsing resource identifiers."""

from __future__ import annotations


def parse_resource_id(resource_id: str) -> tuple[str, str]:
    """Parse resource identifier into provider and resource type.

    Args:
        resource_id: Resource identifier in format 'provider/resource'.

    Returns:
        Tuple of (provider, resource).

    Raises:
        ValueError: If resource_id format is invalid.
    """
    if "/" not in resource_id:
        raise ValueError(f"Invalid resource ID format: {resource_id}. Expected 'provider/resource'.")
    provider, resource = resource_id.split("/", 1)
    return provider, resource
