from __future__ import annotations

import json
import logging
from io import StringIO

from polymarket_trader.logging import clear_log_context, configure_logging, log_context_scope, shutdown_logging


def test_structured_logging_redacts_sensitive_context_and_message() -> None:
    stream = StringIO()
    configure_logging(structured=True, stream=stream, queue_size=16, force=True)
    logger = logging.getLogger("trader.test.logging")
    logger.setLevel(logging.INFO)

    try:
        with log_context_scope(
            trace_id="trace-123",
            nested={
                "api_key": "secret-value",
                "authorization": "Bearer abc123",
            },
        ):
            logger.info(
                "submit token=abcd password=xyz",
                extra={
                    "order_id": "order-1",
                    "payload": {"session_token": "session-secret"},
                },
            )
    finally:
        shutdown_logging()
        clear_log_context()

    payload_lines = [line for line in stream.getvalue().splitlines() if line.strip()]
    assert payload_lines, "expected at least one structured log line"
    payload = json.loads(payload_lines[-1])

    assert payload["trace_id"] == "trace-123"
    assert payload["order_id"] == "order-1"
    assert payload["context"]["nested"]["authorization"] == "[REDACTED]"
    assert payload["context"]["nested"]["api_key"] == "[REDACTED]"
    assert payload["context"]["payload"]["session_token"] == "[REDACTED]"
    assert "secret-value" not in payload_lines[-1]
    assert "abc123" not in payload_lines[-1]
    assert "password=xyz" not in payload["message"]
    assert "[REDACTED]" in payload["message"]
