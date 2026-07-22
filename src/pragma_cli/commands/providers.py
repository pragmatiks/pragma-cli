"""Provider management commands.

Unified commands for registering, installing, deploying, and managing
Pragmatiks providers.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated, Any

import copier
import httpx
import typer
import yaml
from pragma_sdk import (
    DeploymentResult,
    DeploymentStatus,
    PragmaClient,
    ProviderVersionConflictError,
    ProviderVersionMetadata,
)
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.bootstrap_errors import check_bootstrap_error
from pragma_cli.commands.completions import completion_provider_ids
from pragma_cli.helpers import OutputFormat, output_data


app = typer.Typer(help="Provider management commands")
console = Console()

DEFAULT_TEMPLATE_URL = "gh:pragmatiks/pragma-providers"
TEMPLATE_PATH_ENV = "PRAGMA_PROVIDER_TEMPLATE"


def get_template_source() -> str:
    """Get the template source path or URL.

    Priority:
    1. PRAGMA_PROVIDER_TEMPLATE environment variable
    2. Local development path (if running from repo)
    3. Default GitHub URL

    Returns:
        Template path (local) or URL (GitHub).
    """
    if env_template := os.environ.get(TEMPLATE_PATH_ENV):
        return env_template

    local_template = Path(__file__).parents[4] / "pragma-providers"

    if local_template.exists() and (local_template / "copier.yml").exists():
        return str(local_template)

    return DEFAULT_TEMPLATE_URL


def _require_pragma_string(pragma: dict[str, Any], key: str) -> str:
    """Read a required string field from [tool.pragma].

    Args:
        pragma: Parsed [tool.pragma] table.
        key: Field name.

    Returns:
        Stripped non-empty string value.

    Raises:
        typer.Exit: If the field is missing, not a string, or blank.
    """
    if key not in pragma:
        console.print(f"[red]Error:[/red] Missing [tool.pragma].{key} in pyproject.toml.")
        raise typer.Exit(1)

    value = pragma[key]

    if not isinstance(value, str) or not value.strip():
        console.print(
            f"[red]Error:[/red] [tool.pragma].{key} must be a non-empty string, got {type(value).__name__}: {value!r}"
        )
        raise typer.Exit(1)

    return value.strip()


def _optional_pragma_string(pragma: dict[str, Any], key: str) -> str | None:
    """Read an optional string field from [tool.pragma].

    Args:
        pragma: Parsed [tool.pragma] table.
        key: Field name.

    Returns:
        Stripped non-empty string value, or ``None`` when the key is absent.

    Raises:
        typer.Exit: If the key is set but is not a non-empty string.
    """
    if key not in pragma:
        return None

    value = pragma[key]

    if not isinstance(value, str) or not value.strip():
        console.print(
            f"[red]Error:[/red] [tool.pragma].{key} must be a non-empty string when set, "
            f"got {type(value).__name__}: {value!r}"
        )
        raise typer.Exit(1)

    return value.strip()


def _read_provider_metadata(pyproject_path: Path) -> tuple[str, str, ProviderVersionMetadata]:
    """Read provider identity and catalog metadata from pyproject.toml.

    Reads ``[tool.pragma]``: ``provider`` (short name, no slashes),
    ``package`` (importable Python package), ``display_name``,
    ``description``, optional ``icon_url``, optional ``tags``.

    Args:
        pyproject_path: Path to the provider's pyproject.toml.

    Returns:
        Tuple of (provider short name, package name, catalog metadata).

    Raises:
        typer.Exit: If the file is missing, malformed, or any required
            field under [tool.pragma] is missing or invalid.
    """
    if not pyproject_path.exists():
        console.print(f"[red]Error:[/red] pyproject.toml not found: {pyproject_path}")
        raise typer.Exit(1)

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        console.print(f"[red]Error:[/red] Failed to parse {pyproject_path}: {e}")
        raise typer.Exit(1) from e

    pragma = data.get("tool", {}).get("pragma", {})

    if not pragma:
        console.print(
            f"[red]Error:[/red] Missing [tool.pragma] section in {pyproject_path}.\n"
            "Required fields: provider, package, display_name, description."
        )
        raise typer.Exit(1)

    provider = _require_pragma_string(pragma, "provider")

    if "/" in provider:
        console.print(
            "[red]Error:[/red] [tool.pragma].provider must not contain slashes (the org prefix is added automatically)."
        )
        raise typer.Exit(1)

    package = _require_pragma_string(pragma, "package")
    display_name = _require_pragma_string(pragma, "display_name")
    description = _require_pragma_string(pragma, "description")

    icon_url = _optional_pragma_string(pragma, "icon_url")

    tags = pragma.get("tags", [])

    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        console.print("[red]Error:[/red] [tool.pragma].tags must be a list of strings.")
        raise typer.Exit(1)

    metadata = ProviderVersionMetadata(
        display_name=display_name,
        description=description,
        icon_url=icon_url,
        tags=[str(tag) for tag in tags],
    )

    return provider, package, metadata


_SCHEMA_EXTRACTION_SCRIPT = (
    "import json, sys\n"
    "from pragma_sdk.provider import load_provider_schemas\n"
    "package_name, catalog_name, output_path = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "with open(output_path, 'w', encoding='utf-8') as output_file:\n"
    "    json.dump(load_provider_schemas(package_name, catalog_name), output_file)\n"
)


def require_subprocess_success(result: subprocess.CompletedProcess[str], failure_message: str) -> None:
    """Exit with the subprocess stderr when a tool invocation fails.

    Args:
        result: Completed subprocess whose return code to check.
        failure_message: Human-readable description of the failed step.

    Raises:
        typer.Exit: If the subprocess exited non-zero.
    """
    if result.returncode != 0:
        console.print(f"[red]Error:[/red] {failure_message}:\n{result.stderr}")
        raise typer.Exit(1)


def _extract_schemas_dict(
    provider_directory: Path,
    package_name: str,
    catalog_name: str,
) -> dict[str, dict[str, Any]]:
    """Extract resource schemas in the provider's own environment and reshape for the API.

    Runs schema extraction as a subprocess under the provider project's
    uv-managed environment (``uv run`` with the provider directory as working
    directory), so the CLI never needs the provider's runtime dependencies
    installed. The subprocess writes JSON to a temporary file rather than
    stdout, keeping the payload free of any output the provider or SDK might
    print while importing.

    Args:
        provider_directory: Provider project root containing pyproject.toml.
        package_name: Importable package name (e.g. ``postgres_provider``).
        catalog_name: Namespaced provider name (``org/short``).

    Returns:
        Mapping of resource name to ``ResourceSchemaResponse``-shaped dict.
    """
    with tempfile.TemporaryDirectory() as temporary_directory:
        output_path = Path(temporary_directory) / "schemas.json"

        result = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-c",
                _SCHEMA_EXTRACTION_SCRIPT,
                package_name,
                catalog_name,
                str(output_path),
            ],
            cwd=provider_directory,
            capture_output=True,
            text=True,
        )

        require_subprocess_success(result, f"Schema extraction failed for package '{package_name}'")
        entries = json.loads(output_path.read_text(encoding="utf-8"))

    return {
        entry["resource"]: {
            "provider": entry["provider"],
            "resource": entry["resource"],
            "description": entry.get("description"),
            "config_schema": entry.get("config_schema"),
            "outputs_schema": entry.get("outputs_schema"),
        }
        for entry in entries
    }


def parse_wheel_version(wheel_path: Path) -> str:
    """Extract the version encoded in a wheel filename (PEP 427).

    Args:
        wheel_path: Path to a ``.whl`` file.

    Returns:
        The version component of the filename.

    Raises:
        typer.Exit: If the filename is not a valid wheel filename.
    """
    parts = wheel_path.name.removesuffix(".whl").split("-")

    if not wheel_path.name.endswith(".whl") or len(parts) < 5:
        console.print(f"[red]Error:[/red] Not a valid wheel filename: {wheel_path.name}")
        raise typer.Exit(1)

    return parts[1]


def _build_wheel(project_dir: Path) -> Path:
    """Build the provider wheel with ``uv build`` and return its path.

    Args:
        project_dir: Provider project directory containing pyproject.toml.

    Returns:
        Path to the freshly built wheel in ``dist/``.

    Raises:
        typer.Exit: If the build fails or produces no wheel.
    """
    console.print("[dim]Building wheel with 'uv build'...[/dim]")

    result = subprocess.run(["uv", "build", "--wheel"], cwd=project_dir, capture_output=True, text=True)

    require_subprocess_success(result, "uv build failed")

    wheels = sorted((project_dir / "dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)

    if not wheels:
        console.print("[red]Error:[/red] uv build produced no wheel in dist/.")
        raise typer.Exit(1)

    return wheels[-1].resolve()


def _read_changelog(path: Path | None) -> str | None:
    """Read changelog text from a file path, returning ``None`` when not supplied.

    Args:
        path: Path to a UTF-8 text file, or ``None``.

    Returns:
        The file's text content, or ``None`` if no path was given.

    Raises:
        typer.Exit: If the file is missing or cannot be read.
    """
    if path is None:
        return None

    if not path.exists():
        console.print(f"[red]Error:[/red] Changelog file not found: {path}")
        raise typer.Exit(1)

    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as e:
        console.print(f"[red]Error:[/red] Could not read changelog '{path}': {e}")
        raise typer.Exit(1) from e


def _require_auth(client: PragmaClient) -> None:
    """Verify the client is authenticated, exit with error if not.

    Args:
        client: SDK client instance.

    Raises:
        typer.Exit: If authentication is missing.
    """
    if client._auth is None:
        console.print("[red]Error:[/red] Authentication required. Run 'pragma auth login' first.")
        raise typer.Exit(1)


def _fetch_with_spinner(description: str, fetch_fn) -> Any:
    """Execute a function with a spinner progress indicator.

    Args:
        description: Text to display next to the spinner.
        fetch_fn: Zero-argument callable to execute.

    Returns:
        Result from fetch_fn.
    """
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task(description, total=None)
        return fetch_fn()


def _format_api_error(error: httpx.HTTPStatusError) -> str:
    """Extract a human-readable message from an HTTP error response.

    Args:
        error: The HTTP status error from httpx.

    Returns:
        Formatted error message string.
    """
    try:
        detail = error.response.json().get("detail", error.response.text)
    except Exception:
        return error.response.text or str(error)

    if isinstance(detail, str):
        return detail

    if isinstance(detail, dict):
        return detail.get("message", str(error))

    return str(detail)


def _format_deployment_status(status: DeploymentStatus | None) -> str:
    """Format deployment status with color coding.

    Args:
        status: Deployment status or None if not deployed.

    Returns:
        Formatted status string with Rich markup.
    """
    if status is None:
        return "[dim]not deployed[/dim]"

    match status:
        case DeploymentStatus.AVAILABLE:
            return "[green]running[/green]"
        case DeploymentStatus.PROGRESSING:
            return "[yellow]deploying[/yellow]"
        case DeploymentStatus.PENDING:
            return "[yellow]pending[/yellow]"
        case DeploymentStatus.FAILED:
            return "[red]failed[/red]"
        case _:
            return f"[dim]{status}[/dim]"


def _format_version_status(status: str) -> str:
    """Format a version build status with Rich color markup.

    Args:
        status: Version status string.

    Returns:
        Formatted string with Rich markup.
    """
    status_colors = {
        "published": "green",
        "building": "yellow",
        "failed": "red",
        "yanked": "dim",
    }
    color = status_colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


@app.command()
def init(
    name: Annotated[str, typer.Argument(help="Provider name (e.g., 'postgres', 'mycompany')")],
    output_dir: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output directory (default: ./{name}-provider)"),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="Provider description"),
    ] = None,
    author_name: Annotated[
        str | None,
        typer.Option("--author", help="Author name"),
    ] = None,
    author_email: Annotated[
        str | None,
        typer.Option("--email", help="Author email"),
    ] = None,
    defaults: Annotated[
        bool,
        typer.Option("--defaults", help="Accept all defaults without prompting"),
    ] = False,
):
    """Initialize a new provider project.

    Creates a complete provider project structure with:
    - pyproject.toml for packaging
    - README.md with documentation
    - src/{name}_provider/ with example resources
    - tests/ with example tests
    - mise.toml for tool management

    Example:
        pragma providers init mycompany
        pragma providers init postgres --output ./providers/postgres
        pragma providers init mycompany --defaults --description "My provider"

    Raises:
        typer.Exit: If directory already exists or template copy fails.
    """
    project_dir = output_dir or Path(f"./{name}-provider")

    if project_dir.exists():
        typer.echo(f"Error: Directory {project_dir} already exists", err=True)
        raise typer.Exit(1)

    template_source = get_template_source()

    data = {"name": name}

    if description:
        data["description"] = description

    if author_name:
        data["author_name"] = author_name

    if author_email:
        data["author_email"] = author_email

    typer.echo(f"Creating provider project: {project_dir}")
    typer.echo(f"  Template: {template_source}")
    typer.echo("")

    try:
        vcs_ref = "HEAD" if not template_source.startswith("gh:") else None
        copier.run_copy(
            src_path=template_source,
            dst_path=project_dir,
            data=data,
            defaults=defaults,
            unsafe=True,
            vcs_ref=vcs_ref,
        )
    except Exception as e:
        typer.echo(f"Error creating provider: {e}", err=True)
        raise typer.Exit(1) from e

    package_name = name.lower().replace("-", "_").replace(" ", "_") + "_provider"

    typer.echo("")
    typer.echo(f"Created provider project: {project_dir}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {project_dir}")
    typer.echo("  uv sync --dev")
    typer.echo("  uv run pytest tests/")
    typer.echo("")
    typer.echo(f"Edit src/{package_name}/resources/ to add your resources.")
    typer.echo("")
    typer.echo("To update this project when the template changes:")
    typer.echo("  copier update")
    typer.echo("")
    typer.echo("When ready to publish a version:")
    typer.echo("  pragma providers publish")


@app.command()
def update(
    project_dir: Annotated[
        Path,
        typer.Argument(help="Provider project directory"),
    ] = Path("."),
):
    """Update an existing provider project with latest template changes.

    Uses Copier's 3-way merge to preserve your customizations while
    incorporating template updates.

    Example:
        pragma providers update
        pragma providers update ./my-provider

    Raises:
        typer.Exit: If directory is not a Copier project or update fails.
    """
    answers_file = project_dir / ".copier-answers.yml"

    if not answers_file.exists():
        typer.echo(f"Error: {project_dir} is not a Copier-generated project", err=True)
        typer.echo("(missing .copier-answers.yml)", err=True)
        raise typer.Exit(1)

    typer.echo(f"Updating provider project: {project_dir}")
    typer.echo("")

    try:
        copier.run_update(dst_path=project_dir, unsafe=True)
    except Exception as e:
        typer.echo(f"Error updating provider: {e}", err=True)
        raise typer.Exit(1) from e

    typer.echo("")
    typer.echo("Provider project updated successfully.")


def _print_publish_error(error: httpx.HTTPStatusError, catalog_name: str) -> None:
    """Print an actionable message for a failed publish request.

    Args:
        error: The HTTP status error from the publish request.
        catalog_name: Namespaced provider name (``org/short``).
    """
    if error.response.status_code == 403:
        namespace = catalog_name.partition("/")[0]
        console.print(f"[red]Error:[/red] Your organization does not own the '{namespace}' provider namespace.")
    else:
        console.print(f"[red]Error:[/red] {_format_api_error(error)}")


@app.command()
def publish(
    project_dir: Annotated[
        Path,
        typer.Argument(help="Provider project directory"),
    ] = Path("."),
    wheel: Annotated[
        Path | None,
        typer.Option("--wheel", help="Prebuilt .whl to upload (skips 'uv build')"),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--version", help="Version to declare (default: parsed from the wheel filename)"),
    ] = None,
    changelog: Annotated[
        Path | None,
        typer.Option("--changelog", help="Path to a UTF-8 text file with release notes"),
    ] = None,
):
    r"""Publish a new provider version by uploading its wheel to Pragmatiks.

    Builds the wheel with ``uv build`` (or takes a prebuilt one via
    ``--wheel``) and uploads the bytes to the API, which hosts the wheel
    in its own registry and computes the SHA-256 server-side. No external
    registry or wheel URL is involved.

    Reads from the ``\[tool.pragma]`` table in the project's pyproject.toml:
    ``provider`` (short name), ``package`` (importable Python package),
    ``display_name``, ``description``, optional ``icon_url``, optional
    ``tags``. Resource schemas are extracted from the importable provider
    package. The publishing organization is resolved from the
    authenticated user.

    Examples:
        pragma providers publish
        pragma providers publish ./my-provider --changelog NOTES.md
        pragma providers publish --wheel dist/my_provider-1.0.0-py3-none-any.whl

    Raises:
        typer.Exit: If the pyproject is missing or invalid, the build or
            schema extraction fails, or the API rejects the publish.
    """
    pyproject_path = (project_dir / "pyproject.toml").resolve()
    provider_short, package_name, metadata = _read_provider_metadata(pyproject_path)

    wheel_path = wheel.resolve() if wheel else _build_wheel(project_dir)

    if not wheel_path.exists():
        console.print(f"[red]Error:[/red] Wheel not found: {wheel_path}")
        raise typer.Exit(1)

    wheel_version = parse_wheel_version(wheel_path)

    if version is not None and version != wheel_version:
        console.print(
            f"[red]Error:[/red] Declared version '{version}' does not match "
            f"the wheel's version '{wheel_version}' ({wheel_path.name})."
        )
        raise typer.Exit(1)

    declared_version = version or wheel_version

    client = get_client()
    _require_auth(client)

    try:
        organization = client.get_current_organization()
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 401:
            console.print("[red]Error:[/red] Not authenticated. Run 'pragma auth login' first.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    catalog_name = f"{organization.slug}/{provider_short}"

    console.print(f"[bold]Publishing provider:[/bold] {catalog_name} v{declared_version}")
    console.print(f"[dim]Wheel:[/dim] {wheel_path}")
    console.print()

    schemas = _extract_schemas_dict(pyproject_path.parent, package_name, catalog_name)

    changelog_text = _read_changelog(changelog)

    try:
        result = _fetch_with_spinner(
            "Uploading wheel...",
            lambda: client.publish_provider_version(
                wheel_path=wheel_path,
                name=catalog_name,
                version=declared_version,
                schemas=schemas,
                metadata=metadata,
                changelog=changelog_text,
            ),
        )
    except ProviderVersionConflictError as e:
        console.print(
            f"[red]Error:[/red] {catalog_name} v{declared_version} is already published — "
            "bump the version (published versions are immutable)."
        )
        raise typer.Exit(1) from e
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        _print_publish_error(e, catalog_name)
        raise typer.Exit(1) from e

    console.print(f"[green]Published:[/green] {catalog_name} v{result.version} ({result.status.value})")


def _merge_install_config(
    config_flags: list[str] | None,
    config_file_path: str | None,
) -> dict[str, str] | None:
    """Merge config from --config-file and --config flags.

    File values are loaded first, then individual flags override.
    Returns None if no config is provided.
    """  # noqa: DOC201, DOC501
    result: dict[str, str] = {}

    if config_file_path is not None:
        path = Path(config_file_path)

        if not path.exists():
            console.print(f"[red]Error:[/red] Config file not found: {config_file_path}")
            raise typer.Exit(1)

        try:
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            console.print(f"[red]Error:[/red] Failed to parse config file '{config_file_path}': {e}")
            raise typer.Exit(1) from e
        except (OSError, UnicodeError) as e:
            console.print(f"[red]Error:[/red] Could not read config file '{config_file_path}': {e}")
            raise typer.Exit(1) from e

        if data is None:
            console.print(f"[red]Error:[/red] Config file is empty: {config_file_path}")
            raise typer.Exit(1)

        if not isinstance(data, dict):
            console.print(f"[red]Error:[/red] Config file must contain a YAML mapping, got {type(data).__name__}")
            raise typer.Exit(1)

        for key, value in data.items():
            if isinstance(value, bool):
                result[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                result[str(key)] = str(value)
            else:
                console.print(
                    f"[red]Error:[/red] Config key '{key}' has unsupported type {type(value).__name__}. "
                    "Only strings, numbers, and booleans are allowed."
                )
                raise typer.Exit(1)

    if config_flags is not None:
        for entry in config_flags:
            if "=" not in entry:
                console.print(f"[red]Error:[/red] Invalid config format '{entry}'. Expected KEY=VALUE.")
                raise typer.Exit(1)

            key, _, value = entry.partition("=")

            if not key:
                console.print(f"[red]Error:[/red] Config key cannot be empty in '{entry}'.")
                raise typer.Exit(1)

            result[key] = value

    return result if result else None


@app.command()
def install(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str | None, typer.Option("--version", "-v", help="Version to install (default: latest)")] = None,
    resource_tier: Annotated[
        str,
        typer.Option("--resource-tier", help="Resource tier (free, standard, performance)"),
    ] = "standard",
    upgrade_policy: Annotated[
        str,
        typer.Option("--upgrade-policy", help="Upgrade policy (manual, auto-minor, auto-patch)"),
    ] = "manual",
    config: Annotated[
        list[str] | None,
        typer.Option("--config", "-c", help="Configuration key=value pair (repeatable)"),
    ] = None,
    config_file: Annotated[
        str | None,
        typer.Option("--config-file", help="Path to YAML file with configuration key-value pairs"),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Install a provider from the store.

    Examples:
        pragma providers install pragmatiks/qdrant
        pragma providers install pragmatiks/postgres --version 1.2.0
        pragma providers install pragmatiks/redis --resource-tier performance --upgrade-policy auto-minor
        pragma providers install pragmatiks/qdrant --config SOME_KEY=some_value
        pragma providers install pragmatiks/qdrant --config-file config.yaml --config OVERRIDE_KEY=value
        pragma providers install pragmatiks/qdrant -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    merged_config = _merge_install_config(config, config_file)

    try:
        detail = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    provider = detail
    display = getattr(provider, "display_name", None) or name
    install_version = version or getattr(provider, "latest_version", "latest")

    console.print(f"[bold]Provider:[/bold] {display} ({name})")
    console.print(f"[bold]Version:[/bold]  {install_version}")
    console.print(f"[bold]Tier:[/bold]     {resource_tier}")

    if merged_config:
        console.print("[bold]Config:[/bold]")
        for key, value in sorted(merged_config.items()):
            console.print(f"  {key} = {value}", markup=False)

    console.print()

    if not yes:
        confirm = typer.confirm("Install this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Installing provider...",
            lambda: client.install_provider(
                name,
                version=version,
                resource_tier=resource_tier,
                upgrade_policy=upgrade_policy,
                config=merged_config,
            ),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already installed.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Installed:[/green] {name} v{result.installed_version}")


@app.command()
def uninstall(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    cascade: Annotated[
        bool,
        typer.Option("--cascade", help="Delete all resources created by this provider"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Uninstall an installed provider.

    Examples:
        pragma providers uninstall pragmatiks/qdrant
        pragma providers uninstall pragmatiks/postgres --cascade
        pragma providers uninstall pragmatiks/redis --yes
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Provider:[/bold] {name}")

    if cascade:
        console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")

    console.print()

    if not yes:
        action = "UNINSTALL provider and delete all its resources" if cascade else "UNINSTALL provider"
        confirm = typer.confirm(f"Are you sure you want to {action}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        _fetch_with_spinner(
            "Uninstalling provider...",
            lambda: client.uninstall_provider(name, cascade=cascade),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1) from e

        if e.response.status_code == 409:
            console.print(f"[red]Error:[/red] Provider '{name}' has active resources.")
            console.print("[dim]Use --cascade to delete all resources with the provider.[/dim]")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Uninstalled:[/green] {name}")


@app.command()
def upgrade(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str | None, typer.Option("--version", "-v", help="Target version (default: latest)")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Upgrade an installed provider to a newer version.

    Examples:
        pragma providers upgrade pragmatiks/qdrant
        pragma providers upgrade pragmatiks/postgres --version 2.0.0
        pragma providers upgrade pragmatiks/redis -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    target = version or "latest"
    console.print(f"[bold]Upgrading:[/bold] {name} -> {target}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Upgrade {name} to v{target}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Upgrading provider...",
            lambda: client.upgrade_provider(name, target_version=version),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1) from e

        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already on the requested version.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Upgraded:[/green] {name} -> v{result.installed_version}")


@app.command()
def downgrade(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    version: Annotated[str, typer.Option("--version", "-v", help="Target version to downgrade to")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Downgrade an installed provider to a previous version.

    Requires an explicit target version. Migrations run sequentially
    through each intermediate version in reverse order.

    Examples:
        pragma providers downgrade pragmatiks/qdrant --version 1.0.0
        pragma providers downgrade pragmatiks/postgres -v 1.2.0 -y
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Downgrading:[/bold] {name} -> v{version}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Downgrade {name} to v{version}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        result = _fetch_with_spinner(
            "Downgrading provider...",
            lambda: client.downgrade_provider(name, target_version=version),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' is not installed.")
            raise typer.Exit(1) from e

        if e.response.status_code == 409:
            console.print(f"[yellow]Warning:[/yellow] Provider '{name}' is already on v{version}.")
            raise typer.Exit(1) from e

        if e.response.status_code == 422:
            console.print(f"[red]Error:[/red] {_format_api_error(e)}")
            console.print("[dim]The version chain between current and target may be broken.[/dim]")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    console.print(f"[green]Downgraded:[/green] {name} -> v{result.installed_version}")


@app.command("list")
def list_providers(
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Filter by scope (public or tenant)"),
    ] = None,
    installed: Annotated[
        bool,
        typer.Option("--installed", help="Show installed providers only"),
    ] = False,
    query: Annotated[
        str | None,
        typer.Option("--query", "-q", help="Search query"),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Filter by tags (comma-separated)"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum number of results"),
    ] = 20,
    offset: Annotated[
        int,
        typer.Option("--offset", help="Offset for pagination"),
    ] = 0,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List providers in the store or installed providers.

    Combines browsing and searching into a single command. Use --installed
    to show only installed providers, or --query to search the catalog.

    Examples:
        pragma providers list
        pragma providers list --installed
        pragma providers list --query postgres
        pragma providers list --scope public --tags ml,vector
        pragma providers list -o json
    """  # noqa: DOC501
    client = get_client()

    if installed:
        _list_installations(client, output)
        return

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    try:
        result = _fetch_with_spinner(
            "Fetching providers...",
            lambda: client.list_providers(
                query=query,
                scope=scope,
                tags=tag_list,
                limit=limit,
                offset=offset,
            ),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if not result.items:
        if query:
            console.print(f"[dim]No providers found matching '{query}'.[/dim]")
        else:
            console.print("[dim]No providers found.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_store_list_table(result)
    else:
        data = [_provider_summary_to_dict(p) for p in result.items]
        output_data(data, output)


def _list_installations(client: PragmaClient, output: OutputFormat) -> None:
    """List provider installations for the current tenant.

    Args:
        client: SDK client instance.
        output: Output format for display.
    """  # noqa: DOC501
    _require_auth(client)

    try:
        providers = _fetch_with_spinner(
            "Fetching installed providers...",
            lambda: client.list_installations(),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    if output == OutputFormat.TABLE:
        _print_installed_table(providers)
    else:
        data = [_installed_provider_to_dict(p) for p in providers]
        output_data(data, output)


@app.command()
def info(
    name: Annotated[str, typer.Argument(help="Provider name (org/name format)")],
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Show detailed information about a provider.

    Displays provider metadata, version history, and installation status.

    Examples:
        pragma providers info pragmatiks/qdrant
        pragma providers info pragmatiks/postgres -o json
    """  # noqa: DOC501
    client = get_client()

    try:
        provider = _fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Provider '{name}' not found in the store.")
            raise typer.Exit(1) from e

        console.print(f"[red]Error:[/red] {_format_api_error(e)}")
        raise typer.Exit(1) from e

    try:
        versions = _fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        versions = []

    if output == OutputFormat.TABLE:
        _print_provider_info(provider, versions)
    else:
        data = _provider_detail_to_dict(provider, versions)
        output_data(data, output)


@app.command()
def deploy(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    version: Annotated[
        str | None,
        typer.Option("--version", "-v", help="Version to deploy (default: latest)"),
    ] = None,
):
    """Deploy a provider to a specific version.

    Deploys the provider to Kubernetes. If no version is specified, deploys
    the latest successful build.

    Deploy latest:
        pragma providers deploy pragmatiks/postgres

    Deploy specific version:
        pragma providers deploy pragmatiks/postgres --version 1.2.0

    Raises:
        typer.Exit: If deployment fails.
    """
    console.print(f"[bold]Deploying provider:[/bold] {provider_id}")

    if version:
        console.print(f"[dim]Version:[/dim] {version}")
    else:
        console.print("[dim]Version:[/dim] latest")

    console.print()

    client = get_client()
    _require_auth(client)

    try:
        deploy_result = _fetch_with_spinner(
            "Deploying...",
            lambda: client.deploy_provider(provider_id, version),
        )
        console.print(f"[green]Deployment started:[/green] {provider_id}")
        console.print(f"[dim]Deployment:[/dim] {deploy_result.deployment_name}")
        console.print(f"[dim]Status:[/dim] {deploy_result.status.value}")
        console.print(f"[dim]Replicas:[/dim] {deploy_result.ready_replicas}/{deploy_result.available_replicas}")

        if deploy_result.image:
            console.print(f"[dim]Image:[/dim] {deploy_result.image}")
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)
        console.print(_format_api_error(e))
        raise typer.Exit(1) from e
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise

        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


@app.command()
def status(
    provider_id: Annotated[
        str,
        typer.Argument(
            help="Provider ID (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Check the deployment status of a provider.

    Displays:
    - Deployment status (pending/progressing/available/failed)
    - Deployed version
    - Health status
    - Last updated timestamp

    Examples:
        pragma providers status pragmatiks/postgres
        pragma providers status pragmatiks/my-provider -o json

    Raises:
        typer.Exit: If deployment not found or status check fails.
    """  # noqa: DOC501
    client = get_client()
    _require_auth(client)

    try:
        result = client.get_deployment_status(provider_id)
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 404:
            console.print(f"[red]Error:[/red] Deployment not found for provider: {provider_id}")
            raise typer.Exit(1) from e

        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e

    if output == OutputFormat.TABLE:
        _print_deployment_status(provider_id, result)
    else:
        data = result.model_dump(mode="json")
        data["provider_id"] = provider_id
        output_data(data, output)


@app.command()
def delete(
    name: Annotated[
        str,
        typer.Argument(
            help="Provider name (org/name format)",
            autocompletion=completion_provider_ids,
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Delete a provider from the store (author only).

    Removes the provider and all its versions from the store catalog.
    This does not uninstall the provider from tenants that have it installed.

    Examples:
        pragma providers delete myorg/my-provider
        pragma providers delete myorg/my-provider --yes

    Raises:
        typer.Exit: If deletion fails or user cancels.
    """
    client = get_client()
    _require_auth(client)

    console.print(f"[bold]Provider:[/bold] {name}")
    console.print("[yellow]Warning:[/yellow] This will permanently delete the provider from the store.")
    console.print()

    if not yes:
        confirm = typer.confirm("Are you sure you want to DELETE this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        _fetch_with_spinner(
            "Deleting provider...",
            lambda: client.delete_provider(name),
        )
        console.print(f"[green]✓[/green] Provider [bold]{name}[/bold] deleted successfully")
    except httpx.HTTPStatusError as e:
        check_bootstrap_error(e)

        if e.response.status_code == 409:
            try:
                detail = e.response.json().get("detail", "Provider has resources")
            except Exception:
                detail = "Provider has resources"
            console.print(f"[red]Error:[/red] {detail}")
        else:
            console.print(f"[red]Error:[/red] {_format_api_error(e)}")

        raise typer.Exit(1) from e
    except Exception as e:
        if isinstance(e, typer.Exit):
            raise

        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1) from e


def _print_deployment_status(provider_id: str, result: DeploymentResult) -> None:
    """Print deployment status in a formatted table.

    Args:
        provider_id: Provider identifier.
        result: DeploymentResult from the API.
    """
    status_colors = {
        "pending": "yellow",
        "progressing": "cyan",
        "available": "green",
        "failed": "red",
    }
    status_color = status_colors.get(result.status.value, "white")

    console.print()
    console.print(f"[bold]Provider:[/bold] {provider_id}")
    console.print()

    table = Table(show_header=True, header_style="bold")
    table.add_column("Property")
    table.add_column("Value")

    table.add_row("Deployment", result.deployment_name)
    table.add_row("Status", f"[{status_color}]{result.status.value}[/{status_color}]")
    table.add_row("Replicas", f"{result.ready_replicas}/{result.available_replicas}")

    if result.version:
        table.add_row("Version", result.version)

    if result.image:
        table.add_row("Image", result.image)

    if result.updated_at:
        table.add_row("Updated", result.updated_at.strftime("%Y-%m-%d %H:%M:%S UTC"))

    if result.message:
        table.add_row("Message", result.message)

    console.print(table)


def _print_store_list_table(result) -> None:
    """Print store providers in a formatted table.

    Args:
        result: Paginated response of store provider summaries.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Name")
    table.add_column("Display Name")
    table.add_column("Author")
    table.add_column("Latest Version")
    table.add_column("Installs", justify="right")
    table.add_column("Tags")

    for provider in result.items:
        tags_display = ", ".join(getattr(provider, "tags", []) or [])
        install_count = getattr(provider, "install_count", 0) or 0
        author = getattr(provider, "author", None)
        author_display = getattr(author, "display_name", None) or "[dim]-[/dim]"

        table.add_row(
            provider.canonical,
            getattr(provider, "display_name", None) or "[dim]-[/dim]",
            author_display,
            getattr(provider, "latest_version", None) or "[dim]-[/dim]",
            str(install_count),
            tags_display or "[dim]-[/dim]",
        )

    console.print(table)

    total = getattr(result, "total", 0)
    offset = getattr(result, "offset", 0)
    showing_end = min(offset + len(result.items), total)
    console.print(f"[dim]Showing {offset + 1}-{showing_end} of {total} providers[/dim]")


def _print_provider_info(provider, versions: list | None = None) -> None:
    """Print detailed provider information in a panel with version table.

    Args:
        provider: Provider metadata object.
        versions: List of provider version objects.
    """
    versions = versions or []

    author = getattr(provider, "author", None)
    author_display = getattr(author, "display_name", None) or "[dim]-[/dim]"
    tags = ", ".join(getattr(provider, "tags", []) or []) or "[dim]-[/dim]"
    install_count = getattr(provider, "install_count", 0) or 0
    description = getattr(provider, "description", None) or "[dim]No description[/dim]"
    created_at = getattr(provider, "created_at", None)
    updated_at = getattr(provider, "updated_at", None)

    info_lines = [
        f"[bold]Name:[/bold]         {provider.canonical}",
        f"[bold]Display Name:[/bold] {getattr(provider, 'display_name', None) or provider.canonical}",
        f"[bold]Author:[/bold]       {author_display}",
        f"[bold]Description:[/bold]  {description}",
        f"[bold]Tags:[/bold]         {tags}",
        f"[bold]Installs:[/bold]     {install_count}",
    ]

    if created_at:
        info_lines.append(f"[bold]Created:[/bold]      {str(created_at)[:19]}")

    if updated_at:
        info_lines.append(f"[bold]Updated:[/bold]      {str(updated_at)[:19]}")

    panel = Panel("\n".join(info_lines), title=provider.canonical, border_style="blue")
    console.print(panel)

    if versions:
        console.print()

        version_table = Table(show_header=True, header_style="bold")
        version_table.add_column("Version")
        version_table.add_column("Status")
        version_table.add_column("Runtime Version")
        version_table.add_column("Published")

        for v in versions:
            v_status = getattr(v, "status", None) or "-"
            status_display = _format_version_status(v_status)
            runtime = getattr(v, "runtime_version", None) or "[dim]-[/dim]"
            published = str(getattr(v, "published_at", None) or "-")[:19]

            version_table.add_row(
                v.version,
                status_display,
                runtime,
                published,
            )

        console.print(version_table)


def _print_installed_table(providers) -> None:
    """Print installed providers in a formatted table.

    Args:
        providers: List of installed provider summaries.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("Version")
    table.add_column("Tier")
    table.add_column("Upgrade Policy")
    table.add_column("Installed At")
    table.add_column("Upgrade Available")

    for p in providers:
        installed_at = str(getattr(p, "installed_at", None) or "-")[:19]
        upgrade_available = getattr(p, "upgrade_available", False)
        latest = getattr(p, "latest_version", None)

        if upgrade_available and latest:
            upgrade_display = f"[green]yes[/green] ({latest})"
        else:
            upgrade_display = "[dim]-[/dim]"

        table.add_row(
            p.canonical,
            p.installed_version,
            getattr(p, "resource_tier", None) or "[dim]-[/dim]",
            getattr(p, "upgrade_policy", None) or "[dim]-[/dim]",
            installed_at,
            upgrade_display,
        )

    console.print(table)


def _serialize_datetime(obj: object, attr: str) -> str | None:
    val = getattr(obj, attr, None)
    return val.isoformat() if val else None


def _author_to_dict(author) -> dict | None:
    """Convert a ProviderAuthor model to a plain dict for JSON/YAML output.

    Args:
        author: ProviderAuthor object or None.

    Returns:
        Dictionary representation, or None if no author.
    """
    if author is None:
        return None

    return {
        "kind": getattr(author, "kind", None),
        "organization_id": getattr(author, "organization_id", None),
        "display_name": getattr(author, "display_name", None),
    }


def _provider_summary_to_dict(provider) -> dict:
    """Convert a store provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Store provider summary object.

    Returns:
        Dictionary representation.
    """
    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": _author_to_dict(getattr(provider, "author", None)),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
    }


def _provider_detail_to_dict(provider, versions: list | None = None) -> dict:
    """Convert a provider and its versions to a plain dict for JSON/YAML output.

    Args:
        provider: Provider metadata object.
        versions: List of provider version objects.

    Returns:
        Dictionary representation.
    """
    versions = versions or []

    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "display_name": getattr(provider, "display_name", None),
        "description": getattr(provider, "description", None),
        "author": _author_to_dict(getattr(provider, "author", None)),
        "tags": getattr(provider, "tags", []),
        "latest_version": getattr(provider, "latest_version", None),
        "install_count": getattr(provider, "install_count", 0),
        "readme": getattr(provider, "readme", None),
        "created_at": _serialize_datetime(provider, "created_at"),
        "updated_at": _serialize_datetime(provider, "updated_at"),
        "versions": [
            {
                "version": v.version,
                "status": getattr(v, "status", None),
                "runtime_version": getattr(v, "runtime_version", None),
                "published_at": _serialize_datetime(v, "published_at"),
                "changelog": getattr(v, "changelog", None),
            }
            for v in versions
        ],
    }


def _installed_provider_to_dict(provider) -> dict:
    """Convert an installed provider summary to a plain dict for JSON/YAML output.

    Args:
        provider: Installed provider summary object.

    Returns:
        Dictionary representation.
    """
    return {
        "prefix": provider.prefix,
        "name": provider.name,
        "canonical": provider.canonical,
        "installed_version": provider.installed_version,
        "upgrade_policy": getattr(provider, "upgrade_policy", None),
        "resource_tier": getattr(provider, "resource_tier", None),
        "installed_at": _serialize_datetime(provider, "installed_at"),
        "latest_version": getattr(provider, "latest_version", None),
        "upgrade_available": getattr(provider, "upgrade_available", False),
    }
