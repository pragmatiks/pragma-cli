"""CLI auto-completion functions for resource operations."""

from __future__ import annotations

import typer

from pragma_cli import get_client


def completion_resource_ids(incomplete: str):
    """Complete resource identifiers in provider/resource format based on existing resources.

    Args:
        incomplete: Partial input to complete against available resource types.

    Yields:
        Resource identifiers matching the incomplete input.
    """
    client = get_client()
    try:
        resources = client.list_resources()
    except Exception:
        return

    seen = set()
    for res in resources:
        resource_id = f"{res['provider']}/{res['resource']}"
        if resource_id not in seen and resource_id.lower().startswith(incomplete.lower()):
            seen.add(resource_id)
            yield resource_id


def completion_resource_names(ctx: typer.Context, incomplete: str):
    """Complete resource instance names.

    Args:
        ctx: Typer context containing parsed parameters including resource_id.
        incomplete: Partial input to complete.

    Yields:
        Resource names matching the incomplete input for the selected resource type.
    """
    client = get_client()
    resource_id = ctx.params.get("resource_id")
    if not resource_id or "/" not in resource_id:
        return
    provider, resource = resource_id.split("/", 1)
    try:
        resources = client.list_resources(provider=provider, resource=resource)
    except Exception:
        return
    for res in resources:
        if res["name"].startswith(incomplete):
            yield res["name"]
