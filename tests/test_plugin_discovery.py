"""Integration tests for pragma command plugin discovery."""

from collections.abc import Callable

import pytest
import typer
from typer.testing import CliRunner

from pragma_cli.main import app
from pragma_cli.plugins import load_plugins


class FakeEntryPoint:
    """Minimal entry point double for exercising plugin loading behavior."""

    def __init__(self, name: str, loader: Callable[[], object]) -> None:
        """Store entry-point name and loader callable."""
        self.name = name
        self._loader = loader
        self.load_count = 0

    def load(self) -> object:
        """Count loads and return the configured loaded object.

        Returns:
            Object returned by the configured loader.
        """
        self.load_count += 1
        return self._loader()


def test_lint_subcommand_mounted() -> None:
    """Pragma --help lists lint as a top-level subcommand."""
    runner = CliRunner()

    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "lint" in result.stdout


def test_lint_help_works() -> None:
    """Pragma lint --help executes without crashing."""
    runner = CliRunner()

    result = runner.invoke(app, ["lint", "--help"])

    assert result.exit_code == 0
    assert "check" in result.stdout
    assert "rules" in result.stdout


def test_lint_rules_list_runs() -> None:
    """Pragma lint rules list prints at least one rule ID."""
    runner = CliRunner()

    result = runner.invoke(app, ["lint", "rules", "list"])

    assert result.exit_code == 0
    assert "pra-" in result.stdout


def test_broken_plugin_is_skipped(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin whose load raises is logged and skipped without crashing."""
    app = typer.Typer()

    def raise_import_error() -> object:
        raise ImportError("boom")

    broken = FakeEntryPoint("broken", raise_import_error)
    monkeypatch.setattr("pragma_cli.plugins.entry_points", lambda group: [broken])

    load_plugins(app)

    assert "Failed to load pragma plugin" in caplog.text
    assert not any(group.name == "broken" for group in app.registered_groups)


def test_non_typer_plugin_is_skipped(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin whose entry resolves to a non-Typer value is logged and skipped."""
    app = typer.Typer()
    bad = FakeEntryPoint("bad", object)
    monkeypatch.setattr("pragma_cli.plugins.entry_points", lambda group: [bad])

    load_plugins(app)

    assert "expected typer.Typer" in caplog.text
    assert not any(group.name == "bad" for group in app.registered_groups)


def test_plugin_collision_with_static_command_is_skipped(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plugin whose name matches an existing static command is skipped before load."""
    app = typer.Typer()

    @app.command("auth")
    def auth_command() -> None:
        pass

    plugin = FakeEntryPoint("auth", typer.Typer)
    monkeypatch.setattr("pragma_cli.plugins.entry_points", lambda group: [plugin])

    load_plugins(app)

    assert plugin.load_count == 0
    assert "conflicts with an already-registered" in caplog.text
    assert not any(group.name == "auth" for group in app.registered_groups)


def test_duplicate_plugin_names_first_wins(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two plugins with the same name mount only the first plugin."""
    app = typer.Typer()
    first = FakeEntryPoint("dup", typer.Typer)
    second = FakeEntryPoint("dup", typer.Typer)
    monkeypatch.setattr("pragma_cli.plugins.entry_points", lambda group: [first, second])

    load_plugins(app)

    matching = [group for group in app.registered_groups if group.name == "dup"]
    assert len(matching) == 1
    assert first.load_count == 1
    assert second.load_count == 0
    assert "conflicts with an already-registered" in caplog.text
