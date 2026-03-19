"""CLI entry point with Typer application setup and command routing."""

from __future__ import annotations

from importlib.metadata import version as get_version
from typing import Annotated, Any

import click
import httpx
import typer
from pragma_sdk import PragmaClient
from rich.console import Console
from typer.core import TyperGroup

from pragma_cli import set_client
from pragma_cli.commands import auth, config, ops, organizations, providers, resources
from pragma_cli.config import get_current_context


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
        if error.response.status_code == 401:
            console.print("[red]Error:[/red] Not authenticated. Run 'pragma auth login' to authenticate.")
            raise typer.Exit(1) from error

        url = str(error.request.url)
        status = error.response.status_code
        reason = error.response.reason_phrase
        console.print(f"[red]Error:[/red] {status} {reason} for {url}")
        raise typer.Exit(1) from error


class ErrorHandlingGroup(TyperGroup):
    """Click Group subclass that catches unhandled httpx exceptions.

    Wraps command invocation to translate connection errors, timeouts,
    and HTTP status errors into friendly CLI messages instead of
    raw Python tracebacks.
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Invoke the command group with httpx exception handling.

        Args:
            ctx: Click context.

        Returns:
            Command result.
        """
        try:
            return super().invoke(ctx)
        except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
            _handle_httpx_error(e)


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

    set_client(client)


app.add_typer(resources.app, name="resources")
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(ops.app, name="ops")
app.add_typer(organizations.app, name="organizations")
app.add_typer(providers.app, name="providers")

if __name__ == "__main__":  # pragma: no cover
    app()
