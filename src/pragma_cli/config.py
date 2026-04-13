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


_ALLOW_SYMLINK_ENV_VAR = "PRAGMA_ALLOW_CONFIG_DIR_SYMLINK"


class MalformedConfigError(RuntimeError):
    """Raised when the config file exists but cannot be parsed or validated."""


class ConfigDirSymlinkError(RuntimeError):
    """Raised when the config directory itself is a symlink.

    Defense-in-depth against an attacker who pre-creates
    ``~/.config/pragma`` as a symlink to attacker-controlled storage.
    Opt-out via ``PRAGMA_ALLOW_CONFIG_DIR_SYMLINK=1``.
    """


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


def _assert_config_dir_not_symlink() -> None:
    """Reject a symlink anywhere in the config directory chain.

    Defense-in-depth against an attacker who pre-creates the config
    dir — or any parent of it — as a symlink to attacker-controlled
    storage, redirecting the CLI into their namespace. ``lstat`` on
    the final segment alone misses parent-chain attacks: if
    ``XDG_CONFIG_HOME`` is set to ``/home/user/.config`` and
    ``.config`` is a symlink to ``/attacker-root``, the CLI would
    silently load ``/attacker-root/pragma/config``.

    The full-chain check uses ``os.path.realpath`` to canonicalize
    every component and compares against the textual normalization.
    Any divergence indicates a symlink somewhere in the chain. The
    leaf ``lstat`` is kept as a belt-and-braces check in case an
    edge platform handles ``realpath`` differently. Subsequent opens
    under ``CONFIG_DIR`` rely on ``O_NOFOLLOW`` to block tampering
    inside the resolved directory itself.

    Opt-out: set ``PRAGMA_ALLOW_CONFIG_DIR_SYMLINK=1`` if your real
    config dir or any parent is itself a symlink (dotfile managers
    such as chezmoi or stow, home directory relocation, etc.).
    Raising is the safer default.

    Raises:
        ConfigDirSymlinkError: If ``CONFIG_DIR`` or any parent of it
            is a symlink and the opt-out env var is not set.
    """
    if os.getenv(_ALLOW_SYMLINK_ENV_VAR):
        return

    expected = os.path.normpath(str(CONFIG_DIR))
    try:
        resolved = os.path.realpath(CONFIG_DIR)
    except OSError:
        resolved = expected

    if resolved != expected:
        raise ConfigDirSymlinkError(
            f"Refusing to use {CONFIG_DIR}: the path resolves through a symlink "
            f"to {resolved}. Set {_ALLOW_SYMLINK_ENV_VAR}=1 to opt in if this is "
            f"intentional (dotfile managers, home directory relocation, etc.)."
        )

    try:
        st = os.lstat(CONFIG_DIR)
    except FileNotFoundError:
        return
    except OSError:
        return

    if stat.S_ISLNK(st.st_mode):
        raise ConfigDirSymlinkError(
            f"Refusing to use {CONFIG_DIR}: the path is a symlink. "
            f"Set {_ALLOW_SYMLINK_ENV_VAR}=1 to opt in if this is intentional."
        )


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


def _validate_lock_fd(fd: int) -> None:
    """Validate that ``fd`` refers to a regular file and close it on failure.

    Args:
        fd: Already-opened lock file descriptor.

    Raises:
        RuntimeError: If the fd does not refer to a regular file. The
            fd is closed before raising.
        OSError: If ``os.fstat`` fails. The fd is closed before
            propagating.
    """
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


def _open_lock_file_for_write() -> int:
    """Open the config lock file for writing, creating it if missing.

    Uses ``O_NOFOLLOW`` so an attacker cannot point ``config.lock`` at
    another file to break mutual exclusion. Verifies that the opened
    fd refers to a regular file — ``ELOOP`` from the open itself
    blocks symlink attacks, and the ``fstat`` check defends against
    directories, pipes, and device nodes pre-planted at the lock path.

    Returns:
        File descriptor opened ``O_RDWR | O_CREAT | O_NOFOLLOW``.

    Raises:
        RuntimeError: If the lock path is not a regular file.
        OSError: If the lock file cannot be created or opened.
    """  # noqa: DOC502
    fd = os.open(
        CONFIG_LOCK_PATH,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    _validate_lock_fd(fd)
    return fd


def _open_lock_file_for_read() -> int:
    """Open an existing config lock file for a shared read lock.

    Deliberately omits ``O_CREAT`` so the read path never writes to
    disk. On read-only homes (CI sandboxes, restricted NFS) this lets
    every CLI command that only reads config complete successfully,
    even when ``config.lock`` has never been created. Callers fall
    back to lock-free reads on ``FileNotFoundError`` because the
    absence of the lock file implies no concurrent writer exists.

    Returns:
        File descriptor opened ``O_RDONLY | O_NOFOLLOW``.

    Raises:
        RuntimeError: If the lock path is not a regular file.
        FileNotFoundError: If ``config.lock`` does not yet exist.
        OSError: If the lock file cannot be opened for any other reason.
    """  # noqa: DOC502
    fd = os.open(
        CONFIG_LOCK_PATH,
        os.O_RDONLY | os.O_NOFOLLOW,
    )
    _validate_lock_fd(fd)
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

    Write path (exclusive lock) creates the config directory and the
    lock file on demand. Read path (shared lock) never creates
    on-disk state: it opens an existing lock file for read, and when
    ``config.lock`` is absent it yields without acquiring a lock. The
    absence of the lock file means no concurrent writer could have
    created one, so lock-free reads are safe — and this is the only
    way ``pragma`` stays usable on read-only homes (CI sandboxes,
    restricted NFS mounts, read-only containers).

    Args:
        exclusive: ``True`` for a writer lock, ``False`` for a shared
            reader lock.

    Yields:
        ``None`` — the caller performs file access while the lock is held.
    """
    _assert_config_dir_not_symlink()

    if exclusive:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = _open_lock_file_for_write()
    else:
        try:
            fd = _open_lock_file_for_read()
        except FileNotFoundError:
            yield
            return

    lock_op = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    lock_handle = os.fdopen(fd, "r+" if exclusive else "r")
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

    Writes to a sibling tempfile, fsyncs the tempfile, atomically
    renames it over the destination, and fsyncs the parent directory
    so the rename itself is durable across a power loss. Without the
    directory fsync the rename can be reordered on some filesystems
    and a crash leaves the config file missing entirely — even though
    the tempfile's bytes made it to disk.

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

    _fsync_directory(CONFIG_DIR)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory's metadata so a rename is durable on crash.

    Opens the directory with ``O_RDONLY | O_DIRECTORY`` and calls
    ``os.fsync`` on the fd. Silently ignores platforms where the
    syscall is not supported (Windows) — the call is defensive
    hardening, not a correctness requirement on every OS.

    Args:
        directory: Directory whose metadata should be flushed.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return

    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


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
    _assert_config_dir_not_symlink()

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
