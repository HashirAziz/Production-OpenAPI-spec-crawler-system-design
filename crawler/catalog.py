"""
catalog.py — JSON catalog and history persistence.

Design decisions:
- Atomic writes via a temp-file + os.replace() pattern prevent catalog
  corruption if the process is killed mid-write.  os.replace() is
  guaranteed atomic on POSIX and effectively atomic on Windows (same drive).
- The catalog is a flat dict keyed by spec_id rather than a list so
  lookup and update are O(1) without scanning.
- History entries are stored in per-spec sidecar files under data/history/
  so the main catalog stays lean and history can grow unboundedly.
- History files are append-only: we load the existing list, append the new
  entry, and write back.  This preserves the full audit trail.
- No database dependency — plain JSON files work perfectly for this scale
  and are inspectable with any text editor.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

from crawler.models import CatalogEntry, HistoryEntry
from crawler.logger import get_logger, log_event

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Catalog manager
# ---------------------------------------------------------------------------

class CatalogManager:
    """
    Manages the catalog.json file and per-spec history sidecars.

    Args:
        data_dir: Root data directory (contains catalog.json and history/).
    """

    CATALOG_FILENAME = "catalog.json"

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._history_dir = data_dir / "history"
        self._catalog_path = data_dir / self.CATALOG_FILENAME

        # In-memory catalog: spec_id → CatalogEntry
        self._entries: dict[str, CatalogEntry] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        """
        Load the catalog from disk into memory.

        Safe to call even if catalog.json does not yet exist (returns
        an empty catalog).
        """
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._history_dir.mkdir(parents=True, exist_ok=True)

        if not self._catalog_path.exists():
            log_event(logger, "catalog_not_found", path=str(self._catalog_path))
            self._entries = {}
            self._loaded = True
            return

        try:
            raw = self._catalog_path.read_text(encoding="utf-8")
            data: dict[str, dict] = json.loads(raw)
            self._entries = {
                spec_id: CatalogEntry.model_validate(entry)
                for spec_id, entry in data.items()
            }
            log_event(
                logger,
                "catalog_loaded",
                path=str(self._catalog_path),
                entry_count=len(self._entries),
            )
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log_event(
                logger,
                "catalog_load_error",
                level="error",
                path=str(self._catalog_path),
                error=str(exc),
            )
            self._entries = {}

        self._loaded = True

    def save(self) -> None:
        """
        Atomically persist the in-memory catalog to disk.

        Uses a temporary file + os.replace() to prevent partial writes.
        """
        self._ensure_loaded()
        payload = {
            spec_id: entry.model_dump(mode="json")
            for spec_id, entry in self._entries.items()
        }

        # Write to a temp file in the same directory so os.replace() is
        # atomic (same filesystem).
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._data_dir,
            prefix=".catalog_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._catalog_path)
        except OSError as exc:
            # Clean up the temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            log_event(
                logger,
                "catalog_save_error",
                level="error",
                error=str(exc),
            )
            raise

        log_event(
            logger,
            "catalog_saved",
            path=str(self._catalog_path),
            entry_count=len(self._entries),
        )

    # ------------------------------------------------------------------
    # Entry CRUD
    # ------------------------------------------------------------------

    def get(self, spec_id: str) -> Optional[CatalogEntry]:
        """Return the catalog entry for spec_id, or None if not present."""
        self._ensure_loaded()
        return self._entries.get(spec_id)

    def upsert(self, entry: CatalogEntry) -> None:
        """Insert or replace a catalog entry."""
        self._ensure_loaded()
        self._entries[entry.spec_id] = entry

    def all_entries(self) -> list[CatalogEntry]:
        """Return all catalog entries as a list."""
        self._ensure_loaded()
        return list(self._entries.values())

    def count(self) -> int:
        """Return the total number of entries in the catalog."""
        self._ensure_loaded()
        return len(self._entries)

    # ------------------------------------------------------------------
    # History sidecar management
    # ------------------------------------------------------------------

    def append_history(self, entry: HistoryEntry) -> None:
        """
        Append a HistoryEntry to the spec's sidecar history file.

        The sidecar is a JSON array stored at data/history/{spec_id}.json.
        We load → append → write atomically to preserve all past entries.

        Args:
            entry: The immutable history record to append.
        """
        history_path = self._history_dir / f"{entry.spec_id}.json"

        # Load existing history (or start fresh)
        existing: list[dict] = []
        if history_path.exists():
            try:
                existing = json.loads(history_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log_event(
                    logger,
                    "history_load_error",
                    level="warning",
                    path=str(history_path),
                    error=str(exc),
                )

        existing.append(entry.model_dump(mode="json"))

        # Atomic write
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self._history_dir,
            prefix=f".hist_{entry.spec_id}_",
            suffix=".json",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(existing, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, history_path)
        except OSError as exc:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            log_event(
                logger,
                "history_save_error",
                level="error",
                spec_id=entry.spec_id,
                error=str(exc),
            )
            raise

        log_event(
            logger,
            "history_entry_appended",
            level="debug",
            spec_id=entry.spec_id,
            history_path=str(history_path),
            status=entry.status,
        )

    def load_history(self, spec_id: str) -> list[HistoryEntry]:
        """
        Load all historical entries for a spec.

        Args:
            spec_id: The spec identifier.

        Returns:
            List of HistoryEntry objects, oldest first.
        """
        history_path = self._history_dir / f"{spec_id}.json"
        if not history_path.exists():
            return []
        try:
            raw = json.loads(history_path.read_text(encoding="utf-8"))
            return [HistoryEntry.model_validate(e) for e in raw]
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            log_event(
                logger,
                "history_load_error",
                level="warning",
                spec_id=spec_id,
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()