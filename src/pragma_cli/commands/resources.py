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
from pragma_cli.commands.completions import completion_resource_ids
from pragma_cli.helpers import OutputFormat, output_data, parse_resource_id


console = Console()
app = typer.Typer()


def _parse_resource_id(resource_id: str) -> tuple[str, str, str]:
    """Parse and validate a resource identifier.

    Args:
        resource_id: Resource identifier in org/provider/resource/name format.

    Returns:
        Tuple of (provider, resource, name) where provider is 'org/provider'.

    Raises:
        typer.Exit: If the format is invalid.
    """
    try:
        return parse_resource_id(resource_id)
    except ValueError:
        console.print("[red]Error:[/red] Invalid resource ID. Expected 'org/provider/resource/name'.")
        raise typer.Exit(1)


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


def _resolve_at_references(value: object, base_dir: Path) -> object:
    """Recursively resolve @ file references in any dict/list structure.

    String values starting with '@' are replaced with the contents of the
    referenced file (read as text). The '@' prefix is stripped to get the
    file path. Relative paths are resolved against base_dir.

    Args:
        value: Any value from a config structure (dict, list, str, etc.).
        base_dir: Base directory for resolving relative file paths.

    Returns:
        The value with all @ references resolved to file contents.

    Raises:
        typer.Exit: If a referenced file is not found or cannot be read.
    """
    if isinstance(value, dict):
        return {k: _resolve_at_references(v, base_dir) for k, v in value.items()}

    if isinstance(value, list):
        return [_resolve_at_references(item, base_dir) for item in value]

    if isinstance(value, str) and value.startswith("@"):
        file_path = _resolve_path(value[1:], base_dir)

        if not file_path.exists():
            console.print(f"[red]Error:[/red] File not found: {file_path}")
            raise typer.Exit(1)

        try:
            return file_path.read_text()
        except OSError as e:
            console.print(f"[red]Error:[/red] Cannot read file {file_path}: {e}")
            raise typer.Exit(1) from e

    return value


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
        raise typer.Exit(1) from e

    name = resource.get("name")
    if not name:
        console.print("[red]Error:[/red] Resource name is required for pragma/file resources")
        raise typer.Exit(1)

    try:
        client = get_client()
        client.upload_file(name, file_content, content_type)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] Failed to upload file: {_format_api_error(e)}")
        raise typer.Exit(1) from e

    resolved_resource = resource.copy()
    resolved_config = {k: v for k, v in config.items() if k != "content"}
    resolved_resource["config"] = resolved_config

    return resolved_resource


def resolve_file_references(resource: dict, base_dir: Path) -> dict:
    """Resolve file references in resource config.

    For pragmatiks/pragma provider with file resource type, handles binary
    upload via _resolve_file_references. For all other resources,
    recursively resolves '@path' strings in the config to file contents.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Resource dictionary with file references resolved.
    """  # noqa: DOC502
    provider = resource.get("provider")
    resource_type = resource.get("resource")

    if provider == "pragma" and resource_type == "file":
        return _resolve_file_references(resource, base_dir)

    config = resource.get("config")

    if config and isinstance(config, dict):
        resolved_resource = resource.copy()
        resolved_resource["config"] = _resolve_at_references(config, base_dir)
        return resolved_resource

    return resource


def format_state(state: str) -> str:
    """Format lifecycle state for display, escaping Rich markup.

    Returns:
        State string wrapped in brackets and escaped for Rich console.
    """
    return escape(f"[{state}]")


def _print_resource_schemas_table(types: list[dict]) -> None:
    """Print resource schemas in a formatted table.

    Args:
        types: List of resource schema dictionaries to display.
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


@app.command("schemas")
def list_resource_schemas(
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List available resource schemas from deployed providers.

    Displays resource schemas that have been registered by providers.
    Use this to discover what resources you can create.

    Examples:
        pragma resources schemas
        pragma resources schemas --provider gcp
        pragma resources schemas -o json

    Raises:
        typer.Exit: If an error occurs while fetching resource schemas.
    """
    client = get_client()
    try:
        types = client.list_resource_schemas(provider=provider)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if not types:
        console.print("[dim]No resource schemas found.[/dim]")
        return

    output_data([t.model_dump() for t in types], output, table_renderer=_print_resource_schemas_table)


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
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Get resources by type or specific resource by full ID.

    With three segments (org/provider/resource), lists all resources of that type.
    With four segments (org/provider/resource/name), gets a specific resource.

    Examples:
        pragma resources get pragmatiks/pragma/secret
        pragma resources get pragmatiks/pragma/secret/my-secret
        pragma resources get pragmatiks/pragma/secret/my-secret -o json

    Raises:
        typer.Exit: If the resource ID format is invalid.
    """
    client = get_client()
    parts = resource_id.split("/")

    if len(parts) == 3:
        provider = f"{parts[0]}/{parts[1]}"
        resource = parts[2]
        resources = list(client.list_resources(provider=provider, resource=resource))

        if not resources:
            console.print("[dim]No resources found.[/dim]")
            return

        output_data(resources, output, table_renderer=_print_resources_table)
    elif len(parts) == 4:
        provider = f"{parts[0]}/{parts[1]}"
        resource = parts[2]
        name = parts[3]

        try:
            res = client.get_resource(provider=provider, resource=resource, name=name)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]Error:[/red] {_format_api_error(e)}")
            raise typer.Exit(1) from e

        output_data([res], output, table_renderer=_print_resources_table)
    else:
        console.print(
            "[red]Error:[/red] Invalid resource ID. Expected 'org/provider/resource' or 'org/provider/resource/name'."
        )
        raise typer.Exit(1)


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


def _format_config_value(value) -> str:
    """Format a config value for display.

    Renders FieldReference dicts as provider/resource/name#field shorthand.

    Returns:
        Formatted string representation of the value.
    """
    if isinstance(value, dict):
        if "provider" in value and "resource" in value and "name" in value and "field" in value:
            return f"{value['provider']}/{value['resource']}/{value['name']}#{value['field']}"
        formatted = {k: _format_config_value(v) for k, v in value.items()}
        return str(formatted)
    elif isinstance(value, list):
        return str([_format_config_value(v) for v in value])
    return str(value)


def _get_field_metadata(res: dict) -> tuple[set[str], set[str], set[str]]:
    """Fetch field metadata from the resource definition schema.

    Reads both the config schema and outputs schema to determine which
    fields are marked as immutable or sensitive.

    Returns:
        Tuple of (immutable_fields, sensitive_config_fields, sensitive_output_fields).
        Empty sets if the definition cannot be fetched.
    """
    try:
        client = get_client()
        types = client.list_resource_schemas(provider=res["provider"])
    except (httpx.HTTPError, RuntimeError):
        return set(), set(), set()

    for resource_type in types:
        if resource_type.resource != res["resource"]:
            continue

        schema = resource_type.config_schema or {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}

        immutable = {name for name, prop in properties.items() if isinstance(prop, dict) and prop.get("immutable")}
        sensitive = {name for name, prop in properties.items() if isinstance(prop, dict) and prop.get("sensitive")}

        outputs_schema = resource_type.outputs_schema or {}
        output_properties = outputs_schema.get("properties", {}) if isinstance(outputs_schema, dict) else {}

        sensitive_outputs = {
            name for name, prop in output_properties.items() if isinstance(prop, dict) and prop.get("sensitive")
        }

        return immutable, sensitive, sensitive_outputs

    return set(), set(), set()


def _format_field_labels(key: str, immutable_fields: set[str], sensitive_fields: set[str]) -> str:
    r"""Build the metadata label suffix for a field.

    Returns:
        Label string like " [dim]\[immutable] \[sensitive][/dim]" or empty.
    """
    labels: list[str] = []

    if key in immutable_fields:
        labels.append("immutable")
    if key in sensitive_fields:
        labels.append("sensitive")

    if not labels:
        return ""

    tag_str = " ".join(f"\\[{label}]" for label in labels)
    return f" [dim]{tag_str}[/dim]"


def _print_resource_details(res: dict) -> None:
    """Print resource details in a formatted table."""
    resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"
    immutable_fields, sensitive_config_fields, sensitive_output_fields = _get_field_metadata(res)

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
            labels = _format_field_labels(key, immutable_fields, sensitive_config_fields)
            console.print(f"  {key}: {formatted}{labels}")

    outputs = res.get("outputs", {})
    if outputs:
        console.print()
        console.print("[bold]Outputs:[/bold]")
        for key, value in outputs.items():
            labels = _format_field_labels(key, set(), sensitive_output_fields)
            console.print(f"  {key}: {value}{labels}")

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
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show sensitive field values")] = False,
):
    """Show detailed information about a resource.

    Displays the resource's config, outputs, dependencies, and error messages.
    Sensitive fields are redacted by default. Use --reveal to show their values.

    Examples:
        pragma resources describe pragmatiks/gcp/secret/my-test-secret
        pragma resources describe pragmatiks/postgres/database/my-db
        pragma resources describe pragmatiks/gcp/secret/my-secret -o json
        pragma resources describe pragmatiks/gcp/secret/my-secret --reveal

    Raises:
        typer.Exit: If the resource is not found or an error occurs.
    """
    client = get_client()
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        res = client.get_resource(provider=provider, resource=resource, name=name, reveal=reveal)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

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

        try:
            resources = list(yaml.safe_load_all(f.read()))
        except yaml.YAMLError as e:
            console.print(f"[red]Error:[/red] Invalid YAML in {f.name}: {e}")
            raise typer.Exit(1) from e

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
                raise typer.Exit(1) from e


@app.command()
def delete(
    resource_id: Annotated[
        str | None, typer.Argument(autocompletion=completion_resource_ids, show_default=False)
    ] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to delete."),
    ] = None,
):
    """Delete resources by ID or from YAML files.

    Usage:
        pragma resources delete <org/provider/resource/name>
        pragma resources delete -f <file.yaml>

    Raises:
        typer.Exit: If arguments are invalid or deletion fails.
    """
    if file:
        _delete_from_files(file)
    elif resource_id:
        _delete_single(resource_id)
    else:
        console.print("[red]Provide either -f <file> or <org/provider/resource/name>.[/red]")
        raise typer.Exit(1)


def _delete_single(resource_id: str) -> None:
    client = get_client()
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        client.delete_resource(provider=provider, resource=resource, name=name)
        print(f"Deleted {resource_id}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deleting {resource_id}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _delete_from_files(files: list[typer.FileText]) -> None:
    client = get_client()

    for f in files:
        try:
            resources = list(yaml.safe_load_all(f.read()))
        except yaml.YAMLError as e:
            console.print(f"[red]Error:[/red] Invalid YAML in {f.name}: {e}")
            raise typer.Exit(1) from e

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
                raise typer.Exit(1) from e


@app.command()
def deactivate(
    resource_id: Annotated[
        str | None, typer.Argument(autocompletion=completion_resource_ids, show_default=False)
    ] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to deactivate."),
    ] = None,
):
    """Deactivate resources by ID or from YAML files.

    Usage:
        pragma resources deactivate <org/provider/resource/name>
        pragma resources deactivate -f <file.yaml>

    Raises:
        typer.Exit: If arguments are invalid or deactivation fails.
    """
    if file:
        _deactivate_from_files(file)
    elif resource_id:
        _deactivate_single(resource_id)
    else:
        console.print("[red]Provide either -f <file> or <org/provider/resource/name>.[/red]")
        raise typer.Exit(1)


def _deactivate_single(resource_id: str) -> None:
    client = get_client()
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        client.deactivate_resource(provider=provider, resource=resource, name=name)
        print(f"Deactivated {resource_id}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deactivating {resource_id}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _deactivate_from_files(files: list[typer.FileText]) -> None:
    client = get_client()

    for f in files:
        try:
            resources = list(yaml.safe_load_all(f.read()))
        except yaml.YAMLError as e:
            console.print(f"[red]Error:[/red] Invalid YAML in {f.name}: {e}")
            raise typer.Exit(1) from e

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
                client.deactivate_resource(provider=provider, resource=resource_type, name=name)
                print(f"Deactivated {res_id}")
            except httpx.HTTPStatusError as e:
                console.print(f"[red]Error deactivating {res_id}:[/red] {_format_api_error(e)}")
                raise typer.Exit(1) from e


tags_app = typer.Typer()
app.add_typer(tags_app, name="tags", help="Manage resource tags.")


def _fetch_resource(resource_id: str) -> tuple[str, str, str, dict]:
    """Fetch a resource for tag operations.

    Args:
        resource_id: Full resource identifier in org/provider/resource/name format.

    Returns:
        Tuple of (provider, resource_type, name, resource_data).

    Raises:
        typer.Exit: If the resource is not found.
    """
    client = get_client()
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        data = client.get_resource(provider=provider, resource=resource, name=name)
        return provider, resource, name, data
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _apply_tags(
    provider: str, resource: str, name: str, config: dict, lifecycle_state: str, tags: list[str] | None
) -> None:
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
                "lifecycle_state": lifecycle_state,
                "tags": tags,
            }
        )
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


@tags_app.command("list")
def tags_list(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
):
    """List tags for a resource.

    Examples:
        pragma resources tags list pragmatiks/gcp/secret/my-secret
    """
    _, _, _, res = _fetch_resource(resource_id)
    tags = res.get("tags") or []

    if not tags:
        console.print("[dim]No tags.[/dim]")
        return

    for tag in tags:
        console.print(f"  {tag}")


@tags_app.command("add")
def tags_add(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to add (can be repeated)")],
):
    """Add tags to a resource.

    Examples:
        pragma resources tags add pragmatiks/gcp/secret/my-secret --tag production
        pragma resources tags add pragmatiks/gcp/secret/my-secret -t prod -t api

    Raises:
        typer.Exit: If the resource is not found or the operation fails.
    """
    if not tags:
        console.print("[red]Error:[/red] At least one --tag is required.")
        raise typer.Exit(1)

    provider, resource, name, res = _fetch_resource(resource_id)
    current_tags = set(res.get("tags") or [])
    new_tags = set(tags)
    added = new_tags - current_tags

    if not added:
        console.print("[dim]Tags already present, nothing to add.[/dim]")
        return

    _apply_tags(
        provider,
        resource,
        name,
        res.get("config", {}),
        res.get("lifecycle_state", "draft"),
        sorted(current_tags | new_tags),
    )

    for tag in sorted(added):
        console.print(f"[green]+[/green] {tag}")


@tags_app.command("remove")
def tags_remove(
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to remove (can be repeated)")],
):
    """Remove tags from a resource.

    Examples:
        pragma resources tags remove pragmatiks/gcp/secret/my-secret --tag staging
        pragma resources tags remove pragmatiks/gcp/secret/my-secret -t old -t deprecated

    Raises:
        typer.Exit: If the resource is not found or the operation fails.
    """
    if not tags:
        console.print("[red]Error:[/red] At least one --tag is required.")
        raise typer.Exit(1)

    provider, resource, name, res = _fetch_resource(resource_id)
    current_tags = set(res.get("tags") or [])
    to_remove = set(tags)
    removed = current_tags & to_remove

    if not removed:
        console.print("[dim]Tags not present, nothing to remove.[/dim]")
        return

    updated = sorted(current_tags - to_remove)
    _apply_tags(
        provider,
        resource,
        name,
        res.get("config", {}),
        res.get("lifecycle_state", "draft"),
        updated or None,
    )

    for tag in sorted(removed):
        console.print(f"[red]-[/red] {tag}")
