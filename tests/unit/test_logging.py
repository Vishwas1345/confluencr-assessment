import json
import logging
import sys

from app.logging_config import JsonFormatter, redact


def test_redact_removes_connection_strings_and_secrets() -> None:
    value = (
        "connected to postgresql+psycopg://postgres:postgres@localhost:5432/confluencr "
        "password=super-secret token=abc123 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9"
    )

    redacted = redact(value)

    assert "postgres:postgres" not in redacted
    assert "super-secret" not in redacted
    assert "abc123" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "[redacted-connection]" in redacted


def test_json_formatter_includes_structured_event_fields() -> None:
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="webhook_accepted transaction_id=%s",
        args=("txn_123",),
        exc_info=None,
    )
    record.event = "webhook_accepted"
    record.transaction_id = "txn_123"
    record.duration_ms = 12.5

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "webhook_accepted"
    assert payload["transaction_id"] == "txn_123"
    assert payload["duration_ms"] == 12.5
    assert payload["message"] == "webhook_accepted transaction_id=txn_123"
    assert payload["timestamp"].endswith("Z")
    assert payload["level"] == "INFO"


def test_json_formatter_redacts_exception_text() -> None:
    try:
        raise RuntimeError("postgresql://user:password@localhost:5432/db")
    except RuntimeError:
        record = logging.LogRecord(
            name="app.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))

    assert "password@localhost" not in payload["exception"]
    assert "[redacted-connection]" in payload["exception"]
