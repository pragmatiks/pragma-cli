"""Tests for CLI resource commands."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pragma_cli.main import app


def mock_resource(
    provider: str, resource: str, name: str, lifecycle_state: str = "draft", config: dict | None = None
) -> dict:
    """Create a mock resource dict for testing."""
    return {
        "provider": provider,
        "resource": resource,
        "name": name,
        "lifecycle_state": lifecycle_state,
        "config": config or {},
    }


@pytest.fixture
def sample_yaml_content():
    return """provider: postgres
resource: database
name: test-db
config:
  name: TEST_DB
  comment: Test database
"""


@pytest.fixture
def multi_doc_yaml_content():
    return """---
provider: postgres
resource: database
name: db1
config:
  name: DB1
---
provider: postgres
resource: database
name: db2
config:
  name: DB2
"""


def test_list_resources(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "list"])
    assert result.exit_code == 0
    assert "postgres/database/db1 [ready]" in result.stdout
    assert "postgres/database/db2 [draft]" in result.stdout
    mock_cli_client.list_resources.assert_called_once_with(provider=None, resource=None, tags=None)


def test_list_resources_with_provider_filter(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
    ]
    result = cli_runner.invoke(app, ["resources", "list", "--provider", "postgres"])
    assert result.exit_code == 0
    assert "postgres/database/db1" in result.stdout
    mock_cli_client.list_resources.assert_called_once_with(provider="postgres", resource=None, tags=None)


def test_list_resources_with_resource_filter(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = []
    result = cli_runner.invoke(app, ["resources", "list", "--resource", "database"])
    assert result.exit_code == 0
    mock_cli_client.list_resources.assert_called_once_with(provider=None, resource="database", tags=None)


def test_get_single_resource(cli_runner, mock_cli_client):
    mock_cli_client.get_resource.return_value = mock_resource("postgres", "database", "test-db", "ready")
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database", "test-db"])
    assert result.exit_code == 0
    assert "postgres/database/test-db [ready]" in result.stdout
    mock_cli_client.get_resource.assert_called_once_with(provider="postgres", resource="database", name="test-db")


def test_get_all_resources_of_type(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database"])
    assert result.exit_code == 0
    assert "postgres/database/db1 [ready]" in result.stdout
    assert "postgres/database/db2 [draft]" in result.stdout
    mock_cli_client.list_resources.assert_called_once_with(provider="postgres", resource="database")


def test_apply_single_resource_from_file(cli_runner, mock_cli_client, sample_yaml_content):
    mock_cli_client.apply_resource.return_value = mock_resource("postgres", "database", "test-db", "draft")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(sample_yaml_content)
        temp_path = f.name
    try:
        result = cli_runner.invoke(app, ["resources", "apply", temp_path])
        assert result.exit_code == 0
        assert "Applied postgres/database/test-db [draft]" in result.stdout
        mock_cli_client.apply_resource.assert_called_once()
        call_kwargs = mock_cli_client.apply_resource.call_args[1]
        assert call_kwargs["resource"]["name"] == "test-db"
        assert call_kwargs["resource"]["provider"] == "postgres"
    finally:
        Path(temp_path).unlink()


def test_apply_multiple_resources_from_file(cli_runner, mock_cli_client, multi_doc_yaml_content):
    mock_cli_client.apply_resource.side_effect = [
        mock_resource("postgres", "database", "db1", "draft"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(multi_doc_yaml_content)
        temp_path = f.name
    try:
        result = cli_runner.invoke(app, ["resources", "apply", temp_path])
        assert result.exit_code == 0
        assert mock_cli_client.apply_resource.call_count == 2
        assert "Applied postgres/database/db1" in result.stdout
        assert "Applied postgres/database/db2" in result.stdout
    finally:
        Path(temp_path).unlink()


def test_delete_resource(cli_runner, mock_cli_client):
    result = cli_runner.invoke(app, ["resources", "delete", "postgres/database", "test-db"])
    assert result.exit_code == 0
    assert "Deleted postgres/database/test-db" in result.stdout
    mock_cli_client.delete_resource.assert_called_once_with(provider="postgres", resource="database", name="test-db")


def test_register_resource(cli_runner, mock_cli_client):
    result = cli_runner.invoke(app, ["resources", "register", "postgres/database"])
    assert result.exit_code == 0
    assert "Registered postgres/database" in result.stdout
    mock_cli_client.register_resource.assert_called_once_with(
        provider="postgres", resource="database", schema=None, description=None, tags=None
    )


def test_register_resource_with_options(cli_runner, mock_cli_client):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write('{"type": "object"}')
        temp_path = f.name
    try:
        result = cli_runner.invoke(
            app,
            [
                "resources",
                "register",
                "postgres/database",
                "--description",
                "PostgreSQL database",
                "--schema",
                temp_path,
                "--tag",
                "data",
            ],
        )
        assert result.exit_code == 0
        mock_cli_client.register_resource.assert_called_once()
        call_kwargs = mock_cli_client.register_resource.call_args[1]
        assert call_kwargs["description"] == "PostgreSQL database"
        assert call_kwargs["schema"] == {"type": "object"}
        assert call_kwargs["tags"] == ["data"]
    finally:
        Path(temp_path).unlink()


def test_unregister_resource(cli_runner, mock_cli_client):
    result = cli_runner.invoke(app, ["resources", "unregister", "postgres/database"])
    assert result.exit_code == 0
    assert "Unregistered postgres/database" in result.stdout
    mock_cli_client.unregister_resource.assert_called_once_with(provider="postgres", resource="database")


def test_get_nonexistent_resource(cli_runner, mock_cli_client):
    mock_cli_client.get_resource.side_effect = Exception("Resource not found")
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database", "nonexistent"])
    assert result.exit_code != 0


def test_apply_invalid_yaml(cli_runner, mock_cli_client):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write("invalid: yaml: content: [")
        temp_path = f.name
    try:
        result = cli_runner.invoke(app, ["resources", "apply", temp_path])
        assert result.exit_code != 0
        mock_cli_client.apply_resource.assert_not_called()
    finally:
        Path(temp_path).unlink()


def test_delete_with_invalid_resource_id(cli_runner, mock_cli_client):
    result = cli_runner.invoke(app, ["resources", "delete", "invalid-resource-id", "test-db"])
    assert result.exit_code != 0
    mock_cli_client.delete_resource.assert_not_called()


@pytest.fixture
def secret_yaml_with_file_ref():
    return """provider: pragma
resource: secret
name: test-secret
config:
  data:
    credentials.json: "@./creds.json"
"""


@pytest.fixture
def secret_yaml_with_multiple_file_refs():
    return """provider: pragma
resource: secret
name: multi-secret
config:
  data:
    key1.txt: "@./file1.txt"
    key2.txt: "@./file2.txt"
    plain_value: "not a file ref"
"""


def test_apply_secret_with_file_reference(cli_runner, mock_cli_client, tmp_path):
    """Test that file references in pragma/secret resources are resolved."""
    creds_file = tmp_path / "creds.json"
    creds_file.write_text('{"key": "secret_value"}')

    yaml_content = """provider: pragma
resource: secret
name: test-secret
config:
  data:
    credentials.json: "@./creds.json"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource(
        "pragma", "secret", "test-secret", "draft", {"data": {"credentials.json": '{"key": "secret_value"}'}}
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0
    assert "Applied pragma/secret/test-secret" in result.stdout

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["data"]["credentials.json"] == '{"key": "secret_value"}'


def test_apply_secret_with_multiple_file_references(cli_runner, mock_cli_client, tmp_path):
    """Test that multiple file references are resolved correctly."""
    (tmp_path / "file1.txt").write_text("content1")
    (tmp_path / "file2.txt").write_text("content2")

    yaml_content = """provider: pragma
resource: secret
name: multi-secret
config:
  data:
    key1.txt: "@./file1.txt"
    key2.txt: "@./file2.txt"
    plain_value: "not a file ref"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("pragma", "secret", "multi-secret", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    data = call_kwargs["resource"]["config"]["data"]
    assert data["key1.txt"] == "content1"
    assert data["key2.txt"] == "content2"
    assert data["plain_value"] == "not a file ref"


def test_apply_secret_with_missing_file(cli_runner, mock_cli_client, tmp_path):
    """Test that missing file references produce a clear error."""
    yaml_content = """provider: pragma
resource: secret
name: test-secret
config:
  data:
    missing.txt: "@./nonexistent.txt"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "File not found" in result.stdout
    mock_cli_client.apply_resource.assert_not_called()


def test_apply_non_secret_resource_unchanged(cli_runner, mock_cli_client, tmp_path):
    """Test that non-secret resources with @ values are not modified."""
    yaml_content = """provider: postgres
resource: database
name: test-db
config:
  data:
    email: "@user.example.com"
"""
    yaml_file = tmp_path / "db.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("postgres", "database", "test-db", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["data"]["email"] == "@user.example.com"


def test_apply_secret_with_absolute_path(cli_runner, mock_cli_client, tmp_path):
    """Test that absolute file paths work correctly."""
    creds_file = tmp_path / "absolute_creds.json"
    creds_file.write_text("absolute content")

    yaml_content = f"""provider: pragma
resource: secret
name: abs-secret
config:
  data:
    file.txt: "@{creds_file}"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("pragma", "secret", "abs-secret", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["data"]["file.txt"] == "absolute content"


def test_apply_secret_without_data_unchanged(cli_runner, mock_cli_client, tmp_path):
    """Test that secrets without config.data are handled gracefully."""
    yaml_content = """provider: pragma
resource: secret
name: empty-secret
config:
  other_field: "value"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("pragma", "secret", "empty-secret", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["other_field"] == "value"


def test_apply_secret_preserves_other_config_fields(cli_runner, mock_cli_client, tmp_path):
    """Test that file resolution preserves other config fields."""
    (tmp_path / "secret.txt").write_text("secret data")

    yaml_content = """provider: pragma
resource: secret
name: test-secret
config:
  description: "My secret"
  data:
    secret.txt: "@./secret.txt"
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("pragma", "secret", "test-secret", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    config = call_kwargs["resource"]["config"]
    assert config["description"] == "My secret"
    assert config["data"]["secret.txt"] == "secret data"
