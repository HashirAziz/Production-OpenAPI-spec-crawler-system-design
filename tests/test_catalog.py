"""
test_catalog.py — Unit tests for crawler.catalog.CatalogManager.

Tests cover:
- Empty catalog initialisation
- Upsert and retrieval
- Atomic persistence (save + reload round-trip)
- History sidecar append and retrieval
- Graceful handling of a corrupt catalog file
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crawler.catalog import CatalogManager
from crawler.models import (
    CatalogEntry,
    HistoryEntry,
    OpenAPIVersion,
    PathDiff,
    SpecStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sample_entry(spec_id: str = "abc123", url: str = "https://raw.githubusercontent.com/o/r/main/openapi.yaml") -> CatalogEntry:
    return CatalogEntry(
        spec_id=spec_id,
        source_url=url,
        title="Test API",
        openapi_version=OpenAPIVersion.OPENAPI_3,
        latest_info_version="1.0.0",
        latest_hash="deadbeef" * 8,
        status=SpecStatus.NEW,
        first_seen_at="2025-01-01T00:00:00+00:00",
        last_updated_at="2025-01-01T00:00:00+00:00",
        fetched_at="2025-01-01T00:00:00+00:00",
        history_file=f"history/{spec_id}.json",
    )


def sample_history(spec_id: str = "abc123") -> HistoryEntry:
    return HistoryEntry(
        spec_id=spec_id,
        source_url="https://raw.githubusercontent.com/o/r/main/openapi.yaml",
        fetched_at="2025-01-01T00:00:00+00:00",
        content_hash="cafebabe" * 8,
        info_version="1.0.0",
        status=SpecStatus.NEW,
        title="Test API",
        openapi_version=OpenAPIVersion.OPENAPI_3,
        paths_count=3,
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

class TestCatalogInit:
    def test_loads_empty_when_no_file(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        assert cm.count() == 0

    def test_creates_history_dir(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        assert (tmp_data_dir / "history").exists()


# ---------------------------------------------------------------------------
# Upsert & retrieval
# ---------------------------------------------------------------------------

class TestUpsertAndGet:
    def test_get_returns_none_for_unknown(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        assert cm.get("nonexistent") is None

    def test_upsert_and_get(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        entry = sample_entry()
        cm.upsert(entry)
        retrieved = cm.get("abc123")
        assert retrieved is not None
        assert retrieved.title == "Test API"

    def test_upsert_overwrites_existing(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        entry = sample_entry()
        cm.upsert(entry)
        updated = entry.model_copy(update={"title": "Updated Title"})
        cm.upsert(updated)
        assert cm.get("abc123").title == "Updated Title"
        assert cm.count() == 1   # no duplicate

    def test_count_increments(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.upsert(sample_entry("id1", "https://a.com"))
        cm.upsert(sample_entry("id2", "https://b.com"))
        assert cm.count() == 2

    def test_all_entries(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.upsert(sample_entry("id1", "https://a.com"))
        cm.upsert(sample_entry("id2", "https://b.com"))
        entries = cm.all_entries()
        assert len(entries) == 2


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_creates_catalog_file(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.upsert(sample_entry())
        cm.save()
        assert (tmp_data_dir / "catalog.json").exists()

    def test_save_and_reload(self, tmp_data_dir: Path):
        cm1 = CatalogManager(tmp_data_dir)
        cm1.load()
        cm1.upsert(sample_entry())
        cm1.save()

        cm2 = CatalogManager(tmp_data_dir)
        cm2.load()
        assert cm2.count() == 1
        assert cm2.get("abc123").title == "Test API"

    def test_catalog_is_valid_json(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.upsert(sample_entry())
        cm.save()
        raw = (tmp_data_dir / "catalog.json").read_text()
        parsed = json.loads(raw)
        assert "abc123" in parsed

    def test_corrupt_catalog_starts_fresh(self, tmp_data_dir: Path):
        (tmp_data_dir / "catalog.json").write_text("not valid json {{{")
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        assert cm.count() == 0   # corrupt file → empty catalog, no crash


# ---------------------------------------------------------------------------
# History sidecar
# ---------------------------------------------------------------------------

class TestHistorySidecar:
    def test_append_creates_history_file(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        entry = sample_history()
        cm.append_history(entry)
        hist_file = tmp_data_dir / "history" / "abc123.json"
        assert hist_file.exists()

    def test_history_is_valid_json_array(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.append_history(sample_history())
        hist_file = tmp_data_dir / "history" / "abc123.json"
        data = json.loads(hist_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 1

    def test_history_appends_not_overwrites(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        cm.append_history(sample_history())
        cm.append_history(sample_history())   # second append
        entries = cm.load_history("abc123")
        assert len(entries) == 2

    def test_load_history_returns_empty_for_unknown(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        assert cm.load_history("no_such_spec") == []

    def test_history_entry_fields_preserved(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        original = sample_history()
        cm.append_history(original)
        loaded = cm.load_history("abc123")
        assert loaded[0].spec_id == "abc123"
        assert loaded[0].status == SpecStatus.NEW
        assert loaded[0].info_version == "1.0.0"

    def test_history_with_path_diff(self, tmp_data_dir: Path):
        cm = CatalogManager(tmp_data_dir)
        cm.load()
        entry = HistoryEntry(
            spec_id="abc123",
            source_url="https://raw.githubusercontent.com/o/r/main/openapi.yaml",
            fetched_at="2025-06-01T00:00:00+00:00",
            content_hash="new" * 10 + "xxxx",
            info_version="2.0.0",
            status=SpecStatus.UPDATED,
            title="Test API",
            openapi_version=OpenAPIVersion.OPENAPI_3,
            paths_count=2,
            path_diff=PathDiff(added=["/new_endpoint"], removed=["/old_endpoint"]),
        )
        cm.append_history(entry)
        loaded = cm.load_history("abc123")
        diff = loaded[0].path_diff
        assert diff is not None
        assert "/new_endpoint" in diff.added
        assert "/old_endpoint" in diff.removed