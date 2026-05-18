#!/usr/bin/env python3
"""
run_crawler.py — Main entry point for the OpenAPI Spec Crawler.

Usage:
    python scripts/run_crawler.py [--max-results N] [--data-dir PATH]

Environment variables:
    GITHUB_TOKEN   Required for authenticated API access (5000 req/hr).
                   Without it, GitHub limits to 60 req/hr and blocks search.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path when running directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from crawler.catalog import CatalogManager
from crawler.fetcher import SpecFetcher
from crawler.github_search import GitHubSearchClient
from crawler.logger import get_logger, log_event, reset_run_id, get_run_id
from crawler.models import CrawlStats
from crawler.updater import SpecUpdater
from crawler.utils import utc_now_iso


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="OpenAPI Spec Crawler — discovers and tracks OpenAPI specs from GitHub.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Maximum specs to discover per search query.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Directory for catalog.json and history/ files.",
    )
    parser.add_argument(
        "--queries",
        nargs="+",
        default=None,
        help="Override default Code Search queries.",
    )
    return parser


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = build_arg_parser().parse_args()

    run_id = reset_run_id()
    started_at = utc_now_iso()

    log_event(
        logger,
        "crawl_started",
        run_id=run_id,
        started_at=started_at,
        max_results=args.max_results,
        data_dir=str(args.data_dir),
    )

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        log_event(
            logger,
            "no_github_token",
            level="warning",
            message="GITHUB_TOKEN not set. Rate limits will be very restrictive (60 req/hr).",
        )

    stats = CrawlStats(run_id=run_id, started_at=started_at)

    # ---- Initialise components -------------------------------------------
    catalog = CatalogManager(data_dir=args.data_dir)
    catalog.load()

    with SpecFetcher(github_token=github_token) as fetcher, \
         GitHubSearchClient(
             github_token=github_token,
             queries=args.queries,
             max_results=args.max_results,
         ) as search_client:

        updater = SpecUpdater(fetcher=fetcher, catalog=catalog)

        # ---- Discovery loop ---------------------------------------------
        for search_result in search_client.discover():
            stats.total_discovered += 1

            log_event(
                logger,
                "spec_discovered",
                level="debug",
                url=search_result.raw_url,
                repository=search_result.repository,
            )

            updater.process(raw_url=search_result.raw_url, stats=stats)

            # Persist after every spec so a mid-run crash loses at most
            # one spec's worth of work.
            catalog.save()

    # ---- Finalise -------------------------------------------------------
    finished_at = utc_now_iso()
    stats.finished_at = finished_at
    stats.duration_seconds = round(
        (time.time() - time.mktime(time.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S"))),
        2,
    )

    log_event(
        logger,
        "crawl_finished",
        **stats.model_dump(),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())