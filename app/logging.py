"""JSON logs to stdout and a JSONL file. Every line has ts, level, event, user_id, request_id."""

import json
import logging
import sys
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from app.config import Settings

REDACTED_KEYS = frozenset({"api_key", "authorization", "system_prompt"})

_writer: "_TeeWriter | None" = None


class _TeeWriter:
    """File-like object that writes each log line to stdout and the log file."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, s: str) -> None:
        with self._lock:
            sys.stdout.write(s)
            self._file.write(s)

    def flush(self) -> None:
        with self._lock:
            sys.stdout.flush()
            self._file.flush()

    def close(self) -> None:
        self._file.close()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)  # never a float
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    return repr(obj)


def _trace_fields_first(_logger: Any, _name: str, event_dict: dict) -> dict:
    """Guarantee the trace keys exist and put them first for readability."""
    head = {
        "ts": event_dict.pop("ts", None),
        "level": event_dict.pop("level", None),
        "event": event_dict.pop("event", None),
        "user_id": event_dict.pop("user_id", None),
        "request_id": event_dict.pop("request_id", None),
    }
    return {**head, **event_dict}


def _redact(_logger: Any, _name: str, event_dict: dict) -> dict:
    for key in REDACTED_KEYS & event_dict.keys():
        event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(settings: Settings) -> None:
    global _writer
    if _writer is not None:
        _writer.close()
    _writer = _TeeWriter(Path(settings.log_file))

    level = logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            _redact,
            structlog.processors.format_exc_info,
            _trace_fields_first,
            structlog.processors.JSONRenderer(default=_json_default),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.WriteLoggerFactory(file=_writer),
        cache_logger_on_first_use=False,
    )


def get_logger() -> structlog.typing.FilteringBoundLogger:
    return structlog.get_logger()


def dumps(obj: Any) -> str:
    """Serialize with the same Decimal-safe rules as the log lines."""
    return json.dumps(obj, default=_json_default)
