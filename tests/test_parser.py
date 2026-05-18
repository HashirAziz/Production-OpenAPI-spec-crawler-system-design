"""
test_parser.py — Unit tests for crawler.parser.

These tests verify:
- YAML and JSON parsing
- OpenAPI 2.x and 3.x version detection
- Server URL extraction for both formats
- Tag extraction
- Path counting
- Graceful handling of malformed/empty/non-spec input
- Numeric version coercion
"""

from __future__ import annotations

import pytest

from crawler.models import OpenAPIVersion
from crawler.parser import parse_spec
from tests.conftest import (
    EMPTY_BYTES,
    MALFORMED_YAML,
    NOT_A_SPEC,
    NUMERIC_VERSION_YAML,
    OPENAPI_3_JSON,
    OPENAPI_3_YAML,
    SWAGGER_2_YAML,
)


# ---------------------------------------------------------------------------
# OpenAPI 3.x YAML
# ---------------------------------------------------------------------------

class TestOpenAPI3Yaml:
    def test_is_valid(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.is_valid is True
        assert result.parse_error is None

    def test_title_extracted(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.title == "Pet Store API"

    def test_version_extracted(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.version == "1.2.0"

    def test_description_extracted(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.description == "A sample API for testing."

    def test_version_family(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.openapi_version == OpenAPIVersion.OPENAPI_3

    def test_servers_extracted(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.servers == ["https://api.petstore.example.com/v1"]

    def test_tags_extracted(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert "pets" in result.tags
        assert "store" in result.tags
        assert len(result.tags) == 2

    def test_paths_count(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert result.paths_count == 3

    def test_raw_paths(self):
        result = parse_spec(OPENAPI_3_YAML)
        assert "/pets" in result.raw_paths
        assert "/pets/{petId}" in result.raw_paths
        assert "/store/orders" in result.raw_paths


# ---------------------------------------------------------------------------
# Swagger 2.x YAML
# ---------------------------------------------------------------------------

class TestSwagger2Yaml:
    def test_is_valid(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert result.is_valid is True

    def test_version_family(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert result.openapi_version == OpenAPIVersion.SWAGGER_2

    def test_title(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert result.title == "Legacy API"

    def test_version(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert result.version == "2.3.1"

    def test_servers_reconstructed_from_swagger2(self):
        result = parse_spec(SWAGGER_2_YAML)
        # Should reconstruct servers from host + basePath + schemes
        assert any("api.legacy.example.com" in s for s in result.servers)
        assert any(s.startswith("https://") for s in result.servers)
        assert any(s.startswith("http://") for s in result.servers)

    def test_paths_count(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert result.paths_count == 2

    def test_tags(self):
        result = parse_spec(SWAGGER_2_YAML)
        assert "users" in result.tags


# ---------------------------------------------------------------------------
# JSON input
# ---------------------------------------------------------------------------

class TestOpenAPI3Json:
    def test_json_parses_successfully(self):
        result = parse_spec(OPENAPI_3_JSON)
        assert result.is_valid is True

    def test_json_title(self):
        result = parse_spec(OPENAPI_3_JSON)
        assert result.title == "JSON API"

    def test_json_paths_count(self):
        result = parse_spec(OPENAPI_3_JSON)
        assert result.paths_count == 2

    def test_json_no_servers(self):
        result = parse_spec(OPENAPI_3_JSON)
        assert result.servers == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_malformed_yaml_returns_invalid(self):
        result = parse_spec(MALFORMED_YAML)
        assert result.is_valid is False
        assert result.parse_error is not None

    def test_empty_bytes_returns_invalid(self):
        result = parse_spec(EMPTY_BYTES)
        assert result.is_valid is False

    def test_missing_info_block_still_parses(self):
        """Specs without info block should parse with None fields, not crash."""
        result = parse_spec(NOT_A_SPEC)
        assert result.is_valid is True
        assert result.title is None
        assert result.version is None

    def test_numeric_version_coerced_to_string(self):
        """info.version: 2 (integer) should become "2", not crash."""
        result = parse_spec(NUMERIC_VERSION_YAML)
        assert result.is_valid is True
        assert result.version == "2"

    def test_unknown_version_family_for_no_key(self):
        """A doc without openapi or swagger key gets UNKNOWN version family."""
        raw = b"info:\n  title: Mystery\npaths: {}"
        result = parse_spec(raw)
        assert result.openapi_version == OpenAPIVersion.UNKNOWN

    def test_never_raises(self):
        """parse_spec should never raise regardless of input."""
        evil_inputs = [
            b"\x00\x01\x02\xff",          # binary garbage
            b"null",                        # valid YAML but not a dict
            b"[]",                          # YAML list
            b"true",                        # YAML scalar
        ]
        for inp in evil_inputs:
            result = parse_spec(inp)
            assert result is not None        # always returns something
            # If invalid, is_valid is False; it should NOT raise