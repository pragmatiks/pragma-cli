import base64
import json
import time

import pytest

from pragma_cli.commands.auth import CallbackHandler, _is_token_expired, clear_credentials, save_credentials
from pragma_cli.main import app


@pytest.fixture
def temp_credentials_file(tmp_path, monkeypatch):
    """Use a temporary credentials file for testing."""
    temp_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", temp_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", temp_file)
    return temp_file


def test_save_credentials_creates_file(temp_credentials_file):
    """Test that save_credentials creates the credentials file."""
    save_credentials("test_token_123", "default")

    assert temp_credentials_file.exists()
    assert temp_credentials_file.stat().st_mode & 0o777 == 0o600


def test_save_credentials_default_context(temp_credentials_file):
    """Test saving credentials for default context."""
    save_credentials("test_token_123", "default")

    content = temp_credentials_file.read_text()
    assert "default=test_token_123" in content


def test_save_credentials_named_context(temp_credentials_file):
    """Test saving credentials for a named context."""
    save_credentials("prod_token_456", "production")

    content = temp_credentials_file.read_text()
    assert "production=prod_token_456" in content


def test_save_credentials_multiple_contexts(temp_credentials_file):
    """Test saving credentials for multiple contexts."""
    save_credentials("default_token", "default")
    save_credentials("prod_token", "production")
    save_credentials("staging_token", "staging")

    content = temp_credentials_file.read_text()
    assert "default=default_token" in content
    assert "production=prod_token" in content
    assert "staging=staging_token" in content


def test_save_credentials_updates_existing(temp_credentials_file):
    """Test that save_credentials updates existing token."""
    save_credentials("old_token", "default")
    save_credentials("new_token", "default")

    content = temp_credentials_file.read_text()
    assert "default=new_token" in content
    assert "old_token" not in content


def test_clear_credentials_all(temp_credentials_file):
    """Test clearing all credentials."""
    save_credentials("default_token", "default")
    save_credentials("prod_token", "production")

    clear_credentials(None)

    assert not temp_credentials_file.exists()


def test_clear_credentials_specific_context(temp_credentials_file):
    """Test clearing credentials for a specific context."""
    save_credentials("default_token", "default")
    save_credentials("prod_token", "production")

    clear_credentials("production")

    assert temp_credentials_file.exists()
    content = temp_credentials_file.read_text()
    assert "default=default_token" in content
    assert "production" not in content


def test_clear_credentials_last_context(temp_credentials_file):
    """Test clearing the last remaining context removes the file."""
    save_credentials("test_token", "default")

    clear_credentials("default")

    assert not temp_credentials_file.exists()


def test_clear_credentials_nonexistent_file(temp_credentials_file):
    """Test clearing credentials when file doesn't exist."""
    clear_credentials(None)
    assert not temp_credentials_file.exists()


def test_clear_credentials_nonexistent_context(temp_credentials_file):
    """Test clearing credentials for a context that doesn't exist."""
    save_credentials("test_token", "default")

    clear_credentials("nonexistent")

    assert temp_credentials_file.exists()
    content = temp_credentials_file.read_text()
    assert "default=test_token" in content


def test_login_command_missing_context(cli_runner, tmp_path, monkeypatch):
    """Test login command with missing context."""
    config_file = tmp_path / "config"
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    result = cli_runner.invoke(app, ["auth", "login", "--context", "nonexistent"])

    assert result.exit_code == 1
    assert "not found" in result.stdout.lower()


def test_login_command_success(cli_runner, tmp_path, monkeypatch, mocker):
    """Test successful login command."""
    mock_webbrowser = mocker.patch("pragma_cli.commands.auth.webbrowser.open")
    mock_http_server = mocker.patch("pragma_cli.commands.auth.HTTPServer")

    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    credentials_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", credentials_file)

    mock_server_instance = mocker.Mock()
    mock_http_server.return_value = mock_server_instance

    CallbackHandler.token = "test_token_from_clerk"

    result = cli_runner.invoke(app, ["auth", "login"])

    assert result.exit_code == 0
    assert "Successfully authenticated" in result.stdout
    assert mock_webbrowser.called


def test_logout_command_all(cli_runner, temp_credentials_file):
    """Test logout command with --all flag."""
    save_credentials("default_token", "default")
    save_credentials("prod_token", "production")

    result = cli_runner.invoke(app, ["auth", "logout", "--all"])

    assert result.exit_code == 0
    assert "Cleared all credentials" in result.stdout
    assert not temp_credentials_file.exists()


def test_logout_command_specific_context(cli_runner, temp_credentials_file):
    """Test logout command for specific context."""
    save_credentials("default_token", "default")
    save_credentials("prod_token", "production")

    result = cli_runner.invoke(app, ["auth", "logout", "--context", "production"])

    assert result.exit_code == 0
    assert "production" in result.stdout
    assert temp_credentials_file.exists()


def test_logout_command_current_context(cli_runner, tmp_path, monkeypatch):
    """Test logout command for current context."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    credentials_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", credentials_file)
    save_credentials("test_token", "default")

    result = cli_runner.invoke(app, ["auth", "logout"])

    assert result.exit_code == 0
    assert "default" in result.stdout


def _make_jwt(payload: dict, exp: float | None = None) -> str:
    """Build a fake JWT with a given payload for testing."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=").decode()

    if exp is not None:
        payload["exp"] = exp

    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(b"fakesig").rstrip(b"=").decode()
    return f"{header}.{body}.{signature}"


def test_is_token_expired_with_valid_token():
    token = _make_jwt({"sub": "user_123"}, exp=time.time() + 3600)
    assert _is_token_expired(token) is False


def test_is_token_expired_with_expired_token():
    token = _make_jwt({"sub": "user_123"}, exp=time.time() - 3600)
    assert _is_token_expired(token) is True


def test_is_token_expired_with_no_exp_claim():
    token = _make_jwt({"sub": "user_123"})
    assert _is_token_expired(token) is False


def test_is_token_expired_with_garbage():
    assert _is_token_expired("not-a-jwt") is True


def test_whoami_command_with_credentials(cli_runner, tmp_path, monkeypatch, mocker):
    """Test whoami command with stored credentials and API response."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    valid_token = _make_jwt({"sub": "user_123"}, exp=time.time() + 3600)

    credentials_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_sdk.config.get_credentials_file_path", lambda: credentials_file)

    save_credentials(valid_token, "default")

    mock_user_info = mocker.MagicMock()
    mock_user_info.user_id = "user_123"
    mock_user_info.email = "test@example.com"
    mock_user_info.organization_id = "org_456"
    mock_user_info.organization_name = "Test Org"

    mock_client_class = mocker.patch("pragma_cli.main.PragmaClient")
    mock_client_instance = mock_client_class.return_value
    mock_client_instance.get_me.return_value = mock_user_info
    mock_client_instance._auth = mocker.MagicMock()
    mock_client_instance._auth.token = valid_token

    result = cli_runner.invoke(app, ["auth", "whoami"])

    assert result.exit_code == 0
    assert "Authentication Status" in result.stdout
    assert "default" in result.stdout
    assert "Authenticated" in result.stdout
    assert "User Information" in result.stdout
    assert "test@example.com" in result.stdout
    assert "Test Org" in result.stdout


def test_whoami_command_no_credentials(cli_runner, tmp_path, monkeypatch, mocker):
    """Test whoami command with no stored credentials."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    credentials_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_sdk.config.get_credentials_file_path", lambda: credentials_file)

    mock_client_class = mocker.patch("pragma_cli.main.PragmaClient")
    mock_client_instance = mock_client_class.return_value
    mock_client_instance._auth = None

    result = cli_runner.invoke(app, ["auth", "whoami"])

    assert result.exit_code == 0
    assert "Not authenticated" in result.stdout
    assert "pragma auth login" in result.stdout


def test_whoami_command_with_expired_token(cli_runner, tmp_path, monkeypatch, mocker):
    """Test whoami command with an expired stored token shows 'Token expired'."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    expired_token = _make_jwt({"sub": "user_123"}, exp=time.time() - 3600)

    credentials_file = tmp_path / "credentials"
    monkeypatch.setattr("pragma_cli.commands.auth.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_cli.config.CREDENTIALS_FILE", credentials_file)
    monkeypatch.setattr("pragma_sdk.config.get_credentials_file_path", lambda: credentials_file)

    save_credentials(expired_token, "default")

    mock_client_class = mocker.patch("pragma_cli.main.PragmaClient")
    mock_client_instance = mock_client_class.return_value
    mock_client_instance._auth = mocker.MagicMock()
    mock_client_instance._auth.token = expired_token

    result = cli_runner.invoke(app, ["auth", "whoami"])

    assert result.exit_code == 0
    assert "Token expired" in result.stdout
    assert "pragma auth login" in result.stdout
    mock_client_instance.get_me.assert_not_called()


def test_whoami_command_with_token_flag(cli_runner, tmp_path, monkeypatch, mocker):
    """Test that --token flag overrides stored credentials."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    override_token = _make_jwt({"sub": "override_user"}, exp=time.time() + 3600)

    mock_user_info = mocker.MagicMock()
    mock_user_info.user_id = "override_user"
    mock_user_info.email = "override@example.com"
    mock_user_info.organization_id = "org_override"
    mock_user_info.organization_name = "Override Org"

    mock_client_class = mocker.patch("pragma_cli.main.PragmaClient")
    mock_client_instance = mock_client_class.return_value
    mock_client_instance.get_me.return_value = mock_user_info
    mock_client_instance._auth = mocker.MagicMock()
    mock_client_instance._auth.token = override_token

    result = cli_runner.invoke(app, ["--token", override_token, "auth", "whoami"])

    assert result.exit_code == 0
    assert "Authenticated" in result.stdout
    assert "override@example.com" in result.stdout

    mock_client_class.assert_called_once_with(base_url="http://localhost:8000", auth_token=override_token)


def test_whoami_command_with_env_var_token(cli_runner, tmp_path, monkeypatch, mocker):
    """Test that PRAGMA_AUTH_TOKEN env var is used for whoami."""
    config_file = tmp_path / "config"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(
        """
current_context: default
contexts:
  default:
    api_url: http://localhost:8000
"""
    )
    monkeypatch.setattr("pragma_cli.config.CONFIG_PATH", config_file)

    env_token = _make_jwt({"sub": "env_user"}, exp=time.time() + 3600)
    monkeypatch.setenv("PRAGMA_AUTH_TOKEN", env_token)

    mock_user_info = mocker.MagicMock()
    mock_user_info.user_id = "env_user"
    mock_user_info.email = "env@example.com"
    mock_user_info.organization_id = "org_env"
    mock_user_info.organization_name = "Env Org"

    mock_client_class = mocker.patch("pragma_cli.main.PragmaClient")
    mock_client_instance = mock_client_class.return_value
    mock_client_instance.get_me.return_value = mock_user_info
    mock_client_instance._auth = mocker.MagicMock()
    mock_client_instance._auth.token = env_token

    result = cli_runner.invoke(app, ["auth", "whoami"])

    assert result.exit_code == 0
    assert "Authenticated" in result.stdout
    assert "env@example.com" in result.stdout
