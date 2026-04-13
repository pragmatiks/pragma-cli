"""CLI auto-completion functions for resource and provider operations."""

from __future__ import annotations

import click
from pragma_sdk import PragmaClient

from pragma_cli.config import get_current_context
from pragma_cli.project_context import resolve_project_or_none


def _get_completion_client(ctx: click.Context | None = None) -> PragmaClient | None:
    """Get a client for shell completion context.

    Returns:
        PragmaClient instance or None if configuration unavailable.
    """
    try:
        root_ctx = ctx.find_root() if ctx is not None else None
        context = root_ctx.params.get("context") if root_ctx is not None else None
        context_name, context_config = get_current_context(context)
        if context_config is None:
            return None
        return PragmaClient(
            base_url=context_config.api_url,
            context=context_name,
            require_auth=False,
        )
    except Exception:
        return None


def completion_provider_ids(ctx: click.Context, incomplete: str):
    """Complete provider identifiers based on deployed providers.

    Args:
        ctx: Active Click context for resolving the selected CLI context.
        incomplete: Partial input to complete against available providers.

    Yields:
        Provider IDs matching the incomplete input.
    """
    client = _get_completion_client(ctx)
    if client is None:
        return
    try:
        providers = client.list_providers()
    except Exception:
        return

    for provider in providers.items:
        if provider.name.lower().startswith(incomplete.lower()):
            yield provider.name


def completion_resource_ids(ctx: click.Context, incomplete: str):
    """Progressively complete resource identifiers in org/provider/resource/name format.

    Completion progresses through four levels based on slash count:
    - No slash: complete organization names (appends trailing slash)
    - One slash: complete provider names within the org (appends trailing slash)
    - Two slashes: complete resource types for org/provider (appends trailing slash)
    - Three slashes: complete resource instance names

    Args:
        ctx: Active Click context for resolving the selected project context.
        incomplete: Partial input to complete against available resources.

    Yields:
        Resource identifier segments matching the incomplete input.
    """
    client = _get_completion_client(ctx)
    if client is None:
        return

    project_slug = resolve_project_or_none(ctx)
    if project_slug is None:
        return

    try:
        project = client.project(project_slug)
    except Exception:
        return

    slash_count = incomplete.count("/")

    if slash_count == 0:
        try:
            resources = project.list_resources()
        except Exception:
            return

        orgs = sorted({r["provider"].split("/")[0] for r in resources if "/" in r["provider"]})

        for org in orgs:
            if org.lower().startswith(incomplete.lower()):
                yield org + "/"

    elif slash_count == 1:
        org, partial = incomplete.split("/", 1)

        try:
            resources = project.list_resources()
        except Exception:
            return

        provider_names = sorted({
            r["provider"].split("/")[1]
            for r in resources
            if "/" in r["provider"] and r["provider"].split("/")[0] == org
        })

        for provider_name in provider_names:
            if provider_name.lower().startswith(partial.lower()):
                yield f"{org}/{provider_name}/"

    elif slash_count == 2:
        org, provider_name, partial = incomplete.split("/", 2)
        full_provider = f"{org}/{provider_name}"

        try:
            resources = project.list_resources(provider=full_provider)
        except Exception:
            return

        types = sorted({r["resource"] for r in resources})

        for resource_type in types:
            if resource_type.lower().startswith(partial.lower()):
                yield f"{full_provider}/{resource_type}/"

    elif slash_count >= 3:
        org, provider_name, resource_type, partial_name = incomplete.split("/", 3)
        full_provider = f"{org}/{provider_name}"

        try:
            resources = project.list_resources(provider=full_provider, resource=resource_type)
        except Exception:
            return

        for r in resources:
            if r["name"].lower().startswith(partial_name.lower()):
                yield f"{full_provider}/{resource_type}/{r['name']}"
