"""Tests for CLI main entry point."""

import httpx
import pytest
from typer.testing import CliRunner

from pragma_cli.main import app


@pytest.fixture
def cli_runner():
    return CliRunner()


def test_version_flag(cli_runner):
    """Test --version flag displays version and exits."""
    result = cli_runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    # Should output "pragma X.Y.Z" format
    assert result.stdout.startswith("pragma ")
    # Version should have at least major.minor format
    version_parts = result.stdout.strip().split(" ")[1].split(".")
    assert len(version_parts) >= 2


def test_version_flag_short(cli_runner):
    """Test -V short flag also displays version."""
    result = cli_runner.invoke(app, ["-V"])

    assert result.exit_code == 0
    assert result.stdout.startswith("pragma ")


def test_help_shows_version_option(cli_runner):
    """Test that --help includes --version option."""
    result = cli_runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in result.stdout
    assert "-V" in result.stdout


def test_connect_error_shows_friendly_message(cli_runner, mock_cli_client):
    request = httpx.Request("GET", "https://api.pragmatiks.io/resources")
    mock_cli_client.list_resources.side_effect = httpx.ConnectError("Connection refused", request=request)

    result = cli_runner.invoke(app, ["resources", "list"])

    assert result.exit_code == 1
    assert "Could not connect to API at https://api.pragmatiks.io" in result.output
    assert "Check that the API URL is correct and the server is running." in result.output
    assert "Traceback" not in result.output


def test_connect_error_with_port_shows_full_url(cli_runner, mock_cli_client):
    request = httpx.Request("GET", "http://localhost:8000/resources")
    mock_cli_client.list_resources.side_effect = httpx.ConnectError("Connection refused", request=request)

    result = cli_runner.invoke(app, ["resources", "list"])

    assert result.exit_code == 1
    assert "Could not connect to API at http://localhost:8000" in result.output


def test_timeout_error_shows_friendly_message(cli_runner, mock_cli_client):
    request = httpx.Request("GET", "https://api.pragmatiks.io/resources")
    mock_cli_client.list_resources.side_effect = httpx.ReadTimeout("timed out", request=request)

    result = cli_runner.invoke(app, ["resources", "list"])

    assert result.exit_code == 1
    assert "Request timed out connecting to https://api.pragmatiks.io" in result.output
    assert "Traceback" not in result.output


def test_http_401_shows_auth_message(cli_runner, mock_cli_client):
    request = httpx.Request("GET", "https://api.pragmatiks.io/resources")
    response = httpx.Response(401, request=request)
    mock_cli_client.list_resources.side_effect = httpx.HTTPStatusError(
        "401 Unauthorized", request=request, response=response
    )

    result = cli_runner.invoke(app, ["resources", "list"])

    assert result.exit_code == 1
    assert "Not authenticated" in result.output
    assert "pragma auth login" in result.output
    assert "Traceback" not in result.output


def test_http_500_shows_status_and_url(cli_runner, mock_cli_client):
    request = httpx.Request("GET", "https://api.pragmatiks.io/resources")
    response = httpx.Response(500, request=request)
    mock_cli_client.list_resources.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error", request=request, response=response
    )

    result = cli_runner.invoke(app, ["resources", "list"])

    assert result.exit_code == 1
    assert "500" in result.output
    assert "Internal Server Error" in result.output
    assert "https://api.pragmatiks.io/resources" in result.output
    assert "Traceback" not in result.output


def test_already_handled_errors_not_intercepted(cli_runner, mock_cli_client):
    """Verify that HTTP errors caught by command handlers are not double-handled."""
    request = httpx.Request("GET", "https://api.pragmatiks.io/resources/schemas")
    response = httpx.Response(404, request=request, json={"detail": "Not found"})
    mock_cli_client.list_resource_schemas.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=request, response=response
    )

    result = cli_runner.invoke(app, ["resources", "schemas"])

    assert result.exit_code == 1
    assert "Not found" in result.output
