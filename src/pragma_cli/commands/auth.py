"""Authentication commands for browser-based Clerk login."""

import os
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import typer
from pragma_sdk import PragmaClient
from pragma_sdk.config import load_credentials
from rich import print
from rich.console import Console

from pragma_cli.config import CREDENTIALS_FILE, get_current_context, load_config


console = Console()


app = typer.Typer()

CALLBACK_PORT = int(os.getenv("PRAGMA_AUTH_CALLBACK_PORT", "8765"))
CALLBACK_PATH = os.getenv("PRAGMA_AUTH_CALLBACK_PATH", "/auth/callback")
CLERK_FRONTEND_URL = os.getenv("PRAGMA_CLERK_FRONTEND_URL", "https://app.pragmatiks.io")
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"
LOGIN_URL = f"{CLERK_FRONTEND_URL}/auth/callback?callback={CALLBACK_URL}"


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for OAuth callback from Clerk."""

    token = None

    def do_GET(self):
        """Handle GET request from Clerk redirect."""
        parsed = urlparse(self.path)

        if parsed.path == CALLBACK_PATH:
            params = parse_qs(parsed.query)
            token = params.get("token", [None])[0]

            if token:
                CallbackHandler.token = token

                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"""
                    <html>
                        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                            <h1 style="color: green;">&#10003; Authentication Successful</h1>
                            <p>You can close this window and return to the terminal.</p>
                        </body>
                    </html>
                """
                )
            else:
                self.send_response(400)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"""
                    <html>
                        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                            <h1 style="color: red;">&#10007; Authentication Failed</h1>
                            <p>No token received. Please try again.</p>
                        </body>
                    </html>
                """
                )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress server logs."""


def save_credentials(token: str, context_name: str = "default"):
    """Save authentication token to local credentials file.

    Args:
        token: JWT token from Clerk
        context_name: Context to associate with this token
    """
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)

    credentials = {}
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    credentials[key.strip()] = value.strip()

    credentials[context_name] = token

    with open(CREDENTIALS_FILE, "w") as f:
        for key, value in credentials.items():
            f.write(f"{key}={value}\n")

    CREDENTIALS_FILE.chmod(0o600)


def clear_credentials(context_name: str | None = None):
    """Clear stored credentials.

    Args:
        context_name: Specific context to clear, or None for all
    """
    if not CREDENTIALS_FILE.exists():
        return

    if context_name is None:
        CREDENTIALS_FILE.unlink()
        return

    credentials = {}
    with open(CREDENTIALS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                credentials[key.strip()] = value.strip()

    credentials.pop(context_name, None)

    if credentials:
        with open(CREDENTIALS_FILE, "w") as f:
            for key, value in credentials.items():
                f.write(f"{key}={value}\n")
        CREDENTIALS_FILE.chmod(0o600)
    else:
        CREDENTIALS_FILE.unlink()


@app.command()
def login(context: str = typer.Option("default", help="Context to authenticate for")):
    """Authenticate with Pragma using browser-based Clerk login.

    Opens your default browser to Clerk authentication page. After successful
    login, your credentials are stored locally in ~/.config/pragma/credentials.

    Example:
        pragma login
        pragma login --context production

    Raises:
        typer.Exit: If context not found or authentication fails/times out.
    """
    config = load_config()
    if context not in config.contexts:
        print(f"[red]\u2717[/red] Context '{context}' not found")
        print(f"Available contexts: {', '.join(config.contexts.keys())}")
        raise typer.Exit(1)

    api_url = config.contexts[context].api_url

    print(f"[cyan]Authenticating for context:[/cyan] {context}")
    print(f"[cyan]API URL:[/cyan] {api_url}")
    print()

    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)

    print(f"[yellow]Opening browser to:[/yellow] {CLERK_FRONTEND_URL}")
    print()
    print("[dim]If browser doesn't open automatically, visit:[/dim]")
    print(f"[dim]{LOGIN_URL}[/dim]")
    print()
    print("[yellow]Waiting for authentication...[/yellow]")

    webbrowser.open(LOGIN_URL)

    server.timeout = 300
    server.handle_request()

    if CallbackHandler.token:
        save_credentials(CallbackHandler.token, context)
        print()
        print("[green]\u2713 Successfully authenticated![/green]")
        print(f"[dim]Credentials saved to {CREDENTIALS_FILE}[/dim]")
        print()
        print("[bold]You can now use pragma commands:[/bold]")
        print("  pragma resources list-groups")
        print("  pragma resources get <resource-id>")
        print("  pragma resources apply <file.yaml>")
    else:
        print()
        print("[red]\u2717 Authentication failed or timed out[/red]")
        print("[dim]Please try again[/dim]")
        raise typer.Exit(1)


@app.command()
def logout(
    context: str | None = typer.Option(None, help="Context to logout from (all if not specified)"),
    all: bool = typer.Option(False, "--all", help="Logout from all contexts"),
):
    """Clear stored authentication credentials.

    Example:
        pragma logout                    # Clear current context
        pragma logout --all              # Clear all contexts
        pragma logout --context staging  # Clear specific context
    """
    if all:
        clear_credentials(None)
        print("[green]\u2713[/green] Cleared all credentials")
    elif context:
        clear_credentials(context)
        print(f"[green]\u2713[/green] Cleared credentials for context '{context}'")
    else:
        context_name, _ = get_current_context()
        clear_credentials(context_name)
        print(f"[green]\u2713[/green] Cleared credentials for current context '{context_name}'")


@app.command()
def whoami():
    """Show current authentication status and user information.

    Displays the current context, authentication state, and user details
    including email and organization name from the API.
    """
    current_context_name, current_context_config = get_current_context()
    token = load_credentials(current_context_name)

    console.print()
    console.print("[bold]Authentication Status[/bold]")
    console.print()

    if not token:
        console.print(f"  Context: [cyan]{current_context_name}[/cyan]")
        console.print("  Status:  [yellow]Not authenticated[/yellow]")
        console.print()
        console.print("[dim]Run 'pragma auth login' to authenticate[/dim]")
        return

    console.print(f"  Context: [cyan]{current_context_name}[/cyan]")
    console.print("  Status:  [green]\u2713 Authenticated[/green]")

    try:
        client = PragmaClient(base_url=current_context_config.api_url, auth_token=token)
        user_info = client.get_me()

        console.print()
        console.print("[bold]User Information[/bold]")
        console.print()
        console.print(f"  User ID:      [cyan]{user_info.user_id}[/cyan]")
        if user_info.email:
            console.print(f"  Email:        [cyan]{user_info.email}[/cyan]")
        else:
            console.print("  Email:        [dim]Not set[/dim]")
        console.print(f"  Organization: [cyan]{user_info.organization_name or user_info.organization_id}[/cyan]")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            console.print()
            console.print("[yellow]Token expired or invalid. Run 'pragma auth login' to re-authenticate.[/yellow]")
        else:
            console.print()
            console.print(f"[red]Error fetching user info:[/red] {e.response.text}")
    except httpx.RequestError as e:
        console.print()
        console.print(f"[red]Connection error:[/red] {e}")
