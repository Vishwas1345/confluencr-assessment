import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

REDACTED = "[redacted]"
REDACTED_CONNECTION = "[redacted-connection]"

_CONNECTION_PATTERN = re.compile(
    r"\b(?:postgresql(?:\+psycopg)?|rediss?|amqps?)://[^\s\"']+",
    re.IGNORECASE,
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")

_RESERVED_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


def redact(value: str) -> str:
    value = _CONNECTION_PATTERN.sub(REDACTED_CONNECTION, value)
    value = _BEARER_PATTERN.sub("Bearer " + REDACTED, value)
    return _SECRET_PATTERN.sub(r"\1=" + REDACTED, value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {_redact_value(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = _redact_value(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, "_confluencr_handler", False):
            root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._confluencr_handler = True
    root.addHandler(handler)
    root.setLevel(level.upper())
