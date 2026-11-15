"""Structured JSON logging compatible with Cloud Logging.

Each record is one JSON line with ``severity``, ``message``, ``time``,
``logger`` and any ``extra={...}`` fields (event, latency_ms, tool, ...).
Subjects are logged only as a short salted hash, never raw.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from datetime import UTC, datetime

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_SALT = os.environ.get("LOG_SUBJECT_SALT", "propertyops")


def subject_hash(subject: str | None) -> str | None:
    if not subject:
        return None
    return hashlib.sha256(f"{_SALT}:{subject}".encode()).hexdigest()[:16]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
        }
        for key, value in vars(record).items():
            if key in _RESERVED or key.startswith("_"):
                continue
            if key == "subject":
                entry["subject_hash"] = subject_hash(value)
            else:
                entry[key] = value
        from app.observability import request_id_var, trace_var

        if (rid := request_id_var.get()) is not None:
            entry["request_id"] = rid
        if (trace := trace_var.get()) is not None:
            entry["logging.googleapis.com/trace"] = trace
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level or os.environ.get("LOG_LEVEL", "INFO"))
    for noisy in ("httpx", "httpcore", "google_adk", "urllib3", "grpc"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
