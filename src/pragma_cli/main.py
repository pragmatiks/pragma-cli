"""CLI entry point with Typer application setup and command routing."""

from importlib.metadata import version as get_version
from typing import Annotated

import typer
from pragma_sdk import PragmaClient

from pragma_cli import set_client
from pragma_cli.commands import auth, config, ops, providers, resources, store
from pragma_cli.config import get_current_context


app = typer.Typer(pretty_exceptions_enable=False)


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
app.add_typer(providers.app, name="providers")
app.add_typer(store.app, name="store")

if __name__ == "__main__":  # pragma: no cover
    app()
