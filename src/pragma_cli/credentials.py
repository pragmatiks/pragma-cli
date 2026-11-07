"""Stored credentials and the token a client authenticates with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pragma_cli.config import CREDENTIALS_FILE


if TYPE_CHECKING:
    from pragma_sdk import PragmaClient


def extract_client_token(client: PragmaClient) -> str | None:
    """Extract the bearer token a client authenticates with.

    Args:
        client: SDK client instance.

    Returns:
        The client's token, or None when it has no credentials.
    """
    return client.auth_token


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
