"""Tests for CLI dead letter commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from pragma_cli.main import app


if TYPE_CHECKING:
    from pytest_mock import MockerFixture, MockType
    from typer.testing import CliRunner


def make_dead_letter_event(
    event_id: str = "evt_123",
    provider: str = "postgres",
    resource_type: str = "database",
    resource_name: str = "test-db",
    error_message: str = "Connection timeout",
    failed_at: str = "2024-01-15T12:00:00Z",
) -> dict:
    """Create a mock dead letter event dict for testing."""
    return {
        "id": event_id,
        "provider": provider,
        "resource_type": resource_type,
        "resource_name": resource_name,
        "error_message": error_message,
        "failed_at": failed_at,
    }


def test_list_shows_events_in_table(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1", "postgres", "database", "db1"),
        make_dead_letter_event("evt_2", "redis", "connection", "conn1"),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "list"])

    assert result.exit_code == 0
    assert "evt_1" in result.stdout
    assert "postgres" in result.stdout
    assert "evt_2" in result.stdout
    assert "redis" in result.stdout
    mock_cli_client.list_dead_letter_events.assert_called_once_with(provider=None)


def test_list_empty_shows_message(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = []

    result = cli_runner.invoke(app, ["ops", "dead-letter", "list"])

    assert result.exit_code == 0
    assert "No dead letter events found" in result.stdout


def test_list_with_provider_filter(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1", "postgres", "database", "db1"),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "list", "--provider", "postgres"])

    assert result.exit_code == 0
    assert "evt_1" in result.stdout
    mock_cli_client.list_dead_letter_events.assert_called_once_with(provider="postgres")


def test_list_truncates_long_error_messages(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    long_error = "A" * 100
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event(error_message=long_error),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "list"])

    assert result.exit_code == 0
    assert "A" * 100 not in result.stdout
    assert "A" * 10 in result.stdout


def test_show_displays_event_json(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    event = make_dead_letter_event("evt_123")
    mock_cli_client.get_dead_letter_event.return_value = event

    result = cli_runner.invoke(app, ["ops", "dead-letter", "show", "evt_123"])

    assert result.exit_code == 0
    assert '"id": "evt_123"' in result.stdout
    assert '"provider": "postgres"' in result.stdout
    mock_cli_client.get_dead_letter_event.assert_called_once_with("evt_123")


def test_show_not_found_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 404
    mock_cli_client.get_dead_letter_event.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["ops", "dead-letter", "show", "evt_nonexistent"])

    assert result.exit_code == 1
    assert "Event not found" in result.stdout
    assert "evt_nonexistent" in result.stdout


def test_retry_single_event_succeeds(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.retry_dead_letter_event.return_value = None

    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry", "evt_123"])

    assert result.exit_code == 0
    assert "Retried event" in result.stdout
    assert "evt_123" in result.stdout
    mock_cli_client.retry_dead_letter_event.assert_called_once_with("evt_123")


def test_retry_not_found_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 404
    mock_cli_client.retry_dead_letter_event.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry", "evt_nonexistent"])

    assert result.exit_code == 1
    assert "Event not found" in result.stdout
    assert "evt_nonexistent" in result.stdout


def test_retry_all_confirmed_succeeds(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1"),
        make_dead_letter_event("evt_2"),
    ]
    mock_cli_client.retry_all_dead_letter_events.return_value = 2

    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry", "--all"], input="y\n")

    assert result.exit_code == 0
    assert "Retried 2 event(s)" in result.stdout
    mock_cli_client.retry_all_dead_letter_events.assert_called_once()


def test_retry_all_declined_exits_cleanly(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1"),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry", "--all"], input="n\n")

    assert result.exit_code == 0
    mock_cli_client.retry_all_dead_letter_events.assert_not_called()


def test_retry_all_empty_shows_message(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = []

    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry", "--all"])

    assert result.exit_code == 0
    assert "No dead letter events to retry" in result.stdout
    mock_cli_client.retry_all_dead_letter_events.assert_not_called()


def test_retry_without_id_or_all_exits_with_error(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    result = cli_runner.invoke(app, ["ops", "dead-letter", "retry"])

    assert result.exit_code == 1
    assert "Provide an event_id or use --all" in result.stdout


def test_delete_single_event_succeeds(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.delete_dead_letter_event.return_value = None

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "evt_123"])

    assert result.exit_code == 0
    assert "Deleted event" in result.stdout
    assert "evt_123" in result.stdout
    mock_cli_client.delete_dead_letter_event.assert_called_once_with("evt_123")


def test_delete_not_found_exits_with_error(
    cli_runner: CliRunner, mock_cli_client: MockType, mocker: MockerFixture
) -> None:
    mock_response = mocker.MagicMock()
    mock_response.status_code = 404
    mock_cli_client.delete_dead_letter_event.side_effect = httpx.HTTPStatusError(
        "Not found",
        request=mocker.MagicMock(),
        response=mock_response,
    )

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "evt_nonexistent"])

    assert result.exit_code == 1
    assert "Event not found" in result.stdout
    assert "evt_nonexistent" in result.stdout


def test_delete_all_confirmed_succeeds(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1"),
        make_dead_letter_event("evt_2"),
        make_dead_letter_event("evt_3"),
    ]
    mock_cli_client.delete_dead_letter_events.return_value = 3

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--all"], input="y\n")

    assert result.exit_code == 0
    assert "Deleted 3 event(s)" in result.stdout
    mock_cli_client.delete_dead_letter_events.assert_called_once_with(all=True)


def test_delete_all_declined_exits_cleanly(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1"),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--all"], input="n\n")

    assert result.exit_code == 0
    mock_cli_client.delete_dead_letter_events.assert_not_called()


def test_delete_all_empty_shows_message(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = []

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--all"])

    assert result.exit_code == 0
    assert "No dead letter events to delete" in result.stdout
    mock_cli_client.delete_dead_letter_events.assert_not_called()


def test_delete_by_provider_confirmed_succeeds(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1", provider="postgres"),
        make_dead_letter_event("evt_2", provider="postgres"),
    ]
    mock_cli_client.delete_dead_letter_events.return_value = 2

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--provider", "postgres"], input="y\n")

    assert result.exit_code == 0
    assert "Deleted 2 event(s) for provider 'postgres'" in result.stdout
    mock_cli_client.delete_dead_letter_events.assert_called_once_with(provider="postgres")


def test_delete_by_provider_declined_exits_cleanly(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = [
        make_dead_letter_event("evt_1", provider="postgres"),
    ]

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--provider", "postgres"], input="n\n")

    assert result.exit_code == 0
    mock_cli_client.delete_dead_letter_events.assert_not_called()


def test_delete_by_provider_empty_shows_message(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    mock_cli_client.list_dead_letter_events.return_value = []

    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete", "--provider", "postgres"])

    assert result.exit_code == 0
    assert "No dead letter events found for provider 'postgres'" in result.stdout
    mock_cli_client.delete_dead_letter_events.assert_not_called()


def test_delete_without_id_or_options_exits_with_error(cli_runner: CliRunner, mock_cli_client: MockType) -> None:
    result = cli_runner.invoke(app, ["ops", "dead-letter", "delete"])

    assert result.exit_code == 1
    assert "Provide an event_id, --provider, or --all" in result.stdout
