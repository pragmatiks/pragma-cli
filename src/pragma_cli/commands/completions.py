"""CLI auto-completion functions for resource and provider operations."""

from __future__ import annotations

import typer
from pragma_sdk import PragmaClient

from pragma_cli.config import get_current_context


def _get_completion_client() -> PragmaClient | None:
    """Get a client for shell completion context.

    Returns:
        PragmaClient instance or None if configuration unavailable.
    """
    try:
        context_name, context_config = get_current_context()
        if context_config is None:
            return None
        return PragmaClient(
            base_url=context_config.api_url,
            context=context_name,
            require_auth=False,
        )
    except Exception:
        return None


def completion_provider_ids(incomplete: str):
    """Complete provider identifiers based on deployed providers.

    Args:
        incomplete: Partial input to complete against available providers.

    Yields:
        Provider IDs matching the incomplete input.
    """
    client = _get_completion_client()
    if client is None:
        return
    try:
        providers = client.list_providers()
    except Exception:
        return

    for provider in providers.items:
        if provider.name.lower().startswith(incomplete.lower()):
            yield provider.name


def completion_resource_ids(incomplete: str):
    """Complete resource identifiers in provider/resource format based on existing resources.

    Args:
        incomplete: Partial input to complete against available resource types.

    Yields:
        Resource identifiers matching the incomplete input.
    """
    client = _get_completion_client()
    if client is None:
        return
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
    client = _get_completion_client()
    if client is None:
        return
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
