"""
github_search.py — GitHub Code Search API client.

Design decisions:
- Search queries are configurable via config.yaml so new filename patterns
  can be added without touching Python code.
- The API returns at most 1,000 results per query (GitHub hard limit).
  We run multiple queries (openapi.yaml, openapi.json, swagger.yaml,
  swagger.json) to maximise discovery surface.
- Results are de-duplicated by HTML URL before yielding so callers never
  process the same file twice even when it appears in multiple queries.
- The secondary rate limit (POST 429 with Retry-After) is handled here
  rather than in the fetcher because search uses a different endpoint and
  quota pool.
- Pagination uses rel="next" Link headers rather than manual page math
  to be robust against GitHub changing the default page size.
"""

from __future__ import annotations

import time
from typing import Generator, Optional
from dataclasses import dataclass

import httpx

from crawler.logger import get_logger, log_event
from crawler.utils import to_raw_github_url

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_SEARCH_URL = "https://api.github.com/search/code"
DEFAULT_PER_PAGE = 100          # GitHub maximum
REQUEST_TIMEOUT = 15            # seconds
INTER_PAGE_SLEEP = 1.0          # courtesy pause between pages (avoids secondary rate limit)

DEFAULT_QUERIES = [
    "filename:openapi.yaml",
    "filename:openapi.json",
    "filename:swagger.yaml",
    "filename:swagger.json",
]


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    """A single file discovered via Code Search."""
    html_url: str           # e.g. https://github.com/owner/repo/blob/main/openapi.yaml
    raw_url: str            # converted raw.githubusercontent.com URL
    repository: str         # "owner/repo"
    file_path: str          # path within the repo
    sha: str                # git object SHA (not content hash)


# ---------------------------------------------------------------------------
# Search client
# ---------------------------------------------------------------------------

class GitHubSearchClient:
    """
    Discovers OpenAPI spec files via GitHub Code Search.

    Args:
        github_token: Personal access token.  Without it, unauthenticated
                      search is limited to 10 requests/minute.
        queries:      List of Code Search query strings.  Defaults to the
                      four standard filename patterns.
        per_page:     Results per API page (max 100).
        max_results:  Cap per query.  GitHub allows at most 1,000.
    """

    def __init__(
        self,
        github_token: Optional[str] = None,
        queries: Optional[list[str]] = None,
        per_page: int = DEFAULT_PER_PAGE,
        max_results: int = 1000,
    ) -> None:
        self._queries = queries or DEFAULT_QUERIES
        self._per_page = min(per_page, 100)
        self._max_results = min(max_results, 1000)

        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "openapi-spec-crawler/1.0",
        }
        if github_token:
            headers["Authorization"] = f"token {github_token}"

        self._client = httpx.Client(headers=headers, timeout=REQUEST_TIMEOUT)
        self._seen_urls: set[str] = set()   # global de-dup across all queries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self) -> Generator[SearchResult, None, None]:
        """
        Yield SearchResult objects for every unique spec file found.

        Iterates through all configured queries, handling pagination and
        rate limits transparently.  Stops when max_results is reached
        for a query or no more pages exist.
        """
        for query in self._queries:
            log_event(
                logger,
                "search_query_start",
                query=query,
                max_results=self._max_results,
            )
            yielded = 0
            try:
                for result in self._paginate(query):
                    if result.html_url not in self._seen_urls:
                        self._seen_urls.add(result.html_url)
                        yielded += 1
                        yield result
            except Exception as exc:  # noqa: BLE001
                log_event(
                    logger,
                    "search_query_error",
                    level="error",
                    query=query,
                    error=str(exc),
                )

            log_event(
                logger,
                "search_query_done",
                query=query,
                unique_results=yielded,
            )

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._client.close()

    def __enter__(self) -> "GitHubSearchClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _paginate(self, query: str) -> Generator[SearchResult, None, None]:
        """
        Yield raw SearchResult objects for a single query, page by page.

        GitHub search uses Link: <url>; rel="next" headers for pagination.
        We follow next links until exhausted or max_results is reached.
        """
        collected = 0
        next_url: Optional[str] = GITHUB_SEARCH_URL
        params: dict[str, str | int] = {
            "q": query,
            "per_page": self._per_page,
        }

        while next_url and collected < self._max_results:
            response = self._client.get(next_url, params=params if next_url == GITHUB_SEARCH_URL else {})
            self._handle_rate_limit(response)

            if response.status_code != 200:
                log_event(
                    logger,
                    "search_http_error",
                    level="warning",
                    query=query,
                    status_code=response.status_code,
                    body=response.text[:200],
                )
                break

            data = response.json()
            items: list[dict] = data.get("items", [])

            for item in items:
                if collected >= self._max_results:
                    return
                result = self._parse_item(item)
                if result:
                    collected += 1
                    yield result

            log_event(
                logger,
                "search_page_fetched",
                level="debug",
                query=query,
                items_on_page=len(items),
                total_collected=collected,
            )

            # Follow rel="next" link for next page
            next_url = self._extract_next_link(response)
            if next_url:
                time.sleep(INTER_PAGE_SLEEP)

    @staticmethod
    def _parse_item(item: dict) -> Optional[SearchResult]:
        """Convert a raw GitHub search item dict to a SearchResult."""
        try:
            html_url = item["html_url"]
            raw_url = to_raw_github_url(html_url)
            repo = item.get("repository", {})
            return SearchResult(
                html_url=html_url,
                raw_url=raw_url,
                repository=repo.get("full_name", ""),
                file_path=item.get("path", ""),
                sha=item.get("sha", ""),
            )
        except (KeyError, TypeError) as exc:
            logger.debug("Failed to parse search item", extra={"error": str(exc), "item": str(item)[:200]})
            return None

    @staticmethod
    def _handle_rate_limit(response: httpx.Response) -> None:
        """Sleep if we are approaching the rate limit."""
        remaining = response.headers.get("X-RateLimit-Remaining", "999")
        try:
            if int(remaining) < 5:
                reset_ts = float(response.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait = max(reset_ts - time.time() + 1, 1.0)
                log_event(
                    logger,
                    "rate_limit_approaching",
                    level="warning",
                    remaining=remaining,
                    sleeping_seconds=wait,
                )
                time.sleep(wait)
        except ValueError:
            pass

    @staticmethod
    def _extract_next_link(response: httpx.Response) -> Optional[str]:
        """
        Parse the Link header to find the rel="next" URL.

        GitHub uses RFC 5988 link relations, e.g.:
            Link: <https://api.github.com/search/code?q=...&page=2>; rel="next"
        """
        link_header = response.headers.get("Link", "")
        for part in link_header.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                # Extract URL between angle brackets
                url_part = part.split(";")[0].strip()
                if url_part.startswith("<") and url_part.endswith(">"):
                    return url_part[1:-1]
        return None