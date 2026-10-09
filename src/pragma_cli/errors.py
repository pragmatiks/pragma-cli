"""Error reporting on stderr.

Each ``report_*`` function prints one kind of error and exits with the code
``pragma_cli.exit_codes`` assigns it; ``print_api_error`` prints an API error
without exiting.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from pragma_cli.config import CONFIG_PATH
from pragma_cli.exit_codes import (
    FAILURE_EXIT_CODE,
    INPUT_ERROR_EXIT_CODE,
    NOT_FOUND_EXIT_CODE,
    compute_http_exit_code,
)
from pragma_cli.helpers import parse_api_error_message


if TYPE_CHECKING:
    import json

    import jsonschema
    from pragma_sdk import PragmaClient
    from pydantic import ValidationError


LOGIN_REQUIRED_MESSAGE = "Not authenticated. Run 'pragma auth login' to authenticate."
"""Error message for a missing, expired or rejected token, telling the user to run ``pragma auth login``."""

INVALID_REQUEST_MESSAGE = (
    "The request is invalid, so it was not sent. Check the values given to the command, such as the token."
)
"""Error message for a request the HTTP client refused to send, such as one carrying a token with a line break."""

CONNECTION_HINT = "Check that the API URL is correct and the server is running."
"""Hint printed when the API could not be reached or the connection failed for a reason with no phrase of its own."""

ORGANIZATION_BOOTSTRAPPING = "organization_bootstrapping"
"""``error`` value of the API's 503 body while the caller's organization is still being set up."""

ORGANIZATION_BOOTSTRAP_FAILED = "organization_bootstrap_failed"
"""``error`` value of the API's 503 body when setting up the caller's organization failed."""

error_console = Console(stderr=True)
"""Console for errors and warnings; it writes to stderr so stdout carries only command output."""


def require_auth(client: PragmaClient) -> None:
    """Stop a command that needs credentials when the caller has none.

    Args:
        client: SDK client instance.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE`` after printing
            ``LOGIN_REQUIRED_MESSAGE``, if the client has no credentials.
    """  # noqa: DOC502
    if client._auth is None:
        report_login_required()


def report_login_required() -> NoReturn:
    """Print ``LOGIN_REQUIRED_MESSAGE`` and exit.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    error_console.print(f"[red]Error:[/red] {LOGIN_REQUIRED_MESSAGE}")
    raise typer.Exit(FAILURE_EXIT_CODE)


def report_api_error(error: httpx.HTTPStatusError) -> NoReturn:
    """Print an API error and exit.

    A 503 saying the caller's organization is not set up yet prints the
    set-up message instead of the API's.

    Args:
        error: The HTTP status error a request raised.

    Raises:
        typer.Exit: Always, with the code ``compute_http_exit_code`` gives
            the status.
    """
    check_bootstrap_error(error)
    print_api_error(error)
    raise typer.Exit(compute_http_exit_code(error.response.status_code)) from error


def print_api_error(error: httpx.HTTPStatusError, heading: str = "Error") -> None:
    """Print an API error under a heading, without exiting.

    Args:
        error: The HTTP status error a request raised.
        heading: What failed, such as ``Error deleting acme/db/database/main``.
    """
    error_console.print(f"[red]{escape(heading)}:[/red] {escape(format_api_error(error))}")


def format_api_error(error: httpx.HTTPStatusError) -> str:
    """Format what an API error response means for the user.

    Args:
        error: The HTTP status error a request raised.

    Returns:
        ``LOGIN_REQUIRED_MESSAGE`` for a 401, else the API's message, else
        the status, reason and URL, such as ``500 Internal Server Error for
        https://api.pragmatiks.io/providers``. Not escaped for Rich markup.
    """
    response = error.response

    if response.status_code == 401:
        return LOGIN_REQUIRED_MESSAGE

    message = parse_api_error_message(response)

    if message:
        return message

    return f"{response.status_code} {response.reason_phrase} for {error.request.url}"


def check_bootstrap_error(error: httpx.HTTPStatusError) -> None:
    """Report a 503 saying the caller's organization is not set up yet, and exit.

    Returns without printing anything for any other response, so the caller
    reports the error itself.

    Args:
        error: HTTP status error raised by the SDK.

    Raises:
        typer.Exit: With ``FAILURE_EXIT_CODE``, if the API answered 503 with
            an organization-bootstrap ``error`` in its body.
    """
    if error.response.status_code != 503:
        return

    try:
        body = error.response.json()
    except ValueError:
        return

    if not isinstance(body, dict):
        return

    kind = body.get("error")

    if kind == ORGANIZATION_BOOTSTRAPPING:
        error_console.print("[yellow]Your workspace is still being set up. Please try again in a moment.[/yellow]")
        raise typer.Exit(FAILURE_EXIT_CODE)

    if kind == ORGANIZATION_BOOTSTRAP_FAILED:
        error_console.print("[red]Your workspace setup failed. Please contact support.[/red]")
        raise typer.Exit(FAILURE_EXIT_CODE)


def report_not_found(error: httpx.HTTPStatusError, not_found_message: str) -> NoReturn:
    """Print the command's not-found message and exit for a 404; re-raise any other API error.

    Args:
        error: The HTTP status error the request raised.
        not_found_message: What a 404 means for this request, such as
            ``Provider 'acme/x' not found in the store.``, printed with Rich
            markup escaped.

    Raises:
        typer.Exit: With ``NOT_FOUND_EXIT_CODE`` if the API answered 404.
        httpx.HTTPStatusError: For any other status, re-raised unchanged.
    """  # noqa: DOC502
    exit_code = compute_http_exit_code(error.response.status_code)

    if exit_code != NOT_FOUND_EXIT_CODE:
        raise error

    error_console.print(f"[red]Error:[/red] {escape(not_found_message)}")
    raise typer.Exit(exit_code) from error


def report_request_error(error: httpx.RequestError) -> NoReturn:
    """Print what kept a request from getting an answer from the API, and exit.

    Args:
        error: The request error a request raised.

    An invalid request prints ``INVALID_REQUEST_MESSAGE`` and never the
    request, since it can carry the bearer token.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    if isinstance(error, httpx.LocalProtocolError):
        error_console.print(f"[red]Error:[/red] {INVALID_REQUEST_MESSAGE}")
        raise typer.Exit(FAILURE_EXIT_CODE) from error

    base_url = escape(format_request_base_url(error))
    error_console.print(f"[red]Error:[/red] Request to {base_url} failed: {escape(format_request_failure(error))}.")

    if is_connection_failure(error):
        error_console.print(CONNECTION_HINT)

    raise typer.Exit(FAILURE_EXIT_CODE) from error


def report_invalid_api_url(context_name: str, api_url: str) -> NoReturn:
    """Print that a context's API URL cannot be requested, with the command that fixes it, and exit.

    Args:
        context_name: Context whose API URL it is.
        api_url: The context's API URL.

    Raises:
        typer.Exit: Always, with ``INPUT_ERROR_EXIT_CODE``.
    """
    error_console.print(
        f"[red]Error:[/red] The API URL '{escape(api_url)}' of context "
        f"'{escape(context_name)}' is not an http:// or https:// URL with a host."
    )
    error_console.print(f"Fix it with 'pragma config set-context {escape(context_name)} --api-url <url>'.")
    raise typer.Exit(INPUT_ERROR_EXIT_CODE)


def is_request_unsent(error: httpx.RequestError) -> bool:
    """Tell whether a request error happened before the API received any of the request.

    Args:
        error: The request error a request raised.

    Returns:
        ``True`` if the request is invalid, or connecting to the API failed
        or timed out.
    """
    return isinstance(error, httpx.LocalProtocolError | httpx.ConnectError | httpx.ConnectTimeout)


def format_request_base_url(error: httpx.RequestError) -> str:
    """Format the scheme, host and port of the request a request error interrupted.

    Args:
        error: The request error a request raised.

    Returns:
        A URL such as ``https://api.pragmatiks.io``, or ``unknown`` when the
        request URL has no scheme or no host.
    """
    url = error.request.url

    if not url.scheme or not url.host:
        return "unknown"

    return f"{url.scheme}://{url.host}:{url.port}" if url.port else f"{url.scheme}://{url.host}"


def format_request_failure(error: httpx.RequestError) -> str:
    """Format what kept a request from getting an answer from the API, in plain words.

    Args:
        error: The request error the request raised.

    Returns:
        A lowercase phrase naming the cause, such as ``the API did not
        answer in time``, without a trailing period. An invalid request is
        not described further, since its message can carry header values
        such as the bearer token. A cause without a phrase of its own, such
        as a proxy failure, is named by its error type and message: ``the
        connection to the API failed (ProxyError: ...)``.
    """
    match error:
        case httpx.ConnectError() | httpx.ConnectTimeout():
            return "the API could not be reached"
        case httpx.WriteTimeout():
            return "sending to the API timed out"
        case httpx.ReadTimeout():
            return "the API did not answer in time"
        case httpx.TimeoutException():
            return "the connection to the API timed out"
        case httpx.NetworkError():
            return "the connection to the API broke off"
        case httpx.LocalProtocolError():
            return "the request could not be sent because it is invalid"
        case httpx.ProtocolError():
            return "the connection closed before the API answered"
        case _:
            detail = str(error).rstrip(".")
            cause = f"{type(error).__name__}: {detail}" if detail else type(error).__name__
            return f"the connection to the API failed ({cause})"


def is_connection_failure(error: httpx.RequestError) -> bool:
    """Tell whether a request error means the API URL or the server may be wrong.

    Args:
        error: The request error a request raised.

    Returns:
        ``True`` if no request reached the API, or the error has no phrase
        of its own in ``format_request_failure`` (a proxy failure, for
        example); ``False`` for a timeout, a dropped connection or a
        protocol error once connected.
    """
    if is_request_unsent(error):
        return True

    return not isinstance(error, httpx.TimeoutException | httpx.NetworkError | httpx.ProtocolError)


def report_input_error(error: Exception) -> NoReturn:
    """Print a local input or config error and exit.

    Args:
        error: Project-scoping mismatch, invalid resource identity, invalid
            request input, malformed config file, or unknown context.

    Raises:
        typer.Exit: Always, with ``INPUT_ERROR_EXIT_CODE``.
    """
    error_console.print(f"[red]Error:[/red] {escape(str(error))}")
    raise typer.Exit(INPUT_ERROR_EXIT_CODE) from error


def report_unreadable_response(error: ValidationError | json.JSONDecodeError | jsonschema.SchemaError) -> NoReturn:
    """Print that the API answered with data the CLI cannot read, and exit.

    Args:
        error: Error raised while parsing or validating an API response: an
            invalid model, a body that is not JSON, or a resource schema
            that is not a valid JSON schema.

    Raises:
        typer.Exit: Always, with ``FAILURE_EXIT_CODE``.
    """
    error_console.print(f"[red]Error:[/red] Could not read the API's response: {escape(str(error))}")
    raise typer.Exit(FAILURE_EXIT_CODE) from error


def report_os_error(error: OSError) -> NoReturn:
    """Print an operating system error and exit.

    An error naming a file is a local file error: it prints that path, with
    a hint about ``XDG_CONFIG_HOME`` when the path is the config file or its
    directory. Any other OS error, such as a port already in use, is a
    failure.

    Args:
        error: OS error raised by a local file or system operation.

    Raises:
        typer.Exit: With ``INPUT_ERROR_EXIT_CODE`` for an error naming a
            file, else with ``FAILURE_EXIT_CODE``.
    """
    if error.filename is None:
        error_console.print(f"[red]Error:[/red] {escape(str(error))}")
        raise typer.Exit(FAILURE_EXIT_CODE) from error

    failing_path = str(error.filename)
    error_console.print(f"[red]Error:[/red] could not access {escape(failing_path)}: {escape(str(error))}")

    if failing_path in (str(CONFIG_PATH), str(CONFIG_PATH.parent)):
        error_console.print(
            "[dim]Check file permissions and that the directory exists. "
            "You can set XDG_CONFIG_HOME to override the default config location.[/dim]"
        )
    else:
        error_console.print("[dim]Check file permissions and that the directory exists.[/dim]")

    raise typer.Exit(INPUT_ERROR_EXIT_CODE) from error
