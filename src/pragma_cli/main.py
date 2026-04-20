"""CLI entry point with Typer application setup and command routing."""

from __future__ import annotations

from importlib.metadata import version as get_version
from typing import Annotated, Any

import click
import httpx
import typer
from pragma_sdk import InvalidResourceIdentityError, PragmaClient, ProjectMismatchError
from pydantic import ValidationError
from rich.console import Console
from typer.core import TyperGroup

from pragma_cli import set_client
from pragma_cli.bootstrap_errors import check_bootstrap_error
from pragma_cli.commands import auth, config, ops, organizations, projects, providers, resources, settings
from pragma_cli.config import CONFIG_PATH, MalformedConfigError, get_current_context


console = Console(stderr=True)


def _extract_base_url(error: httpx.RequestError) -> str:
    """Extract the base URL from an httpx request error.

    Args:
        error: The httpx request error.

    Returns:
        The base URL string, or "unknown" if not available.
    """
    try:
        url = error.request.url
        return f"{url.scheme}://{url.host}:{url.port}" if url.port else f"{url.scheme}://{url.host}"
    except Exception:
        return "unknown"


def _handle_httpx_error(error: httpx.ConnectError | httpx.TimeoutException | httpx.HTTPStatusError) -> None:
    """Print a friendly message for an httpx error and exit.

    Args:
        error: The httpx error to handle.

    Raises:
        typer.Exit: Always exits with code 1 after printing the message.
    """
    if isinstance(error, httpx.ConnectError):
        base_url = _extract_base_url(error)
        console.print(f"[red]Error:[/red] Could not connect to API at {base_url}")
        console.print("Check that the API URL is correct and the server is running.")
        raise typer.Exit(1) from error

    if isinstance(error, httpx.TimeoutException):
        base_url = _extract_base_url(error)
        console.print(f"[red]Error:[/red] Request timed out connecting to {base_url}")
        raise typer.Exit(1) from error

    if isinstance(error, httpx.HTTPStatusError):
        check_bootstrap_error(error)

        if error.response.status_code == 401:
            console.print("[red]Error:[/red] Not authenticated. Run 'pragma auth login' to authenticate.")
            raise typer.Exit(1) from error

        url = str(error.request.url)
        status = error.response.status_code
        reason = error.response.reason_phrase
        console.print(f"[red]Error:[/red] {status} {reason} for {url}")
        raise typer.Exit(1) from error

    console.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(1) from error


def _handle_project_error(error: ProjectMismatchError | InvalidResourceIdentityError) -> None:
    """Print a friendly message for project-scoping errors and exit.

    Args:
        error: Project-scoping error to surface.

    Raises:
        typer.Exit: Always exits with code 2 after printing the message.
    """
    console.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(2) from error


def _handle_validation_error(error: ValidationError) -> None:
    """Print a friendly message for a Pydantic validation error and exit.

    Args:
        error: Pydantic validation error raised while building a model.

    Raises:
        typer.Exit: Always exits with code 2 after printing the message.
    """
    console.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(2) from error


def _handle_malformed_config_error(error: MalformedConfigError) -> None:
    """Print a friendly message for a malformed config file and exit.

    Args:
        error: Malformed-config error raised while loading the config file.

    Raises:
        typer.Exit: Always exits with code 2 after printing the message.
    """
    console.print(f"[red]Error:[/red] {error}")
    raise typer.Exit(2) from error


def _handle_config_os_error(error: OSError) -> None:
    """Print a friendly message for an unhandled file I/O error and exit.

    Reports the actual failing path from ``error.filename`` rather
    than always blaming the config file. The hint about
    ``XDG_CONFIG_HOME`` is only shown when the failing path is the
    config file (or its parent) — pointing users at the config dir
    when, say, ``pragma auth login`` fails to write the credentials
    file would be misleading.

    Args:
        error: Underlying OS error raised during a file operation.

    Raises:
        typer.Exit: Always exits with code 2 after printing the message.
    """
    failing_path = getattr(error, "filename", None) or str(CONFIG_PATH)
    console.print(f"[red]Error:[/red] could not access {failing_path}: {error}")

    if failing_path in (str(CONFIG_PATH), str(CONFIG_PATH.parent)):
        console.print(
            "[dim]Check file permissions and that the directory exists. "
            "You can set XDG_CONFIG_HOME to override the default config location.[/dim]"
        )
    else:
        console.print("[dim]Check file permissions and that the directory exists.[/dim]")

    raise typer.Exit(2) from error


class ErrorHandlingGroup(TyperGroup):
    """Click Group subclass that catches unhandled CLI-level exceptions.

    Wraps command invocation to translate connection errors, timeouts,
    HTTP status errors, project-scoping mismatches, malformed config
    files, Pydantic validation errors, and file-system I/O failures
    (with the actual failing path surfaced from ``OSError.filename``)
    into friendly CLI messages instead of raw Python tracebacks.
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Invoke the command group with global exception handling.

        Args:
            ctx: Click context.

        Returns:
            Command result.
        """
        try:
            return super().invoke(ctx)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            _handle_httpx_error(e)
        except (ProjectMismatchError, InvalidResourceIdentityError) as e:
            _handle_project_error(e)
        except MalformedConfigError as e:
            _handle_malformed_config_error(e)
        except ValidationError as e:
            _handle_validation_error(e)
        except OSError as e:
            _handle_config_os_error(e)


app = typer.Typer(cls=ErrorHandlingGroup, pretty_exceptions_enable=False)


def _version_callback(value: bool) -> None:
    """Print version and exit if --version flag is provided.

    Args:
        value: True if --version flag was provided.

    Raises:
        typer.Exit: Always exits after displaying version.
    """
    if value:
        package_version = get_version("pragmatiks-cli")
        typer.echo(f"pragma {package_version}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            "-c",
            help="Configuration context to use",
            envvar="PRAGMA_CONTEXT",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            "-t",
            help="Override authentication token (not recommended, use environment variable instead)",
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option(
            "--project",
            help=(
                "Project slug for project-scoped resource commands. Precedence: --project, "
                "PRAGMA_PROJECT, current context config, then 'pragma projects use'."
            ),
        ),
    ] = None,
):
    """Pragma CLI - Declarative resource management.

    Authentication (industry-standard pattern):
      - CLI writes credentials: 'pragma auth login' stores tokens in ~/.config/pragma/credentials
      - SDK reads credentials: Automatic token discovery via precedence chain

    Token Discovery Precedence:
      1. --token flag (explicit override)
      2. PRAGMA_AUTH_TOKEN_<CONTEXT> context-specific environment variable
      3. PRAGMA_AUTH_TOKEN environment variable
      4. ~/.config/pragma/credentials file (from pragma auth login)
      5. No authentication
    """
    context_name, context_config = get_current_context(context)

    if token:
        client = PragmaClient(base_url=context_config.api_url, auth_token=token)
    else:
        client = PragmaClient(base_url=context_config.api_url, context=context_name, require_auth=False)

    ctx.obj = {"context": context_name, "project": project}
    set_client(client)


app.add_typer(resources.app, name="resources")
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(ops.app, name="ops")
app.add_typer(organizations.app, name="organizations")
app.add_typer(providers.app, name="providers")
app.add_typer(projects.app, name="projects")
app.add_typer(settings.app, name="settings")

if __name__ == "__main__":  # pragma: no cover
    app()
