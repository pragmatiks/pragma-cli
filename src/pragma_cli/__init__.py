"""CLI global client management."""

from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable

    from pragma_sdk import PragmaClient


_client: PragmaClient | None = None
_client_factory: Callable[[], PragmaClient] | None = None


def get_client() -> PragmaClient:
    """Get the shared client, building it with the factory on first use.

    Returns:
        The PragmaClient instance every command shares.

    Raises:
        RuntimeError: If no client factory was set via ``set_client_factory``.
        UnknownContextError: On first use, if the context the factory builds
            the client for is not in the configuration.
    """  # noqa: DOC502
    global _client

    if _client is None:
        if _client_factory is None:
            raise RuntimeError("Client not initialized. This should not happen.")

        _client = _client_factory()

    return _client


def set_client_factory(factory: Callable[[], PragmaClient]) -> None:
    """Set how the shared client is built, and drop any client built before.

    Args:
        factory: Zero-argument callable building the PragmaClient; it may
            raise for an unknown context or a malformed config, which then
            surfaces from the first ``get_client`` call.
    """
    global _client, _client_factory
    _client = None
    _client_factory = factory
