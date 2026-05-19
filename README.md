# OpenAPI Spec Crawler

> A production-quality Python system that **discovers, fetches, parses, versions, and tracks** OpenAPI specifications from GitHub — built as an AI Engineering internship assessment for APIMatic.

[![Tests](https://img.shields.io/badge/tests-80%20passed-brightgreen)](./tests/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![CI](https://github.com/HashirAziz/Production-OpenAPI-spec-crawler-system-design/actions/workflows/crawl.yml/badge.svg)](https://github.com/HashirAziz/Production-OpenAPI-spec-crawler-system-design/actions)

---

## Table of Contents

- [What This Project Does](#what-this-project-does)
- [Architecture](#architecture)
- [Data Flow](#data-flow)
- [Module Responsibilities](#module-responsibilities)
- [Versioning Logic](#versioning-logic)
- [Quick Start](#quick-start)
- [Running Tests](#running-tests)
- [GitHub Actions](#github-actions)
- [Catalog Schema](#catalog-schema)
- [Design Decisions](#design-decisions)
- [Tradeoffs](#tradeoffs)
- [Assumptions](#assumptions)
- [Known Limitations](#known-limitations)
- [Future Improvements](#future-improvements)

---

## What This Project Does

At [APIMatic](https://www.apimatic.io), transforming raw API specifications into SDKs, docs, and developer portals requires reliable, up-to-date access to OpenAPI specs. This crawler simulates that real-world challenge:

1. **Discovers** OpenAPI/Swagger spec files across GitHub using the Code Search API
2. **Fetches** raw file content with retry, backoff, and ETag support
3. **Parses** both YAML and JSON, supporting OpenAPI 2.x (Swagger) and 3.x
4. **Detects changes** using dual-signal versioning (SHA-256 hash + `info.version`)
5. **Tracks history** in per-spec immutable append-only sidecar files
6. **Persists** a structured `catalog.json` with full lifecycle metadata
7. **Emits structured JSON logs** for every event, correlated by `run_id`
8. **Runs automatically** via GitHub Actions on a daily schedule

---

## Architecture

openapi-spec-crawler/
├── crawler/
│   ├── models.py          # Pydantic domain models — the shared contract
│   ├── logger.py          # Structured JSON logging + run_id injection
│   ├── utils.py           # Pure utility functions (hashing, URL conversion)
│   ├── github_search.py   # GitHub Code Search API client (paginated)
│   ├── fetcher.py         # HTTP downloader (ETag, retry, rate limiting)
│   ├── parser.py          # YAML/JSON parser — never crashes on bad input
│   ├── versioning.py      # Change detection + path diff + history entries
│   ├── catalog.py         # Atomic JSON persistence + history sidecars
│   └── updater.py         # Per-spec pipeline orchestrator
├── data/
│   ├── catalog.json       # Live catalog (updated each run)
│   └── history/           # Per-spec append-only history files
├── tests/
│   ├── conftest.py        # Shared fixtures and sample specs
│   ├── test_parser.py     # 18 parser tests
│   ├── test_versioning.py # 19 versioning tests
│   ├── test_catalog.py    # 17 catalog tests
│   └── test_utils.py      # 14 utility tests
├── scripts/
│   └── run_crawler.py     # CLI entry point
├── .github/workflows/
│   └── crawl.yml          # Scheduled + manual dispatch CI/CD
├── config.yaml            # Runtime configuration
├── requirements.txt       # Pinned dependencies
└── Makefile               # Developer convenience targets

### Dependency Graph (zero circular dependencies)

models  ←  logger  ←  utils
↓           ↓          ↓
parser      fetcher   github_search
↓           ↓
versioning ←─┘
↓
catalog
↓
updater
↓
run_crawler  ← entry point

Each module depends only on modules below it. `models.py` has zero internal dependencies — it is the shared vocabulary every other module speaks.

---

## Data Flow
GitHub Code Search API
│
│  (paginated, de-duplicated across 4 query patterns)
▼
GitHubSearchClient.discover()
│
│  SearchResult { html_url, raw_url, repository, file_path }
▼
SpecUpdater.process(raw_url)
│
├──► SpecFetcher.fetch()
│    HTTP GET + ETag + exponential backoff + rate limit handling
│         │
│         ▼
│    FetchResult { content_bytes, etag, status_code }
│
├──► parse_spec(content_bytes)
│    YAML/JSON → ParsedSpec (never raises on malformed input)
│         │
│         ▼
│    ParsedSpec { title, version, servers, tags, paths_count }
│
├──► determine_status()
│    NEW / UPDATED / UNCHANGED / ERROR
│
├──► build_history_entry()
│    Immutable snapshot + PathDiff (added/removed endpoints)
│
├──► CatalogManager.upsert()
├──► CatalogManager.append_history()
└──► CatalogManager.save()  ← atomic write to catalog.json

---

## Module Responsibilities

| Module | Single Responsibility |
|---|---|
| `models.py` | Define all shared data types with Pydantic v2. Zero logic. |
| `logger.py` | JSON-format every log record; inject `run_id` for correlation. |
| `utils.py` | Pure functions: hashing, URL conversion, ID generation. |
| `github_search.py` | Talk to GitHub Code Search API; paginate; de-duplicate results. |
| `fetcher.py` | HTTP download with ETag, retry, backoff, rate limit handling. |
| `parser.py` | Deserialise YAML/JSON; extract fields; never raise on bad input. |
| `versioning.py` | Compute status, path diffs, build immutable history entries. |
| `catalog.py` | Atomic JSON persistence; history sidecar file management. |
| `updater.py` | Orchestrate the full pipeline for a single spec URL. |
| `run_crawler.py` | Wire components; parse CLI args; run discovery loop. |

---

## Versioning Logic

Every spec is classified on each crawl as one of four states: