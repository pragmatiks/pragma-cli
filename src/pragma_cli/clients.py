"""Building the SDK client a command calls the API with."""

from __future__ import annotations

from pragma_sdk import PragmaClient

from pragma_cli.config import PragmaConfig, is_valid_api_url, select_context
from pragma_cli.errors import report_invalid_api_url


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
