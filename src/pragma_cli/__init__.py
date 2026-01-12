"""CLI global client management."""

from __future__ import annotations

from pragma_sdk import PragmaClient


_client: PragmaClient | None = None


def get_client() -> PragmaClient:
    """Get the initialized client.

    Returns:
        Initialized PragmaClient instance.

    Raises:
        RuntimeError: If client has not been initialized via main().
    """
    if _client is None:
        raise RuntimeError("Client not initialized. This should not happen.")
    return _client


def set_client(client: PragmaClient):
    """Set the global client instance.

    Args:
        client: The PragmaClient instance to use globally
    """
    global _client
    _client = client
