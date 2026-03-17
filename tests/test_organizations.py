"""Tests for CLI organization commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
from pragma_sdk import Organization, OrganizationStatus

from pragma_cli.main import app


if TYPE_CHECKING:
    from pytest_mock import MockerFixture, MockType
    from typer.testing import CliRunner


def make_organization(
    organization_id: str = "org_123",
    name: str = "Test Organization",
    slug: str = "test-org",
    status: OrganizationStatus = OrganizationStatus.ACTIVE,
) -> Organization:
    """Create a mock Organization for testing."""
    return Organization(
        organization_id=organization_id,
        name=name,
        slug=slug,
        status=status,
        created_at="2024-01-15T12:00:00Z",
        updated_at="2024-01-15T12:00:00Z",
    )


def test_list_shows_organizations_in_table(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_organizations.return_value = [
        make_organization("org_1", "Alpha Corp", "alpha-corp"),
        make_organization("org_2", "Beta Inc", "beta-inc"),
    ]

    result = cli_runner.invoke(app, ["organizations", "list"])

    assert result.exit_code == 0
    assert "org_1" in result.stdout
    assert "Alpha Corp" in result.stdout
    assert "alpha-corp" in result.stdout
    assert "org_2" in result.stdout
    assert "Beta Inc" in result.stdout
    mock_cli_client.list_organizations.assert_called_once()


def test_list_empty_shows_message(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_organizations.return_value = []

    result = cli_runner.invoke(app, ["organizations", "list"])

    assert result.exit_code == 0
    assert "No organizations found" in result.stdout


def test_list_json_output(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_organizations.return_value = [
        make_organization("org_1", "Alpha Corp", "alpha-corp"),
    ]

    result = cli_runner.invoke(app, ["organizations", "list", "-o", "json"])

    assert result.exit_code == 0
    assert '"organization_id": "org_1"' in result.stdout
    assert '"name": "Alpha Corp"' in result.stdout


def test_list_api_error_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal server error"
    mock_cli_client.list_organizations.side_effect = httpx.HTTPStatusError(
        "Server error",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["organizations", "list"])

    assert result.exit_code == 1
    assert "Error" in result.stdout


def test_cleanup_with_confirmation(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.cleanup_organization.return_value = None

    result = cli_runner.invoke(app, ["organizations", "cleanup", "org_123"], input="y\n")

    assert result.exit_code == 0
    assert "Cleanup initiated" in result.stdout
    assert "org_123" in result.stdout
    mock_cli_client.cleanup_organization.assert_called_once_with("org_123")


def test_cleanup_with_yes_flag(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.cleanup_organization.return_value = None

    result = cli_runner.invoke(app, ["organizations", "cleanup", "org_123", "--yes"])

    assert result.exit_code == 0
    assert "Cleanup initiated" in result.stdout
    mock_cli_client.cleanup_organization.assert_called_once_with("org_123")


def test_cleanup_declined_exits_cleanly(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    result = cli_runner.invoke(app, ["organizations", "cleanup", "org_123"], input="n\n")

    assert result.exit_code == 0
    assert "Cancelled" in result.stdout
    mock_cli_client.cleanup_organization.assert_not_called()


def test_cleanup_not_found_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 404
    mock_cli_client.cleanup_organization.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["organizations", "cleanup", "org_nonexistent", "--yes"])

    assert result.exit_code == 1
    assert "Organization not found" in result.stdout
    assert "org_nonexistent" in result.stdout


def test_cleanup_api_error_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal server error"
    mock_cli_client.cleanup_organization.side_effect = httpx.HTTPStatusError(
        "Server error",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["organizations", "cleanup", "org_123", "--yes"])

    assert result.exit_code == 1
    assert "Error" in result.stdout
