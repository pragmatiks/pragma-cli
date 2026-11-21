"""CLI commands for resource management with lifecycle operations."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar, cast

import click
import httpx
import jsonschema
import typer
import yaml
from pragma_sdk import (
    HealthStatus,
    LifecycleState,
    LogEntry,
    LogFollowEnd,
    LogFollowError,
    LogFollowEvent,
    LogFollowInterruptedError,
    ProjectMismatchError,
    ProjectResources,
    ResourceFailedError,
    ResourceHistoryEvent,
    TeardownImpact,
    Vitals,
    VitalsStatus,
)
from pydantic import BaseModel, ConfigDict, ValidationError
from rich import print
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_resource_ids
from pragma_cli.commands.file_upload import (
    FileUploadRecovery,
    PlannedFileUpload,
    open_file_reference,
    plan_file_upload,
    upload_planned_files,
)
from pragma_cli.commands.provider_output import fetch_with_spinner
from pragma_cli.errors import check_bootstrap_error, error_console, format_request_failure, print_api_error
from pragma_cli.exit_codes import (
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
    INTERRUPTED_EXIT_CODE,
    compute_http_exit_code,
)
from pragma_cli.helpers import (
    OutputFormat,
    format_optional_value,
    output_data,
    parse_resource_id,
    validate_resource_id_segments,
)
from pragma_cli.project_context import print_project_override_notice, resolve_project
from pragma_cli.teardown import DEFAULT_WAIT_TIMEOUT_SECONDS, TeardownOptions, print_impact, watch_teardown


console = Console()
app = typer.Typer()


@app.callback()
def announce_project_override(ctx: typer.Context) -> None:
    """Print the ``Using project ...`` notice once per project-scoped resource command.

    Prints nothing for ``schemas``, which no project scopes, or for a help
    request, which targets no project.

    Args:
        ctx: Active Typer context of the ``resources`` group.
    """
    if ctx.resilient_parsing or ctx.invoked_subcommand == "schemas":
        return
    if set(ctx.help_option_names) & set(sys.argv[1:]):
        return
    print_project_override_notice(ctx)


MAX_INLINE_FILE_REFERENCE_SIZE = 10 * 1024 * 1024
"""Largest file a ``@path`` reference inlines into a resource's config, which the planner holds in memory."""

HEALTH_COLORS = {
    "healthy": "green",
    "degraded": "yellow",
    "blocked": "yellow",
    "failed": "red",
}

VITALS_COLORS: dict[VitalsStatus, str] = {
    "nominal": "green",
    "impaired": "yellow",
    "down": "red",
}

LOG_LEVEL_COLORS: dict[str, str] = {
    "debug": "dim",
    "info": "blue",
    "warn": "yellow",
    "error": "red",
}


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
        project_id: Resolved project ID for the active command.

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
class _PlannedResource:
    """A single resource document that has been pre-validated for apply."""

    resource_id: str
    payload: _ScopedResourcePayload
    upload: PlannedFileUpload | None = None


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
    ``(provider, resource)`` lookups from memory. A provider that
    returns a 404 is cached as ``None`` so unknown providers skip
    schema validation (the server remains the authority). Any other
    fetch failure propagates as ``httpx.HTTPStatusError`` or
    ``httpx.TransportError``.
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
            JSON schema dict when available, ``None`` when the
            provider is not known to the API (404). Callers treat
            ``None`` as "skip schema validation" — the server remains
            the ultimate authority for unknown providers.

        Raises:
            httpx.HTTPStatusError: If the schema fetch fails with any
                status but 404.
            httpx.TransportError: If the API cannot be reached.
        """  # noqa: DOC502
        if provider not in self._by_provider:
            self._by_provider[provider] = self._fetch(provider)

        cached = self._by_provider[provider]
        if cached is None:
            return None

        return cached.get(resource_type)

    def _fetch(self, provider: str) -> dict[str, dict[str, Any]] | None:
        """Fetch and index schemas for a single provider.

        Args:
            provider: Provider identifier to describe.

        Returns:
            Dict mapping resource type to JSON schema, or ``None`` if
            the API returned 404 (unknown provider).

        Raises:
            httpx.HTTPStatusError: If the fetch fails with any status
                but 404.
            httpx.TransportError: If the API cannot be reached.
        """  # noqa: DOC502
        try:
            schemas = get_client().list_resource_schemas(provider=provider)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None

            raise

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

    Raises:
        jsonschema.SchemaError: If the schema the API served is not a valid
            JSON schema.
    """  # noqa: DOC502
    try:
        jsonschema.validate(instance=config, schema=schema)
    except jsonschema.ValidationError as e:
        path = ".".join(str(part) for part in e.absolute_path) or "<root>"
        return f"config.{path}: {e.message}"

    return None


def validate_resource_id(context: typer.Context, resource_id: str | None) -> str | None:
    """Check a resource ID is ``org/provider/resource/name``, for use as a Typer argument callback.

    Args:
        context: Click context of the command being parsed.
        resource_id: Resource ID as typed, or ``None`` when the argument is
            optional and was not given.

    Returns:
        The resource ID unchanged, or ``None`` when it was not given.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if the
            ID does not have four non-empty segments.
    """
    if context.resilient_parsing or resource_id is None:
        return resource_id

    try:
        validate_resource_id_segments(resource_id)
    except ValueError as error:
        raise typer.BadParameter(f"must be 'org/provider/resource/name', got '{resource_id}'.") from error

    return resource_id


def validate_resource_path(context: typer.Context, resource_path: str) -> str:
    """Check a resource path names a resource type or one resource, for use as a Typer argument callback.

    A path is ``org/provider/resource`` for a resource type or
    ``org/provider/resource/name`` for one resource.

    Args:
        context: Click context of the command being parsed.
        resource_path: Resource path as typed.

    Returns:
        The argument unchanged.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if it
            does not have three or four non-empty segments.
    """
    if context.resilient_parsing:
        return resource_path

    parts = resource_path.split("/")

    if len(parts) not in (3, 4) or not all(parts):
        raise typer.BadParameter(
            f"must be 'org/provider/resource' or 'org/provider/resource/name', got '{resource_path}'."
        )

    return resource_path


ResourceIdType = TypeVar("ResourceIdType", str, str | None)
"""Type of a resource ID argument: ``str`` when required, ``str | None`` when optional."""

ResourceIdArgument = Annotated[
    ResourceIdType,
    typer.Argument(autocompletion=completion_resource_ids, callback=validate_resource_id, show_default=False),
]
"""Positional ``org/provider/resource/name`` resource ID argument, checked by ``validate_resource_id``."""


def _format_operation_error(error: httpx.HTTPError | ProjectMismatchError) -> str:
    """Render an apply failure that is not an API error response into one line.

    Args:
        error: Transport error, other httpx error, or project-scoping
            mismatch raised by a resource apply.

    Returns:
        What interrupted the exchange for a transport error, the
        mismatch for a project-scoping error, else the error type and
        message. Not escaped for Rich markup.
    """
    if isinstance(error, httpx.TransportError):
        return format_request_failure(error)

    if isinstance(error, ProjectMismatchError):
        return str(error)

    return f"{type(error).__name__}: {error}"


def _read_file_reference(path_str: str, base_dir: Path) -> tuple[Path, bytes]:
    """Read a ``@path`` reference inlined into a resource's config, through one descriptor.

    Opens the file under :func:`open_file_reference`'s rules and reads
    it from the same descriptor. Inlined content is held in memory and
    sent inside the resource's config, so a file larger than
    ``MAX_INLINE_FILE_REFERENCE_SIZE`` is refused. The size is checked
    twice, against ``st_size`` at open time and against the running byte
    count, because a file that grows during the read would otherwise
    pass the cap.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Returns:
        Tuple of ``(resolved_path, file_bytes)``, ``resolved_path`` as
        :func:`open_file_reference` returns it.

    Raises:
        ValueError: As :func:`open_file_reference` describes, or if the
            file exceeds the size limit or cannot be read.
    """  # noqa: DOC502
    limit_mib = MAX_INLINE_FILE_REFERENCE_SIZE / (1024 * 1024)

    with open_file_reference(path_str, base_dir) as opened:
        if opened.status.st_size > MAX_INLINE_FILE_REFERENCE_SIZE:
            size_mib = opened.status.st_size / (1024 * 1024)
            raise ValueError(
                f"@path reference {path_str!r} is {size_mib:.2f} MiB "
                f"({opened.status.st_size} bytes), exceeding the "
                f"{limit_mib:.0f} MiB ({MAX_INLINE_FILE_REFERENCE_SIZE} bytes) limit."
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = opened.file.read(65536)
            except OSError as e:
                raise ValueError(f"Cannot read @path reference {path_str!r}: {e}") from e
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_INLINE_FILE_REFERENCE_SIZE:
                raise ValueError(
                    f"@path reference {path_str!r} grew during read past the "
                    f"{limit_mib:.0f} MiB limit; refusing to inline."
                )
            chunks.append(chunk)

    return opened.path, b"".join(chunks)


def _plan_resource_file_references(resource: dict, base_dir: Path) -> tuple[dict, PlannedFileUpload | None]:
    """Prepare a resource document for submission without side effects.

    For pragma/file resources with an @path reference, checks the
    referenced file and returns it as a planned upload, without reading
    its bytes. For all other resources, recursively resolves @path strings in the
    config into file contents (text) inline.

    Args:
        resource: Resource dictionary from YAML.
        base_dir: Base directory for resolving relative paths.

    Returns:
        Tuple of (prepared resource dict, optional planned upload).

    Raises:
        ValueError: If file references are missing, unreadable, or
            escape the manifest directory.
    """  # noqa: DOC502
    provider = resource.get("provider")
    resource_type = resource.get("resource")

    if provider == "pragmatiks/pragma" and resource_type == "file":
        config = resource.get("config")
        if isinstance(config, dict):
            content = config.get("content")
            if isinstance(content, str) and content.startswith("@"):
                return plan_file_upload(resource, base_dir)
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
    directory (absolute, ``..`` traversal, symlink escape), for
    missing/unreadable files, and for files whose bytes are not
    valid UTF-8, so bulk planning can aggregate failures before any
    network calls are made. The caller is expected to catch
    ``ValueError`` and surface it as a plan error.

    Args:
        value: Any value from a config structure (dict, list, str, etc.).
        base_dir: Base directory for resolving relative file paths.

    Returns:
        The value with all ``@`` references resolved to file contents.

    Raises:
        ValueError: If a referenced file is missing, unreadable,
            escapes the manifest directory, or is not valid UTF-8.
    """
    if isinstance(value, dict):
        return {k: _resolve_at_references_pure(v, base_dir) for k, v in value.items()}

    if isinstance(value, list):
        return [_resolve_at_references_pure(item, base_dir) for item in value]

    if isinstance(value, str) and value.startswith("@"):
        path_str = value[1:]
        _, file_bytes = _read_file_reference(path_str, base_dir)
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"@path reference {path_str!r} is not valid UTF-8") from e

    return value


def format_state(state: str) -> str:
    """Format lifecycle state for display, escaping Rich markup.

    Returns:
        State string wrapped in brackets and escaped for Rich console.
    """
    return escape(f"[{state}]")


def format_resource_state(resource: dict) -> str:
    """Name a resource's state as the CLI shows it.

    Args:
        resource: Resource dictionary the API returned.

    Returns:
        ``processing (apply queued)`` while the resource is processing and
        holds a configuration applied during its operation (the API's
        ``queued_config``), otherwise its ``lifecycle_state``.
    """
    state = resource["lifecycle_state"]

    if state == LifecycleState.PROCESSING and resource.get("queued_config") is not None:
        return f"{state} (apply queued)"
    return state


def _print_resource_schemas_table(types: list[dict]) -> None:
    """Print resource schemas in a formatted table.

    Args:
        types: List of resource schema dictionaries to display.
    """
    console.print()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Resource")
    table.add_column("Serving")
    table.add_column("Description")

    for resource_type in types:
        description = resource_type.get("description")
        table.add_row(
            escape(resource_type["provider"]),
            escape(resource_type["resource"]),
            "yes" if resource_type["serving"] else "[dim]no[/dim]",
            format_optional_value(description),
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
    """
    types = get_client().list_resource_schemas(provider=provider)
    data = [t.model_dump() for t in types]

    if output != OutputFormat.TABLE:
        output_data(data, output)
        return

    if not types:
        console.print("[dim]No resource schemas found.[/dim]")
        return

    _print_resource_schemas_table(data)


@app.command("list")
def list_resources(
    ctx: typer.Context,
    provider: Annotated[str | None, typer.Option("--provider", "-p", help="Filter by provider")] = None,
    resource: Annotated[str | None, typer.Option("--resource", "-r", help="Filter by resource type")] = None,
    tags: Annotated[list[str] | None, typer.Option("--tag", help="Filter by tags")] = None,
    health: Annotated[HealthStatus | None, typer.Option("--health", help="Filter by health")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List resources in the active project.

    Requires a project context. Precedence: ``--project`` flag,
    ``PRAGMA_PROJECT`` env var, then the persistent default set via
    ``pragma projects use <project-id>``. Results can be filtered by
    provider, resource type, tags, or health.

    Examples:
        pragma resources list
        pragma --project my-app resources list
        pragma resources list --provider gcp
        pragma resources list --health failed
        pragma resources list -o json
    """
    project = _project_client(ctx)
    resources = project.list_resources(provider=provider, resource=resource, tags=tags, health=health)
    print_resource_list(resources, output)


def print_resource_list(resources: list[dict], output: OutputFormat) -> None:
    """Print a list of resources in the requested format.

    Args:
        resources: Resource dictionaries the API returned.
        output: Output format; JSON and YAML print an empty list when there
            are no resources.
    """
    if output != OutputFormat.TABLE:
        output_data(resources, output)
        return

    if not resources:
        console.print("[dim]No resources found.[/dim]")
        return

    _print_resources_table(resources)


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
        state = _format_state_color(res)
        updated = res.get("updated_at")
        if updated:
            updated = updated[:19].replace("T", " ")

        table.add_row(
            escape(res["provider"]),
            escape(res["resource"]),
            escape(res["name"]),
            state,
            format_optional_value(updated),
        )

        if res.get("lifecycle_state") == "failed" and res.get("error"):
            resource_id = f"{res['provider']}/{res['resource']}/{res['name']}"
            failed_resources.append((resource_id, res["error"]))

    console.print(table)

    for resource_id, error in failed_resources:
        console.print(f"  [red]{escape(resource_id)}:[/red] {escape(error)}")


@app.command()
def get(
    ctx: typer.Context,
    resource_id: Annotated[
        str, typer.Argument(autocompletion=completion_resource_ids, callback=validate_resource_path)
    ],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Get resources by type or specific resource by full ID.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.
    With three segments (org/provider/resource), lists all resources of
    that type within the project. With four segments
    (org/provider/resource/name), fetches a specific resource.

    Examples:
        pragma resources get pragmatiks/pragma/secret
        pragma resources get pragmatiks/pragma/secret/my-secret
        pragma resources get pragmatiks/pragma/secret/my-secret -o json

    \f

    Raises:
        httpx.HTTPStatusError: If the API refuses the read, a 404 for a
            missing resource included.
    """  # noqa: DOC502
    project = _project_client(ctx)
    parts = resource_id.split("/")

    if len(parts) == 3:
        resources = project.list_resources(provider=f"{parts[0]}/{parts[1]}", resource=parts[2])
        print_resource_list(resources, output)
        return

    provider, resource, name = parse_resource_id(resource_id)
    fetched_resource = project.get_resource(provider=provider, resource=resource, name=name)
    output_data([fetched_resource], output, table_renderer=_print_resources_table)


def _format_state_color(resource: dict) -> str:
    """Format a resource's state with color markup.

    The color follows the lifecycle state; the text is
    ``format_resource_state``'s.

    Args:
        resource: Resource dictionary the API returned.

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
        "deleted": "dim",
    }
    color = state_colors.get(resource["lifecycle_state"].lower(), "white")
    return f"[{color}]{escape(format_resource_state(resource))}[/{color}]"


def format_health(resource: dict) -> str:
    """Format a resource's health with color markup.

    Each failed dependency in ``degraded_by``, when present, is appended
    after the health value: ``degraded — <id> failed, <id> failed``. Without
    one, the health value is shown as it is.

    Args:
        resource: Resource dictionary the API returned, carrying ``health``.

    Returns:
        Health string wrapped in Rich color markup.

    Raises:
        KeyError: If the resource carries no ``health``.
    """  # noqa: DOC502
    health = resource["health"]
    failed_dependencies = ", ".join(f"{entry['resource']} failed" for entry in resource.get("degraded_by") or [])
    text = f"{health} — {failed_dependencies}" if failed_dependencies else health
    color = HEALTH_COLORS.get(health, "white")
    return f"[{color}]{escape(text)}[/{color}]"


def print_blockers(blockers: list[dict]) -> None:
    """Print the ``Blocked by:`` section of a resource's details.

    Each blocker prints its resource and reason on one line, then, dimmed,
    the command that shows that resource.

    Args:
        blockers: Blockers the API returned on the resource; an empty list
            prints nothing.
    """
    if not blockers:
        return

    console.print()
    console.print("[bold]Blocked by:[/bold]")
    for blocker in blockers:
        console.print(f"  {escape(blocker['resource'])} — {escape(blocker['reason'])}")
        console.print(f"    [dim]{escape(blocker['command'])}[/dim]")


def print_owner(owner: dict | None) -> None:
    """Print the ``Owned by:`` section of a resource's details.

    The owner prints as ``provider/resource/name``, then, dimmed, the
    command that shows it.

    Args:
        owner: Owner the API returned on the resource; ``None`` prints
            nothing.
    """
    if not owner:
        return

    console.print()
    console.print("[bold]Owned by:[/bold]")
    console.print(f"  {escape(owner['provider'])}/{escape(owner['resource'])}/{escape(owner['name'])}")
    console.print(f"    [dim]{escape(owner['command'])}[/dim]")


def print_provider_blocker(provider_blocker: dict | None) -> None:
    """Print the ``Waiting for provider:`` section of a resource's details.

    The provider and reason print on one line, then, dimmed, the command
    that shows the provider's installation.

    Args:
        provider_blocker: Provider blocker the API returned on the resource;
            ``None`` prints nothing.
    """
    if not provider_blocker:
        return

    console.print()
    console.print("[bold]Waiting for provider:[/bold]")
    console.print(f"  {escape(provider_blocker['provider'])} — {escape(provider_blocker['reason'])}")
    console.print(f"    [dim]{escape(provider_blocker['command'])}[/dim]")


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
    console.print(f"[bold]Resource:[/bold] {escape(resource_id)}")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("State", _format_state_color(res))

    if res.get("health"):
        table.add_row("Health", format_health(res))

    if res.get("operation_id"):
        table.add_row("Operation", escape(f"{res['operation_type'].lower()} {res['operation_id']}"))

    if res.get("provider_version"):
        table.add_row("Provider version", escape(res["provider_version"]))

    if res.get("error"):
        table.add_row("Error", f"[red]{escape(res['error'])}[/red]")

    if res.get("created_at"):
        table.add_row("Created", escape(res["created_at"]))
    if res.get("updated_at"):
        table.add_row("Updated", escape(res["updated_at"]))

    console.print(table)

    print_owner(res.get("owner"))
    print_blockers(res.get("blockers") or [])
    print_provider_blocker(res.get("provider_blocker"))

    config = res.get("config", {})
    if config:
        console.print()
        console.print("[bold]Config:[/bold]")
        for key, value in config.items():
            formatted = _format_config_value(value)
            labels = _format_field_labels(key, immutable_fields, sensitive_config_fields)
            console.print(f"  {escape(key)}: {escape(formatted)}{labels}")

    outputs = res.get("outputs", {})
    if outputs:
        console.print()
        console.print("[bold]Outputs:[/bold]")
        for key, value in outputs.items():
            labels = _format_field_labels(key, set(), sensitive_output_fields)
            console.print(f"  {escape(key)}: {escape(str(value))}{labels}")

    dependencies = res.get("dependencies", [])
    if dependencies:
        console.print()
        console.print("[bold]Dependencies:[/bold]")
        for dep in dependencies:
            dep_id = f"{dep['provider']}/{dep['resource']}/{dep['name']}"
            console.print(f"  - {escape(dep_id)}")

    tags = res.get("tags", [])
    if tags:
        console.print()
        console.print("[bold]Tags:[/bold]")
        console.print(f"  {escape(', '.join(tags))}")

    console.print()


@app.command()
def describe(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
    reveal: Annotated[bool, typer.Option("--reveal", help="Show sensitive field values")] = False,
):
    """Show detailed information about a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.
    Displays the resource's state, health, owner, blockers, config, outputs,
    dependencies, and error messages. Sensitive fields are redacted by
    default; use --reveal to show their values.

    Examples:
        pragma resources describe pragmatiks/gcp/secret/my-test-secret
        pragma resources describe pragmatiks/postgres/database/my-db
        pragma resources describe pragmatiks/gcp/secret/my-secret -o json
        pragma resources describe pragmatiks/gcp/secret/my-secret --reveal

    \f

    Raises:
        httpx.HTTPStatusError: If the API refuses the read, a 404 for a
            missing resource included.
    """  # noqa: DOC502
    project = _project_client(ctx)
    provider, resource, name = parse_resource_id(resource_id)

    fetched_resource = project.get_resource(provider=provider, resource=resource, name=name, reveal=reveal)
    output_data(fetched_resource, output, table_renderer=_print_resource_details)


def format_resource_history_transition(event: ResourceHistoryEvent) -> str:
    """Format a history entry's transition as raw lifecycle states.

    Args:
        event: One history entry.

    Returns:
        ``<from> -> <to>``, or the entered state alone when the entry has no
        previous state, with Rich markup escaped.
    """
    if event.from_state is None:
        return escape(event.to_state.value)

    return escape(f"{event.from_state.value} -> {event.to_state.value}")


def print_resource_history_table(events: list[ResourceHistoryEvent]) -> None:
    """Print a resource's history in a table, in the order given.

    Args:
        events: History entries, newest first.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Time")
    table.add_column("Transition")
    table.add_column("Operation")
    table.add_column("Version")
    table.add_column("Message")

    for event in events:
        table.add_row(
            event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            format_resource_history_transition(event),
            escape(event.event_type),
            format_optional_value(event.provider_version),
            format_optional_value(event.error),
        )

    console.print(table)


@app.command()
def history(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show the history of a resource, newest first.

    Lists each transition: when, the states it moved between, the recorded
    event, the provider version that ran it, and its failure message. Kept
    for 30 days, also after the resource is deleted; a resource deleted and
    recreated under the same ID shows every lifetime. An ID that never
    existed shows no history.

    Examples:
        pragma resources history acme/gcp/secret/db-pass
        pragma resources history acme/gcp/secret/db-pass -o json

    \f

    Raises:
        httpx.HTTPStatusError: If the API refuses the read, a 404 for an
            unknown project included.
    """  # noqa: DOC502
    project = _project_client(ctx)
    provider, resource, name = parse_resource_id(resource_id)

    events = fetch_with_spinner(
        "Fetching history...",
        lambda: project.list_resource_history(provider=provider, resource=resource, name=name),
    )

    if output != OutputFormat.TABLE:
        output_data([event.model_dump(mode="json") for event in events], output)
        return

    if not events:
        console.print("[dim]No history found.[/dim]")
        return

    print_resource_history_table(events)


def format_vitals_status(status: VitalsStatus) -> str:
    """Format a vitals status with color markup.

    Args:
        status: Status the provider reported.

    Returns:
        The status wrapped in its color: green for ``nominal``, yellow for
        ``impaired``, red for ``down``.
    """
    color = VITALS_COLORS[status]
    return f"[{color}]{status}[/{color}]"


def print_vitals(resource_vitals: Vitals) -> None:
    """Print a resource's vitals: a Status and Message table, then its details.

    Args:
        resource_vitals: Vitals the provider reported.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")
    table.add_row("Status", format_vitals_status(resource_vitals.status))
    table.add_row("Message", format_optional_value(resource_vitals.message))
    console.print(table)

    if not resource_vitals.details:
        return

    console.print()
    console.print("[bold]Details:[/bold]")
    for key, value in resource_vitals.details.items():
        console.print(f"  {escape(key)}: {escape(str(value))}")


@app.command()
def vitals(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show the vitals a resource's provider reports for it.

    The provider reads them live from the object the resource manages:
    ``nominal`` when it works as configured, ``impaired`` when it works with
    reduced capacity, ``down`` when it does not work, with a message and
    provider-specific details. Any status is a successful read.

    Examples:
        pragma resources vitals acme/gcp/secret/db-pass
        pragma resources vitals acme/gcp/secret/db-pass -o json

    \f

    Raises:
        httpx.HTTPStatusError: If the API refuses the read: 404 for a missing
            resource, 409 for one not applied yet, 502 when the provider is
            unavailable or cannot report, 504 when it does not report in time.
    """  # noqa: DOC502
    project = _project_client(ctx)
    provider, resource, name = parse_resource_id(resource_id)

    resource_vitals = fetch_with_spinner(
        "Fetching vitals...",
        lambda: project.get_resource_vitals(provider=provider, resource=resource, name=name),
    )

    if output != OutputFormat.TABLE:
        output_data(resource_vitals.model_dump(mode="json"), output)
        return

    print_vitals(resource_vitals)


def parse_since(value: str) -> datetime:
    """Parse a ``--since`` ISO 8601 timestamp, reading one without an offset as UTC.

    Args:
        value: The option's text.

    Returns:
        The timezone-aware timestamp.

    Raises:
        ValueError: If the text is not an ISO 8601 timestamp.
    """  # noqa: DOC502
    timestamp = datetime.fromisoformat(value)

    if timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=UTC)

    return timestamp


def format_log_entry(entry: LogEntry) -> str:
    """Format one log entry as ``timestamp level message`` with color markup.

    Args:
        entry: Log entry the provider reported.

    Returns:
        The ISO timestamp, the level in its color, and the escaped message.
    """
    color = LOG_LEVEL_COLORS[entry.level]
    return f"{entry.timestamp.isoformat()} [{color}]{entry.level}[/{color}] {escape(entry.message)}"


def print_log_entry(entry: LogEntry) -> None:
    """Print one log entry on one line, never wrapped or highlighted.

    Args:
        entry: Log entry the provider reported.
    """
    console.print(format_log_entry(entry), soft_wrap=True, highlight=False)


def print_logs(entries: list[LogEntry]) -> None:
    """Print log entries one per line, oldest first.

    Args:
        entries: Log entries the provider reported.
    """
    if not entries:
        console.print("[dim]No logs found.[/dim]")
        return
    for entry in entries:
        print_log_entry(entry)


def print_log_entry_json(entry: LogEntry) -> None:
    """Print one log entry as one line of JSON.

    Args:
        entry: Log entry the provider reported.
    """
    sys.stdout.write(json.dumps(entry.model_dump(mode="json")) + "\n")
    sys.stdout.flush()


def print_follow_end(event: LogFollowEnd, last_timestamp: datetime | None, target: Console) -> None:
    """Print why a followed log stream ended.

    Args:
        event: The stream's terminal ``end`` event.
        last_timestamp: Timestamp of the last entry received, if any.
        target: Console the notice goes to: stdout for text output, stderr for JSON.
    """
    target.print(f"[dim]Log stream ended: {escape(event.reason)}[/dim]", soft_wrap=True)
    if event.reason != "provider_restarted":
        return
    if last_timestamp is None:
        target.print("[dim]The provider restarted. Run the command again to keep following.[/dim]", soft_wrap=True)
        return
    target.print(
        f"[dim]The provider restarted after {last_timestamp.isoformat()}. "
        f"Run again with --follow --since {last_timestamp.isoformat()} to continue; "
        "entries at that timestamp print again, and --tail caps how many earlier entries print.[/dim]",
        soft_wrap=True,
    )


def print_followed_logs(events: Generator[LogFollowEvent], output: OutputFormat) -> None:
    """Print a followed log stream until its terminal event.

    Args:
        events: Events of the followed stream, ending with ``end`` or ``error``.
        output: ``table`` prints text lines; ``json`` prints one JSON object per
            entry and sends notices to stderr.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` on an ``error`` event or a
            stream that breaks without a terminal event.
    """
    print_entry = print_log_entry if output == OutputFormat.TABLE else print_log_entry_json
    notice_console = console if output == OutputFormat.TABLE else error_console
    last_timestamp: datetime | None = None
    try:
        for event in events:
            if isinstance(event, LogFollowEnd):
                print_follow_end(event, last_timestamp, notice_console)
                return
            if isinstance(event, LogFollowError):
                error_console.print(f"[red]Log stream failed:[/red] {escape(event.message)}")
                raise typer.Exit(FAILURE_EXIT_CODE)
            print_entry(event.entry)
            last_timestamp = event.entry.timestamp
    except LogFollowInterruptedError as error:
        error_console.print(f"[red]{escape(str(error))}[/red]")
        raise typer.Exit(FAILURE_EXIT_CODE) from error


@app.command()
def logs(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    since: Annotated[
        datetime | None,
        typer.Option(
            "--since",
            parser=parse_since,
            help="Only entries at or after this ISO 8601 timestamp (UTC when it has no offset)",
        ),
    ] = None,
    tail: Annotated[int, typer.Option("--tail", min=1, max=10000, help="Number of recent entries")] = 100,
    follow: Annotated[bool, typer.Option("--follow", help="Keep streaming new entries")] = False,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show a resource's logs from its provider.

    With --follow, the --tail most recent entries print first, then each new
    one as the provider writes it, until the stream ends or Ctrl-C. Delivery
    is at most once: entries written while not connected are not replayed.
    With -o json, --follow prints one JSON object per entry.

    Examples:
        pragma resources logs acme/kubernetes/deployment/web --tail 50
        pragma resources logs acme/kubernetes/deployment/web --follow --tail 5
        pragma resources logs acme/kubernetes/deployment/web -o json

    \f

    Raises:
        httpx.HTTPStatusError: If the API refuses the read: 404 for a missing
            resource, 409 for one not applied yet, 501 for a type that does
            not support following logs, 502 when the provider is unavailable,
            504 when it does not answer in time.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` for ``--follow -o yaml``,
            or ``INTERRUPTED_EXIT_CODE`` on Ctrl-C while following.
    """  # noqa: DOC502
    if follow and output == OutputFormat.YAML:
        error_console.print("[red]--follow prints text or JSON lines; use -o json or leave out -o.[/red]")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    project = _project_client(ctx)
    provider, resource, name = parse_resource_id(resource_id)

    if not follow:
        entries = fetch_with_spinner(
            "Fetching logs...",
            lambda: project.logs(provider, resource, name, since=since, tail=tail),
        )
        if output != OutputFormat.TABLE:
            output_data([entry.model_dump(mode="json") for entry in entries], output)
            return
        print_logs(entries)
        return

    try:
        with closing(project.follow_logs(provider, resource, name, since=since, tail=tail)) as events:
            print_followed_logs(events, output)
    except KeyboardInterrupt as error:
        raise typer.Exit(INTERRUPTED_EXIT_CODE) from error


def _plan_apply_batch(
    files: list[typer.FileText],
    project_id: str,
    *,
    draft: bool,
) -> _ApplyPlan:
    """Parse, validate, and plan every document across all supplied files.

    All documents are parsed up front, a ``pragma/file`` resource's
    ``@path`` file is checked without being read, other file references
    are read into memory, project_id is validated, each
    resource payload is validated through Pydantic, and nested
    ``config`` fields are validated against the per-provider JSON
    schema fetched from the API (a read-only side effect). Errors
    are collected per document so the caller sees the full picture
    before anything is applied.

    Args:
        files: YAML files supplied on the command line.
        project_id: Resolved project ID for the active command.
        draft: Sets every document's ``lifecycle_state`` to ``draft`` when
            True and to ``pending`` when False, whatever the document says.

    Returns:
        Fully-planned batch with either a populated resource list or
        a populated error list.

    Raises:
        httpx.HTTPStatusError: If fetching a provider's schemas fails with
            any status but 404; planning stops there, nothing has been
            applied, and per-document errors found so far are not reported.
        httpx.TransportError: If the API cannot be reached while fetching
            schemas; planning stops as for an API error.
        jsonschema.SchemaError: If a schema the API served is not a valid
            JSON schema; planning stops as for an API error.
    """  # noqa: DOC502
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
        except UnicodeDecodeError as e:
            plan.errors.append(_PlanError(source=source, index=0, resource_id="<yaml>", message=f"Not UTF-8 text: {e}"))
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

            prepared["lifecycle_state"] = "draft" if draft else "pending"

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
        typer.Exit: Always, with ``INPUT_ERROR_EXIT_CODE``.
    """
    error_console.print("[red]Error:[/red] Invalid resource documents; no resource was applied.")

    for err in plan.errors:
        location = f"{err.source} (document {err.index + 1}, {err.resource_id})"
        error_console.print(f"  [red]-[/red] {escape(location)}: {escape(err.message)}")

    raise typer.Exit(INPUT_ERROR_EXIT_CODE)


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
    draft: Annotated[
        bool,
        typer.Option(
            "--draft",
            "-d",
            help="Save as a draft without deploying; only a new or draft resource can be saved as one.",
        ),
    ] = False,
):
    """Apply resources from YAML files (multi-document supported).

    Usage:
        pragma resources apply -f <file.yaml>
        pragma resources apply <file.yaml>

    By default, resources are queued for immediate processing (deployed).
    Use --draft to save new or draft resources without deploying; the API
    refuses a resource that is not a draft and says how to recover it.

    The project context follows the standard chain:
    ``--project`` flag > ``PRAGMA_PROJECT`` env > persistent default.
    Any document whose ``project_id`` does not match the resolved
    project is rejected before any side effects.

    For pragma/secret resources, file references in config.data values
    are resolved before submission. Use '@path/to/file' syntax to inline
    file contents.

    For a pragma/file resource whose config.content is '@path/to/file',
    the file is streamed from disk into the project, with config.content_type
    as its media type, and stored before any resource is applied; a file
    replaced after planning is refused.

    Exit codes: 3 a file upload reached Pragmatiks and is not stored yet;
    no resource was applied, and applying again keeps waiting for it
    without sending the file again.

    \f

    Raises:
        click.UsageError: If no file is given, or files are given both with
            -f and as positional paths.
        typer.Exit: If planning finds invalid documents or the apply fails.
    """  # noqa: DOC502
    if file and positional_file:
        raise click.UsageError("Pass files either with -f or as positional paths, not both.")

    files = file or positional_file
    if not files:
        raise click.UsageError("Provide -f <file> or a positional file path.")

    project_id = resolve_project(ctx)

    plan = _plan_apply_batch(files, project_id, draft=draft)
    if plan.errors:
        _report_plan_errors(plan)

    if not plan.resources:
        console.print("[dim]No resources to apply.[/dim]")
        return

    _execute_plan(plan, project_id, build_apply_again_command(sys.argv[1:]))


def build_apply_again_command(arguments: list[str]) -> str:
    """Build the command that runs this ``pragma resources apply`` again, with the same arguments.

    Args:
        arguments: The command line's arguments after the program name.

    Returns:
        The command, quoted for a POSIX shell.
    """
    return shlex.join(["pragma", *arguments])


def _execute_plan(plan: _ApplyPlan, project_id: str, apply_again_command: str) -> None:
    """Upload every planned file, then apply a pre-validated plan with partial-failure containment.

    Every ``pragma/file`` resource's ``@path`` file is uploaded first,
    each upload returning once the file is stored, so a ``pragma/file``
    resource and every resource reading ``pragma://files/<name>`` are
    applied after their file exists. The files go only to the caller's
    own project and planning has already validated every document, so a
    later failed apply leaves only stored files, which the next apply
    finds stored and does not send again. An upload failure ends the
    batch before any apply, ending with the command that applies the
    batch again
    (:func:`~pragma_cli.commands.file_upload.report_file_upload_failure`).
    If an apply fails mid-flight, whether through an HTTP error, a transport error
    (connect/timeout/read/write), or a project-scoping mismatch, the
    function prints a loud ``PARTIAL APPLY FAILURE`` report listing what
    did and did not complete, then exits with a non-zero status.

    Args:
        plan: Plan returned by ``_plan_apply_batch``.
        project_id: Resolved project ID.
        apply_again_command: Command that runs this apply again, printed
            after a failed upload.

    Raises:
        typer.Exit: On any upload failure, as
            :func:`~pragma_cli.commands.file_upload.report_file_upload_failure`
            describes, or on any apply failure, after reporting which
            resources were touched; with ``NOT_FOUND_EXIT_CODE`` when the
            API answered 404, else with ``FAILURE_EXIT_CODE``.
    """  # noqa: DOC502
    client = get_client()
    project = client.project(project_id)

    uploads = [(planned.resource_id, planned.upload) for planned in plan.resources if planned.upload is not None]
    uploaded = upload_planned_files(project, uploads, FileUploadRecovery(client.base_url, apply_again_command))
    applied: list[tuple[str, str]] = []

    for planned in plan.resources:
        try:
            result = project.apply_resource(cast(Any, planned.payload))
        except (httpx.HTTPError, ProjectMismatchError) as e:
            heading = f"Error applying {planned.resource_id}"
            report_apply_failure(e, heading, plan, applied, uploaded, failed_resource_id=planned.resource_id)

        applied_id = f"{result['provider']}/{result['resource']}/{result['name']}"
        applied_state = format_resource_state(result)
        applied.append((planned.resource_id, applied_state))
        print(f"Applied {escape(applied_id)} {format_state(applied_state)}")

    console.print(f"[green]Applied {len(applied)} resource(s) to project '{escape(project_id)}'.[/green]")


def report_apply_failure(
    error: httpx.HTTPError | ProjectMismatchError,
    heading: str,
    plan: _ApplyPlan,
    applied: list[tuple[str, str]],
    uploaded: list[str],
    *,
    failed_resource_id: str,
) -> NoReturn:
    """Print why an apply failed mid-batch, with what the batch left behind, then exit.

    A 503 saying the caller's organization is not set up yet prints the
    set-up message instead, without the batch report.

    Args:
        error: The error the apply raised.
        heading: What failed, such as ``Error applying acme/db/database/main``.
        plan: Full plan that was being executed.
        applied: ``(resource_id, state)`` pairs applied so far, each state
            as ``format_resource_state`` names it.
        uploaded: Resource IDs whose files are stored.
        failed_resource_id: Resource ID whose apply failed.

    Raises:
        typer.Exit: With the code ``compute_http_exit_code`` gives an API
            error's status, else with ``FAILURE_EXIT_CODE``.
    """
    if isinstance(error, httpx.HTTPStatusError):
        check_bootstrap_error(error)
        print_resource_api_error(error, heading)
        exit_code = compute_http_exit_code(error.response.status_code)
    else:
        error_console.print(f"[red]{escape(heading)}:[/red] {escape(_format_operation_error(error))}")
        exit_code = FAILURE_EXIT_CODE

    _report_partial_apply_failure(plan, applied, uploaded, failed_resource_id=failed_resource_id)
    raise typer.Exit(exit_code) from error


def _report_partial_apply_failure(
    plan: _ApplyPlan,
    applied: list[tuple[str, str]],
    uploaded: list[str],
    *,
    failed_resource_id: str,
) -> None:
    """Print a loud report when an apply batch fails mid-flight.

    The CLI has no atomic multi-resource apply endpoint, so a
    mid-batch failure leaves the server in partial state. Callers
    and humans both need to see exactly what made it through so they
    can reconcile manually. The files uploaded before the applies are
    listed first, under ``Uploaded``; applying again finds them stored
    and does not send them again. The failed resource is reported as a
    dedicated ``Failed`` section so it is visually distinct from the
    rest of the untouched batch listed under ``Not attempted``.

    Args:
        plan: Plan that was executing when the failure occurred.
        applied: Resources that were successfully applied, as a list
            of ``(resource_id, state)`` tuples, each state as
            ``format_resource_state`` names it.
        uploaded: Resource IDs whose files are stored.
        failed_resource_id: Resource ID whose apply failed.
    """
    all_ids = [p.resource_id for p in plan.resources]
    applied_ids = {resource_id for resource_id, _ in applied}

    not_attempted = [
        resource_id for resource_id in all_ids if resource_id not in applied_ids and resource_id != failed_resource_id
    ]

    error_console.print()
    error_console.print("[red bold]PARTIAL APPLY FAILURE[/red bold]")
    error_console.print(
        f"[red]The batch failed at [bold]{escape(failed_resource_id)}[/bold]. "
        "Some resources are already applied and may need manual reconciliation.[/red]"
    )

    error_console.print()
    error_console.print(f"[bold]Uploaded ({len(uploaded)}):[/bold]")
    if uploaded:
        for resource_id in uploaded:
            error_console.print(f"  [green]+[/green] {escape(resource_id)}")
    else:
        error_console.print("  [dim](none)[/dim]")

    error_console.print()
    error_console.print(f"[bold]Applied ({len(applied)}):[/bold]")
    if applied:
        for resource_id, state in applied:
            error_console.print(f"  [green]+[/green] {escape(resource_id)} ({escape(state)})")
    else:
        error_console.print("  [dim](none)[/dim]")

    error_console.print()
    error_console.print("[bold]Failed (1):[/bold]")
    error_console.print(f"  [red]x[/red] {escape(failed_resource_id)}")

    error_console.print()
    error_console.print(f"[bold]Not attempted ({len(not_attempted)}):[/bold]")
    if not_attempted:
        for resource_id in not_attempted:
            error_console.print(f"  [dim]-[/dim] {escape(resource_id)}")
    else:
        error_console.print("  [dim](none)[/dim]")

    error_console.print()


def report_resource_api_error(error: httpx.HTTPStatusError, heading: str) -> NoReturn:
    """Print an API error about a resource request under a heading naming the request, then exit.

    A 503 saying the caller's organization is not set up yet prints the
    set-up message instead of the API's.

    Args:
        error: The HTTP status error the request raised.
        heading: What failed, such as ``Error deleting acme/db/database/main``.

    Raises:
        typer.Exit: With the code ``compute_http_exit_code`` gives the status.
    """
    check_bootstrap_error(error)
    print_resource_api_error(error, heading)
    raise typer.Exit(compute_http_exit_code(error.response.status_code)) from error


def print_resource_api_error(error: httpx.HTTPStatusError, heading: str) -> None:
    """Print an API error about a resource request, followed by the resource details its body carries.

    Args:
        error: The HTTP status error the request raised.
        heading: What failed, such as ``Error applying acme/db/database/main``.
    """
    print_api_error(error, heading)

    for line in build_resource_error_details(error.response):
        error_console.print(escape(line))


def build_resource_error_details(response: httpx.Response) -> list[str]:
    """Build the indented lines describing the resources an API error body names.

    Args:
        response: The API's error response.

    Returns:
        Lines for the missing dependencies, the referenced field, the
        current and target lifecycle states, and the resource an object
        ``detail`` names; empty when the body has no object ``detail``.
    """
    try:
        body = response.json()
    except ValueError:
        return []

    detail = body.get("detail") if isinstance(body, dict) else None

    if not isinstance(detail, dict):
        return []

    lines: list[str] = []

    if missing := detail.get("missing_dependencies"):
        lines.append("  Missing dependencies:")
        lines.extend(f"    - {dependency_id}" for dependency_id in missing)

    if field := detail.get("field"):
        reference_parts = [
            detail.get("reference_provider", ""),
            detail.get("reference_resource", ""),
            detail.get("reference_name", ""),
        ]
        reference_id = "/".join(filter(None, reference_parts))

        if reference_id:
            lines.append(f"  Reference: {reference_id}#{field}")

    if current_state := detail.get("current_state"):
        lines.append(f"  Current state: {current_state}")
        lines.append(f"  Target state: {detail.get('target_state', 'unknown')}")

    if resource_id := detail.get("resource_id"):
        lines.append(f"  Resource: {resource_id}")

    return lines


DEACTIVATION_STARTED = "Deactivating {} — teardown is in progress; the resource returns to draft when it completes."
DELETION_STARTED = "Deleting {} — the resource and everything it owns are removed when this completes."

WAIT_HELP = "Wait for teardown to finish before returning."
WAIT_TIMEOUT_HELP = "Seconds to wait when --wait is passed; 0 waits forever."
DRY_RUN_HELP = "Preview the teardown and the resources it reaches without changing anything."


@app.command()
def delete(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str | None] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to delete."),
    ] = None,
    wait: Annotated[bool, typer.Option("--wait", help=WAIT_HELP)] = False,
    wait_timeout: Annotated[
        float, typer.Option("--wait-timeout", min=0, help=WAIT_TIMEOUT_HELP)
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    dry_run: Annotated[bool, typer.Option("--dry-run", help=DRY_RUN_HELP)] = False,
):
    """Delete resources by ID or from YAML files.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Removal cascades into everything the resource owns and runs
    asynchronously: the command returns as soon as Pragmatiks accepts the
    request unless ``--wait`` is passed. With ``--wait`` the cascade is
    reported one resource at a time as it completes.

    Usage:
        pragma resources delete <org/provider/resource/name>
        pragma resources delete -f <file.yaml>
        pragma resources delete --wait --wait-timeout 900 <org/provider/resource/name>
        pragma resources delete --dry-run <org/provider/resource/name>

    \f

    Raises:
        click.UsageError: If both or neither of a resource ID and -f are given.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` for invalid YAML or an
            invalid resource document in -f, ``NOT_FOUND_EXIT_CODE`` if the
            resource does not exist, else ``FAILURE_EXIT_CODE`` if deletion
            fails.
    """  # noqa: DOC502
    options = TeardownOptions(wait=wait, wait_timeout=wait_timeout, dry_run=dry_run)
    targets = read_teardown_targets(ctx, resource_id, file)
    project = _project_client(ctx)

    for provider, resource, name in targets:
        _delete_one(project, provider, resource, name, options)


def _delete_one(project: ProjectResources, provider: str, resource: str, name: str, options: TeardownOptions) -> None:
    """Remove one resource, reporting everything the removal reaches.

    Args:
        project: Project-scoped SDK handle.
        provider: Provider that manages the resource, as 'org/provider'.
        resource: Resource type name.
        name: Resource instance name.
        options: Wait and dry-run behaviour for this command.

    Raises:
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the resource does not
            exist, or with ``FAILURE_EXIT_CODE`` if the removal is rejected,
            fails, or does not finish within the wait timeout.
    """  # noqa: DOC502
    resource_id = f"{provider}/{resource}/{name}"

    try:
        response = project.delete_resource(provider=provider, resource=resource, name=name, dry_run=options.dry_run)
    except httpx.HTTPStatusError as e:
        report_resource_api_error(e, f"Error deleting {resource_id}")

    if options.dry_run:
        print(f"Dry run — {escape(resource_id)} was not removed.")
        print_impact(response.impact)
        return

    print(DELETION_STARTED.format(escape(resource_id)))
    print_impact(response.impact)

    if options.wait:
        _wait_removed(project, response.impact, resource_id, options.wait_timeout)


def _wait_removed(project: ProjectResources, impact: list[TeardownImpact], resource_id: str, timeout: float) -> None:
    """Block until a removal cascade finishes, reporting each resource as it goes.

    Args:
        project: Project-scoped SDK handle.
        impact: Impact rows returned by the removal.
        resource_id: Full resource identifier shown to the user.
        timeout: Seconds to wait before giving up; ``0`` waits forever.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if teardown fails or does not finish in time.
    """
    try:
        watch_teardown(project, impact, timeout=timeout, settled_state=LifecycleState.DELETED)
    except ResourceFailedError as e:
        error_console.print(
            f"[red]Error deleting {escape(resource_id)}:[/red] {escape(str(e.error or e))}. "
            f"Run 'pragma resources describe {escape(e.resource_id)}' to see the current state, then try again."
        )
        raise typer.Exit(FAILURE_EXIT_CODE) from e
    except TimeoutError as e:
        error_console.print(
            f"[red]Error deleting {escape(resource_id)}:[/red] Removal is still running after {timeout}s. "
            "Run 'pragma resources list' to see what is left."
        )
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    print(f"Deleted {escape(resource_id)}")


@app.command()
def deactivate(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str | None] = None,
    file: Annotated[
        list[typer.FileText] | None,
        typer.Option("--file", "-f", help="YAML file(s) defining resources to deactivate."),
    ] = None,
    wait: Annotated[
        bool,
        typer.Option("--wait", help="Wait for teardown to finish and the resource to return to draft."),
    ] = False,
    wait_timeout: Annotated[
        float, typer.Option("--wait-timeout", min=0, help=WAIT_TIMEOUT_HELP)
    ] = DEFAULT_WAIT_TIMEOUT_SECONDS,
    dry_run: Annotated[bool, typer.Option("--dry-run", help=DRY_RUN_HELP)] = False,
):
    """Deactivate resources by ID or from YAML files.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Teardown cascades into everything the resource owns and runs
    asynchronously: the command returns as soon as Pragmatiks accepts the
    request unless ``--wait`` is passed. With ``--wait`` the cascade is
    reported one resource at a time as it returns to draft.

    Usage:
        pragma resources deactivate <org/provider/resource/name>
        pragma resources deactivate -f <file.yaml>
        pragma resources deactivate --wait <org/provider/resource/name>
        pragma resources deactivate --wait --wait-timeout 0 <org/provider/resource/name>
        pragma resources deactivate --dry-run <org/provider/resource/name>

    \f

    Raises:
        click.UsageError: If both or neither of a resource ID and -f are given.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` for invalid YAML or an
            invalid resource document in -f, ``NOT_FOUND_EXIT_CODE`` if the
            resource does not exist, else ``FAILURE_EXIT_CODE`` if
            deactivation fails.
    """  # noqa: DOC502
    options = TeardownOptions(wait=wait, wait_timeout=wait_timeout, dry_run=dry_run)
    targets = read_teardown_targets(ctx, resource_id, file)
    project = _project_client(ctx)

    for provider, resource, name in targets:
        _deactivate_one(project, provider, resource, name, options)


def _deactivate_one(
    project: ProjectResources, provider: str, resource: str, name: str, options: TeardownOptions
) -> None:
    """Deactivate one resource, reporting everything the teardown reaches.

    Args:
        project: Project-scoped SDK handle.
        provider: Provider that manages the resource, as 'org/provider'.
        resource: Resource type name.
        name: Resource instance name.
        options: Wait and dry-run behaviour for this command.

    Raises:
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the resource does not
            exist, or with ``FAILURE_EXIT_CODE`` if the deactivation is
            rejected, fails, or does not finish within the wait timeout.
    """  # noqa: DOC502
    resource_id = f"{provider}/{resource}/{name}"

    try:
        response = project.deactivate_resource(provider=provider, resource=resource, name=name, dry_run=options.dry_run)
    except httpx.HTTPStatusError as e:
        report_resource_api_error(e, f"Error deactivating {resource_id}")

    if options.dry_run:
        print(f"Dry run — {escape(resource_id)} was not deactivated.")
        print_impact(response.impact)
        return

    print(DEACTIVATION_STARTED.format(escape(resource_id)))
    print_impact(response.impact)

    if options.wait:
        _wait_deactivated(project, response.impact, resource_id, options.wait_timeout)


def _wait_deactivated(
    project: ProjectResources, impact: list[TeardownImpact], resource_id: str, timeout: float
) -> None:
    """Block until a deactivation cascade finishes, reporting each resource as it goes.

    Args:
        project: Project-scoped SDK handle.
        impact: Impact rows returned by the deactivation.
        resource_id: Full resource identifier shown to the user.
        timeout: Seconds to wait before giving up; ``0`` waits forever.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if teardown fails or does not finish in time.
    """
    try:
        watch_teardown(project, impact, timeout=timeout, settled_state=LifecycleState.DRAFT)
    except ResourceFailedError as e:
        error_console.print(
            f"[red]Error deactivating {escape(resource_id)}:[/red] {escape(str(e.error or e))}. "
            f"Run 'pragma resources describe {escape(e.resource_id)}' to see the current state, then try again."
        )
        raise typer.Exit(FAILURE_EXIT_CODE) from e
    except TimeoutError as e:
        error_console.print(
            f"[red]Error deactivating {escape(resource_id)}:[/red] Teardown is still running after {timeout}s. "
            f"Run 'pragma resources describe {escape(resource_id)}' to follow it; "
            "the resource returns to draft when teardown completes."
        )
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    print(f"Deactivated {escape(resource_id)}")


def read_teardown_targets(
    context: typer.Context, resource_id: str | None, files: list[typer.FileText] | None
) -> list[tuple[str, str, str]]:
    """Parse the target resources from a resource ID or from -f files.

    Args:
        context: Typer context of the command, resolving the active project
            for -f documents.
        resource_id: Resource ID argument, already checked to have four
            segments, or ``None``.
        files: Files given with -f, or ``None``.

    Returns:
        ``(provider, resource, name)`` of each target, where provider is
        'org/provider'.

    Raises:
        click.UsageError: If both or neither of a resource ID and -f are given.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` for invalid YAML or an
            invalid resource document in -f.
    """  # noqa: DOC502
    if resource_id and files:
        raise click.UsageError("Pass either a resource ID or -f, not both.")

    if files:
        return read_resource_documents(files, resolve_project(context))

    if resource_id:
        return [parse_resource_id(resource_id)]

    raise click.UsageError("Provide either -f <file> or <org/provider/resource/name>.")


def read_resource_documents(files: list[typer.FileText], project_id: str) -> list[tuple[str, str, str]]:
    """Parse the addressing fields of every resource document across the supplied files.

    Every file is read and checked before the caller acts on any document,
    so an invalid document anywhere leaves every resource untouched. Empty
    documents are skipped.

    Args:
        files: YAML files supplied on the command line.
        project_id: Active project; a document declaring another
            ``project_id`` is invalid.

    Returns:
        ``(provider, resource, name)`` of each document, in file order,
        where provider is 'org/provider'.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE``, after listing every
            problem, if a file is not UTF-8 text or valid YAML, or a document
            is not a mapping with a provider, resource and name, or declares
            a ``project_id`` other than the active project.
    """
    addresses: list[tuple[str, str, str]] = []
    problems: list[str] = []

    for f in files:
        try:
            documents = list(yaml.safe_load_all(f.read()))
        except yaml.YAMLError as e:
            problems.append(f"{f.name}: invalid YAML: {e}")
            continue
        except UnicodeDecodeError as e:
            problems.append(f"{f.name}: not UTF-8 text: {e}")
            continue

        for index, document in enumerate(documents):
            if document is None:
                continue

            location = f"{f.name} (document {index + 1})"

            if not isinstance(document, dict):
                problems.append(f"{location}: expected a mapping at the document root")
                continue

            provider = document.get("provider")
            resource_type = document.get("resource")
            name = document.get("name")

            if not (
                isinstance(provider, str)
                and provider
                and isinstance(resource_type, str)
                and resource_type
                and isinstance(name, str)
                and name
            ):
                problems.append(f"{location}: missing provider, resource, or name")
                continue

            declared_project_id = document.get("project_id")

            if declared_project_id is not None and declared_project_id != project_id:
                problems.append(f"{location}: {ProjectMismatchError(project_id, declared_project_id)}")
                continue

            addresses.append((provider, resource_type, name))

    if problems:
        error_console.print("[red]Error:[/red] Invalid resource documents; no resource was changed.")

        for problem in problems:
            error_console.print(f"  [red]-[/red] {escape(problem)}")

        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    return addresses


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
        httpx.HTTPStatusError: If the API refuses the read, a 404 for a
            missing resource included.
    """  # noqa: DOC502
    project = _project_client(ctx)
    provider, resource, name = parse_resource_id(resource_id)

    data = project.get_resource(provider=provider, resource=resource, name=name)
    return provider, resource, name, data


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
        httpx.HTTPStatusError: If the API refuses the update.
    """  # noqa: DOC502
    project_id = resolve_project(ctx)
    project = get_client().project(project_id)

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


@tags_app.command("list")
def tags_list(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
):
    """List tags for a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Examples:
        pragma resources tags list pragmatiks/gcp/secret/my-secret
    """
    _, _, _, res = _fetch_resource(ctx, resource_id)
    tags = res.get("tags") or []

    if not tags:
        console.print("[dim]No tags.[/dim]")
        return

    for tag in tags:
        console.print(f"  {escape(tag)}")


@tags_app.command("add")
def tags_add(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    tags: Annotated[list[str], typer.Option("--tag", help="Tag to add (can be repeated)")],
):
    """Add tags to a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Examples:
        pragma resources tags add pragmatiks/gcp/secret/my-secret --tag production
        pragma resources tags add pragmatiks/gcp/secret/my-secret --tag prod --tag api

    \f

    Raises:
        click.UsageError: If no --tag is given.
    """
    if not tags:
        raise click.UsageError("At least one --tag is required.")

    provider, resource, name, res = _fetch_resource(ctx, resource_id)
    current_tags = set(res.get("tags") or [])
    new_tags = set(tags)
    added = new_tags - current_tags

    if not added:
        console.print("[dim]Tags already present, nothing to add.[/dim]")
        return

    _apply_tags(ctx, provider, resource, name, sorted(current_tags | new_tags))

    for tag in sorted(added):
        console.print(f"[green]+[/green] {escape(tag)}")


@tags_app.command("remove")
def tags_remove(
    ctx: typer.Context,
    resource_id: ResourceIdArgument[str],
    tags: Annotated[list[str], typer.Option("--tag", help="Tag to remove (can be repeated)")],
):
    """Remove tags from a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Examples:
        pragma resources tags remove pragmatiks/gcp/secret/my-secret --tag staging
        pragma resources tags remove pragmatiks/gcp/secret/my-secret --tag old --tag deprecated

    \f

    Raises:
        click.UsageError: If no --tag is given.
    """
    if not tags:
        raise click.UsageError("At least one --tag is required.")

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
        console.print(f"[red]-[/red] {escape(tag)}")
