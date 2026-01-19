import pytest

from pragma_cli.config import (
    ContextConfig,
    PragmaConfig,
    get_current_context,
    load_config,
    save_config,
)
from pragma_cli.main import app


@pytest.fixture
def temp_config_file(tmp_path, monkeypatch):
    """Use a temporary config file for testing."""
    temp_file = tmp_path / "config"
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", temp_file)
    return temp_file


@pytest.fixture
def temp_credentials_file(tmp_path, monkeypatch):
    """Use a temporary credentials file for testing."""
    temp_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", temp_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", temp_file)
    return temp_file


def test_load_config_default(temp_config_file):
    """Test loading default config when file doesn't exist."""
    config = load_config()

    assert config.current_context == "default"
    assert "default" in config.contexts
    assert config.contexts["default"].api_url == "https://api.pragmatiks.io"


def test_load_config_from_file(temp_config_file):
    """Test loading config from existing file."""
    temp_config_file.parent.mkdir(parents=True, exist_ok=True)
    temp_config_file.write_text(
        """
current_context: production
contexts:
  production:
    api_url: https://api.prod.com
  staging:
    api_url: https://api.staging.com
"""
    )

    config = load_config()

    assert config.current_context == "production"
    assert "production" in config.contexts
    assert "staging" in config.contexts
    assert config.contexts["production"].api_url == "https://api.prod.com"
    assert config.contexts["staging"].api_url == "https://api.staging.com"


def test_save_config(temp_config_file):
    """Test saving config to file."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://api.prod.com"),
            "staging": ContextConfig(api_url="https://api.staging.com"),
        },
    )

    save_config(config)

    assert temp_config_file.exists()
    assert temp_config_file.stat().st_mode & 0o777 == 0o644

    loaded_config = load_config()
    assert loaded_config.current_context == "production"
    assert loaded_config.contexts["production"].api_url == "https://api.prod.com"


def test_get_current_context(temp_config_file):
    """Test getting current context."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    context_name, context_config = get_current_context()

    assert context_name == "production"
    assert context_config.api_url == "https://api.prod.com"


def test_get_current_context_with_explicit_context(temp_config_file):
    """Test getting context with explicit context parameter."""
    config = PragmaConfig(
        current_context="default",
        contexts={
            "default": ContextConfig(api_url="http://localhost:8000"),
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    context_name, context_config = get_current_context("production")

    assert context_name == "production"
    assert context_config.api_url == "https://api.prod.com"


def test_get_current_context_raises_on_nonexistent(temp_config_file):
    """Test that get_current_context raises ValueError for nonexistent context."""
    config = PragmaConfig(
        current_context="default",
        contexts={
            "default": ContextConfig(api_url="http://localhost:8000"),
        },
    )
    save_config(config)

    with pytest.raises(ValueError, match="Context 'nonexistent' not found"):
        get_current_context("nonexistent")


def test_use_context_command(cli_runner, temp_config_file):
    """Test use-context command."""
    config = PragmaConfig(
        current_context="default",
        contexts={
            "default": ContextConfig(api_url="http://localhost:8000"),
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "use-context", "production"])

    assert result.exit_code == 0
    assert "Switched to context 'production'" in result.stdout

    loaded_config = load_config()
    assert loaded_config.current_context == "production"


def test_use_context_command_nonexistent(cli_runner, temp_config_file):
    """Test use-context command with nonexistent context."""
    result = cli_runner.invoke(app, ["config", "use-context", "nonexistent"])

    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


def test_get_contexts_command(cli_runner, temp_config_file):
    """Test get-contexts command."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "default": ContextConfig(api_url="http://localhost:8000"),
            "production": ContextConfig(api_url="https://api.prod.com"),
            "staging": ContextConfig(api_url="https://api.staging.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "get-contexts"])

    assert result.exit_code == 0
    assert "Available contexts:" in result.stdout
    assert "default" in result.stdout
    assert "production" in result.stdout
    assert "staging" in result.stdout
    assert "http://localhost:8000" in result.stdout
    assert "https://api.prod.com" in result.stdout


def test_current_context_command(cli_runner, temp_config_file):
    """Test current-context command."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "current-context"])

    assert result.exit_code == 0
    assert "production" in result.stdout
    assert "https://api.prod.com" in result.stdout


def test_set_context_command(cli_runner, temp_config_file):
    """Test set-context command."""
    result = cli_runner.invoke(app, ["config", "set-context", "production", "--api-url", "https://api.prod.com"])

    assert result.exit_code == 0
    assert "Context 'production' configured" in result.stdout

    loaded_config = load_config()
    assert "production" in loaded_config.contexts
    assert loaded_config.contexts["production"].api_url == "https://api.prod.com"


def test_set_context_command_update_existing(cli_runner, temp_config_file):
    """Test set-context command updating existing context."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://old-api.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "set-context", "production", "--api-url", "https://new-api.com"])

    assert result.exit_code == 0

    loaded_config = load_config()
    assert loaded_config.contexts["production"].api_url == "https://new-api.com"


def test_delete_context_command(cli_runner, temp_config_file):
    """Test delete-context command."""
    config = PragmaConfig(
        current_context="default",
        contexts={
            "default": ContextConfig(api_url="http://localhost:8000"),
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "delete-context", "production"])

    assert result.exit_code == 0
    assert "Context 'production' deleted" in result.stdout

    loaded_config = load_config()
    assert "production" not in loaded_config.contexts
    assert "default" in loaded_config.contexts


def test_delete_context_command_nonexistent(cli_runner, temp_config_file):
    """Test delete-context command with nonexistent context."""
    result = cli_runner.invoke(app, ["config", "delete-context", "nonexistent"])

    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


def test_delete_context_command_current_context(cli_runner, temp_config_file):
    """Test delete-context command trying to delete current context."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://api.prod.com"),
        },
    )
    save_config(config)

    result = cli_runner.invoke(app, ["config", "delete-context", "production"])

    assert result.exit_code == 1
    assert "Cannot delete current context" in result.stdout


def test_context_config_model():
    """Test ContextConfig model."""
    context = ContextConfig(api_url="https://api.example.com")

    assert context.api_url == "https://api.example.com"


def test_context_config_get_auth_url_derived():
    """Test get_auth_url derives auth URL from api_url."""
    context = ContextConfig(api_url="https://api.pragmatiks.io")

    # Should derive app.pragmatiks.io from api.pragmatiks.io
    assert context.get_auth_url() == "https://app.pragmatiks.io"


def test_context_config_get_auth_url_explicit():
    """Test get_auth_url uses explicit auth_url if provided."""
    context = ContextConfig(api_url="https://api.pragmatiks.io", auth_url="https://custom-auth.example.com")

    # Should use the explicit auth_url
    assert context.get_auth_url() == "https://custom-auth.example.com"


def test_context_config_get_auth_url_localhost():
    """Test get_auth_url handles localhost API correctly."""
    context = ContextConfig(api_url="http://localhost:8000")

    # Localhost defaults to port 3000 for the web app
    assert context.get_auth_url() == "http://localhost:3000"


def test_context_config_get_auth_url_localhost_explicit():
    """Test get_auth_url respects explicit auth_url for localhost."""
    context = ContextConfig(api_url="http://localhost:8000", auth_url="http://localhost:4000")

    # Explicit auth_url should be used
    assert context.get_auth_url() == "http://localhost:4000"


def test_context_config_get_auth_url_127():
    """Test get_auth_url handles 127.0.0.1 correctly."""
    context = ContextConfig(api_url="http://127.0.0.1:8000")

    # 127.0.0.1 also defaults to localhost:3000
    assert context.get_auth_url() == "http://localhost:3000"


def test_context_config_get_auth_url_local_api():
    """Test get_auth_url derives correctly for local dev setup."""
    context = ContextConfig(api_url="http://api.localhost:8000")

    # Localhost takes precedence, defaults to port 3000
    assert context.get_auth_url() == "http://localhost:3000"


def test_pragma_config_model():
    """Test PragmaConfig model."""
    config = PragmaConfig(
        current_context="production",
        contexts={
            "production": ContextConfig(api_url="https://api.prod.com"),
            "staging": ContextConfig(api_url="https://api.staging.com"),
        },
    )

    assert config.current_context == "production"
    assert len(config.contexts) == 2
    assert config.contexts["production"].api_url == "https://api.prod.com"


def test_config_file_permissions(temp_config_file):
    """Test that config file has correct permissions (644)."""
    config = PragmaConfig(
        current_context="default",
        contexts={"default": ContextConfig(api_url="http://localhost:8000")},
    )

    save_config(config)

    permissions = temp_config_file.stat().st_mode & 0o777
    assert permissions == 0o644
