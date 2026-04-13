"""CLI commands for resource management with lifecycle operations."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import jsonschema
import typer
import yaml
from pragma_sdk import ProjectMismatchError
from pydantic import BaseModel, ConfigDict, ValidationError
from rich import print
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_resource_ids
from pragma_cli.helpers import OutputFormat, output_data, parse_resource_id
from pragma_cli.project_context import resolve_project


console = Console()
app = typer.Typer()


class _ScopedResourcePayload(BaseModel):
    """Generic project-scoped resource payload for CLI-driven apply operations.

    ``extra="allow"`` is intentional: the CLI does not duplicate the
    per-provider config schema here. Planning validates the nested
    ``config`` field against the real JSON schema fetched from the
    API before anything is applied, so this model only guards the
    identity fields that route the request to the right project and
    resource type.
    """

    model_config = ConfigDict(extra="allow")

    project_id: str
    provider: str
    resource: str
    name: str


def _project_client(ctx: typer.Context):
    """Return a project-scoped SDK handle for the active project."""
    return get_client().project(resolve_project(ctx))


def _resource_payload(resource: dict[str, Any], project_id: str) -> _ScopedResourcePayload:
    """Inject project context into a resource document before submission.

    Args:
        resource: Resource document loaded from CLI input.
        project_id: Resolved project slug for the active command.

    Returns:
        Validated project-scoped payload ready for SDK submission.

    Raises:
        ProjectMismatchError: If the document declares a different project_id.
    """
    payload = dict(resource)
    declared = payload.get("project_id")
    if declared is not None and declared != project_id:
        raise ProjectMismatchError(project_id, declared)

    payload.setdefault("project_id", project_id)
    return _ScopedResourcePayload.model_validate(payload)


@dataclass
class _PendingUpload:
    """Planned file upload that has been read but not yet sent to the API."""

    name: str
    content: bytes
    content_type: str


@dataclass
class _PlannedResource:
    """A single resource document that has been pre-validated for apply."""

    resource_id: str
    payload: _ScopedResourcePayload
    upload: _PendingUpload | None = None


@dataclass
class _PlanError:
    """Structured per-document error discovered during planning."""

    source: str
    index: int
    resource_id: str
    message: str


@dataclass
class _ApplyPlan:
    """Result of pre-validating a batch of resource documents."""

    resources: list[_PlannedResource] = field(default_factory=list)
    errors: list[_PlanError] = field(default_factory=list)


class _SchemaCache:
    """Lazy per-provider resource schema cache for plan-time validation.

    One instance per apply batch. Fetches the full schema list for a
    provider on first request, then serves subsequent
    ``(provider, resource)`` lookups from memory. Providers that the
    API cannot describe (missing auth, 404, server error) are marked
    as unavailable so the planner can skip schema validation cleanly
    instead of erroring on every document.
    """

    def __init__(self) -> None:
        """Initialize an empty cache."""
        self._by_provider: dict[str, dict[str, dict[str, Any]] | None] = {}

    def config_schema(self, provider: str, resource_type: str) -> dict[str, Any] | None:
        """Return the JSON schema for ``(provider, resource_type)``, or None.

        Args:
            provider: Provider identifier (e.g. ``pragmatiks/gcp``).
            resource_type: Resource type name within the provider.

        Returns:
            JSON schema dict if available, ``None`` when the provider
            could not be described or the resource is unknown. Callers
            must treat ``None`` as "skip schema validation" rather than
            failure — the server is still the ultimate authority.
        """
        if provider not in self._by_provider:
            self._by_provider[provider] = self._fetch(provider)

        provider_schemas = self._by_provider[provider]
        if provider_schemas is None:
            return None

        return provider_schemas.get(resource_type)

    def _fetch(self, provider: str) -> dict[str, dict[str, Any]] | None:
        """Fetch and index schemas for a single provider.

        Args:
            provider: Provider identifier to describe.

        Returns:
            Dict mapping resource type to JSON schema, or ``None`` if
            the describe call failed.
        """
        try:
            schemas = get_client().list_resource_schemas(provider=provider)
        except (httpx.HTTPError, RuntimeError):
            return None

        indexed: dict[str, dict[str, Any]] = {}
        for schema in schemas:
            config_schema = schema.config_schema
            if isinstance(config_schema, dict):
                indexed[schema.resource] = config_schema
        return indexed


def _validate_config_against_schema(config: Any, schema: dict[str, Any]) -> str | None:
    """Validate a resource config dict against a JSON schema.

    Args:
        config: Config payload from the resource document. May be any
            type — planner is defensive and does not assume a shape.
        schema: JSON schema fetched from the API for this resource type.

    Returns:
        Error message string on validation failure, ``None`` on success.
    """
    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(part) for part in e.absolute_path) or "<root>"
        return f"config.{path}: {e.message}"
    except jsonschema.SchemaError as e:
        return f"schema for this resource is invalid: {e.message}"

    return None


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
    """Resolve a relative ``@path`` reference confined to ``base_dir``.

    Security model: ``@path`` references in manifests are a data-flow
    from a potentially untrusted YAML author to the local filesystem.
    An attacker who can influence a manifest could otherwise exfiltrate
    local secrets (``~/.ssh/id_rsa``, ``~/.aws/credentials``, etc.) by
    pointing ``@path`` at them — the CLI would read the bytes during
    planning and upload them during apply. To block that class of
    attack, this resolver enforces four rules:

    1. Absolute paths are rejected.
    2. Raw ``..`` segments in the path string are rejected.
    3. The resolved path must land inside ``base_dir``.
    4. Symlinks are followed before the containment check so symlink
       escapes (``base_dir/link`` -> ``/etc/passwd``) are rejected too.

    ``~`` expansion is intentionally NOT performed: a tilde should not
    map to a real path during manifest loading.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Returns:
        Absolute, realpath-resolved path inside ``base_dir``.

    Raises:
        ValueError: If the path is absolute, contains ``..``, or
            resolves outside ``base_dir`` (directly or via symlink).
    """
    raw_path = Path(path_str)

    if raw_path.is_absolute():
        raise ValueError(
            f"@path reference {path_str!r} is absolute; only paths relative to the manifest directory are allowed."
        )

    if ".." in raw_path.parts:
        raise ValueError(f"@path reference {path_str!r} contains '..'; parent traversal is not allowed.")

    base_real = Path(os.path.realpath(base_dir))
    candidate = base_real / raw_path

    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as e:
        raise ValueError(f"File not found: {candidate}") from e
    except OSError as e:
        raise ValueError(f"Cannot resolve @path reference {path_str!r}: {e}") from e

    try:
        resolved.relative_to(base_real)
    except ValueError as e:
        raise ValueError(
            f"@path reference {path_str!r} resolves outside the manifest directory ({base_real}); "
            "refusing to read for security reasons."
        ) from e

    return resolved


def _plan_file_upload(resource: dict, base_dir: Path) -> tuple[dict, _PendingUpload]:
    """Read a pragma/file resource's ``@path`` content into memory.

    Callers must ensure the resource has ``config.content`` starting
    with ``@``. ``_resolve_path`` rejects anything that would escape
    ``base_dir`` (absolute paths, ``..`` traversal, symlink escapes).
    No network calls are made.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Tuple of (resource dict with ``content`` stripped, pending upload).

    Raises:
        ValueError: If the resource is missing required fields, the file
            cannot be found, escapes the manifest directory, or cannot
            be read.
    """
    config = resource["config"]
    content = config["content"]

    content_type = config.get("content_type")
    if not content_type:
        raise ValueError("content_type is required for pragma/file resources with @path syntax")

    name = resource.get("name")
    if not name:
        raise ValueError("Resource name is required for pragma/file resources")

    file_path = _resolve_path(content[1:], base_dir)

    try:
        file_content = file_path.read_bytes()
    except OSError as e:
        raise ValueError(f"Cannot read file {file_path}: {e}") from e

    stripped_resource = resource.copy()
    stripped_resource["config"] = {k: v for k, v in config.items() if k != "content"}

    return stripped_resource, _PendingUpload(name=name, content=file_content, content_type=content_type)


def _plan_resource_file_references(resource: dict, base_dir: Path) -> tuple[dict, _PendingUpload | None]:
    """Prepare a resource document for submission without side effects.

    For pragma/file resources with an @path reference, reads the
    referenced bytes into memory and returns them as a pending upload.
    For all other resources, recursively resolves @path strings in the
    config into file contents (text) inline.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Tuple of (prepared resource dict, optional pending upload).

    Raises:
        ValueError: If file references are missing, unreadable, or
            escape the manifest directory.
    """  # noqa: DOC502
    provider = resource.get("provider")
    resource_type = resource.get("resource")

    if provider == "pragma" and resource_type == "file":
        config = resource.get("config")
        if isinstance(config, dict):
            content = config.get("content")
            if isinstance(content, str) and content.startswith("@"):
                stripped, upload = _plan_file_upload(resource, base_dir)
                return stripped, upload
        return resource, None

    config = resource.get("config")
    if config and isinstance(config, dict):
        resolved_resource = resource.copy()
        resolved_resource["config"] = _resolve_at_references_pure(config, base_dir)
        return resolved_resource, None

    return resource, None


def _resolve_at_references_pure(value: object, base_dir: Path) -> object:
    """Side-effect-free recursive ``@path`` resolver.

    Raises ``ValueError`` for any path that escapes the manifest
    directory (absolute, ``..`` traversal, symlink escape) and for
    missing/unreadable files, so bulk planning can aggregate failures
    before any network calls are made. The caller is expected to
    catch ``ValueError`` and surface it as a plan error.

    Args:
        value: Any value from a config structure (dict, list, str, etc.).
        base_dir: Base directory for resolving relative file paths.

    Returns:
        The value with all ``@`` references resolved to file contents.

    Raises:
        ValueError: If a referenced file is missing, unreadable, or
            escapes the manifest directory.
    """
    if isinstance(value, dict):
        return {k: _resolve_at_references_pure(v, base_dir) for k, v in value.items()}

    if isinstance(value, list):
        return [_resolve_at_references_pure(item, base_dir) for item in value]

    if isinstance(value, str) and value.startswith("@"):
        file_path = _resolve_path(value[1:], base_dir)
        try:
            return file_path.read_text()
        except OSError as e:
            raise ValueError(f"Cannot read file {file_path}: {e}") from e

    return value


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
    ctx: typer.Context,
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    resource: Annotated[str | None, typer.Option("--resource", "-r", help="Filter by resource type")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Filter by tags")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List resources in the active project.

    Requires a project context. Precedence: ``--project`` flag,
    ``PRAGMA_PROJECT`` env var, then the persistent default set via
    ``pragma projects use <slug>``. Results can be filtered by
    provider, resource type, or tags.

    Examples:
        pragma resources list
        pragma --project my-app resources list
        pragma resources list --provider gcp
        pragma resources list -o json
    """
    project = _project_client(ctx)
    resources = project.list_resources(provider=provider, resource=resource, tags=tags)

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
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Get resources by type or specific resource by full ID.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <slug>``.
    With three segments (org/provider/resource), lists all resources of
    that type within the project. With four segments
    (org/provider/resource/name), fetches a specific resource.

    Examples:
        pragma resources get pragmatiks/pragma/secret
        pragma resources get pragmatiks/pragma/secret/my-secret
        pragma resources get pragmatiks/pragma/secret/my-secret -o json

    Raises:
        typer.Exit: If the resource ID format is invalid.
    """
    project = _project_client(ctx)
    parts = resource_id.split("/")

    if len(parts) == 3:
        provider = f"{parts[0]}/{parts[1]}"
        resource = parts[2]
        resources = list(project.list_resources(provider=provider, resource=resource))

        if not resources:
            console.print("[dim]No resources found.[/dim]")
            return

        output_data(resources, output, table_renderer=_print_resources_table)
    elif len(parts) == 4:
        provider = f"{parts[0]}/{parts[1]}"
        resource = parts[2]
        name = parts[3]

        try:
            res = project.get_resource(provider=provider, resource=resource, name=name)
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
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show sensitive field values")] = False,
):
    """Show detailed information about a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <slug>``.
    Displays the resource's config, outputs, dependencies, and error
    messages. Sensitive fields are redacted by default; use --reveal
    to show their values.

    Examples:
        pragma resources describe pragmatiks/gcp/secret/my-test-secret
        pragma resources describe pragmatiks/postgres/database/my-db
        pragma resources describe pragmatiks/gcp/secret/my-secret -o json
        pragma resources describe pragmatiks/gcp/secret/my-secret --reveal

    Raises:
        typer.Exit: If the resource is not found or an error occurs.
    """
    project = _project_client(ctx)
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        res = project.get_resource(provider=provider, resource=resource, name=name, reveal=reveal)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    output_data(res, output, table_renderer=_print_resource_details)


def _plan_apply_batch(
    files: list[typer.FileText],
    project_id: str,
    *,
    draft: bool,
) -> _ApplyPlan:
    """Parse, validate, and plan every document across all supplied files.

    All documents are parsed up front, file references are read into
    memory (without uploading), project_id is validated, each
    resource payload is validated through Pydantic, and nested
    ``config`` fields are validated against the per-provider JSON
    schema fetched from the API (a read-only side effect). Errors
    are collected per document so the caller sees the full picture
    before anything is applied.

    Args:
        files: YAML files supplied on the command line.
        project_id: Resolved project slug for the active command.
        draft: When False, injects ``lifecycle_state=pending``.

    Returns:
        Fully-planned batch with either a populated resource list or
        a populated error list.
    """
    plan = _ApplyPlan()
    schema_cache = _SchemaCache()

    for f in files:
        source = f.name
        base_dir = Path(source).parent

        try:
            documents = list(yaml.safe_load_all(f.read()))
        except yaml.YAMLError as e:
            plan.errors.append(_PlanError(source=source, index=0, resource_id="<yaml>", message=f"Invalid YAML: {e}"))
            continue

        for index, document in enumerate(documents):
            if document is None:
                continue
            if not isinstance(document, dict):
                plan.errors.append(
                    _PlanError(
                        source=source,
                        index=index,
                        resource_id="<unknown>",
                        message="Expected a mapping at the document root.",
                    )
                )
                continue

            resource_id = f"{document.get('provider', '?')}/{document.get('resource', '?')}/{document.get('name', '?')}"

            try:
                prepared, upload = _plan_resource_file_references(document, base_dir)
            except ValueError as e:
                plan.errors.append(_PlanError(source=source, index=index, resource_id=resource_id, message=str(e)))
                continue

            if not draft:
                prepared["lifecycle_state"] = "pending"

            try:
                payload = _resource_payload(prepared, project_id)
            except ProjectMismatchError as e:
                plan.errors.append(_PlanError(source=source, index=index, resource_id=resource_id, message=str(e)))
                continue
            except ValidationError as e:
                plan.errors.append(_PlanError(source=source, index=index, resource_id=resource_id, message=str(e)))
                continue

            provider = prepared.get("provider")
            resource_type = prepared.get("resource")
            if isinstance(provider, str) and isinstance(resource_type, str):
                schema = schema_cache.config_schema(provider, resource_type)
                if schema is not None:
                    schema_error = _validate_config_against_schema(prepared.get("config"), schema)
                    if schema_error is not None:
                        plan.errors.append(
                            _PlanError(source=source, index=index, resource_id=resource_id, message=schema_error)
                        )
                        continue

            plan.resources.append(_PlannedResource(resource_id=resource_id, payload=payload, upload=upload))

    return plan


def _report_plan_errors(plan: _ApplyPlan) -> None:
    """Print every planning error and exit without side effects.

    Args:
        plan: Failed plan containing one or more per-document errors.

    Raises:
        typer.Exit: Always exits with code 2.
    """
    console.print("[red]Error:[/red] Rejected batch before any resources were applied.")
    for err in plan.errors:
        location = f"{err.source} (document {err.index + 1}, {err.resource_id})"
        console.print(f"  [red]-[/red] {location}: {err.message}")
    raise typer.Exit(2)


@app.command()
def apply(
    ctx: typer.Context,
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

    The project context follows the standard chain:
    ``--project`` flag > ``PRAGMA_PROJECT`` env > persistent default.
    Any document whose ``project_id`` does not match the resolved
    project is rejected before any side effects.

    For pragma/secret resources, file references in config.data values
    are resolved before submission. Use '@path/to/file' syntax to inline
    file contents.

    Raises:
        typer.Exit: If planning fails or the apply operation fails.
    """
    files = file or positional_file
    if not files:
        console.print("[red]Provide -f <file> or a positional file path.[/red]")
        raise typer.Exit(1)

    project_id = resolve_project(ctx)

    plan = _plan_apply_batch(files, project_id, draft=draft)
    if plan.errors:
        _report_plan_errors(plan)

    if not plan.resources:
        console.print("[dim]No resources to apply.[/dim]")
        return

    _execute_plan(plan, project_id)


def _execute_plan(plan: _ApplyPlan, project_id: str) -> None:
    """Apply a pre-validated plan with partial-failure containment.

    Ordering strategy: apply every resource first, then upload file
    bytes for ``pragma/file`` resources afterwards. This keeps a
    failing mid-batch apply from leaking uploaded secrets to the
    server. If an apply or upload fails mid-flight, the function
    prints a loud ``PARTIAL APPLY FAILURE`` report listing what did
    and did not complete, then exits with a non-zero status.

    Args:
        plan: Plan returned by ``_plan_apply_batch``.
        project_id: Resolved project slug.

    Raises:
        typer.Exit: With code 1 on any apply or upload failure, after
            reporting which resources were touched and which were left
            in partial state.
    """
    client = get_client()
    project = client.project(project_id)

    applied: list[tuple[str, str]] = []
    uploaded: list[str] = []
    pending_uploads: list[_PlannedResource] = []

    for planned in plan.resources:
        try:
            result = project.apply_resource(cast(Any, planned.payload))
        except httpx.HTTPStatusError as e:
            console.print(f"[red]Error applying {planned.resource_id}:[/red] {_format_api_error(e)}")
            _report_partial_apply_failure(plan, applied, uploaded, failed_resource=planned.resource_id)
            raise typer.Exit(1) from e

        applied_id = f"{result['provider']}/{result['resource']}/{result['name']}"
        applied.append((planned.resource_id, result["lifecycle_state"]))
        print(f"Applied {applied_id} {format_state(result['lifecycle_state'])}")

        if planned.upload is not None:
            pending_uploads.append(planned)

    for planned in pending_uploads:
        upload = planned.upload
        if upload is None:
            continue
        try:
            client.upload_file(upload.name, upload.content, upload.content_type)
        except httpx.HTTPStatusError as e:
            console.print(f"[red]Error uploading file for {planned.resource_id}:[/red] {_format_api_error(e)}")
            _report_partial_apply_failure(plan, applied, uploaded, failed_resource=planned.resource_id)
            raise typer.Exit(1) from e
        uploaded.append(planned.resource_id)

    console.print(f"[green]Applied {len(applied)} resource(s) to project '{project_id}'.[/green]")


def _report_partial_apply_failure(
    plan: _ApplyPlan,
    applied: list[tuple[str, str]],
    uploaded: list[str],
    *,
    failed_resource: str,
) -> None:
    """Print a loud report when an apply batch fails mid-flight.

    The CLI has no atomic multi-resource apply endpoint, so a
    mid-batch failure leaves the server in partial state. Callers
    and humans both need to see exactly what made it through so they
    can reconcile manually.

    Args:
        plan: Plan that was executing when the failure occurred.
        applied: Resources that were successfully applied, as a list
            of ``(resource_id, lifecycle_state)`` tuples.
        uploaded: Resource IDs whose file bytes were uploaded.
        failed_resource: Resource ID whose apply or upload failed.
    """
    all_ids = [p.resource_id for p in plan.resources]
    applied_ids = {rid for rid, _ in applied}
    uploaded_ids = set(uploaded)

    not_applied = [rid for rid in all_ids if rid not in applied_ids]
    orphan_uploads = [rid for rid in applied_ids if rid not in uploaded_ids and _needs_upload(plan, rid)]

    console.print()
    console.print("[red bold]PARTIAL APPLY FAILURE[/red bold]")
    console.print(
        f"[red]The batch failed at [bold]{failed_resource}[/bold]. "
        "Some resources are already applied and may need manual reconciliation.[/red]"
    )

    console.print()
    console.print(f"[bold]Applied ({len(applied)}):[/bold]")
    if applied:
        for rid, state in applied:
            console.print(f"  [green]+[/green] {rid} ({state})")
    else:
        console.print("  [dim](none)[/dim]")

    console.print()
    console.print(f"[bold]Not applied ({len(not_applied)}):[/bold]")
    if not_applied:
        for rid in not_applied:
            console.print(f"  [red]-[/red] {rid}")
    else:
        console.print("  [dim](none)[/dim]")

    if orphan_uploads:
        console.print()
        console.print("[yellow bold]Orphaned pragma/file applies without uploaded bytes:[/yellow bold]")
        for rid in orphan_uploads:
            console.print(f"  [yellow]![/yellow] {rid}")
        console.print("  [yellow]These resources reference file content that was never uploaded.[/yellow]")
        console.print("  [yellow]Re-run apply once the underlying issue is resolved.[/yellow]")

    console.print()


def _needs_upload(plan: _ApplyPlan, resource_id: str) -> bool:
    """Return True if ``resource_id`` in ``plan`` has a pending file upload.

    Args:
        plan: Plan to search.
        resource_id: Resource identifier to look up.

    Returns:
        True if the planned resource has an associated ``_PendingUpload``.
    """
    for planned in plan.resources:
        if planned.resource_id == resource_id:
            return planned.upload is not None
    return False


@app.command()
def delete(
    ctx: typer.Context,
    resource_id: Annotated[
        str | None, typer.Argument(autocompletion=completion_resource_ids, show_default=False)
    ] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to delete."),
    ] = None,
):
    """Delete resources by ID or from YAML files.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <slug>``.

    Usage:
        pragma resources delete <org/provider/resource/name>
        pragma resources delete -f <file.yaml>

    Raises:
        typer.Exit: If arguments are invalid or deletion fails.
    """
    if file:
        _delete_from_files(ctx, file)
    elif resource_id:
        _delete_single(ctx, resource_id)
    else:
        console.print("[red]Provide either -f <file> or <org/provider/resource/name>.[/red]")
        raise typer.Exit(1)


def _delete_single(ctx: typer.Context, resource_id: str) -> None:
    project = _project_client(ctx)
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        project.delete_resource(provider=provider, resource=resource, name=name)
        print(f"Deleted {resource_id}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deleting {resource_id}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _delete_from_files(ctx: typer.Context, files: list[typer.FileText]) -> None:
    project = _project_client(ctx)

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
                project.delete_resource(provider=provider, resource=resource_type, name=name)
                print(f"Deleted {res_id}")
            except httpx.HTTPStatusError as e:
                console.print(f"[red]Error deleting {res_id}:[/red] {_format_api_error(e)}")
                raise typer.Exit(1) from e


@app.command()
def deactivate(
    ctx: typer.Context,
    resource_id: Annotated[
        str | None, typer.Argument(autocompletion=completion_resource_ids, show_default=False)
    ] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to deactivate."),
    ] = None,
):
    """Deactivate resources by ID or from YAML files.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <slug>``.

    Usage:
        pragma resources deactivate <org/provider/resource/name>
        pragma resources deactivate -f <file.yaml>

    Raises:
        typer.Exit: If arguments are invalid or deactivation fails.
    """
    if file:
        _deactivate_from_files(ctx, file)
    elif resource_id:
        _deactivate_single(ctx, resource_id)
    else:
        console.print("[red]Provide either -f <file> or <org/provider/resource/name>.[/red]")
        raise typer.Exit(1)


def _deactivate_single(ctx: typer.Context, resource_id: str) -> None:
    project = _project_client(ctx)
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        project.deactivate_resource(provider=provider, resource=resource, name=name)
        print(f"Deactivated {resource_id}")
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error deactivating {resource_id}:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _deactivate_from_files(ctx: typer.Context, files: list[typer.FileText]) -> None:
    project = _project_client(ctx)

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
                project.deactivate_resource(provider=provider, resource=resource_type, name=name)
                print(f"Deactivated {res_id}")
            except httpx.HTTPStatusError as e:
                console.print(f"[red]Error deactivating {res_id}:[/red] {_format_api_error(e)}")
                raise typer.Exit(1) from e


tags_app = typer.Typer()
app.add_typer(tags_app, name="tags", help="Manage resource tags.")


def _fetch_resource(ctx: typer.Context, resource_id: str) -> tuple[str, str, str, dict]:
    """Fetch a resource for tag operations.

    Args:
        ctx: Active Typer context for resolving the current project.
        resource_id: Full resource identifier in org/provider/resource/name format.

    Returns:
        Tuple of (provider, resource_type, name, resource_data).

    Raises:
        typer.Exit: If the resource is not found.
    """
    project = _project_client(ctx)
    provider, resource, name = _parse_resource_id(resource_id)

    try:
        data = project.get_resource(provider=provider, resource=resource, name=name)
        return provider, resource, name, data
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


def _apply_tags(ctx: typer.Context, provider: str, resource: str, name: str, tags: list[str] | None) -> None:
    """Apply updated tags to a resource.

    Uses PATCH semantics: only identity fields and tags are sent,
    all other fields are preserved by the API.

    Args:
        ctx: Active Typer context for resolving the current project.
        provider: Provider identifier (e.g., "pragmatiks/postgres").
        resource: Resource type (e.g., "database").
        name: Resource name.
        tags: Updated list of tags, or None to clear all tags.

    Raises:
        typer.Exit: If the operation fails.
    """
    project_id = resolve_project(ctx)
    project = get_client().project(project_id)

    try:
        payload = _resource_payload(
            {
                "provider": provider,
                "resource": resource,
                "name": name,
                "tags": tags,
            },
            project_id,
        )
        project.apply_resource(cast(Any, payload))
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e


@tags_app.command("list")
def tags_list(
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(autocompletion=completion_resource_ids)],
):
    """List tags for a resource.

    Examples:
        pragma resources tags list pragmatiks/gcp/secret/my-secret
    """
    _, _, _, res = _fetch_resource(ctx, resource_id)
    tags = res.get("tags") or []

    if not tags:
        console.print("[dim]No tags.[/dim]")
        return

    for tag in tags:
        console.print(f"  {tag}")


@tags_app.command("add")
def tags_add(
    ctx: typer.Context,
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

    provider, resource, name, res = _fetch_resource(ctx, resource_id)
    current_tags = set(res.get("tags") or [])
    new_tags = set(tags)
    added = new_tags - current_tags

    if not added:
        console.print("[dim]Tags already present, nothing to add.[/dim]")
        return

    _apply_tags(ctx, provider, resource, name, sorted(current_tags | new_tags))

    for tag in sorted(added):
        console.print(f"[green]+[/green] {tag}")


@tags_app.command("remove")
def tags_remove(
    ctx: typer.Context,
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

    provider, resource, name, res = _fetch_resource(ctx, resource_id)
    current_tags = set(res.get("tags") or [])
    to_remove = set(tags)
    removed = current_tags & to_remove

    if not removed:
        console.print("[dim]Tags not present, nothing to remove.[/dim]")
        return

    updated = sorted(current_tags - to_remove)
    _apply_tags(ctx, provider, resource, name, updated or None)

    for tag in sorted(removed):
        console.print(f"[red]-[/red] {tag}")
