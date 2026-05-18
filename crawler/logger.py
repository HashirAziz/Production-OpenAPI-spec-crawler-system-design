"""
logger.py — Structured JSON logging for the OpenAPI Spec Crawler.

Design decisions:
- Every log record is a JSON object (not a human-readable string).  This
  makes logs machine-parseable by tools like jq, Datadog, or Loki without
  extra parsing.
- A single run_id (UUID) is injected into every record so all events from
  one crawl run can be correlated in log aggregation systems.
- We wrap Python's stdlib logging rather than replacing it, so existing
  third-party libraries that use logging still emit records through our
  handler chain.
- The module exposes a get_logger() factory so every other module stays
  decoupled from the concrete handler implementation.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

class JsonFormatter(logging.Formatter):
    """
    Formats each LogRecord as a single-line JSON object.

    Extra fields can be injected per-record by passing them as keyword
    arguments to logger.info() / logger.error() etc. via the `extra` dict.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:  # noqa: D102
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        # Attach any extra fields the caller passed via extra={...}
        for key, value in record.__dict__.items():
            if key not in _STDLIB_RECORD_KEYS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


# Keys present on every LogRecord that we don't want to re-emit as extras.
_STDLIB_RECORD_KEYS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
})


# ---------------------------------------------------------------------------
# Logger factory
# ---------------------------------------------------------------------------

# Module-level run_id — shared across all loggers created in this process.
_RUN_ID: str = str(uuid.uuid4())


def get_run_id() -> str:
    """Return the current run's UUID (useful for embedding in CrawlStats)."""
    return _RUN_ID


def reset_run_id() -> str:
    """
    Rotate the run_id.

    Call this at the start of each crawl run (e.g. in main) so that a
    process that runs multiple crawls in the same process lifetime gets
    fresh correlation IDs.
    """
    global _RUN_ID  # noqa: PLW0603
    _RUN_ID = str(uuid.uuid4())
    return _RUN_ID


def get_logger(name: str) -> logging.Logger:
    """
    Return a logger that emits structured JSON to stdout.

    Idempotent — calling get_logger("x") twice returns the same logger
    without adding duplicate handlers.

    Args:
        name: Typically __name__ of the calling module.

    Returns:
        A configured Logger instance.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        # Already configured; avoid adding a second handler on re-import.
        return logger

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(run_id=_RUN_ID))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't double-emit via root logger

    return logger


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def log_event(
    logger: logging.Logger,
    event_type: str,
    level: str = "info",
    **kwargs: Any,
) -> None:
    """
    Emit a structured log event with an explicit event_type field.

    This thin wrapper standardises the `event_type` key so log aggregation
    queries can filter by event category (e.g. event_type=spec_updated).

    Args:
        logger:     The logger instance to use.
        event_type: Machine-readable category string (snake_case).
        level:      Log level string: "debug", "info", "warning", "error".
        **kwargs:   Arbitrary extra fields merged into the JSON payload.
    """
    emit = getattr(logger, level, logger.info)
    emit(event_type, extra={"event_type": event_type, **kwargs})