"""Tests for CLI resource commands."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml
from pragma_sdk.models.api import ResourceSchema

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
    mock_cli_client.get_resource.return_value = mock_resource("pragmatiks/postgres", "database", "test-db", "ready")
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/postgres/database/test-db"])
    assert result.exit_code == 0
    # Table format: Provider, Resource, Name, State columns
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "test-db" in result.stdout
    assert "ready" in result.stdout
    mock_cli_client.get_resource.assert_called_once_with(
        provider="pragmatiks/postgres", resource="database", name="test-db"
    )


def test_get_all_resources_of_type(cli_runner, mock_cli_client):
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1", "ready"),
        mock_resource("pragmatiks/postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/postgres/database"])
    assert result.exit_code == 0
    # Table format: Provider, Resource, Name, State columns
    assert "postgres" in result.stdout
    assert "database" in result.stdout
    assert "db1" in result.stdout
    assert "db2" in result.stdout
    assert "ready" in result.stdout
    assert "draft" in result.stdout
    mock_cli_client.list_resources.assert_called_once_with(provider="pragmatiks/postgres", resource="database")


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


def test_apply_skips_none_documents_from_trailing_separator(cli_runner, mock_cli_client, tmp_path):
    """Test that trailing --- in YAML files does not cause an error."""
    yaml_content = """---
provider: postgres
resource: database
name: db1
config:
  name: DB1
---
"""
    yaml_file = tmp_path / "trailing.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("postgres", "database", "db1", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0
    assert mock_cli_client.apply_resource.call_count == 1
    assert "Applied postgres/database/db1" in result.stdout


def test_delete_resource(cli_runner, mock_cli_client):
    result = cli_runner.invoke(app, ["resources", "delete", "pragmatiks/postgres/database/test-db"])
    assert result.exit_code == 0
    assert "Deleted pragmatiks/postgres/database/test-db" in result.stdout
    mock_cli_client.delete_resource.assert_called_once_with(
        provider="pragmatiks/postgres", resource="database", name="test-db"
    )


def test_get_nonexistent_resource(cli_runner, mock_cli_client):
    response = httpx.Response(404, json={"detail": "Resource not found: postgres_database_nonexistent"})
    mock_cli_client.get_resource.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=httpx.Request("GET", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/postgres/database/nonexistent"])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Resource not found" in result.stdout


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
    result = cli_runner.invoke(app, ["resources", "delete", "invalid-resource-id"])
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


def test_apply_resolves_at_references_for_any_provider(cli_runner, mock_cli_client, tmp_path):
    """Test that @ file references are resolved for any provider."""
    (tmp_path / "connection.txt").write_text("host=db.example.com")

    yaml_content = """provider: postgres
resource: database
name: test-db
config:
  connection_string: "@./connection.txt"
  plain_value: "no-at-prefix"
"""
    yaml_file = tmp_path / "db.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("postgres", "database", "test-db", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["connection_string"] == "host=db.example.com"
    assert call_kwargs["resource"]["config"]["plain_value"] == "no-at-prefix"


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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret"])
    assert result.exit_code == 0
    assert "gcp/secret/my-secret" in result.stdout
    assert "ready" in result.stdout
    assert "project_id" in result.stdout
    assert "my-project" in result.stdout
    assert "secret_name" in result.stdout
    assert "test, gcp" in result.stdout
    mock_cli_client.get_resource.assert_called_once_with(
        provider="pragmatiks/gcp", resource="secret", name="my-secret", reveal=False
    )


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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/failed-secret"])
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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/dep-secret"])
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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/ref-secret"])
    assert result.exit_code == 0
    # FieldReference should be formatted as provider/resource/name#field
    assert "pragma/secret/gcp-creds#config.data.service_account" in result.stdout


def test_describe_resource_not_found(cli_runner, mock_cli_client):
    """Test describe handles not found error."""
    response = httpx.Response(404, json={"detail": "Resource not found: gcp_secret_nonexistent"})
    mock_cli_client.get_resource.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=httpx.Request("GET", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/nonexistent"])
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
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/gcp/secret/failed-resource"])
    assert result.exit_code == 0
    # Table format with error below
    assert "failed" in result.stdout
    assert "Credentials invalid" in result.stdout


def test_get_resource_org_scoped_provider(cli_runner, mock_cli_client):
    """Test get with org-scoped provider ID (4 segments)."""
    mock_cli_client.get_resource.return_value = mock_resource("pragmatiks/pragma", "secret", "test")
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/pragma/secret/test"])
    assert result.exit_code == 0
    mock_cli_client.get_resource.assert_called_once_with(provider="pragmatiks/pragma", resource="secret", name="test")


def test_apply_shows_dependency_validation_error(cli_runner, mock_cli_client, tmp_path):
    """Test apply shows detailed dependency validation errors."""
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
    response = httpx.Response(404, json={"detail": "Resource not found"})
    mock_cli_client.delete_resource.side_effect = httpx.HTTPStatusError(
        "404 Not Found", request=httpx.Request("DELETE", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "delete", "pragmatiks/gcp/secret/nonexistent"])
    assert result.exit_code == 1
    assert "Error deleting" in result.stdout
    assert "Resource not found" in result.stdout


def test_types_shows_table(cli_runner, mock_cli_client):
    """Test schemas command shows resource schemas in a table."""
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(provider="gcp", resource="secret", description="GCP Secret Manager secret"),
        ResourceSchema(provider="gcp", resource="bucket", description="GCP Cloud Storage bucket"),
        ResourceSchema(provider="postgres", resource="database", description=None),
    ]
    result = cli_runner.invoke(app, ["resources", "schemas"])
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
    mock_cli_client.list_resource_schemas.assert_called_once_with(provider=None)


def test_types_with_provider_filter(cli_runner, mock_cli_client):
    """Test types command filters by provider."""
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(provider="gcp", resource="secret", description="GCP Secret Manager secret"),
    ]
    result = cli_runner.invoke(app, ["resources", "schemas", "--provider", "gcp"])
    assert result.exit_code == 0
    assert "gcp" in result.stdout
    mock_cli_client.list_resource_schemas.assert_called_once_with(provider="gcp")


def test_types_empty_list(cli_runner, mock_cli_client):
    """Test types command handles empty list."""
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "schemas"])
    assert result.exit_code == 0
    assert "No resource schemas found" in result.stdout


def test_types_shows_error(cli_runner, mock_cli_client):
    """Test types command shows error on failure."""
    response = httpx.Response(500, json={"detail": "Internal server error"})
    mock_cli_client.list_resource_schemas.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error", request=httpx.Request("GET", "http://test"), response=response
    )
    result = cli_runner.invoke(app, ["resources", "schemas"])
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
    mock_cli_client.get_resource.return_value = mock_resource("pragmatiks/postgres", "database", "test-db", "ready")
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/postgres/database/test-db", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    # get returns a list even for single resource
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "pragmatiks/postgres"
    assert data[0]["name"] == "test-db"


def test_get_resources_by_type_json_output(cli_runner, mock_cli_client):
    """Test get all resources of type with JSON output format."""
    mock_cli_client.list_resources.return_value = [
        mock_resource("pragmatiks/postgres", "database", "db1", "ready"),
        mock_resource("pragmatiks/postgres", "database", "db2", "draft"),
    ]
    result = cli_runner.invoke(app, ["resources", "get", "pragmatiks/postgres/database", "-o", "json"])
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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret", "-o", "json"])
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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret", "--output", "yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert data["provider"] == "gcp"
    assert data["name"] == "my-secret"


def test_types_json_output(cli_runner, mock_cli_client):
    """Test types command with JSON output format."""
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(provider="gcp", resource="secret", description="GCP Secret Manager secret"),
        ResourceSchema(provider="postgres", resource="database", description=None),
    ]
    result = cli_runner.invoke(app, ["resources", "schemas", "-o", "json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["provider"] == "gcp"
    assert data[0]["resource"] == "secret"
    assert data[0]["description"] == "GCP Secret Manager secret"


def test_types_yaml_output(cli_runner, mock_cli_client):
    """Test types command with YAML output format."""
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(provider="gcp", resource="secret", description="GCP Secret Manager secret"),
    ]
    result = cli_runner.invoke(app, ["resources", "schemas", "--output", "yaml"])
    assert result.exit_code == 0
    data = yaml.safe_load(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["provider"] == "gcp"


# --- Tests for pragma/file with @path syntax ---


def test_apply_file_with_content_reference(cli_runner, mock_cli_client, tmp_path):
    """Test that @path syntax in pragma/file resources uploads the file."""
    test_file = tmp_path / "document.pdf"
    test_file.write_bytes(b"%PDF-1.4 binary content here")

    yaml_content = """provider: pragma
resource: file
name: my-document
config:
  content: "@./document.pdf"
  content_type: application/pdf
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.upload_file.return_value = {
        "url": "gs://bucket/tenant/files/my-document",
        "public_url": "https://storage.googleapis.com/bucket/tenant/files/my-document",
        "size": 28,
        "content_type": "application/pdf",
        "checksum": "abc123",
        "uploaded_at": "2026-01-31T10:00:00Z",
    }
    mock_cli_client.apply_resource.return_value = mock_resource(
        "pragma", "file", "my-document", "draft", {"content_type": "application/pdf"}
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0
    assert "Applied pragma/file/my-document" in result.stdout

    mock_cli_client.upload_file.assert_called_once_with(
        "my-document", b"%PDF-1.4 binary content here", "application/pdf"
    )

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert "content" not in call_kwargs["resource"]["config"]
    assert call_kwargs["resource"]["config"]["content_type"] == "application/pdf"


def test_apply_file_with_missing_file(cli_runner, mock_cli_client, tmp_path):
    """Test that missing file references produce a clear error."""
    yaml_content = """provider: pragma
resource: file
name: missing-file
config:
  content: "@./nonexistent.pdf"
  content_type: application/pdf
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "File not found" in result.stdout
    mock_cli_client.upload_file.assert_not_called()
    mock_cli_client.apply_resource.assert_not_called()


def test_apply_file_with_missing_content_type(cli_runner, mock_cli_client, tmp_path):
    """Test that missing content_type produces a clear error."""
    test_file = tmp_path / "document.pdf"
    test_file.write_bytes(b"PDF content")

    yaml_content = """provider: pragma
resource: file
name: no-content-type
config:
  content: "@./document.pdf"
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "content_type is required" in result.stdout
    mock_cli_client.upload_file.assert_not_called()
    mock_cli_client.apply_resource.assert_not_called()


def test_apply_file_upload_failure(cli_runner, mock_cli_client, tmp_path):
    """Test that upload API errors are handled gracefully."""
    test_file = tmp_path / "document.pdf"
    test_file.write_bytes(b"PDF content")

    yaml_content = """provider: pragma
resource: file
name: upload-fail
config:
  content: "@./document.pdf"
  content_type: application/pdf
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    response = httpx.Response(500, json={"detail": "Storage service unavailable"})
    mock_cli_client.upload_file.side_effect = httpx.HTTPStatusError(
        "500 Internal Server Error", request=httpx.Request("POST", "http://test"), response=response
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Failed to upload file" in result.stdout
    mock_cli_client.apply_resource.assert_not_called()


def test_apply_file_content_removed_after_upload(cli_runner, mock_cli_client, tmp_path):
    """Test that content is removed from config after successful upload."""
    test_file = tmp_path / "image.png"
    test_file.write_bytes(b"\x89PNG\r\n\x1a\n binary image data")

    yaml_content = """provider: pragma
resource: file
name: my-image
config:
  content: "@./image.png"
  content_type: image/png
  description: "A test image"
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.upload_file.return_value = {
        "url": "gs://bucket/tenant/files/my-image",
        "size": 25,
        "content_type": "image/png",
    }
    mock_cli_client.apply_resource.return_value = mock_resource(
        "pragma", "file", "my-image", "draft", {"content_type": "image/png", "description": "A test image"}
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    config = call_kwargs["resource"]["config"]
    assert "content" not in config
    assert config["content_type"] == "image/png"
    assert config["description"] == "A test image"


# --- Tests for immutable field indicator in describe ---


def test_describe_shows_immutable_indicator(cli_runner, mock_cli_client):
    """Test describe shows [immutable] marker for immutable config fields."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "bucket",
        "name": "my-bucket",
        "lifecycle_state": "ready",
        "config": {"region": "europe-west4", "storage_class": "STANDARD"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="bucket",
            config_schema={
                "properties": {
                    "region": {"type": "string", "immutable": True},
                    "storage_class": {"type": "string"},
                },
            },
            description="GCP Cloud Storage bucket",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/bucket/my-bucket"])
    assert result.exit_code == 0
    assert "[immutable]" in result.stdout
    assert "region" in result.stdout
    assert "storage_class" in result.stdout


def test_describe_immutable_only_on_marked_fields(cli_runner, mock_cli_client):
    """Test that [immutable] only appears on fields marked immutable in schema."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "bucket",
        "name": "my-bucket",
        "lifecycle_state": "ready",
        "config": {"region": "europe-west4", "storage_class": "STANDARD"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="bucket",
            config_schema={
                "properties": {
                    "region": {"type": "string", "immutable": True},
                    "storage_class": {"type": "string"},
                },
            },
            description="GCP Cloud Storage bucket",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/bucket/my-bucket"])
    assert result.exit_code == 0

    for line in result.stdout.splitlines():
        if "storage_class" in line:
            assert "[immutable]" not in line
        if "region" in line and ":" in line:
            assert "[immutable]" in line


def test_describe_no_immutable_when_schema_unavailable(cli_runner, mock_cli_client):
    """Test describe works without [immutable] when resource type schema is unavailable."""
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

    response = httpx.Response(500, text="Internal Server Error")
    mock_cli_client.list_resource_schemas.side_effect = httpx.HTTPStatusError(
        "API unavailable", request=None, response=response
    )

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret"])
    assert result.exit_code == 0
    assert "project_id" in result.stdout
    assert "[immutable]" not in result.stdout


def test_describe_no_immutable_when_no_matching_type(cli_runner, mock_cli_client):
    """Test describe works without [immutable] when resource type not found in types list."""
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
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="bucket",
            config_schema={"properties": {"region": {"type": "string", "immutable": True}}},
            description="GCP bucket",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret"])
    assert result.exit_code == 0
    assert "project_id" in result.stdout
    assert "[immutable]" not in result.stdout


# --- Tests for --reveal flag ---


def test_describe_passes_reveal_true(cli_runner, mock_cli_client):
    """Test describe passes reveal=True to get_resource when --reveal is used."""
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
    mock_cli_client.list_resource_schemas.return_value = []
    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret", "--reveal"])
    assert result.exit_code == 0
    mock_cli_client.get_resource.assert_called_once_with(
        provider="pragmatiks/gcp", resource="secret", name="my-secret", reveal=True
    )


# --- Tests for [sensitive] indicator in describe ---


def test_describe_shows_sensitive_config_indicator(cli_runner, mock_cli_client):
    """Test describe shows [sensitive] marker for sensitive config fields."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "my-secret",
        "lifecycle_state": "ready",
        "config": {"project_id": "my-project", "credentials": "********"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="secret",
            config_schema={
                "properties": {
                    "project_id": {"type": "string"},
                    "credentials": {"type": "string", "sensitive": True},
                },
            },
            description="GCP Secret Manager secret",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret"])
    assert result.exit_code == 0
    assert "[sensitive]" in result.stdout

    for line in result.stdout.splitlines():
        if "project_id" in line:
            assert "[sensitive]" not in line
        if "credentials" in line and ":" in line:
            assert "[sensitive]" in line


def test_describe_shows_sensitive_output_indicator(cli_runner, mock_cli_client):
    """Test describe shows [sensitive] marker for sensitive output fields."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "secret",
        "name": "my-secret",
        "lifecycle_state": "ready",
        "config": {"project_id": "my-project"},
        "outputs": {"secret_name": "projects/my-project/secrets/test", "access_token": "********"},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="secret",
            config_schema={"properties": {"project_id": {"type": "string"}}},
            outputs_schema={
                "properties": {
                    "secret_name": {"type": "string"},
                    "access_token": {"type": "string", "sensitive": True},
                },
            },
            description="GCP Secret Manager secret",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/secret/my-secret"])
    assert result.exit_code == 0

    for line in result.stdout.splitlines():
        if "secret_name" in line:
            assert "[sensitive]" not in line
        if "access_token" in line and ":" in line:
            assert "[sensitive]" in line


def test_describe_shows_immutable_and_sensitive_labels(cli_runner, mock_cli_client):
    """Test describe shows both [immutable] and [sensitive] for fields with both markers."""
    mock_cli_client.get_resource.return_value = {
        "provider": "gcp",
        "resource": "bucket",
        "name": "my-bucket",
        "lifecycle_state": "ready",
        "config": {"api_key": "********", "region": "europe-west4"},
        "outputs": {},
        "dependencies": [],
        "tags": [],
        "created_at": "2026-01-16T10:00:00Z",
        "updated_at": "2026-01-16T10:30:00Z",
        "error": None,
    }
    mock_cli_client.list_resource_schemas.return_value = [
        ResourceSchema(
            provider="gcp",
            resource="bucket",
            config_schema={
                "properties": {
                    "api_key": {"type": "string", "immutable": True, "sensitive": True},
                    "region": {"type": "string"},
                },
            },
            description="GCP Cloud Storage bucket",
        ),
    ]

    result = cli_runner.invoke(app, ["resources", "describe", "pragmatiks/gcp/bucket/my-bucket"])
    assert result.exit_code == 0

    for line in result.stdout.splitlines():
        if "api_key" in line and ":" in line:
            assert "[immutable]" in line
            assert "[sensitive]" in line
        if "region" in line and ":" in line:
            assert "[immutable]" not in line
            assert "[sensitive]" not in line


def test_tags_add_sends_only_identity_and_tags(cli_runner, mock_cli_client):
    """Test tags add sends only identity fields and tags via PATCH semantics."""
    mock_cli_client.get_resource.return_value = mock_resource(
        "pragmatiks/postgres", "database", "test-db", lifecycle_state="ready", config={"name": "TEST_DB"}
    )
    mock_cli_client.get_resource.return_value["tags"] = ["existing-tag"]

    result = cli_runner.invoke(
        app, ["resources", "tags", "add", "pragmatiks/postgres/database/test-db", "--tag", "newtag"]
    )
    assert result.exit_code == 0

    mock_cli_client.apply_resource.assert_called_once_with(
        resource={
            "provider": "pragmatiks/postgres",
            "resource": "database",
            "name": "test-db",
            "tags": ["existing-tag", "newtag"],
        }
    )


def test_tags_remove_sends_only_identity_and_tags(cli_runner, mock_cli_client):
    """Test tags remove sends only identity fields and tags via PATCH semantics."""
    mock_cli_client.get_resource.return_value = mock_resource(
        "pragmatiks/postgres", "database", "test-db", lifecycle_state="ready", config={"name": "TEST_DB"}
    )
    mock_cli_client.get_resource.return_value["tags"] = ["tag1", "tag2"]

    result = cli_runner.invoke(
        app, ["resources", "tags", "remove", "pragmatiks/postgres/database/test-db", "--tag", "tag1"]
    )
    assert result.exit_code == 0

    mock_cli_client.apply_resource.assert_called_once_with(
        resource={
            "provider": "pragmatiks/postgres",
            "resource": "database",
            "name": "test-db",
            "tags": ["tag2"],
        }
    )


def test_tags_add_without_lifecycle_state(cli_runner, mock_cli_client):
    """Test tags add works when resource has no lifecycle_state."""
    resource_dict = mock_resource("pragmatiks/postgres", "database", "test-db", config={"name": "TEST_DB"})
    del resource_dict["lifecycle_state"]
    resource_dict["tags"] = []
    mock_cli_client.get_resource.return_value = resource_dict

    result = cli_runner.invoke(
        app, ["resources", "tags", "add", "pragmatiks/postgres/database/test-db", "--tag", "newtag"]
    )
    assert result.exit_code == 0

    mock_cli_client.apply_resource.assert_called_once_with(
        resource={
            "provider": "pragmatiks/postgres",
            "resource": "database",
            "name": "test-db",
            "tags": ["newtag"],
        }
    )


# --- Tests for recursive @ file resolution ---


def test_resolve_at_references_nested_dict(cli_runner, mock_cli_client, tmp_path):
    """Test that @ references in nested dicts are resolved."""
    (tmp_path / "cert.pem").write_text("-----BEGIN CERTIFICATE-----")

    yaml_content = """provider: gcp
resource: instance
name: my-vm
config:
  ssl:
    certificate: "@./cert.pem"
    enabled: true
"""
    yaml_file = tmp_path / "vm.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("gcp", "instance", "my-vm", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert call_kwargs["resource"]["config"]["ssl"]["certificate"] == "-----BEGIN CERTIFICATE-----"
    assert call_kwargs["resource"]["config"]["ssl"]["enabled"] is True


def test_resolve_at_references_in_list(cli_runner, mock_cli_client, tmp_path):
    """Test that @ references inside lists are resolved."""
    (tmp_path / "script1.sh").write_text("#!/bin/bash\necho hello")
    (tmp_path / "script2.sh").write_text("#!/bin/bash\necho world")

    yaml_content = """provider: gcp
resource: instance
name: my-vm
config:
  startup_scripts:
    - "@./script1.sh"
    - "@./script2.sh"
    - "inline command"
"""
    yaml_file = tmp_path / "vm.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.apply_resource.return_value = mock_resource("gcp", "instance", "my-vm", "draft")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    scripts = call_kwargs["resource"]["config"]["startup_scripts"]
    assert scripts[0] == "#!/bin/bash\necho hello"
    assert scripts[1] == "#!/bin/bash\necho world"
    assert scripts[2] == "inline command"


def test_resolve_at_references_file_not_found(cli_runner, mock_cli_client, tmp_path):
    """Test that missing @ file references produce a clear error."""
    yaml_content = """provider: gcp
resource: instance
name: my-vm
config:
  certificate: "@./missing.pem"
"""
    yaml_file = tmp_path / "vm.yaml"
    yaml_file.write_text(yaml_content)

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "File not found" in result.stdout
    mock_cli_client.apply_resource.assert_not_called()


def test_resolve_at_references_pragma_file_unchanged(cli_runner, mock_cli_client, tmp_path):
    """Test that pragma/file resources still use the binary upload path."""
    test_file = tmp_path / "document.pdf"
    test_file.write_bytes(b"%PDF-1.4 binary content")

    yaml_content = """provider: pragma
resource: file
name: my-doc
config:
  content: "@./document.pdf"
  content_type: application/pdf
"""
    yaml_file = tmp_path / "file.yaml"
    yaml_file.write_text(yaml_content)

    mock_cli_client.upload_file.return_value = {"url": "gs://bucket/file"}
    mock_cli_client.apply_resource.return_value = mock_resource(
        "pragma", "file", "my-doc", "draft", {"content_type": "application/pdf"}
    )

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 0

    mock_cli_client.upload_file.assert_called_once_with("my-doc", b"%PDF-1.4 binary content", "application/pdf")

    call_kwargs = mock_cli_client.apply_resource.call_args[1]
    assert "content" not in call_kwargs["resource"]["config"]


# --- Tests for invalid YAML error handling ---


def test_apply_invalid_yaml_shows_clean_error(cli_runner, mock_cli_client, tmp_path):
    """Test that invalid YAML produces a clean error message on apply."""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("invalid: yaml: content: [")

    result = cli_runner.invoke(app, ["resources", "apply", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Invalid YAML" in result.stdout
    mock_cli_client.apply_resource.assert_not_called()


def test_delete_invalid_yaml_shows_clean_error(cli_runner, mock_cli_client, tmp_path):
    """Test that invalid YAML produces a clean error message on delete."""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("invalid: yaml: content: [")

    result = cli_runner.invoke(app, ["resources", "delete", "-f", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Invalid YAML" in result.stdout
    mock_cli_client.delete_resource.assert_not_called()


def test_deactivate_invalid_yaml_shows_clean_error(cli_runner, mock_cli_client, tmp_path):
    """Test that invalid YAML produces a clean error message on deactivate."""
    yaml_file = tmp_path / "bad.yaml"
    yaml_file.write_text("invalid: yaml: content: [")

    result = cli_runner.invoke(app, ["resources", "deactivate", "-f", str(yaml_file)])
    assert result.exit_code == 1
    assert "Error" in result.stdout
    assert "Invalid YAML" in result.stdout
    mock_cli_client.deactivate_resource.assert_not_called()
