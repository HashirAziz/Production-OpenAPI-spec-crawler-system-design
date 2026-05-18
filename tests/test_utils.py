"""
test_utils.py — Unit tests for crawler.utils.
"""

from __future__ import annotations

from crawler.utils import (
    make_spec_id,
    sha256_of_bytes,
    sha256_of_str,
    to_raw_github_url,
    truncate,
    is_yaml_url,
    is_json_url,
)


class TestSha256:
    def test_sha256_of_bytes_length(self):
        assert len(sha256_of_bytes(b"hello")) == 64

    def test_sha256_deterministic(self):
        assert sha256_of_bytes(b"data") == sha256_of_bytes(b"data")

    def test_sha256_of_str_matches_encoded(self):
        assert sha256_of_str("hello") == sha256_of_bytes("hello".encode("utf-8"))


class TestMakeSpecId:
    def test_stable_across_calls(self):
        url = "https://raw.githubusercontent.com/x/y/main/openapi.yaml"
        assert make_spec_id(url) == make_spec_id(url)

    def test_different_urls_different_ids(self):
        assert make_spec_id("https://a.com") != make_spec_id("https://b.com")

    def test_length_is_12(self):
        assert len(make_spec_id("https://example.com/openapi.yaml")) == 12


class TestToRawGithubUrl:
    def test_converts_blob_url(self):
        html = "https://github.com/owner/repo/blob/main/openapi.yaml"
        expected = "https://raw.githubusercontent.com/owner/repo/main/openapi.yaml"
        assert to_raw_github_url(html) == expected

    def test_nested_path(self):
        html = "https://github.com/owner/repo/blob/main/api/v2/openapi.yaml"
        raw = to_raw_github_url(html)
        assert "raw.githubusercontent.com" in raw
        assert "api/v2/openapi.yaml" in raw

    def test_non_github_url_returned_unchanged(self):
        url = "https://example.com/api/openapi.yaml"
        assert to_raw_github_url(url) == url


class TestUrlTypeDetection:
    def test_yaml_extension(self):
        assert is_yaml_url("https://example.com/api/openapi.yaml") is True
        assert is_yaml_url("https://example.com/api/swagger.yml") is True

    def test_json_extension(self):
        assert is_json_url("https://example.com/api/openapi.json") is True

    def test_yaml_not_json(self):
        assert is_json_url("https://example.com/openapi.yaml") is False

    def test_json_not_yaml(self):
        assert is_yaml_url("https://example.com/openapi.json") is False


class TestTruncate:
    def test_short_string_unchanged(self):
        assert truncate("hello", max_len=10) == "hello"

    def test_long_string_truncated(self):
        result = truncate("a" * 200, max_len=10)
        assert len(result) == 10
        assert result.endswith("…")

    def test_exactly_max_len(self):
        s = "a" * 10
        assert truncate(s, max_len=10) == s