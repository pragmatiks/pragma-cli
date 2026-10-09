"""CLI entry point with Typer application setup and command routing."""

from __future__ import annotations

import json
from functools import partial
from importlib.metadata import version as get_version
from typing import Annotated, Any

import click
import httpx
import jsonschema
import typer
from pragma_sdk import InvalidResourceIdentityError, PragmaClient, ProjectMismatchError
from pydantic import ValidationError
from typer.core import TyperGroup

from pragma_cli import set_client_factory
from pragma_cli.commands import auth, config, ops, organizations, projects, providers, resources
from pragma_cli.config import (
    MalformedConfigError,
    PragmaConfig,
    UnknownContextError,
    is_valid_api_url,
    load_config,
    select_context,
)
from pragma_cli.errors import (
    report_api_error,
    report_input_error,
    report_invalid_api_url,
    report_os_error,
    report_request_error,
    report_unreadable_response,
)
from pragma_cli.exit_codes import EXIT_CODES_HELP
from pragma_cli.plugins import load_plugins


def build_client(config: PragmaConfig, context_name: str, token: str | None) -> PragmaClient:
    """Build the SDK client for a context.

    Args:
        config: Loaded configuration.
        context_name: Context whose API URL the client calls.
        token: Bearer token overriding the context's stored credentials, or
            ``None`` to use them.

    Returns:
        A PragmaClient for the context's API URL, authenticated with
        ``token`` when given, else with the context's stored credentials if
        any.

    Raises:
        UnknownContextError: If the context is not in the configuration.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if the context's API URL
            is not an ``http://`` or ``https://`` URL with a host.
    """  # noqa: DOC502
    context_config = select_context(config, context_name)

    if not is_valid_api_url(context_config.api_url):
        report_invalid_api_url(context_name, context_config.api_url)

    if token:
        return PragmaClient(base_url=context_config.api_url, auth_token=token)

    return PragmaClient(base_url=context_config.api_url, context=context_name, require_auth=False)


class ErrorHandlingGroup(TyperGroup):
    """Click Group subclass that catches unhandled CLI-level exceptions.

    Wraps command invocation to translate request errors, HTTP status
    errors, project-scoping mismatches, malformed config files, unknown
    contexts, unreadable API responses, and OS errors into messages on
    stderr instead of raw Python tracebacks. A broken stdout
    pipe, such as ``pragma ... -o json | head -1``, reaches Typer, which
    exits with ``FAILURE_EXIT_CODE`` without printing anything.
    """

    def invoke(self, ctx: click.Context) -> Any:
        """Invoke the command group with global exception handling.

        Args:
            ctx: Click context.

        Returns:
            Command result.

        Raises:
            BrokenPipeError: If stdout was closed early, left to Typer.
        """
        try:
            return super().invoke(ctx)
        except httpx.RequestError as e:
            report_request_error(e)
        except httpx.HTTPStatusError as e:
            report_api_error(e)
        except (
            ProjectMismatchError,
            InvalidResourceIdentityError,
            MalformedConfigError,
            UnknownContextError,
        ) as e:
            report_input_error(e)
        except (ValidationError, json.JSONDecodeError, jsonschema.SchemaError) as e:
            report_unreadable_response(e)
        except BrokenPipeError:
            raise
        except OSError as e:
            report_os_error(e)


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


@app.callback(epilog=EXIT_CODES_HELP)
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
                "Project ID for project-scoped resource commands. Precedence: --project, "
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
    loaded_config = load_config()
    context_name = context or loaded_config.current_context

    ctx.obj = {"context": context_name, "project": project}
    set_client_factory(partial(build_client, loaded_config, context_name, token))


app.add_typer(resources.app, name="resources")
app.add_typer(auth.app, name="auth")
app.add_typer(config.app, name="config")
app.add_typer(ops.app, name="ops")
app.add_typer(organizations.app, name="organizations")
app.add_typer(providers.app, name="providers")
app.add_typer(projects.app, name="projects")

load_plugins(app)

if __name__ == "__main__":
    app()
