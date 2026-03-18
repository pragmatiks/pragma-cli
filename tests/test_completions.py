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


def test_completion_resource_ids_no_slash_lists_orgs(mock_cli_client):
    """No slash in input completes organization names with trailing slash."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/gcp", "secret", "my-secret"),
        mock_resource("acme/redis", "cache", "session"),
    ]

    results = list(completion_resource_ids(""))

    assert "acme/" in results
    assert "pragmatiks/" in results


def test_completion_resource_ids_no_slash_partial_match(mock_cli_client):
    """Partial org name filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("acme/gcp", "secret", "my-secret"),
    ]

    results = list(completion_resource_ids("prag"))

    assert results == ["pragmatiks/"]


def test_completion_resource_ids_no_slash_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("PRAG"))

    assert "pragmatiks/" in results


def test_completion_resource_ids_no_slash_deduplicates(mock_cli_client):
    """Multiple resources from same org yield one org entry."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "database", "db2"),
        mock_resource("pragmatiks/gcp", "secret", "my-secret"),
    ]

    results = list(completion_resource_ids(""))

    assert results == ["pragmatiks/"]


def test_completion_resource_ids_one_slash_lists_providers(mock_cli_client):
    """One slash completes provider names within the org."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/gcp", "secret", "my-secret"),
        mock_resource("acme/redis", "cache", "session"),
    ]

    results = list(completion_resource_ids("pragmatiks/"))

    assert "pragmatiks/gcp/" in results
    assert "pragmatiks/postgres/" in results


def test_completion_resource_ids_one_slash_partial_provider(mock_cli_client):
    """Partial provider name within org filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/gcp", "secret", "my-secret"),
    ]

    results = list(completion_resource_ids("pragmatiks/post"))

    assert results == ["pragmatiks/postgres/"]


def test_completion_resource_ids_one_slash_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("pragmatiks/POST"))

    assert "pragmatiks/postgres/" in results


def test_completion_resource_ids_one_slash_deduplicates(mock_cli_client):
    """Multiple resources from same provider yield one provider entry."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "database", "db2"),
        mock_resource("pragmatiks/postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids("pragmatiks/"))

    assert results == ["pragmatiks/postgres/"]


def test_completion_resource_ids_two_slashes_lists_types(mock_cli_client):
    """Two slashes completes resource types for the given org/provider."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/"))

    assert "pragmatiks/postgres/database/" in results
    assert "pragmatiks/postgres/schema/" in results
    mock_cli_client.list_resources.assert_called_once_with(provider="pragmatiks/postgres")


def test_completion_resource_ids_two_slashes_partial_type(mock_cli_client):
    """Partial resource type filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "schema", "schema1"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/dat"))

    assert results == ["pragmatiks/postgres/database/"]


def test_completion_resource_ids_two_slashes_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/DAT"))

    assert "pragmatiks/postgres/database/" in results


def test_completion_resource_ids_two_slashes_deduplicates(mock_cli_client):
    """Multiple resources of same type yield one type entry."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "database", "db2"),
        mock_resource("pragmatiks/postgres", "database", "db3"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/"))

    assert results == ["pragmatiks/postgres/database/"]


def test_completion_resource_ids_three_slashes_lists_names(mock_cli_client):
    """Three slashes completes resource instance names."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "database", "db2"),
        mock_resource("pragmatiks/postgres", "database", "production-db"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/database/"))

    assert "pragmatiks/postgres/database/db1" in results
    assert "pragmatiks/postgres/database/db2" in results
    assert "pragmatiks/postgres/database/production-db" in results
    mock_cli_client.list_resources.assert_called_once_with(provider="pragmatiks/postgres", resource="database")


def test_completion_resource_ids_three_slashes_partial_name(mock_cli_client):
    """Partial resource name filters correctly."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
        mock_resource("pragmatiks/postgres", "database", "db2"),
        mock_resource("pragmatiks/postgres", "database", "production-db"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/database/prod"))

    assert results == ["pragmatiks/postgres/database/production-db"]


def test_completion_resource_ids_three_slashes_no_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("pragmatiks/postgres/database/xyz"))

    assert results == []


def test_completion_resource_ids_no_slash_no_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("xyz"))

    assert results == []


def test_completion_resource_ids_handles_api_error(mock_cli_client):
    """Completion gracefully returns empty when API fails."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids(""))

    assert results == []


def test_completion_resource_ids_handles_api_error_one_slash(mock_cli_client):
    """Completion gracefully returns empty when API fails at provider level."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids("pragmatiks/"))

    assert results == []


def test_completion_resource_ids_handles_api_error_two_slashes(mock_cli_client):
    """Completion gracefully returns empty when API fails at type level."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids("pragmatiks/postgres/"))

    assert results == []


def test_completion_resource_ids_handles_api_error_three_slashes(mock_cli_client):
    """Completion gracefully returns empty when API fails at name level."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids("pragmatiks/postgres/database/"))

    assert results == []
