"""Tests for CLI store commands."""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from pragma_cli.main import app


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def mock_store_client(mocker: MockerFixture):
    mock_client = mocker.Mock()
    mock_client._auth = mocker.Mock()
    mocker.patch("pragma_cli.commands.store.get_client", return_value=mock_client)
    return mock_client


def _make_provider_summary(
    mocker: MockerFixture,
    name: str = "qdrant",
    display_name: str = "Qdrant",
    trust_tier: str = "official",
    latest_version: str = "1.0.0",
    install_count: int = 42,
    tags: list[str] | None = None,
):
    provider = mocker.Mock()
    provider.name = name
    provider.display_name = display_name
    provider.trust_tier = trust_tier
    provider.latest_version = latest_version
    provider.install_count = install_count
    provider.tags = tags or ["vector", "ml"]
    provider.author = mocker.Mock()
    provider.author.org_name = "Pragmatiks"
    provider.description = f"A {name} provider"
    return provider


def _make_paginated_response(mocker: MockerFixture, items, total=None):
    response = mocker.Mock()
    response.items = items
    response.total = total if total is not None else len(items)
    response.limit = 20
    response.offset = 0
    return response


def _make_store_detail(mocker: MockerFixture, provider=None, versions=None):
    detail = mocker.Mock()
    detail.provider = provider or mocker.Mock()
    detail.versions = versions or []
    return detail


def _make_version(mocker: MockerFixture, version="1.0.0", status="published", runtime_version="0.5.0"):
    v = mocker.Mock()
    v.version = version
    v.status = status
    v.runtime_version = runtime_version
    v.published_at = "2026-02-01T10:00:00Z"
    v.changelog = "Initial release"
    return v


def _make_installed_provider(
    mocker: MockerFixture,
    name: str = "qdrant",
    version: str = "1.0.0",
    upgrade_policy: str = "manual",
    resource_tier: str = "standard",
    upgrade_available: bool = False,
    latest_version: str | None = None,
):
    p = mocker.Mock()
    p.store_provider_name = name
    p.installed_version = version
    p.upgrade_policy = upgrade_policy
    p.resource_tier = resource_tier
    p.installed_at = "2026-02-01T10:00:00Z"
    p.upgrade_available = upgrade_available
    p.latest_version = latest_version
    return p


def test_store_list_table(cli_runner, mock_store_client, mocker):
    """List command displays providers in a formatted table."""
    providers = [
        _make_provider_summary(mocker, name="qdrant", display_name="Qdrant", trust_tier="official"),
        _make_provider_summary(mocker, name="postgres", display_name="PostgreSQL", trust_tier="verified"),
    ]
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, providers)

    result = cli_runner.invoke(app, ["store", "list"])

    assert result.exit_code == 0
    assert "qdrant" in result.output
    assert "Qdrant" in result.output
    assert "official" in result.output
    assert "postgres" in result.output
    assert "PostgreSQL" in result.output
    assert "verified" in result.output
    assert "Showing 1-2 of 2 providers" in result.output


def test_store_list_json(cli_runner, mock_store_client, mocker):
    """List command outputs JSON format."""
    providers = [
        _make_provider_summary(mocker, name="qdrant"),
    ]
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, providers)

    result = cli_runner.invoke(app, ["store", "list", "-o", "json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "qdrant"
    assert data[0]["trust_tier"] == "official"


def test_store_list_with_filters(cli_runner, mock_store_client, mocker):
    """List command passes trust-tier and tags filters to SDK."""
    providers = [
        _make_provider_summary(mocker, name="qdrant", trust_tier="official"),
    ]
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, providers)

    result = cli_runner.invoke(
        app, ["store", "list", "--trust-tier", "official", "--tags", "vector,ml", "--limit", "10"]
    )

    assert result.exit_code == 0
    mock_store_client.list_store_providers.assert_called_once_with(
        trust_tier="official",
        tags=["vector", "ml"],
        limit=10,
        offset=0,
    )


def test_store_list_empty(cli_runner, mock_store_client, mocker):
    """List command shows message when no providers found."""
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, [])

    result = cli_runner.invoke(app, ["store", "list"])

    assert result.exit_code == 0
    assert "No providers found" in result.output


def test_store_search_table(cli_runner, mock_store_client, mocker):
    """Search command displays matching providers."""
    providers = [
        _make_provider_summary(mocker, name="postgres", display_name="PostgreSQL"),
    ]
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, providers)

    result = cli_runner.invoke(app, ["store", "search", "postgres"])

    assert result.exit_code == 0
    assert "postgres" in result.output
    assert "PostgreSQL" in result.output
    mock_store_client.list_store_providers.assert_called_once_with(
        q="postgres",
        trust_tier=None,
        tags=None,
        limit=20,
        offset=0,
    )


def test_store_search_no_results(cli_runner, mock_store_client, mocker):
    """Search command shows message for no results."""
    mock_store_client.list_store_providers.return_value = _make_paginated_response(mocker, [])

    result = cli_runner.invoke(app, ["store", "search", "nonexistent"])

    assert result.exit_code == 0
    assert "No providers found matching 'nonexistent'" in result.output


def test_store_info_table(cli_runner, mock_store_client, mocker):
    """Info command displays provider details with versions."""
    provider = _make_provider_summary(mocker, name="qdrant", display_name="Qdrant")
    provider.readme = "# Qdrant Provider"
    provider.created_at = "2026-01-01T00:00:00Z"
    provider.updated_at = "2026-02-01T00:00:00Z"
    versions = [
        _make_version(mocker, version="1.0.0", status="published"),
        _make_version(mocker, version="0.9.0", status="published"),
    ]
    detail = _make_store_detail(mocker, provider=provider, versions=versions)
    mock_store_client.get_store_provider.return_value = detail

    result = cli_runner.invoke(app, ["store", "info", "qdrant"])

    assert result.exit_code == 0
    assert "qdrant" in result.output
    assert "Qdrant" in result.output
    assert "official" in result.output
    assert "1.0.0" in result.output
    assert "0.9.0" in result.output
    assert "published" in result.output


def test_store_info_not_found(cli_runner, mock_store_client):
    """Info command handles 404 error."""
    mock_response = httpx.Response(404, json={"detail": "Not found"})
    mock_store_client.get_store_provider.side_effect = httpx.HTTPStatusError(
        "Not found", request=httpx.Request("GET", "http://test"), response=mock_response
    )

    result = cli_runner.invoke(app, ["store", "info", "nonexistent"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_store_install_success(cli_runner, mock_store_client, mocker):
    """Install command installs provider with confirmation."""
    provider = _make_provider_summary(mocker, name="qdrant", display_name="Qdrant")
    detail = _make_store_detail(mocker, provider=provider)
    mock_store_client.get_store_provider.return_value = detail

    installed = mocker.Mock()
    installed.store_provider_name = "qdrant"
    installed.installed_version = "1.0.0"
    mock_store_client.install_store_provider.return_value = installed

    result = cli_runner.invoke(app, ["store", "install", "qdrant"], input="y\n")

    assert result.exit_code == 0
    assert "Installed" in result.output
    assert "qdrant" in result.output
    assert "1.0.0" in result.output
    mock_store_client.install_store_provider.assert_called_once_with(
        "qdrant",
        version=None,
        resource_tier="standard",
        upgrade_policy="manual",
    )


def test_store_install_yes_flag(cli_runner, mock_store_client, mocker):
    """Install command skips confirmation with --yes."""
    provider = _make_provider_summary(mocker, name="qdrant")
    detail = _make_store_detail(mocker, provider=provider)
    mock_store_client.get_store_provider.return_value = detail

    installed = mocker.Mock()
    installed.store_provider_name = "qdrant"
    installed.installed_version = "1.0.0"
    mock_store_client.install_store_provider.return_value = installed

    result = cli_runner.invoke(app, ["store", "install", "qdrant", "-y"])

    assert result.exit_code == 0
    assert "Installed" in result.output
    mock_store_client.install_store_provider.assert_called_once()


def test_store_install_already_installed(cli_runner, mock_store_client, mocker):
    """Install command handles 409 (already installed)."""
    provider = _make_provider_summary(mocker, name="qdrant")
    detail = _make_store_detail(mocker, provider=provider)
    mock_store_client.get_store_provider.return_value = detail

    mock_response = httpx.Response(409, json={"detail": "Already installed"})
    mock_store_client.install_store_provider.side_effect = httpx.HTTPStatusError(
        "Conflict", request=httpx.Request("POST", "http://test"), response=mock_response
    )

    result = cli_runner.invoke(app, ["store", "install", "qdrant", "-y"])

    assert result.exit_code == 1
    assert "already installed" in result.output


def test_store_uninstall_success(cli_runner, mock_store_client):
    """Uninstall command removes provider with confirmation."""
    mock_store_client.uninstall_store_provider.return_value = None

    result = cli_runner.invoke(app, ["store", "uninstall", "qdrant"], input="y\n")

    assert result.exit_code == 0
    assert "Uninstalled" in result.output
    assert "qdrant" in result.output
    mock_store_client.uninstall_store_provider.assert_called_once_with("qdrant", cascade=False)


def test_store_uninstall_force(cli_runner, mock_store_client):
    """Uninstall command skips confirmation with --force."""
    mock_store_client.uninstall_store_provider.return_value = None

    result = cli_runner.invoke(app, ["store", "uninstall", "qdrant", "--force"])

    assert result.exit_code == 0
    assert "Uninstalled" in result.output
    mock_store_client.uninstall_store_provider.assert_called_once_with("qdrant", cascade=False)


def test_store_uninstall_has_resources(cli_runner, mock_store_client):
    """Uninstall command handles 409 (has resources)."""
    mock_response = httpx.Response(409, json={"detail": "Provider has active resources"})
    mock_store_client.uninstall_store_provider.side_effect = httpx.HTTPStatusError(
        "Conflict", request=httpx.Request("DELETE", "http://test"), response=mock_response
    )

    result = cli_runner.invoke(app, ["store", "uninstall", "qdrant", "--force"])

    assert result.exit_code == 1
    assert "active resources" in result.output
    assert "--cascade" in result.output


def test_store_upgrade_success(cli_runner, mock_store_client, mocker):
    """Upgrade command upgrades provider."""
    upgraded = mocker.Mock()
    upgraded.installed_version = "2.0.0"
    mock_store_client.upgrade_store_provider.return_value = upgraded

    result = cli_runner.invoke(app, ["store", "upgrade", "qdrant", "-y"])

    assert result.exit_code == 0
    assert "Upgraded" in result.output
    assert "2.0.0" in result.output
    mock_store_client.upgrade_store_provider.assert_called_once_with("qdrant", version=None)


def test_store_upgrade_already_on_version(cli_runner, mock_store_client):
    """Upgrade command handles 409 (already on version)."""
    mock_response = httpx.Response(409, json={"detail": "Already on version"})
    mock_store_client.upgrade_store_provider.side_effect = httpx.HTTPStatusError(
        "Conflict", request=httpx.Request("POST", "http://test"), response=mock_response
    )

    result = cli_runner.invoke(app, ["store", "upgrade", "qdrant", "-y"])

    assert result.exit_code == 1
    assert "already on the requested version" in result.output


def test_store_installed_table(cli_runner, mock_store_client, mocker):
    """Installed command displays providers with upgrade indicators."""
    providers = [
        _make_installed_provider(
            mocker, name="qdrant", version="1.0.0", upgrade_available=True, latest_version="2.0.0"
        ),
        _make_installed_provider(mocker, name="postgres", version="3.1.0", upgrade_available=False),
    ]
    mock_store_client.list_installed_providers.return_value = providers

    result = cli_runner.invoke(app, ["store", "installed"])

    assert result.exit_code == 0
    assert "qdrant" in result.output
    assert "1.0.0" in result.output
    assert "postgres" in result.output
    assert "3.1.0" in result.output
    assert "2.0.0" in result.output


def test_store_installed_empty(cli_runner, mock_store_client):
    """Installed command shows message when nothing installed."""
    mock_store_client.list_installed_providers.return_value = []

    result = cli_runner.invoke(app, ["store", "installed"])

    assert result.exit_code == 0
    assert "No providers installed" in result.output
