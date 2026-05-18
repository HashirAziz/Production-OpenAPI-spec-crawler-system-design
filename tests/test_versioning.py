"""
test_versioning.py — Unit tests for crawler.versioning.

Tests cover:
- NEW detection (no existing entry)
- UPDATED detection via hash change
- UPDATED detection via version string change
- UNCHANGED detection
- Path diff computation (added / removed)
- History entry construction
- Catalog entry update logic
"""

from __future__ import annotations

import pytest

from crawler.models import (
    CatalogEntry,
    OpenAPIVersion,
    ParsedSpec,
    SpecStatus,
)
from crawler.versioning import (
    build_history_entry,
    compute_path_diff,
    determine_status,
    make_new_catalog_entry,
    update_catalog_entry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_parsed(**kwargs) -> ParsedSpec:
    defaults = dict(
        title="Test API",
        version="1.0.0",
        description="A test spec.",
        openapi_version=OpenAPIVersion.OPENAPI_3,
        servers=["https://api.example.com"],
        tags=["pets"],
        paths_count=3,
        raw_paths=["/a", "/b", "/c"],
        is_valid=True,
    )
    defaults.update(kwargs)
    return ParsedSpec(**defaults)


def make_catalog_entry(**kwargs) -> CatalogEntry:
    defaults = dict(
        spec_id="abc123",
        source_url="https://raw.githubusercontent.com/owner/repo/main/openapi.yaml",
        title="Test API",
        latest_info_version="1.0.0",
        latest_hash="aabbccdd" * 8,
        status=SpecStatus.UNCHANGED,
        first_seen_at="2024-01-01T00:00:00+00:00",
        last_updated_at="2024-01-01T00:00:00+00:00",
        fetched_at="2024-01-01T00:00:00+00:00",
    )
    defaults.update(kwargs)
    return CatalogEntry(**defaults)


# ---------------------------------------------------------------------------
# determine_status
# ---------------------------------------------------------------------------

class TestDetermineStatus:
    def test_new_when_no_existing(self):
        parsed = make_parsed()
        status = determine_status(parsed, existing=None, new_hash="newhash")
        assert status == SpecStatus.NEW

    def test_updated_when_hash_changes(self):
        existing = make_catalog_entry(latest_hash="oldhash")
        parsed = make_parsed()
        status = determine_status(parsed, existing=existing, new_hash="newhash")
        assert status == SpecStatus.UPDATED

    def test_updated_when_version_changes(self):
        existing = make_catalog_entry(latest_info_version="1.0.0", latest_hash="samehash")
        parsed = make_parsed(version="2.0.0")
        status = determine_status(parsed, existing=existing, new_hash="samehash")
        assert status == SpecStatus.UPDATED

    def test_unchanged_when_hash_and_version_same(self):
        the_hash = "a" * 64
        existing = make_catalog_entry(latest_hash=the_hash, latest_info_version="1.0.0")
        parsed = make_parsed(version="1.0.0")
        status = determine_status(parsed, existing=existing, new_hash=the_hash)
        assert status == SpecStatus.UNCHANGED

    def test_error_when_parse_invalid(self):
        parsed = make_parsed(is_valid=False)
        status = determine_status(parsed, existing=None, new_hash="hash")
        assert status == SpecStatus.ERROR

    def test_unchanged_when_only_version_is_none(self):
        """If spec has no version field, version change shouldn't trigger UPDATED."""
        the_hash = "b" * 64
        existing = make_catalog_entry(latest_hash=the_hash, latest_info_version="1.0.0")
        parsed = make_parsed(version=None)
        status = determine_status(parsed, existing=existing, new_hash=the_hash)
        assert status == SpecStatus.UNCHANGED


# ---------------------------------------------------------------------------
# compute_path_diff
# ---------------------------------------------------------------------------

class TestComputePathDiff:
    def test_added_paths(self):
        diff = compute_path_diff(old_paths=["/a", "/b"], new_paths=["/a", "/b", "/c"])
        assert diff.added == ["/c"]
        assert diff.removed == []

    def test_removed_paths(self):
        diff = compute_path_diff(old_paths=["/a", "/b", "/c"], new_paths=["/a"])
        assert diff.added == []
        assert diff.removed == ["/b", "/c"]

    def test_added_and_removed(self):
        diff = compute_path_diff(old_paths=["/a", "/b"], new_paths=["/b", "/c"])
        assert diff.added == ["/c"]
        assert diff.removed == ["/a"]

    def test_no_change(self):
        diff = compute_path_diff(old_paths=["/a", "/b"], new_paths=["/a", "/b"])
        assert diff.added == []
        assert diff.removed == []
        assert diff.has_changes is False

    def test_both_empty(self):
        diff = compute_path_diff(old_paths=[], new_paths=[])
        assert not diff.has_changes

    def test_results_are_sorted(self):
        diff = compute_path_diff(old_paths=["/z"], new_paths=["/a", "/b", "/c"])
        assert diff.added == ["/a", "/b", "/c"]   # alphabetical
        assert diff.removed == ["/z"]


# ---------------------------------------------------------------------------
# build_history_entry
# ---------------------------------------------------------------------------

class TestBuildHistoryEntry:
    def test_new_entry_has_no_diff(self):
        parsed = make_parsed()
        entry = build_history_entry(
            source_url="https://raw.githubusercontent.com/x/y/main/openapi.yaml",
            parsed=parsed,
            content_hash="abc",
            status=SpecStatus.NEW,
            old_paths=None,
        )
        assert entry.path_diff is None
        assert entry.status == SpecStatus.NEW

    def test_updated_entry_includes_diff(self):
        parsed = make_parsed(raw_paths=["/a", "/b", "/new"])
        entry = build_history_entry(
            source_url="https://raw.githubusercontent.com/x/y/main/openapi.yaml",
            parsed=parsed,
            content_hash="newhash",
            status=SpecStatus.UPDATED,
            old_paths=["/a", "/b", "/old"],
        )
        assert entry.path_diff is not None
        assert "/new" in entry.path_diff.added
        assert "/old" in entry.path_diff.removed

    def test_entry_is_frozen(self):
        parsed = make_parsed()
        entry = build_history_entry(
            source_url="https://raw.githubusercontent.com/x/y/main/openapi.yaml",
            parsed=parsed,
            content_hash="abc",
            status=SpecStatus.NEW,
        )
        with pytest.raises(Exception):  # ValidationError or TypeError
            entry.content_hash = "mutated"  # type: ignore[misc]

    def test_spec_id_is_deterministic(self):
        url = "https://raw.githubusercontent.com/x/y/main/openapi.yaml"
        parsed = make_parsed()
        e1 = build_history_entry(url, parsed, "h1", SpecStatus.NEW)
        e2 = build_history_entry(url, parsed, "h2", SpecStatus.UPDATED)
        assert e1.spec_id == e2.spec_id   # same URL → same ID


# ---------------------------------------------------------------------------
# make_new_catalog_entry
# ---------------------------------------------------------------------------

class TestMakeNewCatalogEntry:
    def test_creates_entry_with_new_status(self):
        parsed = make_parsed()
        entry = make_new_catalog_entry(
            source_url="https://raw.githubusercontent.com/x/y/main/openapi.yaml",
            parsed=parsed,
            content_hash="cafebabe",
            fetched_at="2025-01-01T00:00:00+00:00",
        )
        assert entry.status == SpecStatus.NEW
        assert entry.title == "Test API"
        assert entry.latest_hash == "cafebabe"
        assert entry.history_file.startswith("history/")

    def test_spec_id_is_stable(self):
        url = "https://raw.githubusercontent.com/x/y/main/openapi.yaml"
        parsed = make_parsed()
        e1 = make_new_catalog_entry(url, parsed, "h1", "2025-01-01T00:00:00+00:00")
        e2 = make_new_catalog_entry(url, parsed, "h1", "2025-01-01T00:00:00+00:00")
        assert e1.spec_id == e2.spec_id


# ---------------------------------------------------------------------------
# update_catalog_entry
# ---------------------------------------------------------------------------

class TestUpdateCatalogEntry:
    def test_hash_is_updated(self):
        existing = make_catalog_entry()
        parsed = make_parsed()
        updated = update_catalog_entry(existing, parsed, "newerfasthash", SpecStatus.UPDATED, "2025-06-01T00:00:00+00:00")
        assert updated.latest_hash == "newerfasthash"

    def test_status_is_updated(self):
        existing = make_catalog_entry(status=SpecStatus.UNCHANGED)
        parsed = make_parsed()
        updated = update_catalog_entry(existing, parsed, "h", SpecStatus.UPDATED, "2025-06-01T00:00:00+00:00")
        assert updated.status == SpecStatus.UPDATED

    def test_original_not_mutated(self):
        existing = make_catalog_entry(status=SpecStatus.UNCHANGED)
        parsed = make_parsed()
        _ = update_catalog_entry(existing, parsed, "h", SpecStatus.UPDATED, "2025-06-01T00:00:00+00:00")
        assert existing.status == SpecStatus.UNCHANGED  # original untouched