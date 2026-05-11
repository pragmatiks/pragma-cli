"""Integration tests for pragma command plugin discovery."""

from typer.testing import CliRunner

from pragma_cli.main import app


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
