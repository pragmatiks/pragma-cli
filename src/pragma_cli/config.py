"""CLI configuration management for contexts and credentials."""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ValidationError


class MalformedConfigError(RuntimeError):
    """Raised when the config file exists but cannot be parsed or validated."""


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
CONFIG_LOCK_PATH = CONFIG_DIR / "config.lock"
CREDENTIALS_FILE = CONFIG_DIR / "credentials"


class ContextConfig(BaseModel):
    """Configuration for a single CLI context."""

    api_url: str
    auth_url: str | None = None
    project: str | None = None

    def get_auth_url(self) -> str:
        """Get the auth URL, deriving from api_url if not explicitly set.

        Returns:
            Auth URL for Clerk authentication.
        """
        if self.auth_url:
            return self.auth_url

        parsed = urlparse(self.api_url)
        if parsed.hostname in ("localhost", "127.0.0.1"):
            return "http://localhost:3000"

        return self.api_url.replace("://api.", "://app.")


class PragmaConfig(BaseModel):
    """CLI configuration with multiple named contexts."""

    current_context: str
    contexts: dict[str, ContextConfig]


def _default_config() -> PragmaConfig:
    """Return the built-in default config used when no file exists.

    Returns:
        PragmaConfig with a single ``default`` context pointing at prod.
    """
    return PragmaConfig(
        current_context="default",
        contexts={"default": ContextConfig(api_url="https://api.pragmatiks.io")},
    )


def _parse_config_text(text: str) -> PragmaConfig:
    """Parse raw YAML into a validated PragmaConfig.

    Args:
        text: Raw config file contents.

    Returns:
        Validated PragmaConfig.

    Raises:
        MalformedConfigError: If the text is invalid YAML or fails Pydantic validation.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MalformedConfigError(f"config file at {CONFIG_PATH} is malformed: {e}") from e

    try:
        return PragmaConfig.model_validate(data)
    except ValidationError as e:
        raise MalformedConfigError(f"config file at {CONFIG_PATH} is malformed: {e}") from e


def _open_lock_file() -> int:
    """Open the config lock file, refusing to follow symlinks.

    Uses ``O_NOFOLLOW`` so an attacker cannot point ``config.lock`` at
    ``config`` (or anywhere else) to break mutual exclusion. Verifies
    that the opened fd refers to a regular file — a symlink attack
    would cause the open to fail with ``ELOOP``, but defensive checks
    for other special files (directories, pipes, devices) are also
    made.

    Returns:
        File descriptor for the lock file, opened O_RDWR.

    Raises:
        RuntimeError: If the lock path is not a regular file.
        OSError: If the lock file cannot be opened (e.g., ELOOP when
            the path is a symlink, or permission errors).
    """
    fd = os.open(
        CONFIG_LOCK_PATH,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        raise

    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise RuntimeError(
            f"Refusing to use {CONFIG_LOCK_PATH}: not a regular file. "
            "This may indicate tampering or a misconfigured config directory."
        )

    return fd


@contextmanager
def _config_lock(exclusive: bool) -> Iterator[None]:
    """Acquire an advisory lock on the config lock file.

    Uses ``fcntl.flock`` on a sibling lock file opened with
    ``O_NOFOLLOW`` so the lock file cannot be swapped for a symlink
    mid-hold. The fd is verified to refer to a regular file before
    locking. Serializes concurrent ``pragma`` invocations so readers
    cannot observe a truncated file and writers cannot lose each
    other's updates.

    Read path (shared lock) is gated: if the config directory does
    not exist and this is a shared lock, the caller short-circuits
    without creating any on-disk state. Only the exclusive lock
    (writer) may create the directory or lock file.

    Args:
        exclusive: ``True`` for a writer lock, ``False`` for a shared
            reader lock.

    Yields:
        ``None`` — the caller performs file access while the lock is held.
    """
    if exclusive:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    fd = _open_lock_file()
    lock_op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    lock_handle = os.fdopen(fd, "r+")
    try:
        fcntl.flock(lock_handle.fileno(), lock_op)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def _atomic_write(text: str) -> None:
    """Atomically replace the config file with ``text``.

    Writes to a sibling tempfile and calls ``os.replace`` so readers
    never observe a truncated or partially-written file.

    Args:
        text: Serialized config contents.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        prefix=CONFIG_PATH.name + ".",
        suffix=".tmp",
        dir=str(CONFIG_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w") as tmp_handle:
            tmp_handle.write(text)
            tmp_handle.flush()
            os.fsync(tmp_handle.fileno())
        os.replace(tmp_path, CONFIG_PATH)
        CONFIG_PATH.chmod(0o644)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_config() -> PragmaConfig:
    """Load config from ``~/.config/pragma/config`` under a shared lock.

    Read path: never creates any on-disk state. If the config
    directory or file does not exist, returns built-in defaults
    without touching the filesystem. This keeps the CLI usable on
    read-only home directories (CI sandboxes, read-only containers,
    restricted NFS mounts).

    Returns:
        PragmaConfig with contexts loaded from file, or default if not found.

    Raises:
        MalformedConfigError: If the config file exists but is corrupted.
    """  # noqa: DOC502
    if not CONFIG_DIR.exists() or not CONFIG_PATH.exists():
        return _default_config()

    with _config_lock(exclusive=False):
        if not CONFIG_PATH.exists():
            return _default_config()
        text = CONFIG_PATH.read_text()

    return _parse_config_text(text)


def save_config(config: PragmaConfig) -> None:
    """Serialize and atomically persist ``config`` under an exclusive lock.

    Args:
        config: Config to persist.
    """
    text = yaml.safe_dump(config.model_dump())
    with _config_lock(exclusive=True):
        _atomic_write(text)


@contextmanager
def update_config() -> Iterator[PragmaConfig]:
    """Atomically read-modify-write the CLI config file.

    Acquires an exclusive advisory lock for the entire
    read-modify-write window so that concurrent
    ``pragma config set-context`` / ``pragma projects use`` invocations
    cannot lose updates or observe a truncated file.

    Yields:
        Mutable PragmaConfig that will be persisted on context exit.

    Raises:
        MalformedConfigError: If the existing file is corrupted.
    """  # noqa: DOC502
    with _config_lock(exclusive=True):
        if CONFIG_PATH.exists():
            text = CONFIG_PATH.read_text()
            config = _parse_config_text(text) if text.strip() else _default_config()
        else:
            config = _default_config()

        yield config

        _atomic_write(yaml.safe_dump(config.model_dump()))


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
