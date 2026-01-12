"""Tests for CLI auto-completion functions."""

import typer

from pragma_cli.commands.completions import completion_resource_ids, completion_resource_names


def mock_resource(provider: str, resource: str, name: str) -> dict:
    """Create a mock resource dict for testing completions."""
    return {"provider": provider, "resource": resource, "name": name}


def test_completion_resource_ids_all_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "schema", "schema1"),
        mock_resource("postgres", "database", "pg1"),
    ]

    results = list(completion_resource_ids(""))

    assert "postgres/database" in results
    assert "postgres/schema" in results
    assert "postgres/database" in results


def test_completion_resource_ids_partial_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "schema", "schema1"),
        mock_resource("postgres", "database", "pg1"),
    ]

    results = list(completion_resource_ids("post"))

    assert "postgres/database" in results
    assert "postgres/schema" in results


def test_completion_resource_ids_no_match(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("xyz"))

    assert results == []


def test_completion_resource_ids_case_insensitive(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]

    results = list(completion_resource_ids("POST"))

    assert "postgres/database" in results


def test_completion_resource_ids_deduplicates(mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "db3"),
    ]

    results = list(completion_resource_ids(""))

    assert results == ["postgres/database"]


def test_completion_resource_names_all_match(mocker, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "production-db"),
    ]
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "postgres/database"}

    results = list(completion_resource_names(ctx, ""))

    assert results == ["db1", "db2", "production-db"]
    mock_cli_client.list_resources.assert_called_once_with(provider="postgres", resource="database")


def test_completion_resource_names_partial_match(mocker, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
        mock_resource("postgres", "database", "db2"),
        mock_resource("postgres", "database", "production-db"),
    ]
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "postgres/database"}

    results = list(completion_resource_names(ctx, "prod"))

    assert results == ["production-db"]


def test_completion_resource_names_no_match(mocker, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1"),
    ]
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "postgres/database"}

    results = list(completion_resource_names(ctx, "xyz"))

    assert results == []


def test_completion_resource_names_empty_list(mocker, mock_cli_client):
    mock_cli_client.list_resources.return_value = []
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "postgres/database"}

    results = list(completion_resource_names(ctx, ""))

    assert results == []


def test_completion_resource_names_no_resource_id(mocker, mock_cli_client):
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {}

    results = list(completion_resource_names(ctx, ""))

    assert results == []
    mock_cli_client.list_resources.assert_not_called()


def test_completion_resource_names_invalid_resource_id(mocker, mock_cli_client):
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "invalid-no-slash"}

    results = list(completion_resource_names(ctx, ""))

    assert results == []
    mock_cli_client.list_resources.assert_not_called()


def test_completion_resource_ids_handles_api_error(mock_cli_client):
    """Completion gracefully returns empty when API fails."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")

    results = list(completion_resource_ids(""))

    assert results == []


def test_completion_resource_names_handles_api_error(mocker, mock_cli_client):
    """Completion gracefully returns empty when API fails."""
    mock_cli_client.list_resources.side_effect = Exception("API connection failed")
    ctx = mocker.Mock(spec=typer.Context)
    ctx.params = {"resource_id": "postgres/database"}

    results = list(completion_resource_names(ctx, ""))

    assert results == []
