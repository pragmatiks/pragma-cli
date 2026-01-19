"""Context and configuration management commands."""

import typer
from rich import print

from pragma_cli.config import ContextConfig, get_current_context, load_config, save_config


app = typer.Typer()


@app.command()
def use_context(context_name: str):
    """Switch to a different context.

    Raises:
        typer.Exit: If context not found.
    """
    config = load_config()
    if context_name not in config.contexts:
        print(f"[red]\u2717[/red] Context '{context_name}' not found")
        print(f"Available contexts: {', '.join(config.contexts.keys())}")
        raise typer.Exit(1)

    config.current_context = context_name
    save_config(config)
    print(f"[green]\u2713[/green] Switched to context '{context_name}'")


@app.command()
def get_contexts():
    """List available contexts."""
    config = load_config()
    print("\n[bold]Available contexts:[/bold]")
    for name, ctx in config.contexts.items():
        marker = "[green]*[/green]" if name == config.current_context else " "
        print(f"{marker} [cyan]{name}[/cyan]: {ctx.api_url}")
    print()


@app.command()
def current_context():
    """Show current context."""
    context_name, context_config = get_current_context()
    print(f"[bold]Current context:[/bold] [cyan]{context_name}[/cyan]")
    print(f"[bold]API URL:[/bold] {context_config.api_url}")
    print(f"[bold]Auth URL:[/bold] {context_config.get_auth_url()}")


@app.command()
def set_context(
    name: str = typer.Argument(..., help="Context name"),
    api_url: str = typer.Option(..., help="API endpoint URL"),
    auth_url: str | None = typer.Option(None, help="Auth endpoint URL (derived from api_url if not set)"),
):
    """Create or update a context."""
    config = load_config()
    config.contexts[name] = ContextConfig(api_url=api_url, auth_url=auth_url)
    save_config(config)

    # Show the effective auth URL
    effective_auth = config.contexts[name].get_auth_url()
    print(f"[green]\u2713[/green] Context '{name}' configured")
    print(f"  API URL:  {api_url}")
    print(f"  Auth URL: {effective_auth}")


@app.command()
def delete_context(name: str):
    """Delete a context.

    Raises:
        typer.Exit: If context not found or is current context.
    """
    config = load_config()
    if name not in config.contexts:
        print(f"[red]\u2717[/red] Context '{name}' not found")
        raise typer.Exit(1)

    if name == config.current_context:
        print("[red]\u2717[/red] Cannot delete current context")
        raise typer.Exit(1)

    del config.contexts[name]
    save_config(config)
    print(f"[green]\u2713[/green] Context '{name}' deleted")
