"""Structured logging helpers for Lambda handlers."""

import contextvars
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_LOG_CONTEXT: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar(
    "lambda_runtime_pypy_log_context",
    default={},
)


class JsonFormatter(logging.Formatter):
    """Format log records as compact JSON for CloudWatch and downstream sinks."""

    def __init__(self, static_fields: Optional[Dict[str, Any]] = None) -> None:
        super().__init__()
        self.static_fields = static_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(self.static_fields)
        payload.update(_LOG_CONTEXT.get())

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info).splitlines()

        return json.dumps(payload, default=str, separators=(",", ":"))


def set_context(**values: Any) -> None:
    """Add invocation metadata to the current logging context."""
    current = dict(_LOG_CONTEXT.get())
    current.update({key: value for key, value in values.items() if value is not None})
    _LOG_CONTEXT.set(current)


def clear_context() -> None:
    """Clear invocation metadata from the current logging context."""
    _LOG_CONTEXT.set({})


def get_logger(
    name: str = "lambda",
    *,
    level: Optional[str] = None,
    service: Optional[str] = None,
) -> logging.Logger:
    """Return a logger configured for structured Lambda logging."""
    logger = logging.getLogger(name)

    if not any(getattr(handler, "_runtime_helper_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._runtime_helper_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter({"service": service} if service else None))
        logger.addHandler(handler)
        logger.propagate = False
    elif service:
        for handler in logger.handlers:
            if getattr(handler, "_runtime_helper_handler", False):
                formatter = handler.formatter
                if isinstance(formatter, JsonFormatter):
                    formatter.static_fields["service"] = service

    logger.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())

    return logger
