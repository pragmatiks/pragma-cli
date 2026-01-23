"""CLI commands for resource management with lifecycle operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import httpx
import typer
import yaml
from rich import print
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import (
    completion_resource_ids,
    completion_resource_names,
)
from pragma_cli.helpers import OutputFormat, output_data, parse_resource_id


console = Console()
app = typer.Typer()


def _format_api_error(error: httpx.HTTPStatusError) -> str:
    """Format an API error response with structured details.

    Returns:
        Formatted error message with details extracted from JSON response.
    """
    try:
        detail = error.response.json().get("detail", {})
    except (json.JSONDecodeError, ValueError):
        # Fall back to plain text if not JSON
        return error.response.text or str(error)

    # Handle simple string details
    if isinstance(detail, str):
        return detail

    # Handle structured error responses
    message = detail.get("message", str(error))
    parts = [message]

    # DependencyValidationError details
    if missing := detail.get("missing_dependencies"):
        parts.append("\n  Missing dependencies:")
        for dep_id in missing:
            parts.append(f"    - {dep_id}")
    if not_ready := detail.get("not_ready_dependencies"):
        parts.append("\n  Dependencies not ready:")
        for item in not_ready:
            if isinstance(item, dict):
                parts.append(f"    - {item['id']} (state: {item['state']})")
            else:
                parts.append(f"    - {item}")

    # FieldReferenceError details
    if field := detail.get("field"):
        ref_parts = [
            detail.get("reference_provider", ""),
            detail.get("reference_resource", ""),
            detail.get("reference_name", ""),
        ]
        ref_id = "/".join(filter(None, ref_parts))
        if ref_id:
            parts.append(f"\n  Reference: {ref_id}#{field}")

    # InvalidLifecycleTransitionError details
    if current_state := detail.get("current_state"):
        target_state = detail.get("target_state", "unknown")
        parts.append(f"\n  Current state: {current_state}")
        parts.append(f"  Target state: {target_state}")

    # ResourceInProcessingError details
    if resource_id := detail.get("resource_id"):
        parts.append(f"\n  Resource: {resource_id}")

    return "".join(parts)


def resolve_file_references(resource: dict, base_dir: Path) -> dict:
    """Resolve file references in secret resource config.

    For pragma/secret resources, scans config.data values for '@' prefix
    and replaces them with the file contents.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Resource dictionary with file references resolved.

    Raises:
        typer.Exit: If a referenced file is not found.
    """
    is_secret = resource.get("provider") == "pragma" and resource.get("resource") == "secret"
    if not is_secret:
        return resource

    config = resource.get("config")
    if not config or not isinstance(config, dict):
        return resource

    data = config.get("data")
    if not data or not isinstance(data, dict):
        return resource

    resolved_data = {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("@"):
            file_path = Path(value[1:]).expanduser()
            if not file_path.is_absolute():
                file_path = base_dir / file_path

            if not file_path.exists():
                console.print(f"[red]Error:[/red] File not found: {file_path}")
                raise typer.Exit(1)

            try:
                resolved_data[key] = file_path.read_text()
            except OSError as e:
                console.print(f"[red]Error:[/red] Cannot read file {file_path}: {e}")
                raise typer.Exit(1)
        else:
            resolved_data[key] = value

    resolved_resource = resource.copy()
    resolved_resource["config"] = {**config, "data": resolved_data}
    return resolved_resource


def format_state(state: str) -> str:
    """Format lifecycle state for display, escaping Rich markup.

    Returns:
        State string wrapped in brackets and escaped for Rich console.
    """
    return escape(f"[{state}]")


def _print_resource_types_table(types: list[dict]) -> None:
    """Print resource types in a formatted table.

    Args:
        types: List of resource type dictionaries to display.
    """
    console.print()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Resource")
    table.add_column("Description")

    for resource_type in types:
        description = resource_type.get("description") or "[dim]—[/dim]"
        table.add_row(
            resource_type["provider"],
            resource_type["resource"],
            description,
        )

    console.print(table)
    console.print()


@app.command("types")
def list_resource_types(
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List available resource types from deployed providers.

    Displays resource definitions (types) that have been registered by providers.
    Use this to discover what resources you can create.

    Examples:
        pragma resources types
        pragma resources types --provider gcp
        pragma resources types -o json

    Raises:
        typer.Exit: If an error occurs while fetching resource types.
    """
    client = get_client()
    try:
        types = client.list_resource_types(provider=provider)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    if not types:
        console.print("[dim]No resource types found.[/dim]")
        return

    output_data(types, output, table_renderer=_print_resource_types_table)


@app.command("list")
def list_resources(
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    resource: Annotated[str | None, typer.Option("--resource", "-r", help="Filter by resource type")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Filter by tags")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List resources, optionally filtered by provider, resource type, or tags.

    Examples:
        pragma resources list
        pragma resources list --provider gcp
        pragma resources list -o json
    """
    client = get_client()
    resources = list(client.list_resources(provider=provider, resource=resource, tags=tags))

    if not resources:
        console.print("[dim]No resources found.[/dim]")
        return

    output_data(resources, output, table_renderer=_print_resources_table)


def _print_resources_table(resources: list[dict]) -> None:
    """Print resources in a formatted table.

    Args:
        resources: List of resource dictionaries to display.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Resource")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("Updated")

    # Track failed resources to show errors after table
    failed_resources: list[tuple[str, str]] = []

    for res in resources:
        state = _format_state_color(res["lifecycle_state"])
        updated = res.get("updated_at", "[dim]-[/dim]")
        if updated and updated != "[dim]-[/dim]":
            # Truncate to datetime portion if it's a full ISO string
            updated = updated[:19].replace("T", " ") if len(updated) > 19 else updated

        table.add_row(
            res["provider"],
            res["resource"],
            res["name"],
            state,
            updated,
        )

        # Track failed resources for error display
        if res.get("lifecycle_state") == "failed" and res.get("error"):
            resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"
            failed_resources.append((resource_id, res["error"]))

    console.print(table)

    # Show errors for failed resources below the table
    for resource_id, error in failed_resources:
        console.print(f"  [red]{resource_id}:[/red] {escape(error)}")


@app.command()
def get(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str | None, typer.Argument(autocompletion=completion_resource_names)] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Get resources by provider/resource type, optionally filtered by name.

    Examples:
        pragma resources get gcp/secret
        pragma resources get gcp/secret my-secret
        pragma resources get gcp/secret my-secret -o json
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    if name:
        res = client.get_resource(provider=provider, resource=resource, name=name)
        output_data([res], output, table_renderer=_print_resources_table)
    else:
        resources = list(client.list_resources(provider=provider, resource=resource))
        if not resources:
            console.print("[dim]No resources found.[/dim]")
            return
        output_data(resources, output, table_renderer=_print_resources_table)


def _format_state_color(state: str) -> str:
    """Format lifecycle state with color markup.

    Returns:
        State string wrapped in Rich color markup.
    """
    state_colors = {
        "draft": "dim",
        "pending": "yellow",
        "processing": "cyan",
        "ready": "green",
        "failed": "red",
    }
    color = state_colors.get(state.lower(), "white")
    return f"[{color}]{state}[/{color}]"


def _format_config_value(value, *, redact_keys: set[str] | None = None) -> str:
    """Format a config value, redacting sensitive fields.

    Returns:
        Formatted string representation with sensitive values masked.
    """
    redact_keys = redact_keys or {"credentials", "password", "secret", "token", "key", "data"}
    if isinstance(value, dict):
        # Check if this is a FieldReference
        if "provider" in value and "resource" in value and "name" in value and "field" in value:
            return f"{value['provider']}/{value['resource']}/{value['name']}#{value['field']}"
        # Recursively format nested dicts
        formatted = {}
        for k, v in value.items():
            if k.lower() in redact_keys:
                formatted[k] = "********"
            else:
                formatted[k] = _format_config_value(v, redact_keys=redact_keys)
        return str(formatted)
    elif isinstance(value, list):
        return str([_format_config_value(v, redact_keys=redact_keys) for v in value])
    return str(value)


def _print_resource_details(res: dict) -> None:
    """Print resource details in a formatted table."""
    resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"

    console.print()
    console.print(f"[bold]Resource:[/bold] {resource_id}")
    console.print()

    # Main properties table
    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    # State with color
    table.add_row("State", _format_state_color(res["lifecycle_state"]))

    # Error if failed
    if res.get("error"):
        table.add_row("Error", f"[red]{escape(res['error'])}[/red]")

    # Timestamps
    if res.get("created_at"):
        table.add_row("Created", res["created_at"])
    if res.get("updated_at"):
        table.add_row("Updated", res["updated_at"])

    console.print(table)

    # Config section
    config = res.get("config", {})
    if config:
        console.print()
        console.print("[bold]Config:[/bold]")
        for key, value in config.items():
            formatted = _format_config_value(value)
            console.print(f"  {key}: {formatted}")

    # Outputs section
    outputs = res.get("outputs", {})
    if outputs:
        console.print()
        console.print("[bold]Outputs:[/bold]")
        for key, value in outputs.items():
            console.print(f"  {key}: {value}")

    # Dependencies section
    dependencies = res.get("dependencies", [])
    if dependencies:
        console.print()
        console.print("[bold]Dependencies:[/bold]")
        for dep in dependencies:
            dep_id = f"{dep['provider']}/{dep['resource']}/{dep['name']}"
            console.print(f"  - {dep_id}")

    # Tags section
    tags = res.get("tags", [])
    if tags:
        console.print()
        console.print("[bold]Tags:[/bold]")
        console.print(f"  {', '.join(tags)}")

    console.print()


@app.command()
def describe(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show detailed information about a resource.

    Displays the resource's config, outputs, dependencies, and error messages.

    Examples:
        pragma resources describe gcp/secret my-test-secret
        pragma resources describe postgres/database my-db
        pragma resources describe gcp/secret my-secret -o json

    Raises:
        typer.Exit: If the resource is not found or an error occurs.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    try:
        res = client.get_resource(provider=provider, resource=resource, name=name)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    output_data(res, output, table_renderer=_print_resource_details)


@app.command()
def apply(
    file: list[typer.FileText],
    pending: Annotated[
        bool, typer.Option("--pending", "-p", help="Queue for processing (set lifecycle_state to PENDING)")
    ] = False,
):
    """Apply resources from YAML files (multi-document supported).

    By default, resources are created in DRAFT state (not processed).
    Use --pending to queue for immediate processing.

    For pragma/secret resources, file references in config.data values
    are resolved before submission. Use '@path/to/file' syntax to inline
    file contents.

    Raises:
        typer.Exit: If the apply operation fails.
    """
    client = get_client()
    for f in file:
        base_dir = Path(f.name).parent
        resources = yaml.safe_load_all(f.read())

        for resource in resources:
            resource = resolve_file_references(resource, base_dir)
            if pending:
                resource["lifecycle_state"] = "pending"
            res_id = f"{resource.get('provider', '?')}/{resource.get('resource', '?')}/{resource.get('name', '?')}"
            try:
                result = client.apply_resource(resource=resource)
                res_id = f"{result['provider']}/{result['resource']}/{result['name']}"
                print(f"Applied {res_id} {format_state(result['lifecycle_state'])}")
            except httpx.HTTPStatusError as e:
                console.print(f"[red]Error applying {res_id}:[/red] {_format_api_error(e)}")
                raise typer.Exit(1)


@app.command()
def delete(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
):
    """Delete a resource.

    Raises:
        typer.Exit: If the resource is not found or deletion fails.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    try:
        client.delete_resource(provider=provider, resource=resource, name=name)
        print(f"Deleted {resource_id}/{name}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deleting {resource_id}/{name}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)


# Tags subcommand group
tags_app = typer.Typer()
app.add_typer(tags_app, name="tags", help="Manage resource tags.")


@tags_app.command("list")
def tags_list(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
):
    """List tags for a resource.

    Examples:
        pragma resources tags list gcp/secret my-secret

    Raises:
        typer.Exit: If the resource is not found.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    try:
        res = client.get_resource(provider=provider, resource=resource, name=name)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    tags = res.get("tags") or []
    if not tags:
        console.print("[dim]No tags.[/dim]")
        return

    for tag in tags:
        console.print(f"  {tag}")


@tags_app.command("add")
def tags_add(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to add (can be repeated)")],
):
    """Add tags to a resource.

    Examples:
        pragma resources tags add gcp/secret my-secret --tag production
        pragma resources tags add gcp/secret my-secret -t prod -t api

    Raises:
        typer.Exit: If the resource is not found or the operation fails.
    """
    if not tags:
        console.print("[red]Error:[/red] At least one --tag is required.")
        raise typer.Exit(1)

    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    try:
        res = client.get_resource(provider=provider, resource=resource, name=name)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    current_tags = set(res.get("tags") or [])
    new_tags = set(tags)
    added = new_tags - current_tags
    updated_tags = sorted(current_tags | new_tags)

    if not added:
        console.print("[dim]Tags already present, nothing to add.[/dim]")
        return

    try:
        client.apply_resource(
            resource={
                "provider": provider,
                "resource": resource,
                "name": name,
                "config": res.get("config", {}),
                "tags": updated_tags,
            }
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    for tag in sorted(added):
        console.print(f"[green]+[/green] {tag}")


@tags_app.command("remove")
def tags_remove(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to remove (can be repeated)")],
):
    """Remove tags from a resource.

    Examples:
        pragma resources tags remove gcp/secret my-secret --tag staging
        pragma resources tags remove gcp/secret my-secret -t old -t deprecated

    Raises:
        typer.Exit: If the resource is not found or the operation fails.
    """
    if not tags:
        console.print("[red]Error:[/red] At least one --tag is required.")
        raise typer.Exit(1)

    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    try:
        res = client.get_resource(provider=provider, resource=resource, name=name)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    current_tags = set(res.get("tags") or [])
    to_remove = set(tags)
    removed = current_tags & to_remove
    updated_tags = sorted(current_tags - to_remove)

    if not removed:
        console.print("[dim]Tags not present, nothing to remove.[/dim]")
        return

    try:
        client.apply_resource(
            resource={
                "provider": provider,
                "resource": resource,
                "name": name,
                "config": res.get("config", {}),
                "tags": updated_tags if updated_tags else None,
            }
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)

    for tag in sorted(removed):
        console.print(f"[red]-[/red] {tag}")
