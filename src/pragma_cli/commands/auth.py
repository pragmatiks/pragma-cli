"""Authentication commands: browser sign-in, or a session token read from stdin."""

import base64
import json
import os
import sys
import time
import webbrowser
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import click
import httpx
import typer
from rich import print
from rich.console import Console
from rich.markup import escape

from pragma_cli import get_client
from pragma_cli.clients import build_client
from pragma_cli.commands.provider_output import fetch_with_spinner
from pragma_cli.config import CREDENTIALS_FILE, ContextConfig, PragmaConfig, load_config, select_context
from pragma_cli.credentials import clear_credentials, extract_client_token, save_credentials
from pragma_cli.errors import error_console, report_input_error, report_login_required
from pragma_cli.exit_codes import FAILURE_EXIT_CODE


console = Console()


app = typer.Typer()

CALLBACK_PORT = int(os.getenv("PRAGMA_AUTH_CALLBACK_PORT", "8765"))
CALLBACK_PATH = os.getenv("PRAGMA_AUTH_CALLBACK_PATH", "/auth/callback")

WITH_TOKEN_TERMINAL_MESSAGE = (
    "--with-token reads a session token from stdin, but stdin is a terminal. Pipe the token in."
)
"""Input error printed when ``auth login --with-token`` runs with a terminal on stdin instead of a pipe."""

WITH_TOKEN_EMPTY_MESSAGE = "--with-token reads a session token from stdin, but stdin was empty."
"""Input error printed when ``auth login --with-token`` reads no token."""

WITH_TOKEN_MULTIPLE_MESSAGE = "--with-token reads one session token from stdin, but stdin held more than one."
"""Input error printed when ``auth login --with-token`` reads more than one whitespace-separated value."""

WITH_TOKEN_NOT_SESSION_MESSAGE = (
    "--with-token takes a session token. Pass an API key with --token or PRAGMA_AUTH_TOKEN."
)
"""Input error printed when ``auth login --with-token`` reads a token that is not a JWT, such as an API key."""

WITH_TOKEN_EXPIRED_MESSAGE = "--with-token read an expired session token. Pipe in a fresh one."
"""Input error printed when ``auth login --with-token`` reads an expired session token."""

WITH_TOKEN_REJECTED_MESSAGE = "The API of context '{context}' refused the session token from stdin; nothing was saved."
"""Error printed when the context's API refuses the ``auth login --with-token`` token (401); takes ``context``."""


def parse_token_claims(token: str) -> dict[str, Any] | None:
    """Parse the claims of a JWT without verifying its signature.

    Args:
        token: Token string, a JWT or anything else.

    Returns:
        The decoded payload, or None when the token is not three
        dot-separated parts whose middle part is a base64url JSON object.
    """
    parts = token.split(".")

    if len(parts) != 3:
        return None

    encoded_payload = parts[1]
    padding = 4 - len(encoded_payload) % 4

    if padding != 4:
        encoded_payload += "=" * padding

    try:
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None

    return payload if isinstance(payload, dict) else None


def is_token_expired(token: str) -> bool:
    """Check if a JWT token is expired by decoding the payload.

    Decodes the payload without verifying the signature.

    Args:
        token: JWT token string.

    Returns:
        True if the token is expired, carries an ``exp`` claim that is not
        a number, or is not a JWT (see :func:`parse_token_claims`); False
        otherwise, a JWT without ``exp`` included.
    """
    claims = parse_token_claims(token)

    if claims is None:
        return True

    exp = claims.get("exp")

    if exp is None:
        return False

    if isinstance(exp, bool) or not isinstance(exp, int | float):
        return True

    return time.time() > exp


def build_callback_url() -> str:
    """Build the local callback URL for OAuth flow.

    Returns:
        Callback URL for the local OAuth server.
    """
    return f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"


def build_login_url(context_config: ContextConfig, org: str | None = None) -> str:
    """Build the login URL for a given context.

    Args:
        context_config: The context configuration to get auth URL from.
        org: Organization slug to pre-select (skips org picker).

    Returns:
        Full login URL with callback parameter.
    """
    auth_url = context_config.derive_auth_url()
    callback_url = build_callback_url()
    url = f"{auth_url}/auth/callback?callback={quote(callback_url, safe='')}"

    if org:
        url += f"&org={quote(org, safe='')}"

    return url


class CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OAuth callback from the sign-in page."""

    token = None

    def do_GET(self):
        """Handle the GET request the sign-in page redirects to."""
        parsed = urlparse(self.path)

        if parsed.path == CALLBACK_PATH:
            params = parse_qs(parsed.query)
            token = params.get("token", [None])[0]

            if token:
                CallbackHandler.token = token
                self.respond(
                    200,
                    b"""
                    <html>
                        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                            <h1 style="color: green;">&#10003; Authentication Successful</h1>
                            <p>You can close this window and return to the terminal.</p>
                        </body>
                    </html>
                """,
                )
            else:
                self.respond(
                    400,
                    b"""
                    <html>
                        <body style="font-family: sans-serif; text-align: center; padding: 50px;">
                            <h1 style="color: red;">&#10007; Authentication Failed</h1>
                            <p>No token received. Please try again.</p>
                        </body>
                    </html>
                """,
                )
        else:
            self.respond(404)

    def respond(self, status: int, body: bytes = b""):
        """Write a response to the browser, tolerating a closed connection.

        The browser may close the callback connection before the page is
        written. The login outcome is already decided by then, so a closed
        connection is ignored instead of surfacing as a traceback.

        Args:
            status: HTTP status code of the response.
            body: HTML page to send; empty sends headers only.
        """
        with suppress(ConnectionError):
            self.send_response(status)
            if body:
                self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress server logs."""


@app.command()
def login(
    ctx: typer.Context,
    context: str | None = typer.Option(None, help="Context to authenticate for (default: the resolved context)"),
    org: str | None = typer.Option(None, help="Organization slug to authenticate with (skips org picker)"),
    with_token: bool = typer.Option(
        False,
        "--with-token",
        help=(
            "Read a session token from stdin, check it and save it, without opening a browser. "
            "To use a token without saving it, pass the global --token instead"
        ),
    ),
):
    """Authenticate with Pragmatiks in your browser, or with a session token read from stdin.

    Opens your default browser to the Pragmatiks sign-in page. After successful
    login, your credentials are stored locally in ~/.config/pragma/credentials.
    The token is saved under this command's ``--context`` if given, otherwise
    under the context resolved by the top-level CLI (``--context`` / ``-c`` /
    ``PRAGMA_CONTEXT``), so the same key every reader uses.

    With ``--with-token`` it opens no browser: it reads one session token
    from a pipe on stdin, checks it with the API, and saves it. A session
    token is the token sign-in issues; API keys are not stored, pass them
    with ``--token`` or ``PRAGMA_AUTH_TOKEN``.

    Example:
        pragma auth login
        pragma auth login --context production
        pragma auth login --org ycombinator
        pragma auth token | pragma auth login --with-token --context staging

    \f

    Args:
        ctx: Typer context carrying the top-level resolved context name.
        context: Explicit context to authenticate for, overriding the resolved one.
        org: Organization slug to pre-select, skipping the org picker.
        with_token: Read the session token from stdin instead of the browser.

    Raises:
        click.UsageError: If ``--with-token`` is combined with ``--org``.
        UnknownContextError: If the context is not in the configuration.
        typer.Exit: If authentication fails/times out, the token is already
            expired, stdin is a terminal, empty, holds more than one value or
            not a session token, or the API refuses the stdin token.
    """  # noqa: DOC502
    if with_token and org:
        raise click.UsageError("--with-token cannot be combined with --org; the token carries its organization.")

    if context is None:
        context = ctx.obj["context"]

    config = load_config()
    context_config = select_context(config, context)

    error_console.print(f"[cyan]Authenticating for context:[/cyan] {escape(context)}")
    error_console.print(f"[cyan]API URL:[/cyan] {escape(context_config.api_url)}")
    error_console.print()

    if with_token:
        token = read_stdin_session_token()
        verify_session_token(config, context, token)
    else:
        token = receive_browser_token(context_config, org)
        require_unexpired_token(token)

    save_credentials(token, context)
    print_login_success(context, config.current_context)


def read_stdin_session_token() -> str:
    """Read one unexpired session token from a pipe on stdin.

    Returns:
        The only whitespace-separated value on stdin.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` if stdin is a terminal,
            holds no value or more than one, or the value is not a JWT,
            such as an API key, or is an expired one.
    """  # noqa: DOC502
    if sys.stdin.isatty():
        report_input_error(ValueError(WITH_TOKEN_TERMINAL_MESSAGE))

    values = sys.stdin.read().split()

    if not values:
        report_input_error(ValueError(WITH_TOKEN_EMPTY_MESSAGE))

    if len(values) > 1:
        report_input_error(ValueError(WITH_TOKEN_MULTIPLE_MESSAGE))

    token = values[0]

    if parse_token_claims(token) is None:
        report_input_error(ValueError(WITH_TOKEN_NOT_SESSION_MESSAGE))

    if is_token_expired(token):
        report_input_error(ValueError(WITH_TOKEN_EXPIRED_MESSAGE))

    return token


def verify_session_token(config: PragmaConfig, context: str, token: str) -> None:
    """Check a session token with the context's API before it is saved.

    Args:
        config: Loaded configuration.
        context: Context whose API checks the token.
        token: Session token read from stdin.

    Raises:
        httpx.HTTPStatusError: If the API refuses the check with any status
            but 401, for the global handler.
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the API refuses the token
            (401), or with ``INPUT_ERROR_EXIT_CODE`` if the context's API URL
            is not usable.
    """  # noqa: DOC502
    with build_client(config, context, token) as verification_client:
        try:
            fetch_with_spinner("Verifying the token...", verification_client.get_me)
        except httpx.HTTPStatusError as error:
            if error.response.status_code != 401:
                raise

            rejected_message = WITH_TOKEN_REJECTED_MESSAGE.format(context=context)
            error_console.print(f"[red]Error:[/red] {escape(rejected_message)}")
            raise typer.Exit(FAILURE_EXIT_CODE) from error


def receive_browser_token(context_config: ContextConfig, org: str | None) -> str:
    """Open the browser login page and wait for its callback to deliver a token.

    Args:
        context_config: Context whose auth URL the browser opens.
        org: Organization slug to pre-select, skipping the org picker.

    Returns:
        The token the callback delivered.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if no token arrives before
            the callback server times out.
    """
    auth_url = context_config.derive_auth_url()
    login_url = build_login_url(context_config, org=org)

    server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)

    error_console.print(f"[yellow]Opening browser to:[/yellow] {escape(auth_url)}")
    error_console.print()
    error_console.print("[dim]If browser doesn't open automatically, visit:[/dim]")
    error_console.print(f"[dim]{escape(login_url)}[/dim]")
    error_console.print()
    error_console.print("[yellow]Waiting for authentication...[/yellow]")

    webbrowser.open(login_url)

    server.timeout = 300
    server.handle_request()

    if not CallbackHandler.token:
        error_console.print()
        error_console.print("[red]✗ Authentication failed or timed out[/red]")
        error_console.print("[dim]Please try again[/dim]")
        raise typer.Exit(FAILURE_EXIT_CODE)

    return CallbackHandler.token


def require_unexpired_token(token: str) -> None:
    """Stop the browser login when the token its callback delivered is already expired.

    Args:
        token: Token the browser callback delivered.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` if the token is expired.
    """
    if is_token_expired(token):
        error_console.print()
        error_console.print("[red]✗ Received an expired token[/red]")
        error_console.print("[dim]Please run 'pragma auth login' again[/dim]")
        raise typer.Exit(FAILURE_EXIT_CODE)


def print_login_success(context: str, current_context: str) -> None:
    """Print where the credentials were saved and what to run next.

    Args:
        context: Context the token was saved under.
        current_context: The configured current context, to hint at
            activating ``context`` when they differ.
    """
    print()
    print("[green]✓ Successfully authenticated![/green]")
    print(f"[dim]Credentials saved to {escape(str(CREDENTIALS_FILE))}[/dim]")

    if context != current_context:
        activation_command = f"pragma config use-context {escape(context)}"
        print(f"[dim]Context '{escape(context)}' is not active; run '{activation_command}' to activate it.[/dim]")

    print()
    print("[bold]You can now use pragma commands:[/bold]")
    print("  pragma resources list")
    print(escape("  pragma resources get <org/provider/resource[/name]>"))
    print("  pragma resources apply <file.yaml>")


@app.command()
def logout(
    ctx: typer.Context,
    context: str | None = typer.Option(None, help="Context to logout from (all if not specified)"),
    all: bool = typer.Option(False, "--all", help="Logout from all contexts"),
):
    """Clear stored authentication credentials.

    Clears the credential for this command's ``--context`` if given, otherwise
    the context resolved by the top-level CLI (``--context`` / ``-c`` /
    ``PRAGMA_CONTEXT``), so it removes the same key ``login`` wrote.

    Example:
        pragma logout                    # Clear current context
        pragma logout --all              # Clear all contexts
        pragma logout --context staging  # Clear specific context

    \f

    Args:
        ctx: Typer context carrying the top-level resolved context name.
        context: Explicit context to clear, overriding the resolved one.
        all: Clear credentials for every context.
    """
    if all:
        clear_credentials(None)
        print("[green]✓[/green] Cleared all credentials")
    elif context:
        clear_credentials(context)
        print(f"[green]✓[/green] Cleared credentials for context '{escape(context)}'")
    else:
        context_name = ctx.obj["context"]
        clear_credentials(context_name)
        print(f"[green]✓[/green] Cleared credentials for current context '{escape(context_name)}'")


@app.command()
def token():
    """Print the current context's bearer token to stdout.

    Emits only the raw token on stdout so it can be captured in a shell:
    ``TOKEN=$(pragma auth token)``. Resolves the token the same way as every
    other command (--token flag > PRAGMA_AUTH_TOKEN env var > stored
    credentials). A missing or expired token is an error: the login-required
    message goes to stderr and stdout stays empty.

    \f

    Raises:
        typer.Exit: If no token is stored or it is expired.
    """  # noqa: DOC502
    client = get_client()
    current_token = extract_client_token(client)

    if not current_token or is_token_expired(current_token):
        report_login_required()

    typer.echo(current_token)


@app.command()
def whoami(ctx: typer.Context):
    """Show current authentication status and user information.

    Uses the same token resolution as all other commands:
    --token flag > PRAGMA_AUTH_TOKEN env var > stored credentials.

    Displays the resolved context (``--context`` / ``-c`` / ``PRAGMA_CONTEXT``),
    authentication state, and user details including email and organization
    name from the API. No token or an expired one is a status report on
    stdout, not an error; a token the API rejects, or an API that cannot be
    reached, is an error on stderr.

    \f

    Args:
        ctx: Typer context carrying the top-level resolved context name.
    """
    current_context_name = ctx.obj["context"]
    client = get_client()

    token = extract_client_token(client)

    if not token:
        print_authentication_status(current_context_name, "[yellow]Not authenticated[/yellow]")
        console.print()
        console.print("[dim]Run 'pragma auth login' to authenticate[/dim]")
        return

    if is_token_expired(token):
        print_authentication_status(current_context_name, "[yellow]Token expired[/yellow]")
        console.print()
        console.print("[dim]Run 'pragma auth login' to re-authenticate.[/dim]")
        return

    user_info = client.get_me()

    print_authentication_status(current_context_name, "[green]✓ Authenticated[/green]")

    console.print()
    console.print("[bold]User Information[/bold]")
    console.print()
    console.print(f"  User ID:      [cyan]{escape(user_info.user_id)}[/cyan]")

    if user_info.email:
        console.print(f"  Email:        [cyan]{escape(user_info.email)}[/cyan]")
    else:
        console.print("  Email:        [dim]Not set[/dim]")

    console.print(f"  Organization: [cyan]{escape(user_info.organization_name or user_info.organization_id)}[/cyan]")


def print_authentication_status(context_name: str, status: str) -> None:
    """Print the Authentication Status heading with the context name and a status line.

    Args:
        context_name: Resolved context name.
        status: Status text, with Rich markup.
    """
    console.print()
    console.print("[bold]Authentication Status[/bold]")
    console.print()
    console.print(f"  Context: [cyan]{escape(context_name)}[/cyan]")
    console.print(f"  Status:  {status}")
