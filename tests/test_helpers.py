"""Tests for CLI helper functions."""

import pytest

from pragma_cli.helpers import parse_resource_id


def test_parse_valid_resource_id():
    provider, resource, name = parse_resource_id("postgres/database/my-db")
    assert provider == "postgres"
    assert resource == "database"
    assert name == "my-db"


def test_parse_resource_id_with_hyphen():
    provider, resource, name = parse_resource_id("my-provider/my-resource/my-name")
    assert provider == "my-provider"
    assert resource == "my-resource"
    assert name == "my-name"


def test_parse_resource_id_with_underscore():
    provider, resource, name = parse_resource_id("my_provider/my_resource/my_name")
    assert provider == "my_provider"
    assert resource == "my_resource"
    assert name == "my_name"


def test_parse_resource_id_single_char():
    provider, resource, name = parse_resource_id("a/b/c")
    assert provider == "a"
    assert resource == "b"
    assert name == "c"


def test_parse_resource_id_invalid_format_no_slash():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("postgres-database")


def test_parse_resource_id_empty_string():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("")


def test_parse_resource_id_only_two_segments():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("provider/resource")


def test_parse_resource_id_four_segments():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("provider/resource/name/extra")


def test_parse_resource_id_only_slash():
    with pytest.raises(ValueError, match="Invalid resource ID"):
        parse_resource_id("/")
