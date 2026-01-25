"""Tests for CLI resource commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

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
    # Table columns: Provider, Resource, Name, State, Updated
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "db1" in result.stdout
    assert "db2" in result.stdout
    assert "ready" in result.stdout
    assert "draft" in result.stdout
    mock_cli_client.list_resources.assert_called_once_with(provider=None, resource=None, tags=None)


def test_list_resources_with_provider_filter(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
    ]
    result = cli_runner.invoke(app, ["resources", "list", "--provider", "postgres"])
    assert result.exit_code == 0
    # Table format: Provider, Resource, Name columns
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "db1" in result.stdout
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
    # Table format: Provider, Resource, Name, State columns
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "test-db" in result.stdout
    assert "ready" in result.stdout
    mock_cli_client.get_resource.assert_called_once_with(provider="postgres", resource="database", name="test-db")


def test_get_all_resources_of_type(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database"])
    assert result.exit_code == 0
    # Table format: Provider, Resource, Name, State columns
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "db1" in result.stdout
    assert "db2" in result.stdout
    assert "ready" in result.stdout
    assert "draft" in result.stdout
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


def test_describe_resource_ready(cli_runner, mock_cli_client):
    """Test describe shows full resource details for ready resource."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "my-secret",
        "lifecycle_state": "ready",
        "config": {"project_id": "my-project", "secret_id": "test-secret"},
        "outputs": {"secret_name": "projects/my-project/secrets/test-secret"},
        "dependencies": [],
        "tags": ["test", "gcp"],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "my-secret"])
    assert result.exit_code == 0
    assert "gcp/secret/my-secret" in result.stdout
    assert "ready" in result.stdout
    assert "project_id" in result.stdout
    assert "my-project" in result.stdout
    assert "secret_name" in result.stdout
    assert "test, gcp" in result.stdout
    mock_cli_client.get_resource.assert_called_once_with(provider="gcp", resource="secret", name="my-secret")


def test_describe_resource_failed(cli_runner, mock_cli_client):
    """Test describe shows error message for failed resource."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "failed-secret",
        "lifecycle_state": "failed",
        "config": {"project_id": "my-project"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": "Secret Manager API not enabled for project",
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "failed-secret"])
    assert result.exit_code == 0
    assert "failed" in result.stdout
    assert "Error" in result.stdout
    assert "Secret Manager API not enabled" in result.stdout


def test_describe_resource_with_dependencies(cli_runner, mock_cli_client):
    """Test describe shows dependencies."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "dep-secret",
        "lifecycle_state": "ready",
        "config": {},
        "outputs": {},
        "dependencies": [
            {"provider": "pragma", "resource": "secret", "name": "credentials"},
        ],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "dep-secret"])
    assert result.exit_code == 0
    assert "Dependencies" in result.stdout
    assert "pragma/secret/credentials" in result.stdout


def test_describe_resource_with_field_reference(cli_runner, mock_cli_client):
    """Test describe formats FieldReference in config."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "ref-secret",
        "lifecycle_state": "ready",
        "config": {
            "project_id": "my-project",
            "credentials": {
                "provider": "pragma",
                "resource": "secret",
                "name": "gcp-creds",
                "field": "config.data.service_account",
            },
        },
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "ref-secret"])
    assert result.exit_code == 0
    # FieldReference should be formatted as provider/resource/name#field
    assert "pragma/secret/gcp-creds#config.data.service_account" in result.stdout


def test_describe_resource_not_found(cli_runner, mock_cli_client):
    """Test describe handles not found error."""
    import httpx

    response = httpx.Response(404, json={"detail": "Resource not found: gcp_secret_nonexistent"})
    mock_cli_client.get_resource.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=httpx.Request("GET", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "nonexistent"])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Resource not found" in result.stdout


def test_list_resources_shows_error_for_failed(cli_runner, mock_cli_client):
    """Test list shows error message for failed resources."""
    mock_cli_client.list_resources.return_value = [
        {
            "provider": "gcp",
            "resource": "secret",
            "name": "failed-resource",
            "lifecycle_state": "failed",
            "error": "Secret Manager API not enabled",
        },
    ]
    result = cli_runner.invoke(app, ["resources", "list"])
    assert result.exit_code == 0
    # Table format with separate columns plus error below
    assert "gcp" in result.stdout
    assert "secret" in result.stdout
    assert "failed-resource" in result.stdout
    assert "failed" in result.stdout
    assert "Secret Manager API not enabled" in result.stdout


def test_get_resource_shows_error_for_failed(cli_runner, mock_cli_client):
    """Test get shows error message for failed resource."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "failed-resource",
        "lifecycle_state": "failed",
        "error": "Credentials invalid",
    }
    result = cli_runner.invoke(app, ["resources", "get", "gcp/secret", "failed-resource"])
    assert result.exit_code == 0
    # Table format with error below
    assert "failed" in result.stdout
    assert "Credentials invalid" in result.stdout


def test_apply_shows_dependency_validation_error(cli_runner, mock_cli_client, tmp_path):
    """Test apply shows detailed dependency validation errors."""
    import httpx

    yaml_content = """provider: gcp
resource: secret
name: test-secret
config:
  project_id: my-project
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    response = httpx.Response(
        422,
        json={
            "detail": {
                "message": "Dependency validation failed",
                "missing_dependencies": [],
                "not_ready_dependencies": [{"id": "resource:pragma_secret_gcp-credentials", "state": "pending"}],
            }
        },
    )
    mock_cli_client.apply_resource.side_effect = httpx.HTTPStatusError(
        "422 Unprocessable Entity", request=httpx.Request("POST", "http://test"), response=response
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Dependency validation failed" in result.stdout
    assert "Dependencies not ready" in result.stdout
    assert "pending" in result.stdout


def test_apply_shows_field_reference_error(cli_runner, mock_cli_client, tmp_path):
    """Test apply shows detailed field reference errors."""
    import httpx

    yaml_content = """provider: gcp
resource: secret
name: test-secret
config:
  project_id: my-project
"""
    yaml_file = tmp_path / "secret.yaml"
    yaml_file.write_text(yaml_content)

    response = httpx.Response(
        422,
        json={
            "detail": {
                "message": "Field reference resolution failed",
                "reference_provider": "pragma",
                "reference_resource": "secret",
                "reference_name": "gcp-creds",
                "field": "config.data.service_account",
            }
        },
    )
    mock_cli_client.apply_resource.side_effect = httpx.HTTPStatusError(
        "422 Unprocessable Entity", request=httpx.Request("POST", "http://test"), response=response
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Field reference resolution failed" in result.stdout
    assert "pragma/secret/gcp-creds#config.data.service_account" in result.stdout


def test_delete_shows_error(cli_runner, mock_cli_client):
    """Test delete shows error message on failure."""
    import httpx

    response = httpx.Response(404, json={"detail": "Resource not found"})
    mock_cli_client.delete_resource.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=httpx.Request("DELETE", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "delete", "gcp/secret", "nonexistent"])
    assert result.exit_code == 1
    assert "Error deleting" in result.stdout
    assert "Resource not found" in result.stdout


def test_types_shows_table(cli_runner, mock_cli_client):
    """Test types command shows resource types in a table."""
    mock_cli_client.list_resource_types.return_value = [
        {"provider": "gcp", "resource": "secret", "description": "GCP Secret Manager secret"},
        {"provider": "gcp", "resource": "bucket", "description": "GCP Cloud Storage bucket"},
        {"provider": "postgres", "resource": "database", "description": None},
    ]
    result = cli_runner.invoke(app, ["resources", "types"])
    assert result.exit_code == 0
    assert "Provider" in result.stdout
    assert "Resource" in result.stdout
    assert "Description" in result.stdout
    assert "gcp" in result.stdout
    assert "secret" in result.stdout
    assert "GCP Secret Manager secret" in result.stdout
    assert "bucket" in result.stdout
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    mock_cli_client.list_resource_types.assert_called_once_with(provider=None)


def test_types_with_provider_filter(cli_runner, mock_cli_client):
    """Test types command filters by provider."""
    mock_cli_client.list_resource_types.return_value = [
        {"provider": "gcp", "resource": "secret", "description": "GCP Secret Manager secret"},
    ]
    result = cli_runner.invoke(app, ["resources", "types", "--provider", "gcp"])
    assert result.exit_code == 0
    assert "gcp" in result.stdout
    mock_cli_client.list_resource_types.assert_called_once_with(provider="gcp")


def test_types_empty_list(cli_runner, mock_cli_client):
    """Test types command handles empty list."""
    mock_cli_client.list_resource_types.return_value = []
    result = cli_runner.invoke(app, ["resources", "types"])
    assert result.exit_code == 0
    assert "No resource types found" in result.stdout


def test_types_shows_error(cli_runner, mock_cli_client):
    """Test types command shows error on failure."""
    import httpx

    response = httpx.Response(500, json={"detail": "Internal server error"})
    mock_cli_client.list_resource_types.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error", request=httpx.Request("GET", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "types"])
    assert result.exit_code == 1
    assert "Error" in result.stdout


# --- Tests for --output/-o flag ---


def test_list_resources_json_output(cli_runner, mock_cli_client):
    """Test list resources with JSON output format."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "list", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["provider"] == "postgres"
    assert data[0]["resource"] == "database"
    assert data[0]["name"] == "db1"
    assert data[0]["lifecycle_state"] == "ready"


def test_list_resources_yaml_output(cli_runner, mock_cli_client):
    """Test list resources with YAML output format."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
    ]
    result = cli_runner.invoke(app, ["resources", "list", "--output", "yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "postgres"
    assert data[0]["name"] == "db1"


def test_get_resource_json_output(cli_runner, mock_cli_client):
    """Test get single resource with JSON output format."""
    mock_cli_client.get_resource.return_value = mock_resource("postgres", "database", "test-db", "ready")
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database", "test-db", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    # get returns a list even for single resource
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "postgres"
    assert data[0]["name"] == "test-db"


def test_get_resources_by_type_json_output(cli_runner, mock_cli_client):
    """Test get all resources of type with JSON output format."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("postgres", "database", "db1", "ready"),
        mock_resource("postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "get", "postgres/database", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_describe_json_output(cli_runner, mock_cli_client):
    """Test describe resource with JSON output format."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "my-secret",
        "lifecycle_state": "ready",
        "config": {"project_id": "my-project"},
        "outputs": {"secret_name": "projects/my-project/secrets/test-secret"},
        "dependencies": [],
        "tags": ["test"],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "my-secret", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["provider"] == "gcp"
    assert data["name"] == "my-secret"
    assert data["config"]["project_id"] == "my-project"
    assert data["outputs"]["secret_name"] == "projects/my-project/secrets/test-secret"


def test_describe_yaml_output(cli_runner, mock_cli_client):
    """Test describe resource with YAML output format."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "my-secret",
        "lifecycle_state": "ready",
        "config": {"project_id": "my-project"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    result = cli_runner.invoke(app, ["resources", "describe", "gcp/secret", "my-secret", "--output", "yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert data["provider"] == "gcp"
    assert data["name"] == "my-secret"


def test_types_json_output(cli_runner, mock_cli_client):
    """Test types command with JSON output format."""
    mock_cli_client.list_resource_types.return_value = [
        {"provider": "gcp", "resource": "secret", "description": "GCP Secret Manager secret"},
        {"provider": "postgres", "resource": "database", "description": None},
    ]
    result = cli_runner.invoke(app, ["resources", "types", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["provider"] == "gcp"
    assert data[0]["resource"] == "secret"
    assert data[0]["description"] == "GCP Secret Manager secret"


def test_types_yaml_output(cli_runner, mock_cli_client):
    """Test types command with YAML output format."""
    mock_cli_client.list_resource_types.return_value = [
        {"provider": "gcp", "resource": "secret", "description": "GCP Secret Manager secret"},
    ]
    result = cli_runner.invoke(app, ["resources", "types", "--output", "yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "gcp"
