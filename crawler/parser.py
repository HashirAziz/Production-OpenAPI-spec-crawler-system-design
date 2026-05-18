"""
parser.py — OpenAPI / Swagger spec parser.

Design decisions:
- The parser NEVER raises.  Any malformed input returns a ParsedSpec with
  is_valid=False and a parse_error message.  This is deliberate: a single
  bad spec should not abort an entire crawl run.
- YAML and JSON are both normalised to a plain Python dict before any
  field extraction, so all downstream logic is format-agnostic.
- Version detection uses the presence of the "openapi" or "swagger" key
  rather than trying to parse version strings, which is more resilient to
  non-standard documents.
- Servers extraction handles both OpenAPI 3.x (servers[].url) and
  Swagger 2.x (host + basePath + schemes) formats.
- Tags are extracted from the top-level tags array (spec-level declaration),
  not inferred from individual operation tags, to keep parsing O(1) in
  path count.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import yaml

from crawler.models import OpenAPIVersion, ParsedSpec
from crawler.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_spec(raw_content: bytes, source_url: str = "") -> ParsedSpec:
    """
    Parse raw spec bytes into a structured ParsedSpec.

    Attempts YAML first (YAML is a superset of JSON, so json files parse
    fine via PyYAML), then falls back to strict JSON parsing if YAML
    raises a non-recoverable error.

    Args:
        raw_content: Raw bytes of the spec file.
        source_url:  Used only for log context; not stored in the result.

    Returns:
        ParsedSpec — always.  Check .is_valid and .parse_error for issues.
    """
    doc: Optional[dict[str, Any]] = _load_document(raw_content, source_url)
    if doc is None:
        return ParsedSpec(
            is_valid=False,
            parse_error="Failed to deserialise document as YAML or JSON.",
        )

    if not isinstance(doc, dict):
        return ParsedSpec(
            is_valid=False,
            parse_error=f"Expected a mapping at the top level, got {type(doc).__name__}.",
        )

    return _extract_fields(doc)


# ---------------------------------------------------------------------------
# Internal helpers — document loading
# ---------------------------------------------------------------------------

def _load_document(raw_content: bytes, source_url: str) -> Optional[dict[str, Any]]:
    """Attempt to deserialise bytes as YAML (covers JSON too)."""
    try:
        # yaml.safe_load handles both YAML and JSON without code execution risk.
        doc = yaml.safe_load(raw_content)
        return doc  # type: ignore[return-value]
    except yaml.YAMLError as yaml_err:
        logger.debug(
            "YAML parse failed, trying strict JSON",
            extra={"source_url": source_url, "yaml_error": str(yaml_err)},
        )

    # Fallback: strict JSON (handles edge cases where PyYAML's JSON mode fails)
    try:
        text = raw_content.decode("utf-8", errors="replace")
        return json.loads(text)  # type: ignore[return-value]
    except (json.JSONDecodeError, UnicodeDecodeError) as json_err:
        logger.warning(
            "Both YAML and JSON parsing failed",
            extra={"source_url": source_url, "json_error": str(json_err)},
        )
        return None


# ---------------------------------------------------------------------------
# Internal helpers — field extraction
# ---------------------------------------------------------------------------

def _extract_fields(doc: dict[str, Any]) -> ParsedSpec:
    """
    Extract structured fields from a successfully loaded document dict.

    Extraction is intentionally defensive: each field is wrapped
    individually so one bad field doesn't prevent extraction of others.
    """
    openapi_version = _detect_version(doc)
    info = doc.get("info") or {}

    title = _safe_str(info.get("title"))
    version = _safe_str(info.get("version"))
    description = _safe_str(info.get("description"))

    servers = _extract_servers(doc, openapi_version)
    tags = _extract_tags(doc)
    paths = _extract_paths(doc)

    return ParsedSpec(
        title=title,
        version=version,
        description=description,
        openapi_version=openapi_version,
        servers=servers,
        tags=tags,
        paths_count=len(paths),
        raw_paths=paths,
        is_valid=True,
    )


def _detect_version(doc: dict[str, Any]) -> OpenAPIVersion:
    """
    Detect whether this is a Swagger 2.x or OpenAPI 3.x document.

    Relies on the top-level key ("swagger" vs "openapi") rather than
    the version string value, which is more resilient to edge cases like
    `openapi: "3.0"` (missing patch version).
    """
    if "openapi" in doc:
        return OpenAPIVersion.OPENAPI_3
    if "swagger" in doc:
        return OpenAPIVersion.SWAGGER_2
    return OpenAPIVersion.UNKNOWN


def _extract_servers(doc: dict[str, Any], version: OpenAPIVersion) -> list[str]:
    """
    Extract server URLs, normalising across spec versions.

    - OpenAPI 3.x: servers[].url
    - Swagger 2.x: reconstruct from host, basePath, schemes
    """
    servers: list[str] = []

    if version == OpenAPIVersion.OPENAPI_3:
        for entry in doc.get("servers") or []:
            if isinstance(entry, dict) and "url" in entry:
                url = _safe_str(entry["url"])
                if url:
                    servers.append(url)

    elif version == OpenAPIVersion.SWAGGER_2:
        host = _safe_str(doc.get("host")) or ""
        base_path = _safe_str(doc.get("basePath")) or "/"
        schemes = doc.get("schemes") or ["https"]
        if host:
            for scheme in schemes:
                if isinstance(scheme, str):
                    servers.append(f"{scheme}://{host}{base_path}")

    return servers


def _extract_tags(doc: dict[str, Any]) -> list[str]:
    """
    Extract tag names from the top-level tags declaration.

    Tags may be objects {name, description} or plain strings.
    """
    tags: list[str] = []
    for tag in doc.get("tags") or []:
        if isinstance(tag, dict):
            name = _safe_str(tag.get("name"))
            if name:
                tags.append(name)
        elif isinstance(tag, str) and tag:
            tags.append(tag)
    return tags


def _extract_paths(doc: dict[str, Any]) -> list[str]:
    """Return the list of path keys from the paths object."""
    paths_obj = doc.get("paths")
    if not isinstance(paths_obj, dict):
        return []
    return list(paths_obj.keys())


def _safe_str(value: Any) -> Optional[str]:
    """
    Coerce a value to string or return None.

    Handles cases like `version: 1` (integer in YAML) which should
    become "1" rather than causing a type error.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    # Numeric versions (e.g. version: 2) are valid in some specs.
    return str(value)