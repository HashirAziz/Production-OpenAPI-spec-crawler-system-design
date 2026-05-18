"""
fetcher.py — HTTP fetcher for raw spec content from GitHub.

Design decisions:
- httpx is preferred over requests for its first-class timeout handling
  and async-readiness (easy to upgrade later).
- ETag / Last-Modified support avoids re-downloading unchanged files and
  reduces GitHub API quota consumption.
- Exponential backoff with jitter handles transient 5xx errors and rate
  limiting (HTTP 429 / 403 with X-RateLimit-Reset header).
- FetchResult is a dataclass (not Pydantic) because it is transient — it
  never needs to be serialised to JSON.
- A session (httpx.Client) is constructed once per crawl run and reused
  across requests to benefit from HTTP keep-alive.
"""

from __future__ import annotations

import time
import random
from dataclasses import dataclass, field
from typing import Optional

import httpx

from crawler.logger import get_logger, log_event

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 20          # seconds per request
MAX_RETRIES = 3
BASE_BACKOFF = 1.0            # seconds; doubles on each retry
MAX_BACKOFF = 30.0            # cap to avoid indefinite waits
JITTER_RANGE = 0.5            # ±0.5s random jitter


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """
    Outcome of a single fetch attempt.

    content is None on failure or HTTP 304 (not modified).
    etag / last_modified are persisted in the catalog so subsequent
    requests can skip unchanged files.
    """
    url: str
    success: bool
    content: Optional[bytes] = None
    status_code: Optional[int] = None
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    not_modified: bool = False          # True on HTTP 304
    error: Optional[str] = None
    attempts: int = 0


# ---------------------------------------------------------------------------
# Fetcher class
# ---------------------------------------------------------------------------

class SpecFetcher:
    """
    Downloads raw spec files from GitHub with resilience features.

    Args:
        github_token: Personal access token for GitHub API auth.
                      Increases rate limit from 60 to 5,000 req/hr.
        timeout:      Per-request timeout in seconds.
        max_retries:  Number of retry attempts on transient failures.
    """

    def __init__(
        self,
        github_token: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries

        headers = {
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "openapi-spec-crawler/1.0",
        }
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        # A single client instance is reused across all fetch calls within
        # one crawl run, enabling HTTP/1.1 keep-alive connection pooling.
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch(
        self,
        url: str,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
    ) -> FetchResult:
        """
        Download a spec URL, with optional cache validation headers.

        Args:
            url:           Full URL of the raw spec file.
            etag:          Previously seen ETag value (triggers HTTP 304).
            last_modified: Previously seen Last-Modified value.

        Returns:
            FetchResult with content on success, or error details on failure.
        """
        request_headers: dict[str, str] = {}
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified

        for attempt in range(1, self._max_retries + 1):
            try:
                result = self._attempt(url, request_headers, attempt)
                if result is not None:
                    return result
            except Exception as exc:  # noqa: BLE001
                wait = self._backoff(attempt)
                log_event(
                    logger,
                    "fetch_error",
                    level="warning",
                    url=url,
                    attempt=attempt,
                    error=str(exc),
                    retry_in=wait,
                )
                if attempt < self._max_retries:
                    time.sleep(wait)

        return FetchResult(
            url=url,
            success=False,
            error=f"All {self._max_retries} attempts failed.",
            attempts=self._max_retries,
        )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "SpecFetcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt(
        self,
        url: str,
        extra_headers: dict[str, str],
        attempt: int,
    ) -> Optional[FetchResult]:
        """
        Make one HTTP GET attempt.

        Returns a FetchResult on definitive outcomes (success, 304, 4xx).
        Returns None on transient failures (5xx, network errors) so the
        caller knows to retry.
        """
        response = self._client.get(url, headers=extra_headers)
        status = response.status_code

        log_event(
            logger,
            "fetch_attempt",
            level="debug",
            url=url,
            attempt=attempt,
            status_code=status,
        )

        # HTTP 304 — content unchanged since last fetch
        if status == 304:
            return FetchResult(
                url=url,
                success=True,
                not_modified=True,
                status_code=304,
                attempts=attempt,
            )

        # Rate limiting — honour Retry-After or X-RateLimit-Reset
        if status in (429, 403):
            wait = self._rate_limit_wait(response)
            log_event(
                logger,
                "rate_limit_hit",
                level="warning",
                url=url,
                status_code=status,
                retry_after=wait,
            )
            time.sleep(wait)
            return None  # signal: retry

        # Client errors (404, 401, etc.) — no point retrying
        if 400 <= status < 500:
            return FetchResult(
                url=url,
                success=False,
                status_code=status,
                error=f"HTTP {status} — client error, not retrying.",
                attempts=attempt,
            )

        # Server errors — retry
        if status >= 500:
            return None

        # 2xx success
        if 200 <= status < 300:
            return FetchResult(
                url=url,
                success=True,
                content=response.content,
                status_code=status,
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
                attempts=attempt,
            )

        # Unexpected status — treat as transient
        return None

    @staticmethod
    def _backoff(attempt: int) -> float:
        """
        Exponential backoff with full jitter.

        Jitter prevents the thundering-herd problem when many URLs fail
        simultaneously (e.g. during a GitHub outage).
        """
        base = min(BASE_BACKOFF * (2 ** (attempt - 1)), MAX_BACKOFF)
        return base + random.uniform(-JITTER_RANGE, JITTER_RANGE)  # noqa: S311

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> float:
        """
        Compute how long to sleep after a 429 / 403 rate-limit response.

        Prefers X-RateLimit-Reset (epoch seconds) over a fixed default.
        """
        reset_header = response.headers.get("X-RateLimit-Reset")
        if reset_header:
            try:
                wait = float(reset_header) - time.time()
                return max(wait + 1.0, 1.0)   # +1s safety margin
            except ValueError:
                pass

        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass

        return 60.0   # conservative default: wait 1 minute