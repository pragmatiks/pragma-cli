"""Tests for CLI helper functions."""

import pytest

from pragma_cli.helpers import parse_resource_id


def test_parse_valid_resource_id():
    provider, resource = parse_resource_id("postgres/database")
    assert provider == "postgres"
    assert resource == "database"


def test_parse_resource_id_with_hyphen():
    provider, resource = parse_resource_id("my-provider/my-resource")
    assert provider == "my-provider"
    assert resource == "my-resource"


def test_parse_resource_id_with_underscore():
    provider, resource = parse_resource_id("my_provider/my_resource")
    assert provider == "my_provider"
    assert resource == "my_resource"


def test_parse_resource_id_single_char():
    provider, resource = parse_resource_id("a/b")
    assert provider == "a"
    assert resource == "b"


def test_parse_resource_id_invalid_format_no_slash():
    with pytest.raises(ValueError, match="Invalid resource ID format"):
        parse_resource_id("postgres-database")


def test_parse_resource_id_empty_string():
    with pytest.raises(ValueError, match="Invalid resource ID format"):
        parse_resource_id("")


def test_parse_resource_id_only_slash():
    provider, resource = parse_resource_id("/")
    assert provider == ""
    assert resource == ""


def test_parse_resource_id_multiple_slashes():
    provider, resource = parse_resource_id("provider/resource/extra")
    assert provider == "provider"
    assert resource == "resource/extra"
