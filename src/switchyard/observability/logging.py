"""JSON logs: one line per request, each with its request id."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

REQUEST_LOGGER = "switchyard.request"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            line.update(fields)
        if record.exc_info:
            line["exception"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("switchyard")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def log_request(fields: dict[str, Any]) -> None:
    logging.getLogger(REQUEST_LOGGER).info("request", extra={"fields": fields})
