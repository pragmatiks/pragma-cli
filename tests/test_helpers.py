"""Tests for CLI helper functions."""

import pytest

from pragma_cli.helpers import parse_resource_id


def test_parse_resource_id_org_provider():
    provider, resource, name = parse_resource_id("pragmatiks/pragma/secret/test")
    assert provider == "pragmatiks/pragma"
    assert resource == "secret"
    assert name == "test"


def test_parse_resource_id_org_provider_with_complex_names():
    provider, resource, name = parse_resource_id("pragmatiks/gcp/cloudsql_instance/prod-db")
    assert provider == "pragmatiks/gcp"
    assert resource == "cloudsql_instance"
    assert name == "prod-db"


def test_parse_resource_id_org_provider_with_hyphens():
    provider, resource, name = parse_resource_id("my-org/my-provider/my-resource/my-name")
    assert provider == "my-org/my-provider"
    assert resource == "my-resource"
    assert name == "my-name"


def test_parse_resource_id_org_provider_with_underscores():
    provider, resource, name = parse_resource_id("my_org/my_provider/my_resource/my_name")
    assert provider == "my_org/my_provider"
    assert resource == "my_resource"
    assert name == "my_name"


def test_parse_resource_id_single_char_segments():
    provider, resource, name = parse_resource_id("a/b/c/d")
    assert provider == "a/b"
    assert resource == "c"
    assert name == "d"


def test_parse_resource_id_three_segments_raises():
    with pytest.raises(ValueError, match="Expected 'org/provider/resource/name'"):
        parse_resource_id("postgres/database/my-db")


def test_parse_resource_id_five_segments_raises():
    with pytest.raises(ValueError, match="Expected 'org/provider/resource/name'"):
        parse_resource_id("a/b/c/d/e")


def test_parse_resource_id_invalid_format_no_slash():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("postgres-database")


def test_parse_resource_id_empty_string():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("")


def test_parse_resource_id_only_two_segments():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("provider/resource")


def test_parse_resource_id_only_slash():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("/")
