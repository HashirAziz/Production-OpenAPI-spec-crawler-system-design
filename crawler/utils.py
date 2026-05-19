"""
utils.py — Shared utility functions for the OpenAPI Spec Crawler.

Design decisions:
- All utilities are pure functions with no side effects (easy to unit-test).
- Timestamp helpers always return UTC ISO-8601 strings — no naive datetimes
  anywhere in the codebase to avoid timezone bugs.
- spec_id is derived deterministically from the source URL so the catalog
  key is stable across runs without needing a database sequence.
- make_human_id generates APIMatic-compatible "github:owner/repo/filename"
  format IDs for human-readable catalog entries.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with timezone."""
    return datetime.now(timezone.utc).isoformat()


def utc_now() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_of_bytes(data: bytes) -> str:
    """
    Return the hex-encoded SHA-256 digest of raw bytes.

    Used to detect content changes between crawl runs regardless of
    whether the spec's info.version field was bumped.

    Args:
        data: Raw file bytes (before any parsing).

    Returns:
        64-character lowercase hex string.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_of_str(text: str) -> str:
    """Return the hex-encoded SHA-256 digest of a UTF-8 string."""
    return sha256_of_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Stable spec identifier
# ---------------------------------------------------------------------------

def make_spec_id(source_url: str) -> str:
    """
    Derive a stable, filesystem-safe identifier from a source URL.

    We take the first 12 hex chars of the URL's SHA-256 so IDs are:
    - Deterministic (same URL → same ID across runs).
    - Short enough to use as filenames and JSON keys.
    - Collision-resistant for the scale this crawler targets.

    Args:
        source_url: The canonical raw URL of the spec file.

    Returns:
        12-character lowercase hex string, e.g. "a3f9b2c11d04".
    """
    return sha256_of_str(source_url)[:12]


def make_human_id(source_url: str) -> str:
    """
    Generate an APIMatic-compatible human-readable ID from a raw GitHub URL.

    Converts:
        https://raw.githubusercontent.com/stripe/openapi/master/openapi/spec3.yaml
    To:
        github:stripe/openapi/spec3.yaml

    Falls back to a hash-based ID for non-standard URLs.

    Args:
        source_url: The canonical raw URL of the spec file.

    Returns:
        Human-readable ID string in "github:owner/repo/filename" format.
    """
    pattern = r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/[^/]+/(.+)"
    match = re.match(pattern, source_url)
    if match:
        owner, repo, path = match.groups()
        filename = path.split("/")[-1]
        return f"github:{owner}/{repo}/{filename}"
    # Fallback for non-GitHub or non-standard URLs
    return f"github:{sha256_of_str(source_url)[:12]}"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def to_raw_github_url(html_url: str) -> str:
    """
    Convert a GitHub blob URL to its raw.githubusercontent.com equivalent.

    GitHub Code Search returns HTML URLs like:
        https://github.com/owner/repo/blob/main/openapi.yaml

    We need the raw URL to download the file content:
        https://raw.githubusercontent.com/owner/repo/main/openapi.yaml

    Args:
        html_url: GitHub HTML file URL.

    Returns:
        Raw content URL, or the original URL if conversion is not possible.
    """
    pattern = r"https://github\.com/([^/]+)/([^/]+)/blob/(.+)"
    match = re.match(pattern, html_url)
    if match:
        owner, repo, rest = match.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{rest}"
    return html_url  # return as-is; fetcher will handle the failure


def is_yaml_url(url: str) -> bool:
    """Return True if the URL path ends with .yaml or .yml."""
    path = urlparse(url).path.lower()
    return path.endswith((".yaml", ".yml"))


def is_json_url(url: str) -> bool:
    """Return True if the URL path ends with .json."""
    path = urlparse(url).path.lower()
    return path.endswith(".json")


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def truncate(text: str, max_len: int = 120) -> str:
    """Truncate a string for display/logging, appending '…' if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"