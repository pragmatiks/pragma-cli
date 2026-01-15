"""Tests for CLI providers commands."""

import tarfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pragma_sdk import BuildInfo, BuildResult, BuildStatus, DeploymentResult, DeploymentStatus, ProviderInfo, PushResult
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from pragma_cli.commands.providers import (
    DEFAULT_TEMPLATE_URL,
    TARBALL_EXCLUDES,
    create_tarball,
    detect_provider_package,
    get_template_source,
)
from pragma_cli.main import app


@pytest.fixture
def cli_runner():
    return CliRunner()


@pytest.fixture
def template_path(monkeypatch):
    """Set up environment to use local template."""
    template_dir = Path(__file__).parents[3] / "templates" / "provider"
    monkeypatch.setenv("PRAGMA_PROVIDER_TEMPLATE", str(template_dir))
    return template_dir


@pytest.fixture
def mock_pragma_client(mocker: MockerFixture):
    """Mock get_client for testing."""
    mock_client = mocker.Mock()
    mock_client._auth = mocker.Mock()  # Simulate authenticated client
    mocker.patch("pragma_cli.commands.providers.get_client", return_value=mock_client)
    return mock_client


@pytest.fixture
def provider_project(tmp_path, monkeypatch):
    """Create a minimal provider project structure."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "test-provider"')

    src_dir = tmp_path / "src" / "test_provider"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("")
    (src_dir / "resources.py").write_text("# Resources go here")

    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_get_template_source_uses_env_variable(monkeypatch):
    """Returns environment variable value when set."""
    monkeypatch.setenv("PRAGMA_PROVIDER_TEMPLATE", "/custom/template/path")
    assert get_template_source() == "/custom/template/path"


def test_get_template_source_default():
    """Default template URL is GitHub."""
    assert DEFAULT_TEMPLATE_URL == "gh:pragmatiks/provider-template"


def test_init_creates_project_structure(cli_runner, tmp_path, template_path):
    """Init creates complete project structure with all expected files."""
    result = cli_runner.invoke(
        app,
        ["providers", "init", "mycompany", "--output", str(tmp_path / "mycompany-provider"), "--defaults"],
    )
    assert result.exit_code == 0

    project_dir = tmp_path / "mycompany-provider"
    assert project_dir.exists()
    assert (project_dir / "pyproject.toml").exists()
    assert (project_dir / "README.md").exists()
    assert (project_dir / ".copier-answers.yml").exists()
    assert (project_dir / "src" / "mycompany_provider" / "__init__.py").exists()
    assert (project_dir / "src" / "mycompany_provider" / "resources" / "__init__.py").exists()
    assert (project_dir / "tests" / "conftest.py").exists()

    pyproject = (project_dir / "pyproject.toml").read_text()
    assert "pragmatiks-sdk" in pyproject

    assert "uv sync" in result.stdout
    assert "pragma providers push" in result.stdout


def test_init_fails_if_directory_exists(cli_runner, tmp_path, template_path):
    """Init fails if target directory already exists."""
    existing_dir = tmp_path / "existing-provider"
    existing_dir.mkdir()

    result = cli_runner.invoke(app, ["providers", "init", "existing", "--output", str(existing_dir)])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_update_fails_without_answers_file(cli_runner, tmp_path):
    """Update fails when .copier-answers.yml is missing."""
    result = cli_runner.invoke(app, ["providers", "update", str(tmp_path)])
    assert result.exit_code == 1
    assert "not a Copier-generated project" in result.output


def test_push_fails_without_pyproject(cli_runner, tmp_path, monkeypatch):
    """Push fails when no pyproject.toml exists."""
    monkeypatch.chdir(tmp_path)
    result = cli_runner.invoke(app, ["providers", "push"])
    assert result.exit_code == 1
    assert "Could not detect provider package" in result.output


def test_push_uploads_tarball_and_polls_status(cli_runner, provider_project, mock_pragma_client):
    """Push creates tarball, uploads to API, and polls for completion."""
    mock_pragma_client.push_provider.return_value = PushResult(
        version="20250114.153045",
        job_name="build-test-abc12345",
        status=BuildStatus.PENDING,
        message="Build started",
    )
    mock_pragma_client.get_build_status.return_value = BuildResult(
        job_name="build-test-abc12345",
        status=BuildStatus.SUCCESS,
        image="registry.local/test:abc123",
    )

    result = cli_runner.invoke(app, ["providers", "push"])

    assert result.exit_code == 0
    assert "Pushing provider: test" in result.output
    assert "Created tarball:" in result.output
    assert "Build started:" in result.output
    assert "Build successful:" in result.output

    mock_pragma_client.push_provider.assert_called_once()
    call_args = mock_pragma_client.push_provider.call_args
    assert call_args[0][0] == "test"  # provider_id
    assert isinstance(call_args[0][1], bytes)  # tarball


def test_push_with_deploy_flag_deploys_after_build(cli_runner, provider_project, mock_pragma_client):
    """Push with --deploy deploys after successful build."""
    mock_pragma_client.push_provider.return_value = PushResult(
        version="20250114.153045",
        job_name="build-test-abc12345",
        status=BuildStatus.PENDING,
        message="Build started",
    )
    mock_pragma_client.get_build_status.return_value = BuildResult(
        job_name="build-test-abc12345",
        status=BuildStatus.SUCCESS,
        image="registry.local/test:abc123",
    )
    mock_pragma_client.deploy_provider.return_value = DeploymentResult(
        deployment_name="provider-test",
        status=DeploymentStatus.PROGRESSING,
        message="Deployment started",
    )

    result = cli_runner.invoke(app, ["providers", "push", "--deploy"])

    assert result.exit_code == 0
    assert "Deployment started:" in result.output
    mock_pragma_client.deploy_provider.assert_called_once_with("test", "registry.local/test:abc123")


def test_push_with_no_wait_returns_immediately(cli_runner, provider_project, mock_pragma_client):
    """Push with --no-wait returns immediately after upload."""
    mock_pragma_client.push_provider.return_value = PushResult(
        version="20250114.153045",
        job_name="build-test-abc12345",
        status=BuildStatus.PENDING,
        message="Build started",
    )

    result = cli_runner.invoke(app, ["providers", "push", "--no-wait"])

    assert result.exit_code == 0
    assert "Build running in background" in result.output
    mock_pragma_client.get_build_status.assert_not_called()


def test_push_handles_build_failure(cli_runner, provider_project, mock_pragma_client):
    """Push handles build failures gracefully."""
    mock_pragma_client.push_provider.return_value = PushResult(
        version="20250114.153045",
        job_name="build-test-abc12345",
        status=BuildStatus.PENDING,
        message="Build started",
    )
    mock_pragma_client.get_build_status.return_value = BuildResult(
        job_name="build-test-abc12345",
        status=BuildStatus.FAILED,
        error_message="Dockerfile syntax error",
    )

    result = cli_runner.invoke(app, ["providers", "push"])

    assert result.exit_code == 1
    assert "Build failed:" in result.output
    assert "Dockerfile syntax error" in result.output


def test_push_with_package_option_uses_specified_name(cli_runner, tmp_path, mock_pragma_client, monkeypatch):
    """Push uses --package option instead of detecting from pyproject."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other-provider"')

    mock_pragma_client.push_provider.return_value = PushResult(
        version="20250114.153045",
        job_name="build-custom-abc12345",
        status=BuildStatus.PENDING,
        message="Build started",
    )
    mock_pragma_client.get_build_status.return_value = BuildResult(
        job_name="build-custom-abc12345",
        status=BuildStatus.SUCCESS,
        image="registry.local/custom:abc123",
    )

    result = cli_runner.invoke(app, ["providers", "push", "--package", "custom_provider"])

    assert result.exit_code == 0
    assert "Pushing provider: custom" in result.output
    mock_pragma_client.push_provider.assert_called_once()
    assert mock_pragma_client.push_provider.call_args[0][0] == "custom"


def test_create_tarball_includes_source_files(tmp_path):
    """Tarball includes provider source files."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print('hello')")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"')

    tarball_bytes = create_tarball(tmp_path)

    with tarfile.open(fileobj=BytesIO(tarball_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert "./src/main.py" in names or "src/main.py" in names
        assert "./pyproject.toml" in names or "pyproject.toml" in names


def test_create_tarball_excludes_git_directory(tmp_path):
    """Tarball excludes .git directory."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git config")
    (tmp_path / "main.py").write_text("print('hello')")

    tarball_bytes = create_tarball(tmp_path)

    with tarfile.open(fileobj=BytesIO(tarball_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert not any(".git" in name for name in names)


def test_create_tarball_excludes_pycache(tmp_path):
    """Tarball excludes __pycache__ directories."""
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "module.cpython-313.pyc").write_bytes(b"bytecode")
    (tmp_path / "main.py").write_text("print('hello')")

    tarball_bytes = create_tarball(tmp_path)

    with tarfile.open(fileobj=BytesIO(tarball_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert not any("__pycache__" in name for name in names)


def test_create_tarball_excludes_venv(tmp_path):
    """Tarball excludes .venv directory."""
    venv = tmp_path / ".venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text("home = /usr/bin")
    (tmp_path / "main.py").write_text("print('hello')")

    tarball_bytes = create_tarball(tmp_path)

    with tarfile.open(fileobj=BytesIO(tarball_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert not any(".venv" in name for name in names)


def test_create_tarball_excludes_pyc_files(tmp_path):
    """Tarball excludes .pyc files."""
    (tmp_path / "module.pyc").write_bytes(b"bytecode")
    (tmp_path / "main.py").write_text("print('hello')")

    tarball_bytes = create_tarball(tmp_path)

    with tarfile.open(fileobj=BytesIO(tarball_bytes), mode="r:gz") as tar:
        names = tar.getnames()
        assert not any(".pyc" in name for name in names)


def test_tarball_excludes_contains_common_patterns():
    """TARBALL_EXCLUDES contains expected patterns."""
    expected = {".git", "__pycache__", ".venv", ".env", "*.pyc", "dist", "build"}
    assert expected.issubset(TARBALL_EXCLUDES)


def test_detect_provider_package_from_pyproject(tmp_path, monkeypatch):
    """Detects package name from pyproject.toml."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nname = "postgres-provider"')
    monkeypatch.chdir(tmp_path)

    result = detect_provider_package()
    assert result == "postgres_provider"


def test_detect_provider_package_returns_none_without_pyproject(tmp_path, monkeypatch):
    """Returns None when no pyproject.toml exists."""
    monkeypatch.chdir(tmp_path)
    result = detect_provider_package()
    assert result is None


def test_rollback_with_explicit_version(cli_runner, mock_pragma_client):
    """Rollback deploys the specified version."""
    mock_pragma_client.rollback_provider.return_value = DeploymentResult(
        deployment_name="provider-test",
        status=DeploymentStatus.PROGRESSING,
        message="Deployment started",
    )

    result = cli_runner.invoke(app, ["providers", "rollback", "test", "--version", "20250114.120000"])

    assert result.exit_code == 0
    assert "Rolling back provider: test" in result.output
    assert "Target version: 20250114.120000" in result.output
    assert "Rollback initiated:" in result.output
    mock_pragma_client.rollback_provider.assert_called_once_with("test", "20250114.120000")


def test_rollback_without_version_finds_previous(cli_runner, mock_pragma_client):
    """Rollback without version finds the second-most-recent successful build."""
    mock_pragma_client.list_builds.return_value = [
        BuildInfo(
            provider_id="test",
            version="20250114.150000",
            status=BuildStatus.SUCCESS,
            created_at=datetime(2025, 1, 14, 15, 0, 0),
        ),
        BuildInfo(
            provider_id="test",
            version="20250114.120000",
            status=BuildStatus.SUCCESS,
            created_at=datetime(2025, 1, 14, 12, 0, 0),
        ),
    ]
    mock_pragma_client.rollback_provider.return_value = DeploymentResult(
        deployment_name="provider-test",
        status=DeploymentStatus.PROGRESSING,
        message="Deployment started",
    )

    result = cli_runner.invoke(app, ["providers", "rollback", "test"])

    assert result.exit_code == 0
    assert "Target version: 20250114.120000" in result.output
    mock_pragma_client.rollback_provider.assert_called_once_with("test", "20250114.120000")


def test_rollback_without_version_fails_if_no_previous_build(cli_runner, mock_pragma_client):
    """Rollback without version fails if only one successful build exists."""
    mock_pragma_client.list_builds.return_value = [
        BuildInfo(
            provider_id="test",
            version="20250114.150000",
            status=BuildStatus.SUCCESS,
            created_at=datetime(2025, 1, 14, 15, 0, 0),
        ),
    ]

    result = cli_runner.invoke(app, ["providers", "rollback", "test"])

    assert result.exit_code == 1
    assert "No previous successful build found" in result.output
    mock_pragma_client.rollback_provider.assert_not_called()


def test_rollback_skips_failed_builds_when_finding_previous(cli_runner, mock_pragma_client):
    """Rollback skips failed builds when finding previous version."""
    mock_pragma_client.list_builds.return_value = [
        BuildInfo(
            provider_id="test",
            version="20250114.160000",
            status=BuildStatus.SUCCESS,
            created_at=datetime(2025, 1, 14, 16, 0, 0),
        ),
        BuildInfo(
            provider_id="test",
            version="20250114.150000",
            status=BuildStatus.FAILED,
            error_message="Build error",
            created_at=datetime(2025, 1, 14, 15, 0, 0),
        ),
        BuildInfo(
            provider_id="test",
            version="20250114.120000",
            status=BuildStatus.SUCCESS,
            created_at=datetime(2025, 1, 14, 12, 0, 0),
        ),
    ]
    mock_pragma_client.rollback_provider.return_value = DeploymentResult(
        deployment_name="provider-test",
        status=DeploymentStatus.PROGRESSING,
        message="Deployment started",
    )

    result = cli_runner.invoke(app, ["providers", "rollback", "test"])

    assert result.exit_code == 0
    assert "Target version: 20250114.120000" in result.output
    mock_pragma_client.rollback_provider.assert_called_once_with("test", "20250114.120000")


def test_rollback_fails_if_version_not_found(cli_runner, mock_pragma_client):
    """Rollback fails if specified version doesn't exist."""
    import httpx

    mock_pragma_client.rollback_provider.side_effect = httpx.HTTPStatusError(
        "404 Not Found",
        request=httpx.Request("POST", "http://localhost:8000/providers/test/rollback"),
        response=httpx.Response(404, json={"detail": "Build not found: test@20250101.000000"}),
    )

    result = cli_runner.invoke(app, ["providers", "rollback", "test", "--version", "20250101.000000"])

    assert result.exit_code == 1
    assert "Build not found" in result.output


def test_rollback_fails_if_build_not_deployable(cli_runner, mock_pragma_client):
    """Rollback fails if build status is not SUCCESS."""
    import httpx

    mock_pragma_client.rollback_provider.side_effect = httpx.HTTPStatusError(
        "400 Bad Request",
        request=httpx.Request("POST", "http://localhost:8000/providers/test/rollback"),
        response=httpx.Response(400, json={"detail": "Build 20250114.120000 is not deployable (status: failed)"}),
    )

    result = cli_runner.invoke(app, ["providers", "rollback", "test", "--version", "20250114.120000"])

    assert result.exit_code == 1
    assert "not deployable" in result.output


def test_status_displays_deployment_info(cli_runner, mock_pragma_client):
    """Status command displays deployment information."""
    mock_pragma_client.get_deployment_status.return_value = DeploymentResult(
        deployment_name="provider-test-abc12345",
        status=DeploymentStatus.AVAILABLE,
        available_replicas=2,
        ready_replicas=2,
        image="europe-west4-docker.pkg.dev/project/repo/test:20250114.153045",
        updated_at=datetime(2025, 1, 14, 15, 30, 45, tzinfo=UTC),
        message="Deployment has minimum availability",
    )

    result = cli_runner.invoke(app, ["providers", "status", "test"])

    assert result.exit_code == 0
    assert "Provider:" in result.output
    assert "test" in result.output
    assert "available" in result.output
    assert "2 available / 2 ready" in result.output
    assert "20250114.153045" in result.output
    assert "2025-01-14" in result.output


def test_status_handles_not_found(cli_runner, mock_pragma_client):
    """Status command handles deployment not found."""
    import httpx

    mock_response = httpx.Response(404, json={"detail": "Deployment not found"})
    mock_pragma_client.get_deployment_status.side_effect = httpx.HTTPStatusError(
        "Not found", request=httpx.Request("GET", "http://test"), response=mock_response
    )

    result = cli_runner.invoke(app, ["providers", "status", "nonexistent"])

    assert result.exit_code == 1
    assert "Deployment not found" in result.output


def test_status_handles_progressing_deployment(cli_runner, mock_pragma_client):
    """Status command shows progressing status correctly."""
    mock_pragma_client.get_deployment_status.return_value = DeploymentResult(
        deployment_name="provider-test-abc12345",
        status=DeploymentStatus.PROGRESSING,
        available_replicas=1,
        ready_replicas=1,
        image="europe-west4-docker.pkg.dev/project/repo/test:20250114.160000",
        message="ReplicaSet is progressing",
    )

    result = cli_runner.invoke(app, ["providers", "status", "test"])

    assert result.exit_code == 0
    assert "progressing" in result.output
    assert "1 available / 1 ready" in result.output


def test_status_handles_failed_deployment(cli_runner, mock_pragma_client):
    """Status command shows failed status with error message."""
    mock_pragma_client.get_deployment_status.return_value = DeploymentResult(
        deployment_name="provider-test-abc12345",
        status=DeploymentStatus.FAILED,
        available_replicas=0,
        ready_replicas=0,
        message="ProgressDeadlineExceeded",
    )

    result = cli_runner.invoke(app, ["providers", "status", "test"])

    assert result.exit_code == 0
    assert "failed" in result.output
    assert "0 available / 0 ready" in result.output
    assert "ProgressDeadlineExceeded" in result.output


def test_list_providers_displays_table(cli_runner, mock_pragma_client):
    """List command displays providers in a formatted table."""
    mock_pragma_client.list_providers.return_value = [
        ProviderInfo(
            provider_id="postgres",
            current_version="20250114.120000",
            deployment_status=DeploymentStatus.AVAILABLE,
            updated_at=datetime(2025, 1, 14, 12, 0, 0),
        ),
        ProviderInfo(
            provider_id="redis",
            current_version="20250114.100000",
            deployment_status=DeploymentStatus.PROGRESSING,
            updated_at=datetime(2025, 1, 14, 10, 0, 0),
        ),
    ]

    result = cli_runner.invoke(app, ["providers", "list"])

    assert result.exit_code == 0
    assert "postgres" in result.output
    assert "20250114.120000" in result.output
    assert "running" in result.output
    assert "redis" in result.output
    assert "deploying" in result.output


def test_list_providers_handles_empty_list(cli_runner, mock_pragma_client):
    """List command shows message when no providers exist."""
    mock_pragma_client.list_providers.return_value = []

    result = cli_runner.invoke(app, ["providers", "list"])

    assert result.exit_code == 0
    assert "No providers found" in result.output


def test_list_providers_handles_not_deployed(cli_runner, mock_pragma_client):
    """List command shows 'not deployed' for providers without deployments."""
    mock_pragma_client.list_providers.return_value = [
        ProviderInfo(
            provider_id="test",
            current_version=None,
            deployment_status=None,
            updated_at=None,
        ),
    ]

    result = cli_runner.invoke(app, ["providers", "list"])

    assert result.exit_code == 0
    assert "test" in result.output
    assert "not deployed" in result.output or "never deployed" in result.output


def test_list_providers_requires_auth(cli_runner, mock_pragma_client):
    """List command fails without authentication."""
    mock_pragma_client._auth = None

    result = cli_runner.invoke(app, ["providers", "list"])

    assert result.exit_code == 1
    assert "Authentication required" in result.output


def test_list_providers_handles_failed_status(cli_runner, mock_pragma_client):
    """List command shows failed status for providers with failed deployments."""
    mock_pragma_client.list_providers.return_value = [
        ProviderInfo(
            provider_id="broken",
            current_version="20250114.120000",
            deployment_status=DeploymentStatus.FAILED,
            updated_at=datetime(2025, 1, 14, 12, 0, 0),
        ),
    ]

    result = cli_runner.invoke(app, ["providers", "list"])

    assert result.exit_code == 0
    assert "broken" in result.output
    assert "failed" in result.output
