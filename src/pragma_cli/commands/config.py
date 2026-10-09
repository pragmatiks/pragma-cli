"""Context and configuration management commands."""

import typer
from rich import print
from rich.markup import escape

from pragma_cli.config import (
    ContextConfig,
    check_context_exists,
    get_current_context,
    is_valid_api_url,
    load_config,
    update_config,
)
from pragma_cli.errors import error_console
from pragma_cli.exit_codes import INPUT_ERROR_EXIT_CODE


app = typer.Typer()


def validate_api_url(context: typer.Context, api_url: str) -> str:
    """Check an API URL is an ``http://`` or ``https://`` URL with a host, for use as a Typer option callback.

    Args:
        context: Click context of the command being parsed.
        api_url: API URL as typed.

    Returns:
        The URL unchanged.

    Raises:
        typer.BadParameter: Exiting with ``INPUT_ERROR_EXIT_CODE``, if the URL
            does not use http or https or has no host.
    """
    if context.resilient_parsing or is_valid_api_url(api_url):
        return api_url

    raise typer.BadParameter(f"must be an http:// or https:// URL with a host, got '{api_url}'.")


@app.command()
def use_context(context_name: str):
    """Switch to a different context.

    \f

    Raises:
        UnknownContextError: If the context is not in the configuration.
    """  # noqa: DOC502
    with update_config() as config:
        check_context_exists(config, context_name)
        config.current_context = context_name

    print(f"[green]✓[/green] Switched to context '{escape(context_name)}'")


@app.command()
def get_contexts():
    """List available contexts."""
    config = load_config()
    print("\n[bold]Available contexts:[/bold]")
    for name, ctx in config.contexts.items():
        marker = "[green]*[/green]" if name == config.current_context else " "
        print(f"{marker} [cyan]{escape(name)}[/cyan]: {escape(ctx.api_url)}")
    print()


@app.command()
def current_context():
    """Show current context."""
    context_name, context_config = get_current_context()
    print(f"[bold]Current context:[/bold] [cyan]{escape(context_name)}[/cyan]")
    print(f"[bold]API URL:[/bold] {escape(context_config.api_url)}")
    print(f"[bold]Auth URL:[/bold] {escape(context_config.get_auth_url())}")
    print(f"[bold]Project:[/bold] {escape(context_config.project or 'none set')}")


@app.command()
def set_context(
    name: str = typer.Argument(..., help="Context name"),
    api_url: str = typer.Option(..., help="API endpoint URL", callback=validate_api_url),
    auth_url: str | None = typer.Option(None, help="Auth endpoint URL (derived from api_url if not set)"),
):
    """Create or update a context."""
    with update_config() as config:
        existing = config.contexts.get(name)
        config.contexts[name] = ContextConfig(
            api_url=api_url,
            auth_url=auth_url,
            project=existing.project if existing else None,
        )
        effective_auth = config.contexts[name].get_auth_url()

    print(f"[green]✓[/green] Context '{escape(name)}' configured")
    print(f"  API URL:  {escape(api_url)}")
    print(f"  Auth URL: {escape(effective_auth)}")


@app.command()
def delete_context(name: str):
    """Delete a context.

    \f

    Raises:
        UnknownContextError: If the context is not in the configuration.
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if it is the current context.
    """  # noqa: DOC502
    with update_config() as config:
        check_context_exists(config, name)

        if name == config.current_context:
            error_console.print(
                "[red]Error:[/red] Cannot delete the current context. "
                "Switch to another context first with 'pragma config use-context <name>'."
            )
            raise typer.Exit(INPUT_ERROR_EXIT_CODE)

        del config.contexts[name]

    print(f"[green]✓[/green] Context '{escape(name)}' deleted")
