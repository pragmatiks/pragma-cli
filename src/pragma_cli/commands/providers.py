"""Provider management commands.

Unified commands for installing and managing Pragmatiks providers; the
publish flow lives in ``provider_publish``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import copier
import httpx
import typer
import yaml
from pragma_sdk import (
    InstallationLifecycleState,
    PragmaClient,
    ProviderInstallation,
    ProviderInstallationEvent,
    ProviderModel,
    ProviderVersion,
    VersionStatus,
)
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from pragma_cli import get_client
from pragma_cli.commands.completions import completion_provider_ids
from pragma_cli.commands.provider_output import (
    VERSION_STATUS_LABELS,
    build_version_record,
    console,
    fetch_with_spinner,
    format_version_status,
)
from pragma_cli.commands.provider_publish import publish
from pragma_cli.errors import (
    error_console,
    print_api_error,
    report_not_found,
    require_auth,
)
from pragma_cli.exit_codes import (
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
)
from pragma_cli.helpers import OutputFormat, format_optional_value, output_data


app = typer.Typer(help="Provider management commands")


DEFAULT_TEMPLATE_URL = "gh:pragmatiks/pragma-providers"


TEMPLATE_PATH_ENV = "PRAGMA_PROVIDER_TEMPLATE"


AWAITING_WHEEL_HINT = (
    "If your publish stopped before its upload finished, run 'pragma providers publish' again. "
    "A finished upload shows as wheel received once Pragmatiks records it."
)
"""Message the versions table shows for an ``awaiting_wheel`` version, which has no message of its own."""


INSTALLATION_STATE_COLORS = {
    "ready": "green",
    "failed": "red",
    "deleting": "dim",
}
"""Rich color of each installation state outside an operation; every state during an operation shows yellow."""


INSTALLATION_STATE_LABELS = {
    "pending": "Queued",
    "processing": "Starting",
    "draining": "Draining",
    "translating": "Translating",
    "swapping": "Switching",
    "ready": "Running",
    "failed": "Failed",
    "deleting": "Uninstalling",
    "uninstalled": "Uninstalled",
}
"""Label the tables show for each installation state, matching the web console; json and yaml keep the raw state."""


INSTALLATION_OPERATION_LABELS = {"start": "restart"}
"""Word the tables show for an operation whose type reads differently on the web console; other types show as is."""


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


def validate_provider_name(context: typer.Context, name: str) -> str:
    """Check a provider name is namespaced as ``org/name``, for use as a Typer argument callback.

    Args:
        context: Click context of the command being parsed.
        name: Provider name as typed.

    Returns:
        The name unchanged.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if the
            name is not ``org/name``.
    """
    if context.resilient_parsing:
        return name

    organization, _, provider = name.partition("/")

    if not organization or not provider or "/" in provider:
        raise typer.BadParameter(f"must be 'org/name', got '{name}'.")

    return name


ProviderNameArgument = Annotated[
    str,
    typer.Argument(help="Provider name (org/name format)", callback=validate_provider_name),
]
"""Positional ``org/name`` provider argument, checked by ``validate_provider_name``."""


CompletedProviderNameArgument = Annotated[
    str,
    typer.Argument(
        help="Provider name (org/name format)",
        autocompletion=completion_provider_ids,
        callback=validate_provider_name,
    ),
]
"""``ProviderNameArgument`` that shell completion fills from the provider catalog."""


def format_version_message(version: ProviderVersion) -> str:
    """Format the message column of a version row.

    Args:
        version: The version to show.

    Returns:
        The version's own message, ``AWAITING_WHEEL_HINT`` for an
        ``awaiting_wheel`` version without one, or a dim dash, with Rich
        markup escaped.
    """
    if not version.error_message and version.status == VersionStatus.AWAITING_WHEEL:
        return escape(AWAITING_WHEEL_HINT)

    return format_optional_value(version.error_message)


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

    Example:
        pragma providers init mycompany
        pragma providers init postgres --output ./providers/postgres
        pragma providers init mycompany --defaults --description "My provider"

    \f

    Raises:
        typer.Exit: If directory already exists or template copy fails.
    """
    project_directory = output_dir or Path(f"./{name}-provider")

    if project_directory.exists():
        error_console.print(f"[red]Error:[/red] Directory {escape(str(project_directory))} already exists.")
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    template_source = get_template_source()

    data = {"name": name}

    if description:
        data["description"] = description

    if author_name:
        data["author_name"] = author_name

    if author_email:
        data["author_email"] = author_email

    typer.echo(f"Creating provider project: {project_directory}")
    typer.echo(f"  Template: {template_source}")
    typer.echo("")

    try:
        vcs_ref = "HEAD" if not template_source.startswith("gh:") else None
        copier.run_copy(
            src_path=template_source,
            dst_path=project_directory,
            data=data,
            defaults=defaults,
            unsafe=True,
            vcs_ref=vcs_ref,
        )
    except Exception as e:
        error_console.print(f"[red]Error:[/red] Could not create the provider project: {escape(str(e))}")
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    package_name = name.lower().replace("-", "_").replace(" ", "_") + "_provider"

    typer.echo("")
    typer.echo(f"Created provider project: {project_directory}")
    typer.echo("")
    typer.echo("Next steps:")
    typer.echo(f"  cd {project_directory}")
    typer.echo("  uv sync")
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
    project_directory: Annotated[
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

    \f

    Raises:
        typer.Exit: If directory is not a Copier project or update fails.
    """
    answers_file = project_directory / ".copier-answers.yml"

    if not answers_file.exists():
        error_console.print(
            f"[red]Error:[/red] {escape(str(project_directory))} is not a Copier-generated project "
            "(missing .copier-answers.yml)."
        )
        raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    typer.echo(f"Updating provider project: {project_directory}")
    typer.echo("")

    try:
        copier.run_update(dst_path=project_directory, unsafe=True)
    except Exception as e:
        error_console.print(f"[red]Error:[/red] Could not update the provider project: {escape(str(e))}")
        raise typer.Exit(FAILURE_EXIT_CODE) from e

    typer.echo("")
    typer.echo("Provider project updated successfully.")


app.command()(publish)


def _merge_install_config(
    config_flags: list[str] | None,
    config_file_path: Path | None,
) -> dict[str, str] | None:
    """Merge config from --config-file and --config flags.

    File values are loaded first, then individual flags override them.

    Args:
        config_flags: ``KEY=VALUE`` entries from repeated ``--config`` flags,
            or ``None``.
        config_file_path: Existing YAML file holding a mapping of strings,
            numbers and booleans, or ``None``.

    Returns:
        The merged config as strings, booleans written ``true`` or
        ``false``, or ``None`` if neither source gives any value.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the file cannot be read
            or parsed, is empty, is not a mapping, or holds a value of
            another type, or a flag is not ``KEY=VALUE`` with a non-empty key.
    """
    result: dict[str, str] = {}

    if config_file_path is not None:
        shown_path = escape(str(config_file_path))

        try:
            with config_file_path.open(encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except yaml.YAMLError as e:
            error_console.print(f"[red]Error:[/red] Failed to parse config file '{shown_path}': {escape(str(e))}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e
        except (OSError, UnicodeError) as e:
            error_console.print(f"[red]Error:[/red] Could not read config file '{shown_path}': {escape(str(e))}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE) from e

        if data is None:
            error_console.print(f"[red]Error:[/red] Config file is empty: {shown_path}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE)

        if not isinstance(data, dict):
            error_console.print(f"[red]Error:[/red] Config file must contain a YAML mapping, got {type(data).__name__}")
            raise typer.Exit(INPUT_ERROR_EXIT_CODE)

        for key, value in data.items():
            if isinstance(value, bool):
                result[str(key)] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                result[str(key)] = str(value)
            else:
                error_console.print(
                    f"[red]Error:[/red] Config key '{escape(str(key))}' has unsupported type {type(value).__name__}. "
                    "Only strings, numbers, and booleans are allowed."
                )
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

    if config_flags is not None:
        for entry in config_flags:
            if "=" not in entry:
                error_console.print(f"[red]Error:[/red] Invalid config format '{escape(entry)}'. Expected KEY=VALUE.")
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

            key, _, value = entry.partition("=")

            if not key:
                error_console.print(f"[red]Error:[/red] Config key cannot be empty in '{escape(entry)}'.")
                raise typer.Exit(INPUT_ERROR_EXIT_CODE)

            result[key] = value

    return result if result else None


def fetch_install_preview(client: PragmaClient, name: str, version: str | None) -> tuple[str, str]:
    """Fetch the label and version an install confirms before it runs.

    With an explicit version, reads that version in any status, so a version
    the caller's organization is still admitting, or one that failed, reaches
    the install request and its refusal rather than stopping at the catalog.
    Without one, reads the catalog entry and the caller's installations: a
    ``failed`` installation previews its stored version, the one installing
    it again installs, and anything else previews the catalog's latest version.

    Args:
        client: Authenticated SDK client.
        name: Provider name (``org/name``).
        version: Version to install, or ``None`` for the default.

    Returns:
        Tuple of (label, version to show); the label is the catalog display
        name, or ``name`` when ``version`` is given or the catalog has none.

    Raises:
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the provider or version
            is not found.
        httpx.HTTPStatusError: If the read fails with any other status.
    """  # noqa: DOC502
    if version is None:
        provider = fetch_catalog_provider(client, name)
        label = provider.display_name or name
        stored_version = fetch_install_again_version(client, name)
        preview_version = stored_version or provider.latest_version or "latest"

        return label, preview_version

    try:
        provider_version = fetch_with_spinner(
            f"Fetching {name} {version}...",
            lambda: client.get_provider_version(name, version),
        )
        return name, provider_version.version
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Version {version} of '{name}' not found in the store.")


def fetch_catalog_provider(client: PragmaClient, name: str) -> ProviderModel:
    """Fetch a provider's catalog entry for the install preview.

    Args:
        client: Authenticated SDK client.
        name: Provider name (``org/name``).

    Returns:
        The catalog entry.

    Raises:
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the provider is not found.
        httpx.HTTPStatusError: If the read fails with any other status.
    """  # noqa: DOC502
    try:
        return fetch_with_spinner(f"Fetching provider '{name}'...", lambda: client.get_provider(name))
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Provider '{name}' not found in the store.")


def fetch_install_again_version(client: PragmaClient, name: str) -> str | None:
    """Fetch the version installing ``name`` again without a version installs.

    Args:
        client: Authenticated SDK client.
        name: Provider name (``org/name``).

    Returns:
        The stored version (``operation_version``) of the caller's ``failed``
        installation of ``name``, or ``None`` when it has none.

    Raises:
        httpx.HTTPStatusError: If the read fails.
    """  # noqa: DOC502
    installations = fetch_with_spinner("Fetching installations...", lambda: client.list_installations())

    for installation in installations:
        if installation.canonical == name and installation.lifecycle_state == InstallationLifecycleState.FAILED:
            return installation.operation_version

    return None


@app.command()
def install(
    name: ProviderNameArgument,
    version: Annotated[
        str | None,
        typer.Option(
            "--version",
            "-v",
            help=(
                "Version to install (default: the latest published version; "
                "installing a failed installation again: its stored version)"
            ),
        ),
    ] = None,
    config: Annotated[
        list[str] | None,
        typer.Option("--config", "-c", help="Configuration key=value pair (repeatable)"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            help="Path to YAML file with configuration key-value pairs",
            exists=True,
            dir_okay=False,
            readable=True,
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Install a provider from the store.

    Queues the operation and returns. Follow it with: pragma providers history NAME.

    Examples:
        pragma providers install pragmatiks/qdrant
        pragma providers install pragmatiks/postgres --version 1.2.0
        pragma providers install pragmatiks/qdrant --config SOME_KEY=some_value
        pragma providers install pragmatiks/qdrant --config-file config.yaml --config OVERRIDE_KEY=value
        pragma providers install pragmatiks/qdrant -y
    """  # noqa: DOC501
    merged_config = _merge_install_config(config, config_file)

    client = get_client()
    require_auth(client)

    display, install_version = fetch_install_preview(client, name, version)

    provider_label = name if display == name else f"{display} ({name})"
    console.print(f"[bold]Provider:[/bold] {escape(provider_label)}")
    console.print(f"[bold]Version:[/bold]  {escape(install_version)}")

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

    result = fetch_with_spinner(
        "Installing provider...",
        lambda: client.install_provider(
            name,
            version=version,
            config=merged_config,
        ),
    )

    console.print(f"[green]Install queued:[/green] {escape(name)} {format_target_version(result.operation_version)}")
    print_history_pointer(name)


@app.command()
def uninstall(
    name: ProviderNameArgument,
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

    Queues the operation and returns. Follow it with: pragma providers history NAME.

    Examples:
        pragma providers uninstall pragmatiks/qdrant
        pragma providers uninstall pragmatiks/postgres --cascade
        pragma providers uninstall pragmatiks/redis --yes

    \f

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` and the API's message if the
            API refuses the uninstall with a conflict, such as resources left
            without --cascade or an installed provider that embeds this one,
            with ``NOT_FOUND_EXIT_CODE``
            if it is not installed, or with code 0 if the user cancels.
        httpx.HTTPStatusError: If the API refuses the uninstall for any
            other reason.
    """  # noqa: DOC502
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Provider:[/bold] {escape(name)}")

    if cascade:
        error_console.print("[yellow]Warning:[/yellow] --cascade will delete all resources for this provider")

    console.print()

    if not yes:
        action = "UNINSTALL provider and delete all its resources" if cascade else "UNINSTALL provider"
        confirm = typer.confirm(f"Are you sure you want to {action}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    try:
        fetch_with_spinner(
            "Uninstalling provider...",
            lambda: client.uninstall_provider(name, cascade=cascade),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 409:
            print_api_error(e)
            raise typer.Exit(FAILURE_EXIT_CODE) from e

        report_not_found(e, f"Provider '{name}' is not installed.")

    console.print(f"[green]Uninstall queued:[/green] {escape(name)}")
    print_history_pointer(name)


@app.command()
def upgrade(
    name: ProviderNameArgument,
    version: Annotated[str | None, typer.Option("--version", "-v", help="Target version (default: latest)")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Upgrade an installed provider to a newer version.

    Queues the operation and returns. Follow it with: pragma providers history NAME.

    Examples:
        pragma providers upgrade pragmatiks/qdrant
        pragma providers upgrade pragmatiks/postgres --version 2.0.0
        pragma providers upgrade pragmatiks/redis -y
    """  # noqa: DOC501
    client = get_client()
    require_auth(client)

    target = f"v{version}" if version else "latest"
    console.print(f"[bold]Upgrading:[/bold] {escape(name)} -> {escape(target)}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Upgrade {name} to {target}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    result = fetch_with_spinner(
        "Upgrading provider...",
        lambda: client.upgrade_provider(name, target_version=version),
    )

    console.print(f"[green]Upgrade queued:[/green] {escape(name)} -> {format_target_version(result.operation_version)}")
    print_history_pointer(name)


@app.command()
def downgrade(
    name: ProviderNameArgument,
    version: Annotated[str, typer.Option("--version", "-v", help="Target version to downgrade to")],
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Downgrade an installed provider to a previous version.

    Queues the operation and returns. Follow it with: pragma providers history NAME.

    Requires an explicit target version.

    Examples:
        pragma providers downgrade pragmatiks/qdrant --version 1.0.0
        pragma providers downgrade pragmatiks/postgres -v 1.2.0 -y

    \f

    Raises:
        typer.Exit: With code 0 if the user cancels.
    """
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Downgrading:[/bold] {escape(name)} -> v{escape(version)}")
    console.print()

    if not yes:
        confirm = typer.confirm(f"Downgrade {name} to v{version}?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    result = fetch_with_spinner(
        "Downgrading provider...",
        lambda: client.downgrade_provider(name, target_version=version),
    )

    console.print(
        f"[green]Downgrade queued:[/green] {escape(name)} -> {format_target_version(result.operation_version)}"
    )
    print_history_pointer(name)


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
    """
    client = get_client()

    if installed:
        _list_installations(client, output)
        return

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    result = fetch_with_spinner(
        "Fetching providers...",
        lambda: client.list_providers(
            query=query,
            scope=scope,
            tags=tag_list,
            limit=limit,
            offset=offset,
        ),
    )

    if output != OutputFormat.TABLE:
        output_data([_provider_summary_to_dict(p) for p in result.items], output)
        return

    if not result.items:
        if query:
            console.print(f"[dim]No providers found matching '{escape(query)}'.[/dim]")
        else:
            console.print("[dim]No providers found.[/dim]")
        return

    _print_store_list_table(result)


def _list_installations(client: PragmaClient, output: OutputFormat) -> None:
    """List provider installations for the current tenant.

    Args:
        client: SDK client instance.
        output: Output format for display; JSON and YAML print an empty
            list when nothing is installed.
    """
    require_auth(client)

    providers = fetch_with_spinner(
        "Fetching installed providers...",
        lambda: client.list_installations(),
    )

    if output != OutputFormat.TABLE:
        output_data([build_installation_record(installation) for installation in providers], output)
        return

    if not providers:
        console.print("[dim]No providers installed.[/dim]")
        return

    print_installed_table(providers)


@app.command()
def info(
    name: ProviderNameArgument,
    output: Annotated[
        OutputFormat,
        typer.Option("--output", "-o", help="Output format"),
    ] = OutputFormat.TABLE,
):
    """Show detailed information about a provider.

    Displays provider metadata, install count, and version history.

    Examples:
        pragma providers info pragmatiks/qdrant
        pragma providers info pragmatiks/postgres -o json

    \f

    Raises:
        httpx.HTTPStatusError: If reading the provider's versions fails with
            any status but 404, which shows no versions.
    """
    client = get_client()

    try:
        provider = fetch_with_spinner(
            f"Fetching provider '{name}'...",
            lambda: client.get_provider(name),
        )
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Provider '{name}' not found in the store.")

    try:
        versions = fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code != 404:
            raise

        versions = []

    if output != OutputFormat.TABLE:
        output_data(_provider_detail_to_dict(provider, versions), output)
        return

    _print_provider_info(provider, versions)


@app.command()
def versions(
    name: ProviderNameArgument,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """List the versions of a provider with their status.

    A version still being published shows where it is, from "waiting for
    the wheel" to "publishing to the registry"; a failed version shows why
    it failed. Versions that are not published are visible only to the
    organization that published them. The Wheel SHA-256 column is the
    digest of the stored wheel, renamed to the organization's distribution
    name, not of the file you built.

    Examples:
        pragma providers versions acme/pipeline
        pragma providers versions acme/pipeline -o json
    """
    client = get_client()

    try:
        provider_versions = fetch_with_spinner(
            "Fetching versions...",
            lambda: client.list_provider_versions(name),
        )
    except httpx.HTTPStatusError as e:
        report_not_found(e, f"Provider '{name}' not found in the store.")

    if output != OutputFormat.TABLE:
        data = [build_version_record(provider_version) for provider_version in provider_versions]
        output_data(data, output)
        return

    if not provider_versions:
        console.print("[dim]No versions found.[/dim]")
        return

    print_versions_table(provider_versions)


@app.command()
def history(
    name: CompletedProviderNameArgument,
    output: Annotated[OutputFormat, typer.Option("--output", "-o", help="Output format")] = OutputFormat.TABLE,
):
    """Show the history of an installed provider, newest first.

    Lists each transition of the installation: when it happened, the
    states it moved between, the operation and version behind it, its
    message and, for a failure, its cause. The history is kept for 30 days,
    also after the provider is uninstalled.

    Examples:
        pragma providers history acme/pipeline
        pragma providers history acme/pipeline -o json
    """
    client = get_client()
    require_auth(client)

    events = fetch_with_spinner(
        "Fetching history...",
        lambda: client.list_provider_history(name),
    )

    if output != OutputFormat.TABLE:
        output_data([event.model_dump(mode="json") for event in events], output)
        return

    if not events:
        console.print("[dim]No history found.[/dim]")
        return

    print_history_table(events)


@app.command()
def delete(
    name: CompletedProviderNameArgument,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
):
    """Delete a provider from the catalog (admins of the owning organization only).

    Removes the provider and all its versions from the catalog. A provider
    installed in at least one organization is refused; it can be deleted
    once every installation is uninstalled.

    Examples:
        pragma providers delete myorg/my-provider
        pragma providers delete myorg/my-provider --yes

    \f

    Raises:
        typer.Exit: If the user cancels.
    """
    client = get_client()
    require_auth(client)

    console.print(f"[bold]Provider:[/bold] {escape(name)}")
    error_console.print("[yellow]Warning:[/yellow] This will permanently delete the provider from the catalog.")
    console.print()

    if not yes:
        confirm = typer.confirm("Are you sure you want to DELETE this provider?")

        if not confirm:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)

    fetch_with_spinner(
        "Deleting provider...",
        lambda: client.delete_provider(name),
    )

    console.print(f"[green]✓[/green] Provider [bold]{escape(name)}[/bold] deleted successfully")


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

        table.add_row(
            escape(provider.canonical),
            format_optional_value(getattr(provider, "display_name", None)),
            format_optional_value(getattr(author, "display_name", None)),
            format_optional_value(getattr(provider, "latest_version", None)),
            str(install_count),
            format_optional_value(tags_display),
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
    tags = ", ".join(getattr(provider, "tags", []) or [])
    install_count = getattr(provider, "install_count", 0) or 0
    description = getattr(provider, "description", None)
    description_display = escape(description) if description else "[dim]No description[/dim]"
    created_at = getattr(provider, "created_at", None)
    updated_at = getattr(provider, "updated_at", None)

    info_lines = [
        f"[bold]Name:[/bold]         {escape(provider.canonical)}",
        f"[bold]Display Name:[/bold] {escape(getattr(provider, 'display_name', None) or provider.canonical)}",
        f"[bold]Author:[/bold]       {format_optional_value(getattr(author, 'display_name', None))}",
        f"[bold]Description:[/bold]  {description_display}",
        f"[bold]Tags:[/bold]         {format_optional_value(tags)}",
        f"[bold]Installs:[/bold]     {install_count}",
    ]

    if created_at:
        info_lines.append(f"[bold]Created:[/bold]      {str(created_at)[:19]}")

    if updated_at:
        info_lines.append(f"[bold]Updated:[/bold]      {str(updated_at)[:19]}")

    panel = Panel("\n".join(info_lines), title=escape(provider.canonical), border_style="blue")
    console.print(panel)

    if versions:
        console.print()
        print_versions_table(versions)


def print_versions_table(versions: list[ProviderVersion]) -> None:
    """Print provider versions with their admission status in a table.

    Args:
        versions: Provider versions to list.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Version")
    table.add_column("Status")
    table.add_column("Published")
    table.add_column("Package")
    table.add_column("Wheel SHA-256")
    table.add_column("Message")

    for provider_version in versions:
        published_at = provider_version.published_at
        published = published_at.strftime("%Y-%m-%d %H:%M:%S") if published_at else "[dim]-[/dim]"
        status_label = VERSION_STATUS_LABELS[provider_version.status]

        table.add_row(
            escape(provider_version.version),
            format_version_status(provider_version.status, status_label),
            published,
            format_package(provider_version.package),
            format_wheel_digest(provider_version.wheel_sha256),
            format_version_message(provider_version),
        )

    console.print(table)


def format_package(package: str | None) -> str:
    """Format a version's importable package for a table cell.

    Args:
        package: The package its entry point names, or ``None`` before the publish declared one.

    Returns:
        The package with Rich markup escaped, or a dim dash.
    """
    if not package:
        return "[dim]-[/dim]"

    return escape(package)


def format_wheel_digest(wheel_sha256: str | None) -> str:
    """Format a wheel's SHA-256 digest for a table cell, shortened to its first 12 hex characters.

    The full digest is in the ``-o json|yaml`` output.

    Args:
        wheel_sha256: The wheel's hex digest, or ``None`` before the publish declared one.

    Returns:
        The shortened digest, or a dim dash.
    """
    if not wheel_sha256:
        return "[dim]-[/dim]"

    return escape(wheel_sha256[:12])


def format_target_version(version: str | None) -> str:
    """Format the version an operation targets, such as ``v3.0.0``.

    Args:
        version: The target version, or ``None`` when the API sent none.

    Returns:
        The version prefixed with ``v`` with Rich markup escaped, or a dim dash.
    """
    if not version:
        return "[dim]-[/dim]"

    return escape(f"v{version}")


def print_history_pointer(name: str) -> None:
    """Print the command that follows an installation operation started for a provider.

    Args:
        name: The provider's namespaced name.
    """
    console.print(f"[dim]Follow it with: pragma providers history {escape(name)}[/dim]")


def format_installation_state_label(state: str) -> str:
    """Format an installation state as the label the tables show, such as ``Running`` for ``ready``.

    Args:
        state: An installation state, or ``uninstalled`` from a history entry.

    Returns:
        The state's label with Rich markup escaped, or the state itself when
        it has no label.
    """
    return escape(INSTALLATION_STATE_LABELS.get(state, state))


def format_installation_state(state: str | None) -> str:
    """Format an installation's state for the terminal, colored by the state.

    Args:
        state: The installation's lifecycle state, or ``None`` when the API
            sent none.

    Returns:
        The state's label in Rich markup: green when running, red when
        failed, dim while uninstalling, yellow while an operation runs, or a
        dim dash.
    """
    if not state:
        return "[dim]-[/dim]"

    color = INSTALLATION_STATE_COLORS.get(state, "yellow")
    return f"[{color}]{format_installation_state_label(state)}[/{color}]"


def format_running_installation_operation(installation: ProviderInstallation) -> str:
    """Format an installation's operation for the installed table, only while it runs.

    Args:
        installation: The installed provider.

    Returns:
        The operation and its target from :func:`format_installation_operation`
        while ``operation_running``, or a dim dash once it settled.
    """
    if not installation.operation_running:
        return "[dim]-[/dim]"

    return format_installation_operation(installation.operation_type, installation.operation_version)


def format_installation_operation(operation_type: str | None, operation_version: str | None) -> str:
    """Format an operation and the version it targets, such as ``upgrade v3.0.0`` or ``restart v3.0.0``.

    Args:
        operation_type: The operation's type, or ``None`` when there is none.
        operation_version: The version the operation targets, or ``None``.

    Returns:
        The operation and its version with Rich markup escaped, or a dim
        dash when there is no operation.
    """
    if not operation_type:
        return "[dim]-[/dim]"

    operation_label = escape(INSTALLATION_OPERATION_LABELS.get(operation_type, operation_type))
    if not operation_version:
        return operation_label

    return f"{operation_label} {format_target_version(operation_version)}"


def format_history_transition(event: ProviderInstallationEvent) -> str:
    """Format the states a history entry moved between, such as ``Queued -> Starting``.

    Args:
        event: The history entry.

    Returns:
        ``<from> -> <to>`` as state labels with Rich markup escaped, or the
        entered state's label alone when the entry has no previous state.
    """
    to_label = format_installation_state_label(event.to_state)
    if not event.from_state:
        return to_label

    return f"{format_installation_state_label(event.from_state)} -> {to_label}"


def print_installed_table(installations: list[ProviderInstallation]) -> None:
    """Print installed providers with their state and latest operation in a table.

    Args:
        installations: Installed providers to list.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Provider")
    table.add_column("State")
    table.add_column("Version")
    table.add_column("Operation")
    table.add_column("Message")

    for installation in installations:
        table.add_row(
            escape(installation.canonical),
            format_installation_state(installation.lifecycle_state),
            format_target_version(installation.installed_version),
            format_running_installation_operation(installation),
            format_optional_value(installation.failure_message),
        )

    console.print(table)


def print_history_table(events: list[ProviderInstallationEvent]) -> None:
    """Print an installation's history in a table, in the order given.

    Args:
        events: History entries, newest first.
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("Time")
    table.add_column("Transition")
    table.add_column("Operation")
    table.add_column("Version")
    table.add_column("Message")
    table.add_column("Cause")

    for event in events:
        table.add_row(
            event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            format_history_transition(event),
            format_installation_operation(event.operation_type, None),
            format_target_version(event.version),
            format_optional_value(event.message),
            format_optional_value(event.cause),
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


def _provider_detail_to_dict(provider, versions: list[ProviderVersion] | None = None) -> dict:
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
        "versions": [build_version_record(provider_version) for provider_version in versions],
    }


def build_installation_record(installation: ProviderInstallation) -> dict:
    """Build the JSON or YAML record of an installed provider.

    Args:
        installation: The installed provider.

    Returns:
        The installation's identity, served version, state, latest
        operation and its target version, failure message, and installation
        time in ISO 8601.
    """
    return installation.model_dump(mode="json", exclude={"config", "created_at", "updated_at"})
