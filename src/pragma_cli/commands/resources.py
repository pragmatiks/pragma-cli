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
        return error.response.text or str(error)

    if isinstance(detail, str):
        return detail

    message = detail.get("message", str(error))
    parts = [message]

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

    if field := detail.get("field"):
        ref_parts = [
            detail.get("reference_provider", ""),
            detail.get("reference_resource", ""),
            detail.get("reference_name", ""),
        ]
        ref_id = "/".join(filter(None, ref_parts))
        if ref_id:
            parts.append(f"\n  Reference: {ref_id}#{field}")

    if current_state := detail.get("current_state"):
        target_state = detail.get("target_state", "unknown")
        parts.append(f"\n  Current state: {current_state}")
        parts.append(f"  Target state: {target_state}")

    if resource_id := detail.get("resource_id"):
        parts.append(f"\n  Resource: {resource_id}")

    return "".join(parts)


def _resolve_path(path_str: str, base_dir: Path) -> Path:
    """Resolve a path string relative to base directory.

    Args:
        path_str: Path string (without @ prefix).
        base_dir: Base directory for relative paths.

    Returns:
        Resolved absolute path.
    """
    file_path = Path(path_str).expanduser()

    if not file_path.is_absolute():
        file_path = base_dir / file_path

    return file_path


def _resolve_secret_references(resource: dict, base_dir: Path) -> dict:
    """Resolve file references in secret resource config.

    For pragma/secret resources, scans config.data values for '@' prefix
    and replaces them with the file contents (as text).

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Resource dictionary with file references resolved.

    Raises:
        typer.Exit: If a referenced file is not found or cannot be read.
    """
    config = resource.get("config")
    if not config or not isinstance(config, dict):
        return resource

    data = config.get("data")
    if not data or not isinstance(data, dict):
        return resource

    resolved_data = {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("@"):
            file_path = _resolve_path(value[1:], base_dir)

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


def _resolve_file_references(resource: dict, base_dir: Path) -> dict:
    """Resolve file references in file resource config.

    For pragma/file resources, if config.content starts with '@', reads
    the file as binary and uploads it via the API.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Resource dictionary with content removed (file uploaded separately).

    Raises:
        typer.Exit: If file not found, cannot be read, or upload fails.
    """
    config = resource.get("config")
    if not config or not isinstance(config, dict):
        return resource

    content = config.get("content")
    if not isinstance(content, str) or not content.startswith("@"):
        return resource

    content_type = config.get("content_type")
    if not content_type:
        console.print("[red]Error:[/red] content_type is required for pragma/file resources with @path syntax")
        raise typer.Exit(1)

    file_path = _resolve_path(content[1:], base_dir)

    if not file_path.exists():
        console.print(f"[red]Error:[/red] File not found: {file_path}")
        raise typer.Exit(1)

    try:
        file_content = file_path.read_bytes()
    except OSError as e:
        console.print(f"[red]Error:[/red] Cannot read file {file_path}: {e}")
        raise typer.Exit(1)

    name = resource.get("name")
    if not name:
        console.print("[red]Error:[/red] Resource name is required for pragma/file resources")
        raise typer.Exit(1)

    try:
        client = get_client()
        client.upload_file(name, file_content, content_type)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] Failed to upload file: {_format_api_error(e)}")
        raise typer.Exit(1)

    resolved_resource = resource.copy()
    resolved_config = {k: v for k, v in config.items() if k != "content"}
    resolved_resource["config"] = resolved_config

    return resolved_resource


def resolve_file_references(resource: dict, base_dir: Path) -> dict:
    """Resolve file references in resource config.

    Handles two resource types:
    - pragma/secret: Scans config.data values for '@' prefix and replaces
      with file contents (as text).
    - pragma/file: If config.content starts with '@', uploads the file
      and removes content from config.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Resource dictionary with file references resolved.
    """  # noqa: DOC502
    provider = resource.get("provider")
    resource_type = resource.get("resource")

    if provider != "pragma":
        return resource

    if resource_type == "secret":
        return _resolve_secret_references(resource, base_dir)

    if resource_type == "file":
        return _resolve_file_references(resource, base_dir)

    return resource


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

    failed_resources: list[tuple[str, str]] = []

    for res in resources:
        state = _format_state_color(res["lifecycle_state"])
        updated = res.get("updated_at")
        if updated:
            updated = updated[:19].replace("T", " ")
        else:
            updated = "[dim]-[/dim]"

        table.add_row(
            res["provider"],
            res["resource"],
            res["name"],
            state,
            updated,
        )

        if res.get("lifecycle_state") == "failed" and res.get("error"):
            resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"
            failed_resources.append((resource_id, res["error"]))

    console.print(table)

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
        "waiting": "yellow",
        "pending": "yellow",
        "processing": "cyan",
        "ready": "green",
        "failed": "red",
        "deleting": "dark_orange",
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
        if "provider" in value and "resource" in value and "name" in value and "field" in value:
            return f"{value['provider']}/{value['resource']}/{value['name']}#{value['field']}"
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


def _get_immutable_fields(res: dict) -> set[str]:
    """Fetch the set of immutable config field names from the resource definition schema.

    Returns:
        Set of field names marked as immutable in the resource type schema.
        Empty set if the definition cannot be fetched.
    """
    try:
        client = get_client()
        types = client.list_resource_types(provider=res["provider"])
    except Exception:
        return set()

    for resource_type in types:
        if resource_type.get("resource") != res["resource"]:
            continue

        schema = resource_type.get("schema", {})
        properties = schema.get("properties", {})

        return {name for name, prop in properties.items() if prop.get("immutable")}

    return set()


def _print_resource_details(res: dict) -> None:
    """Print resource details in a formatted table."""
    resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"
    immutable_fields = _get_immutable_fields(res)

    console.print()
    console.print(f"[bold]Resource:[/bold] {resource_id}")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("State", _format_state_color(res["lifecycle_state"]))

    if res.get("error"):
        table.add_row("Error", f"[red]{escape(res['error'])}[/red]")

    if res.get("created_at"):
        table.add_row("Created", res["created_at"])
    if res.get("updated_at"):
        table.add_row("Updated", res["updated_at"])

    console.print(table)

    config = res.get("config", {})
    if config:
        console.print()
        console.print("[bold]Config:[/bold]")
        for key, value in config.items():
            formatted = _format_config_value(value)

            if key in immutable_fields:
                console.print(f"  {key}: {formatted} [dim]\\[immutable][/dim]")
            else:
                console.print(f"  {key}: {formatted}")

    outputs = res.get("outputs", {})
    if outputs:
        console.print()
        console.print("[bold]Outputs:[/bold]")
        for key, value in outputs.items():
            console.print(f"  {key}: {value}")

    dependencies = res.get("dependencies", [])
    if dependencies:
        console.print()
        console.print("[bold]Dependencies:[/bold]")
        for dep in dependencies:
            dep_id = f"{dep['provider']}/{dep['resource']}/{dep['name']}"
            console.print(f"  - {dep_id}")

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
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to apply."),
    ] = None,
    positional_file: Annotated[
        list[typer.FileText] | None, typer.Argument(show_default=False, help="YAML file(s) (same as -f).")
    ] = None,
    draft: Annotated[bool, typer.Option("--draft", "-d", help="Keep in draft state (don't deploy)")] = False,
):
    """Apply resources from YAML files (multi-document supported).

    Usage:
        pragma resources apply -f <file.yaml>
        pragma resources apply <file.yaml>

    By default, resources are queued for immediate processing (deployed).
    Use --draft to keep resources in draft state without deploying.

    For pragma/secret resources, file references in config.data values
    are resolved before submission. Use '@path/to/file' syntax to inline
    file contents.

    Raises:
        typer.Exit: If the apply operation fails.
    """
    files = file or positional_file
    if not files:
        console.print("[red]Provide -f <file> or a positional file path.[/red]")
        raise typer.Exit(1)

    client = get_client()
    for f in files:
        base_dir = Path(f.name).parent
        resources = yaml.safe_load_all(f.read())

        for resource in resources:
            resource = resolve_file_references(resource, base_dir)
            if not draft:
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
    resource_id: Annotated[
        str | None, typer.Argument(autocompletion=completion_resource_ids, show_default=False)
    ] = None,
    name: Annotated[str | None, typer.Argument(autocompletion=completion_resource_names, show_default=False)] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to delete."),
    ] = None,
):
    """Delete resources by type/name or from YAML files.

    Usage:
        pragma resources delete <provider/resource> <name>
        pragma resources delete -f <file.yaml>

    Raises:
        typer.Exit: If arguments are invalid or deletion fails.
    """
    if file:
        _delete_from_files(file)
    elif resource_id and name:
        _delete_single(resource_id, name)
    else:
        console.print("[red]Provide either -f <file> or <provider/resource> <name>.[/red]")
        raise typer.Exit(1)


def _delete_single(resource_id: str, name: str) -> None:
    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    try:
        client.delete_resource(provider=provider, resource=resource, name=name)
        print(f"Deleted {resource_id}/{name}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deleting {resource_id}/{name}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)


def _delete_from_files(files: list[typer.FileText]) -> None:
    client = get_client()

    for f in files:
        resources = yaml.safe_load_all(f.read())

        for resource in resources:
            if not isinstance(resource, dict):
                continue

            provider = resource.get("provider")
            resource_type = resource.get("resource")
            name = resource.get("name")

            if not all([provider, resource_type, name]):
                console.print(f"[red]Skipping invalid resource (missing provider, resource, or name):[/red] {resource}")
                continue

            res_id = f"{provider}/{resource_type}/{name}"

            try:
                client.delete_resource(provider=provider, resource=resource_type, name=name)
                print(f"Deleted {res_id}")
            except httpx.HTTPStatusError as e:
                console.print(f"[red]Error deleting {res_id}:[/red] {_format_api_error(e)}")
                raise typer.Exit(1)


tags_app = typer.Typer()
app.add_typer(tags_app, name="tags", help="Manage resource tags.")


def _fetch_resource(resource_id: str, name: str) -> tuple[str, str, dict]:
    """Fetch a resource for tag operations.

    Returns:
        Tuple of (provider, resource_type, resource_data).

    Raises:
        typer.Exit: If the resource is not found.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    try:
        data = client.get_resource(provider=provider, resource=resource, name=name)
        return provider, resource, data
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)


def _apply_tags(provider: str, resource: str, name: str, config: dict, tags: list[str] | None) -> None:
    """Apply updated tags to a resource.

    Raises:
        typer.Exit: If the operation fails.
    """
    client = get_client()
    try:
        client.apply_resource(
            resource={
                "provider": provider,
                "resource": resource,
                "name": name,
                "config": config,
                "tags": tags,
            }
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1)


@tags_app.command("list")
def tags_list(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
):
    """List tags for a resource.

    Examples:
        pragma resources tags list gcp/secret my-secret
    """
    _, _, res = _fetch_resource(resource_id, name)
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

    provider, resource, res = _fetch_resource(resource_id, name)
    current_tags = set(res.get("tags") or [])
    new_tags = set(tags)
    added = new_tags - current_tags

    if not added:
        console.print("[dim]Tags already present, nothing to add.[/dim]")
        return

    _apply_tags(provider, resource, name, res.get("config", {}), sorted(current_tags | new_tags))

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

    provider, resource, res = _fetch_resource(resource_id, name)
    current_tags = set(res.get("tags") or [])
    to_remove = set(tags)
    removed = current_tags & to_remove

    if not removed:
        console.print("[dim]Tags not present, nothing to remove.[/dim]")
        return

    updated = sorted(current_tags - to_remove)
    _apply_tags(provider, resource, name, res.get("config", {}), updated or None)

    for tag in sorted(removed):
        console.print(f"[red]-[/red] {tag}")
