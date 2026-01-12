"""Shared test fixtures for CLI tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pragma_sdk import PragmaClient
from pytest_mock import MockerFixture
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pytest_mock import MockType


@pytest.fixture
def cli_runner() -> CliRunner:
    """CLI test runner for invoking commands."""
    return CliRunner()


@pytest.fixture
def mock_pragma_client(mocker: MockerFixture) -> MockType:
    """Mock PragmaClient for testing."""
    return mocker.Mock(spec=PragmaClient)


@pytest.fixture
def mock_cli_client(mocker: MockerFixture) -> MockType:
    """Mock the get_client function to return a mock client."""
    mock_client = mocker.Mock()
    mocker.patch("pragma_cli.get_client", return_value=mock_client)
    mocker.patch("pragma_cli.commands.completions.get_client", return_value=mock_client)
    mocker.patch("pragma_cli.commands.resources.get_client", return_value=mock_client)
    mocker.patch("pragma_cli.commands.dead_letter.get_client", return_value=mock_client)
    return mock_client
