"""CLI commands for resource management with lifecycle operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich import print
from rich.console import Console
from rich.markup import escape

from pragma_cli import get_client
from pragma_cli.commands.completions import (
    completion_resource_ids,
    completion_resource_names,
)
from pragma_cli.helpers import parse_resource_id


console = Console()
app = typer.Typer()


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
            file_path = Path(value[1:])
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


@app.command("list")
def list_resources(
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    resource: Annotated[str | None, typer.Option("--resource", "-r", help="Filter by resource type")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Filter by tags")] = None,
):
    """List resources, optionally filtered by provider, resource type, or tags."""
    client = get_client()
    resources = list(client.list_resources(provider=provider, resource=resource, tags=tags))

    if not resources:
        console.print("[dim]No resources found.[/dim]")
        return

    for res in resources:
        print(f"{res['provider']}/{res['resource']}/{res['name']} {format_state(res['lifecycle_state'])}")


@app.command()
def get(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str | None, typer.Argument(autocompletion=completion_resource_names)] = None,
):
    """Get resources by provider/resource type, optionally filtered by name."""
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    if name:
        res = client.get_resource(provider=provider, resource=resource, name=name)
        print(f"{resource_id}/{res['name']} {format_state(res['lifecycle_state'])}")
    else:
        for res in client.list_resources(provider=provider, resource=resource):
            print(f"{resource_id}/{res['name']} {format_state(res['lifecycle_state'])}")


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
    """
    client = get_client()
    for f in file:
        base_dir = Path(f.name).parent
        resources = yaml.safe_load_all(f.read())

        for resource in resources:
            resource = resolve_file_references(resource, base_dir)
            if pending:
                resource["lifecycle_state"] = "pending"
            result = client.apply_resource(resource=resource)
            res_id = f"{result['provider']}/{result['resource']}/{result['name']}"
            print(f"Applied {res_id} {format_state(result['lifecycle_state'])}")


@app.command()
def delete(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    name: Annotated[str, typer.Argument(autocompletion=completion_resource_names)],
):
    """Delete a resource."""
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    client.delete_resource(provider=provider, resource=resource, name=name)
    print(f"Deleted {resource_id}/{name}")


@app.command()
def register(
    resource_id: Annotated[str, typer.Argument(help="Resource type in provider/resource format")],
    description: Annotated[str | None, typer.Option("--description", "-d", help="Resource type description")] = None,
    schema_file: Annotated[typer.FileText | None, typer.Option("--schema", "-s", help="JSON schema file")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Tags for categorization")] = None,
):
    """Register a new resource type.

    Registers a resource type so that resources of this type can be created.
    Providers use this to declare what resources they can manage.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)

    schema = None
    if schema_file:
        schema = json.load(schema_file)

    client.register_resource(
        provider=provider,
        resource=resource,
        schema=schema,
        description=description,
        tags=tags,
    )
    print(f"Registered {resource_id}")


@app.command()
def unregister(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
):
    """Unregister a resource type.

    Removes a resource type registration. Existing resources of this type
    will no longer be manageable.
    """
    client = get_client()
    provider, resource = parse_resource_id(resource_id)
    client.unregister_resource(provider=provider, resource=resource)
    print(f"Unregistered {resource_id}")
