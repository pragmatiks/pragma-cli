"""CLI configuration management for contexts and credentials."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel


def _get_config_dir() -> Path:
    """Get the configuration directory following XDG Base Directory specification.

    Returns:
        Path to configuration directory (~/.config/pragma by default).
    """
    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "pragma"
    return Path.home() / ".config" / "pragma"


CONFIG_DIR = _get_config_dir()
CONFIG_PATH = CONFIG_DIR / "config"
CREDENTIALS_FILE = CONFIG_DIR / "credentials"


class ContextConfig(BaseModel):
    """Configuration for a single CLI context."""

    api_url: str
    auth_url: str | None = None

    def get_auth_url(self) -> str:
        """Get the auth URL, deriving from api_url if not explicitly set.

        Returns:
            Auth URL for Clerk authentication.
        """
        if self.auth_url:
            return self.auth_url

        # Handle localhost: default to port 3000 for web app
        parsed = urlparse(self.api_url)
        if parsed.hostname in ("localhost", "127.0.0.1"):
            return "http://localhost:3000"

        # Derive from api_url: api.pragmatiks.io -> app.pragmatiks.io
        return self.api_url.replace("://api.", "://app.")


class PragmaConfig(BaseModel):
    """CLI configuration with multiple named contexts."""

    current_context: str
    contexts: dict[str, ContextConfig]


def load_config() -> PragmaConfig:
    """Load config from ~/.config/pragma/config.

    Returns:
        PragmaConfig with contexts loaded from file, or default if not found.
    """
    if not CONFIG_PATH.exists():
        return PragmaConfig(
            current_context="default", contexts={"default": ContextConfig(api_url="https://api.pragmatiks.io")}
        )

    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f)
        return PragmaConfig.model_validate(data)


def save_config(config: PragmaConfig):
    """Save config to ~/.config/pragma/config."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(config.model_dump(), f)
    CONFIG_PATH.chmod(0o644)


def get_current_context(context_name: str | None = None) -> tuple[str, ContextConfig]:
    """Get context name and configuration.

    Args:
        context_name: Explicit context name. If None, uses current context from config.

    Returns:
        Tuple of (context_name, context_config).

    Raises:
        ValueError: If context not found in configuration.
    """
    config = load_config()

    if context_name is None:
        context_name = config.current_context

    if context_name not in config.contexts:
        raise ValueError(f"Context '{context_name}' not found in configuration")

    return context_name, config.contexts[context_name]
