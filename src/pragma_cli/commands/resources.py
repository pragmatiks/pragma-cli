"""CLI commands for resource management with lifecycle operations."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, NoReturn, TypeVar, cast

import click
import httpx
import jsonschema
import typer
import yaml
from pragma_sdk import LifecycleState, ProjectMismatchError, ProjectResources, ResourceFailedError, TeardownImpact
from pydantic import BaseModel, ConfigDict, ValidationError
from rich import print
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_resource_ids
from pragma_cli.errors import check_bootstrap_error, error_console, format_request_failure, print_api_error
from pragma_cli.exit_codes import FAILURE_EXIT_CODE, INPUT_ERROR_EXIT_CODE, compute_http_exit_code
from pragma_cli.helpers import OutputFormat, format_optional_value, output_data, parse_resource_id
from pragma_cli.project_context import resolve_project
from pragma_cli.teardown import DEFAULT_WAIT_TIMEOUT_SECONDS, TeardownOptions, print_impact, watch_teardown


console = Console()
app = typer.Typer()


_MAX_FILE_REFERENCE_SIZE = 10 * 1024 * 1024


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
        The resource ID unchanged.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if the
            ID does not have four non-empty segments.
    """
    if context.resilient_parsing or resource_id is None:
        return resource_id

    try:
        parse_resource_id(resource_id)
    except ValueError as e:
        raise typer.BadParameter(f"must be 'org/provider/resource/name', got '{resource_id}'.") from e

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
    """Render an apply or upload failure that is not an API error response into one line.

    Args:
        error: Transport error, other httpx error, or project-scoping
            mismatch raised by a resource apply or file upload.

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


def _describe_file_type(mode: int) -> str:
    """Describe the file type of a stat ``st_mode`` for error messages.

    Args:
        mode: ``st_mode`` field from a stat result.

    Returns:
        Lowercase short label such as ``"FIFO"``, ``"socket"``,
        ``"directory"``, ``"character device"``, ``"block device"``,
        or ``"unknown"`` when the type does not match a known kind.
    """
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISCHR(mode):
        return "character device"
    if stat.S_ISBLK(mode):
        return "block device"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "unknown"


def _open_and_read_file_reference(path_str: str, base_dir: Path) -> tuple[Path, bytes]:
    """Open and read a ``@path`` reference atomically through one fd.

    Security model: ``@path`` references in manifests are a data-flow
    from a potentially untrusted YAML author to the local filesystem.
    An attacker who can influence a manifest could otherwise exfiltrate
    local secrets (``~/.ssh/id_rsa``, ``~/.aws/credentials``, etc.) by
    pointing ``@path`` at them — the CLI would read the bytes during
    planning and upload them during apply. To block that class of
    attack, this loader enforces seven rules:

    1. Absolute paths are rejected.
    2. Raw ``..`` segments in the path string are rejected.
    3. The resolved path must land inside ``base_dir``.
    4. Symlinks are followed before the containment check so symlink
       escapes (``base_dir/link`` -> ``/etc/passwd``) are rejected.
    5. The file is opened with ``O_NOFOLLOW`` and validated via
       ``fstat`` on the same descriptor — type and size checks happen
       on the exact bytes that are about to be read, closing the
       stat-to-open TOCTOU race where an attacker swaps the file
       between the planner's check and the planner's read.
    6. Only regular files are accepted — FIFOs, sockets, devices, and
       directories would hang or mis-upload the planner.
    7. Files larger than ``_MAX_FILE_REFERENCE_SIZE`` are rejected so
       a manifest cannot OOM the planner by pointing at a huge blob.
       The size is checked twice — once against ``st_size`` at open
       time and once against the running byte count in the read
       loop — because ``fstat`` returns the file size as of the open
       call, and a file that grows during the read would otherwise
       blow past the cap on the back of kernel EOF alone.

    ``~`` expansion is intentionally NOT performed: a tilde should not
    map to a real path during manifest loading.

    Args:
        path_str: Path string (without ``@`` prefix).
        base_dir: Base directory for the manifest; resolved file must
            stay inside it.

    Returns:
        Tuple of ``(resolved_path, file_bytes)``. ``resolved_path`` is
        the absolute, realpath-resolved path inside ``base_dir`` and
        is suitable for display in error messages. ``file_bytes`` is
        the raw file contents read from the validated descriptor.

    Raises:
        ValueError: If the path is absolute, contains ``..``, resolves
            outside ``base_dir`` (directly or via symlink), is not a
            regular file, exceeds the size limit, or cannot be opened.
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

    open_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(resolved, open_flags)
    except OSError as e:
        raise ValueError(f"Cannot open @path reference {path_str!r}: {e}") from e

    try:
        try:
            st = os.fstat(fd)
        except OSError as e:
            raise ValueError(f"Cannot stat @path reference {path_str!r}: {e}") from e

        if not stat.S_ISREG(st.st_mode):
            raise ValueError(
                f"@path reference {path_str!r} is not a regular file "
                f"(detected {_describe_file_type(st.st_mode)}); "
                "FIFOs, sockets, devices, and directories are refused."
            )

        if st.st_size > _MAX_FILE_REFERENCE_SIZE:
            size_mib = st.st_size / (1024 * 1024)
            limit_mib = _MAX_FILE_REFERENCE_SIZE / (1024 * 1024)
            raise ValueError(
                f"@path reference {path_str!r} is {size_mib:.2f} MiB "
                f"({st.st_size} bytes), exceeding the "
                f"{limit_mib:.0f} MiB ({_MAX_FILE_REFERENCE_SIZE} bytes) limit."
            )

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                raise ValueError(f"Cannot read @path reference {path_str!r}: {e}") from e
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_FILE_REFERENCE_SIZE:
                limit_mib = _MAX_FILE_REFERENCE_SIZE / (1024 * 1024)
                raise ValueError(
                    f"@path reference {path_str!r} grew during read past the "
                    f"{limit_mib:.0f} MiB limit; refusing to upload."
                )
            chunks.append(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)

    return resolved, data


def _plan_file_upload(resource: dict, base_dir: Path) -> tuple[dict, _PendingUpload]:
    """Read a pragma/file resource's ``@path`` content into memory.

    Callers must ensure the resource has ``config.content`` starting
    with ``@``. ``_open_and_read_file_reference`` rejects anything
    that would escape ``base_dir`` (absolute paths, ``..`` traversal,
    symlink escapes) and atomically validates the file type and size
    against the same descriptor it reads from. No network calls are
    made.

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

    _, file_content = _open_and_read_file_reference(content[1:], base_dir)

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
        _, file_bytes = _open_and_read_file_reference(path_str, base_dir)
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
        description = resource_type.get("description")
        table.add_row(
            escape(resource_type["provider"]),
            escape(resource_type["resource"]),
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
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t", help="Filter by tags")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List resources in the active project.

    Requires a project context. Precedence: ``--project`` flag,
    ``PRAGMA_PROJECT`` env var, then the persistent default set via
    ``pragma projects use <project-id>``. Results can be filtered by
    provider, resource type, or tags.

    Examples:
        pragma resources list
        pragma --project my-app resources list
        pragma resources list --provider gcp
        pragma resources list -o json
    """
    project = _project_client(ctx)
    resources = project.list_resources(provider=provider, resource=resource, tags=tags)
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
        state = _format_state_color(res["lifecycle_state"])
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
        "deleted": "dim",
    }
    color = state_colors.get(state.lower(), "white")
    return f"[{color}]{escape(state)}[/{color}]"


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

    table.add_row("State", _format_state_color(res["lifecycle_state"]))

    if res.get("error"):
        table.add_row("Error", f"[red]{escape(res['error'])}[/red]")

    if res.get("created_at"):
        table.add_row("Created", escape(res["created_at"]))
    if res.get("updated_at"):
        table.add_row("Updated", escape(res["updated_at"]))

    console.print(table)

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
    Displays the resource's config, outputs, dependencies, and error
    messages. Sensitive fields are redacted by default; use --reveal
    to show their values.

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
        project_id: Resolved project ID for the active command.
        draft: When False, injects ``lifecycle_state=pending``.

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

    _execute_plan(plan, project_id)


def _execute_plan(plan: _ApplyPlan, project_id: str) -> None:
    """Apply a pre-validated plan with partial-failure containment.

    Ordering strategy: apply every resource first, then upload file
    bytes for ``pragma/file`` resources afterwards. This keeps a
    failing mid-batch apply from leaking uploaded secrets to the
    server. If an apply or upload fails mid-flight — whether through
    an HTTP error, a transport error (connect/timeout/read/write),
    or a project-scoping mismatch — the function prints a loud
    ``PARTIAL APPLY FAILURE`` report listing what did and did not
    complete, then exits with a non-zero status.

    Args:
        plan: Plan returned by ``_plan_apply_batch``.
        project_id: Resolved project ID.

    Raises:
        typer.Exit: On any apply or upload failure, after reporting which
            resources were touched and which were left in partial state;
            with ``NOT_FOUND_EXIT_CODE`` when the API answered 404, else
            with ``FAILURE_EXIT_CODE``.
    """  # noqa: DOC502
    client = get_client()
    project = client.project(project_id)

    applied: list[tuple[str, str]] = []
    uploaded: list[str] = []
    pending_uploads: list[_PlannedResource] = []

    for planned in plan.resources:
        try:
            result = project.apply_resource(cast(Any, planned.payload))
        except (httpx.HTTPError, ProjectMismatchError) as e:
            heading = f"Error applying {planned.resource_id}"
            report_apply_failure(e, heading, plan, applied, uploaded, failed_resource_id=planned.resource_id)

        applied_id = f"{result['provider']}/{result['resource']}/{result['name']}"
        applied.append((planned.resource_id, result["lifecycle_state"]))
        print(f"Applied {escape(applied_id)} {format_state(result['lifecycle_state'])}")

        if planned.upload is not None:
            pending_uploads.append(planned)

    for planned in pending_uploads:
        upload = planned.upload

        if upload is None:
            continue

        try:
            client.upload_file(upload.name, upload.content, upload.content_type)
        except (httpx.HTTPError, ProjectMismatchError) as e:
            heading = f"Error uploading file for {planned.resource_id}"
            report_apply_failure(e, heading, plan, applied, uploaded, failed_resource_id=planned.resource_id)

        uploaded.append(planned.resource_id)

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
    """Print why an apply or upload failed mid-batch, with what the batch left behind, then exit.

    A 503 saying the caller's organization is not set up yet prints the
    set-up message instead, without the batch report.

    Args:
        error: The error the apply or upload raised.
        heading: What failed, such as ``Error applying acme/db/database/main``.
        plan: Full plan that was being executed.
        applied: ``(resource_id, lifecycle_state)`` pairs applied so far.
        uploaded: Resource IDs whose file content was uploaded so far.
        failed_resource_id: Resource ID whose apply or upload failed.

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
    can reconcile manually. The failed resource is reported as a
    dedicated ``Failed`` section so it is visually distinct from the
    rest of the untouched batch listed under ``Not attempted``.

    Args:
        plan: Plan that was executing when the failure occurred.
        applied: Resources that were successfully applied, as a list
            of ``(resource_id, lifecycle_state)`` tuples.
        uploaded: Resource IDs whose file bytes were uploaded.
        failed_resource_id: Resource ID whose apply or upload failed.
    """
    all_ids = [p.resource_id for p in plan.resources]
    applied_ids = {rid for rid, _ in applied}
    uploaded_ids = set(uploaded)

    not_attempted = [rid for rid in all_ids if rid not in applied_ids and rid != failed_resource_id]
    orphan_uploads = [rid for rid in applied_ids if rid not in uploaded_ids and _needs_upload(plan, rid)]

    error_console.print()
    error_console.print("[red bold]PARTIAL APPLY FAILURE[/red bold]")
    error_console.print(
        f"[red]The batch failed at [bold]{escape(failed_resource_id)}[/bold]. "
        "Some resources are already applied and may need manual reconciliation.[/red]"
    )

    error_console.print()
    error_console.print(f"[bold]Applied ({len(applied)}):[/bold]")
    if applied:
        for rid, state in applied:
            error_console.print(f"  [green]+[/green] {escape(rid)} ({escape(state)})")
    else:
        error_console.print("  [dim](none)[/dim]")

    error_console.print()
    error_console.print("[bold]Failed (1):[/bold]")
    error_console.print(f"  [red]x[/red] {escape(failed_resource_id)}")

    error_console.print()
    error_console.print(f"[bold]Not attempted ({len(not_attempted)}):[/bold]")
    if not_attempted:
        for rid in not_attempted:
            error_console.print(f"  [dim]-[/dim] {escape(rid)}")
    else:
        error_console.print("  [dim](none)[/dim]")

    if orphan_uploads:
        error_console.print()
        error_console.print("[yellow bold]Orphaned pragma/file applies without uploaded bytes:[/yellow bold]")
        for rid in orphan_uploads:
            error_console.print(f"  [yellow]![/yellow] {escape(rid)}")
        error_console.print("  [yellow]These resources reference file content that was never uploaded.[/yellow]")
        error_console.print("  [yellow]Re-run apply once the underlying issue is resolved.[/yellow]")

    error_console.print()


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
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to add (can be repeated)")],
):
    """Add tags to a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Examples:
        pragma resources tags add pragmatiks/gcp/secret/my-secret --tag production
        pragma resources tags add pragmatiks/gcp/secret/my-secret -t prod -t api

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
    tags: Annotated[list[str], typer.Option("--tag", "-t", help="Tag to remove (can be repeated)")],
):
    """Remove tags from a resource.

    Resolves the active project from ``--project``, ``PRAGMA_PROJECT``,
    or the persistent default set via ``pragma projects use <project-id>``.

    Examples:
        pragma resources tags remove pragmatiks/gcp/secret/my-secret --tag staging
        pragma resources tags remove pragmatiks/gcp/secret/my-secret -t old -t deprecated

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
