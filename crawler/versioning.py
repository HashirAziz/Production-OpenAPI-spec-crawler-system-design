"""
versioning.py — Spec change detection and history entry generation.

Design decisions:
- "Updated" is detected on two independent signals (OR logic):
    1. info.version changed — catches explicit version bumps.
    2. SHA-256 content hash changed — catches edits without version bumps,
       which is common in practice (docs fixes, schema tweaks, etc.).
  Using both signals makes the detector robust without being noisy.
- Path diffs (added / removed endpoints) are generated via set arithmetic,
  which is O(n) and needs no external library.
- HistoryEntry is only written on NEW or UPDATED status so history files
  don't accumulate identical duplicate records on each crawl.
- The module is stateless — all context (previous hash, version, paths) is
  passed in explicitly.  This makes it trivially unit-testable.
"""

from __future__ import annotations

from typing import Optional

from crawler.models import (
    CatalogEntry,
    HistoryEntry,
    ParsedSpec,
    PathDiff,
    SpecStatus,
)
from crawler.utils import make_spec_id, make_human_id, utc_now_iso


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def determine_status(
    parsed: ParsedSpec,
    existing: Optional[CatalogEntry],
    new_hash: str,
) -> SpecStatus:
    """
    Classify a freshly fetched spec relative to what we already know.

    Args:
        parsed:   The newly parsed spec.
        existing: The current catalog entry, or None if spec is brand new.
        new_hash: SHA-256 of the raw bytes just fetched.

    Returns:
        SpecStatus.NEW       — first time we've seen this spec.
        SpecStatus.UPDATED   — hash or info.version has changed.
        SpecStatus.UNCHANGED — no detectable change.
        SpecStatus.ERROR     — parse failed (handled upstream).
    """
    if not parsed.is_valid:
        return SpecStatus.ERROR

    if existing is None:
        return SpecStatus.NEW

    hash_changed = existing.latest_hash != new_hash
    version_changed = (
        existing.latest_info_version != parsed.version
        and parsed.version is not None
    )

    if hash_changed or version_changed:
        return SpecStatus.UPDATED

    return SpecStatus.UNCHANGED


def compute_path_diff(
    old_paths: list[str],
    new_paths: list[str],
) -> PathDiff:
    """
    Compute which API paths were added or removed between two spec versions.

    Args:
        old_paths: Paths from the previously stored spec.
        new_paths: Paths from the newly fetched spec.

    Returns:
        PathDiff with added and removed lists (sorted for determinism).
    """
    old_set = set(old_paths)
    new_set = set(new_paths)
    return PathDiff(
        added=sorted(new_set - old_set),
        removed=sorted(old_set - new_set),
    )


def build_history_entry(
    source_url: str,
    parsed: ParsedSpec,
    content_hash: str,
    status: SpecStatus,
    old_paths: Optional[list[str]] = None,
) -> HistoryEntry:
    """
    Construct an immutable history snapshot for the current crawl.

    Args:
        source_url:   Canonical raw URL of the spec.
        parsed:       Parsed spec data.
        content_hash: SHA-256 of raw bytes.
        status:       Determined lifecycle status.
        old_paths:    Paths from the previous catalog entry (for diff).
                      Pass None for new specs.

    Returns:
        A frozen HistoryEntry ready to be written to a sidecar file.
    """
    path_diff: Optional[PathDiff] = None
    if status == SpecStatus.UPDATED and old_paths is not None:
        path_diff = compute_path_diff(old_paths, parsed.raw_paths)

    return HistoryEntry(
        spec_id=make_spec_id(source_url),
        source_url=source_url,
        fetched_at=utc_now_iso(),
        content_hash=content_hash,
        info_version=parsed.version,
        status=status,
        title=parsed.title,
        openapi_version=parsed.openapi_version,
        paths_count=parsed.paths_count,
        path_diff=path_diff,
    )


def update_catalog_entry(
    entry: CatalogEntry,
    parsed: ParsedSpec,
    content_hash: str,
    status: SpecStatus,
    fetched_at: str,
) -> CatalogEntry:
    """
    Return an updated CatalogEntry by merging freshly parsed data.

    We return a new model instance rather than mutating in place.
    Pydantic v2 supports model_copy(update=...) for this pattern.

    Args:
        entry:        The existing catalog entry.
        parsed:       Freshly parsed spec data.
        content_hash: SHA-256 of the fetched bytes.
        status:       Determined lifecycle status.
        fetched_at:   ISO-8601 timestamp of this fetch.

    Returns:
        Updated CatalogEntry (new object, entry is not mutated).
    """
    updates: dict = {
        "title": parsed.title or entry.title,
        "description": parsed.description or entry.description,
        "openapi_version": parsed.openapi_version,
        "servers": parsed.servers,
        "tags": parsed.tags,
        "paths_count": parsed.paths_count,
        "latest_info_version": parsed.version or entry.latest_info_version,
        "latest_hash": content_hash,
        "status": status,
        "fetched_at": fetched_at,
    }

    if status in (SpecStatus.NEW, SpecStatus.UPDATED):
        updates["last_updated_at"] = fetched_at

    return entry.model_copy(update=updates)


def make_new_catalog_entry(
    source_url: str,
    parsed: ParsedSpec,
    content_hash: str,
    fetched_at: str,
) -> CatalogEntry:
    """
    Create a brand-new CatalogEntry for a previously unseen spec.

    Args:
        source_url:   Canonical raw URL.
        parsed:       Parsed spec data.
        content_hash: SHA-256 of the fetched bytes.
        fetched_at:   ISO-8601 timestamp.

    Returns:
        A fresh CatalogEntry with status=NEW and APIMatic-compatible id.
    """
    spec_id = make_spec_id(source_url)
    return CatalogEntry(
        spec_id=spec_id,
        id=make_human_id(source_url),       # APIMatic: "github:owner/repo/filename"
        source_url=source_url,
        title=parsed.title,
        description=parsed.description,
        openapi_version=parsed.openapi_version,
        servers=parsed.servers,
        tags=parsed.tags,
        paths_count=parsed.paths_count,
        latest_info_version=parsed.version,
        latest_hash=content_hash,
        status=SpecStatus.NEW,
        first_seen_at=fetched_at,
        last_updated_at=fetched_at,
        fetched_at=fetched_at,
        history_file=f"history/{spec_id}.json",
    )