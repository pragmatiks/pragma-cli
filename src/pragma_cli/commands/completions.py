"""CLI auto-completion functions for resource and provider operations."""

from __future__ import annotations

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
    """Progressively complete resource identifiers in provider/resource/name format.

    Completion progresses through three levels based on slash count:
    - No slash: complete provider names (appends trailing slash)
    - One slash: complete resource types for the given provider (appends trailing slash)
    - Two slashes: complete resource instance names

    Args:
        incomplete: Partial input to complete against available resources.

    Yields:
        Resource identifier segments matching the incomplete input.
    """
    client = _get_completion_client()
    if client is None:
        return

    slash_count = incomplete.count("/")

    if slash_count == 0:
        try:
            resources = client.list_resources()
        except Exception:
            return

        providers = sorted({r["provider"] for r in resources})

        for provider in providers:
            if provider.lower().startswith(incomplete.lower()):
                yield provider + "/"

    elif slash_count == 1:
        provider, partial = incomplete.split("/", 1)

        try:
            resources = client.list_resources(provider=provider)
        except Exception:
            return

        types = sorted({r["resource"] for r in resources})

        for resource_type in types:
            if resource_type.lower().startswith(partial.lower()):
                yield f"{provider}/{resource_type}/"

    elif slash_count >= 2:
        provider, resource_type, partial_name = incomplete.split("/", 2)

        try:
            resources = client.list_resources(provider=provider, resource=resource_type)
        except Exception:
            return

        for r in resources:
            if r["name"].lower().startswith(partial_name.lower()):
                yield f"{provider}/{resource_type}/{r['name']}"
