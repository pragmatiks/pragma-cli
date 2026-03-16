"""Tests for CLI auto-completion functions."""

from __future__ import annotations

from types import SimpleNamespace

from pragma_cli.commands.completions import (
    completion_provider_ids,
    completion_resource_ids,
)


def mock_resource(provider: str, resource: str, name: str) -> dict:
    """Create a mock resource dict for testing completions."""
    return {"provider": provider, "resource": resource, "name": name}


def mock_provider(name: str) -> SimpleNamespace:
    """Create a mock provider summary for testing completions."""
    return SimpleNamespace(name=name)


def mock_paginated(*providers: SimpleNamespace) -> SimpleNamespace:
    """Wrap providers in a mock paginated response."""
    return SimpleNamespace(items=list(providers))


def test_completion_provider_ids_all_match(mock_cli_client):
    mock_cli_client.list_providers.return_value = mock_paginated(
        mock_provider("postgres"),
        mock_provider("mysql"),
        mock_provider("redis"),
    )

    results = list(completion_provider_ids(""))

    assert "postgres" in results
    assert "mysql" in results
    assert "redis" in results


def test_completion_provider_ids_partial_match(mock_cli_client):
    mock_cli_client.list_providers.return_value = mock_paginated(
        mock_provider("postgres"),
        mock_provider("mysql"),
        mock_provider("redis"),
    )

    results = list(completion_provider_ids("post"))

    assert results == ["postgres"]


def test_completion_provider_ids_no_match(mock_cli_client):
    mock_cli_client.list_providers.return_value = mock_paginated(
        mock_provider("postgres"),
    )

    results = list(completion_provider_ids("xyz"))

    assert results == []


def test_completion_provider_ids_case_insensitive(mock_cli_client):
    mock_cli_client.list_providers.return_value = mock_paginated(
        mock_provider("postgres"),
    )

    results = list(completion_provider_ids("POST"))

    assert "postgres" in results


def test_completion_provider_ids_handles_api_error(mock_cli_client):
    """Completion gracefully returns empty when API fails."""
    mock_cli_client.list_providers.side_effect = Exception("API connection failed")

    results = list(completion_provider_ids(""))

    assert results == []


def test_completion_resource_ids_no_slash_lists_providers(mock_cli_client):
    """No slash in input completes provider names with trailing slash."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "schema", "schema1"),
        mock_resource("gcp", "secret", "my-secret"),
    ]

    results = list(completion_resource_ids(""))

    assert "gcp/" in results
    assert "postgres/" in results


def test_completion_resource_ids_no_slash_partial_match(mock_cli_client):
    """Partial provider name filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("gcp", "secret", "my-secret"),
    ]

    results = list(completion_resource_ids("post"))

    assert results == ["postgres/"]


def test_completion_resource_ids_no_slash_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("POST"))

    assert "postgres/" in results


def test_completion_resource_ids_no_slash_deduplicates(mock_cli_client):
    """Multiple resources from same provider yield one provider entry."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids(""))

    assert results == ["postgres/"]


def test_completion_resource_ids_one_slash_lists_types(mock_cli_client):
    """One slash completes resource types for the given provider."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids("postgres/"))

    assert "postgres/database/" in results
    assert "postgres/schema/" in results
    mock_cli_client.list_resources.assert_called_once_with(provider="postgres")


def test_completion_resource_ids_one_slash_partial_type(mock_cli_client):
    """Partial resource type filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids("postgres/dat"))

    assert results == ["postgres/database/"]


def test_completion_resource_ids_one_slash_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("postgres/DAT"))

    assert "postgres/database/" in results


def test_completion_resource_ids_one_slash_deduplicates(mock_cli_client):
    """Multiple resources of same type yield one type entry."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "db3"),
    ]

    results = list(completion_resource_ids("postgres/"))

    assert results == ["postgres/database/"]


def test_completion_resource_ids_two_slashes_lists_names(mock_cli_client):
    """Two slashes completes resource instance names."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "production-db"),
    ]

    results = list(completion_resource_ids("postgres/database/"))

    assert "postgres/database/db1" in results
    assert "postgres/database/db2" in results
    assert "postgres/database/production-db" in results
    mock_cli_client.list_resources.assert_called_once_with(provider="postgres", resource="database")


def test_completion_resource_ids_two_slashes_partial_name(mock_cli_client):
    """Partial resource name filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "production-db"),
    ]

    results = list(completion_resource_ids("postgres/database/prod"))

    assert results == ["postgres/database/production-db"]


def test_completion_resource_ids_two_slashes_no_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("postgres/database/xyz"))

    assert results == []


def test_completion_resource_ids_no_slash_no_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("xyz"))

    assert results == []


def test_completion_resource_ids_handles_api_error(mock_cli_client):
    """Completion gracefully returns empty when API fails."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids(""))

    assert results == []


def test_completion_resource_ids_handles_api_error_one_slash(mock_cli_client):
    """Completion gracefully returns empty when API fails at type level."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids("postgres/"))

    assert results == []


def test_completion_resource_ids_handles_api_error_two_slashes(mock_cli_client):
    """Completion gracefully returns empty when API fails at name level."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids("postgres/database/"))

    assert results == []
