"""
updater.py — Per-spec update pipeline orchestrator.

Design decisions:
- The Updater is a thin coordinator: it delegates to fetcher, parser,
  versioning, and catalog modules without containing logic of its own.
  This keeps each module independently testable and the orchestration
  readable at a glance.
- History entries are only written for NEW and UPDATED specs to avoid
  filling history files with redundant UNCHANGED records.
- On UNCHANGED specs we still update the fetched_at timestamp in the
  catalog so the "last seen" field reflects the most recent crawl.
- Errors at the fetch or parse stage produce an ERROR status entry in
  the catalog rather than silently skipping — this makes failures visible
  in the catalog for debugging.
- The method signature accepts both the raw URL and the CatalogEntry
  (or None) so the updater can be called from any orchestration layer
  without tight coupling to the search client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from crawler.catalog import CatalogManager
from crawler.fetcher import SpecFetcher
from crawler.models import CatalogEntry, CrawlStats, SpecStatus
from crawler.parser import parse_spec
from crawler.versioning import (
    build_history_entry,
    determine_status,
    make_new_catalog_entry,
    update_catalog_entry,
)
from crawler.utils import make_spec_id, sha256_of_bytes, utc_now_iso
from crawler.logger import get_logger, log_event

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class SpecUpdater:
    """
    Orchestrates the fetch → parse → diff → catalog pipeline for one spec.

    Args:
        fetcher: Configured SpecFetcher instance (shared across specs).
        catalog: CatalogManager instance (shared across specs).
    """

    def __init__(self, fetcher: SpecFetcher, catalog: CatalogManager) -> None:
        self._fetcher = fetcher
        self._catalog = catalog

    def process(self, raw_url: str, stats: CrawlStats) -> SpecStatus:
        """
        Run the full update pipeline for a single spec URL.

        Side effects:
        - Updates the in-memory catalog entry.
        - Appends a HistoryEntry if status is NEW or UPDATED.

        Args:
            raw_url: The raw.githubusercontent.com URL to fetch.
            stats:   Mutable CrawlStats accumulator updated in place.

        Returns:
            The SpecStatus determined for this run.
        """
        spec_id = make_spec_id(raw_url)
        existing: Optional[CatalogEntry] = self._catalog.get(spec_id)
        fetched_at = utc_now_iso()

        # ---- 1. Fetch -------------------------------------------------------
        fetch_result = self._fetcher.fetch(
            url=raw_url,
            etag=None,          # ETag support can be layered in via CatalogEntry extension
            last_modified=None,
        )
        stats.total_fetched += 1

        if not fetch_result.success:
            log_event(
                logger,
                "spec_fetch_failed",
                level="error",
                url=raw_url,
                error=fetch_result.error,
            )
            self._record_error(raw_url, existing, fetched_at)
            stats.error_specs += 1
            return SpecStatus.ERROR

        if fetch_result.not_modified:
            # HTTP 304: server confirmed nothing changed; trust it.
            log_event(logger, "spec_not_modified", url=raw_url)
            if existing:
                updated = update_catalog_entry(
                    existing, _empty_parsed_stub(existing), existing.latest_hash or "", SpecStatus.UNCHANGED, fetched_at
                )
                self._catalog.upsert(updated)
            stats.unchanged_specs += 1
            return SpecStatus.UNCHANGED

        content: bytes = fetch_result.content  # type: ignore[assignment]
        content_hash = sha256_of_bytes(content)

        # ---- 2. Parse -------------------------------------------------------
        parsed = parse_spec(content, source_url=raw_url)
        stats.total_parsed += 1

        if not parsed.is_valid:
            log_event(
                logger,
                "spec_parse_failed",
                level="warning",
                url=raw_url,
                error=parsed.parse_error,
            )
            self._record_error(raw_url, existing, fetched_at)
            stats.error_specs += 1
            return SpecStatus.ERROR

        # ---- 3. Determine status --------------------------------------------
        status = determine_status(parsed, existing, content_hash)

        # ---- 4. Build / update catalog entry --------------------------------
        if existing is None:
            entry = make_new_catalog_entry(raw_url, parsed, content_hash, fetched_at)
        else:
            entry = update_catalog_entry(existing, parsed, content_hash, status, fetched_at)

        self._catalog.upsert(entry)

        # ---- 5. Append history (only on meaningful change) ------------------
        if status in (SpecStatus.NEW, SpecStatus.UPDATED):
            old_paths = existing.raw_paths if existing and hasattr(existing, "raw_paths") else None
            history = build_history_entry(
                source_url=raw_url,
                parsed=parsed,
                content_hash=content_hash,
                status=status,
                old_paths=old_paths,
            )
            self._catalog.append_history(history)

        # ---- 6. Update stats ------------------------------------------------
        if status == SpecStatus.NEW:
            stats.new_specs += 1
        elif status == SpecStatus.UPDATED:
            stats.updated_specs += 1
        else:
            stats.unchanged_specs += 1

        log_event(
            logger,
            "spec_processed",
            url=raw_url,
            status=status,
            title=parsed.title,
            paths_count=parsed.paths_count,
            openapi_version=parsed.openapi_version,
            info_version=parsed.version,
            content_hash=content_hash[:8] + "…",
        )

        return status

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_error(
        self,
        raw_url: str,
        existing: Optional[CatalogEntry],
        fetched_at: str,
    ) -> None:
        """Persist an ERROR status to the catalog without overwriting good data."""
        if existing:
            updated = existing.model_copy(update={"status": SpecStatus.ERROR, "fetched_at": fetched_at})
            self._catalog.upsert(updated)
        else:
            # First time seeing this URL and it immediately errored.
            spec_id = make_spec_id(raw_url)
            entry = CatalogEntry(
                spec_id=spec_id,
                source_url=raw_url,
                status=SpecStatus.ERROR,
                first_seen_at=fetched_at,
                fetched_at=fetched_at,
                history_file=f"history/{spec_id}.json",
            )
            self._catalog.upsert(entry)


def _empty_parsed_stub(existing: CatalogEntry):  # type: ignore[return]
    """Create a minimal ParsedSpec stub from existing catalog data (for 304 responses)."""
    from crawler.models import ParsedSpec
    return ParsedSpec(
        title=existing.title,
        version=existing.latest_info_version,
        description=existing.description,
        openapi_version=existing.openapi_version,
        servers=existing.servers,
        tags=existing.tags,
        paths_count=existing.paths_count,
        is_valid=True,
    )