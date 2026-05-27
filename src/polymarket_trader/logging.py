from __future__ import annotations

import json
import logging
import logging.handlers
import queue
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping

from polymarket_trader.domain.events import sanitize_raw_response
from polymarket_trader.observability.trace import current_trace_id
from polymarket_trader.serialization import utc_now

_LOG_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("trader_log_context", default={})
_ACTIVE_RUNTIME: "LoggingRuntime | None" = None

_SENSITIVE_KEY_SUFFIXES = ("_secret", "_password", "_token", "_signature", "_cookie")
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "mnemonic",
    "passphrase",
    "password",
    "private_key",
    "seed",
    "seed_phrase",
    "secret",
    "session_token",
    "access_token",
    "refresh_token",
    "signature",
    "token",
}
_STRUCTURED_FIELDS = (
    "trace_id",
    "event_type",
    "priority",
    "market_slug",
    "condition_id",
    "token_id",
    "order_id",
    "status",
    "reason",
    "latency_ms",
)
_EXCLUDED_RECORD_KEYS = {
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
    "thread",
    "threadName",
    "observability_context",
    "observability_message",
}
_EXCLUDED_RECORD_KEYS.update(_STRUCTURED_FIELDS)


def _normalize_datetime(value: datetime | None = None) -> datetime:
    value = value or utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _sanitize_value(value: Any, *, limit: int = 4096, depth: int = 0) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if depth >= 8:
        return "[REDACTED]"
    if isinstance(value, str):
        return _truncate_text(sanitize_raw_response(value, max_length=limit) or "", limit)
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
        return _truncate_text(sanitize_raw_response(text, max_length=limit) or "", limit)
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = _sanitize_value(item, limit=limit, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_value(item, limit=limit, depth=depth + 1) for item in value]
    if isinstance(value, datetime):
        return _normalize_datetime(value).isoformat()
    return _truncate_text(sanitize_raw_response(str(value), max_length=limit) or "", limit)


def _merge_log_context(fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(_LOG_CONTEXT.get())
    if fields:
        merged.update(fields)
    trace_id = current_trace_id()
    if trace_id and "trace_id" not in merged:
        merged["trace_id"] = trace_id
    return merged


def bind_log_context(**fields: Any) -> dict[str, Any]:
    """Bind structured log context for the current task/thread."""

    merged = _merge_log_context(fields)
    _LOG_CONTEXT.set(merged)
    return merged


def clear_log_context() -> None:
    _LOG_CONTEXT.set({})


@contextmanager
def log_context_scope(**fields: Any) -> Iterator[dict[str, Any]]:
    token = _LOG_CONTEXT.set(_merge_log_context(fields))
    try:
        yield _LOG_CONTEXT.get()
    finally:
        _LOG_CONTEXT.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_merge_log_context())


def _coerce_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    return logging._nameToLevel.get(str(level).upper(), logging.INFO)


class ContextRedactionFilter(logging.Filter):
    """Attach structured context and strip sensitive fields before queueing."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = _merge_log_context()
        for key, value in record.__dict__.items():
            if key in _EXCLUDED_RECORD_KEYS or key.startswith("_"):
                continue
            context[key] = value
        sanitized_context = {
            str(key): _sanitize_value(value) for key, value in context.items() if value is not None
        }
        record.observability_context = sanitized_context
        for field_name in _STRUCTURED_FIELDS:
            if getattr(record, field_name, None) is None and field_name in sanitized_context:
                setattr(record, field_name, sanitized_context[field_name])
        try:
            record.observability_message = _sanitize_value(record.getMessage())
        except Exception:
            record.observability_message = _sanitize_value(record.msg)
        if record.exc_info is not None:
            record.observability_exception = _sanitize_value(
                logging.Formatter().formatException(record.exc_info)
            )
        else:
            record.observability_exception = None
        return True


class StructuredJsonFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__()

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": getattr(record, "observability_message", _sanitize_value(record.getMessage())),
        }
        for field_name in _STRUCTURED_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = _sanitize_value(value)
        context = getattr(record, "observability_context", None)
        if context:
            payload["context"] = context
        if record.exc_info is not None:
            payload["exception"] = getattr(
                record,
                "observability_exception",
                _sanitize_value(self.formatException(record.exc_info)),
            )
        if record.stack_info:
            payload["stack_info"] = _sanitize_value(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


class SafeTextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
        parts = [timestamp, record.levelname, f"[{record.name}]"]
        message = getattr(record, "observability_message", None)
        if message is None:
            try:
                message = _sanitize_value(record.getMessage())
            except Exception:
                message = _sanitize_value(record.msg)
        parts.append(str(message))
        context = getattr(record, "observability_context", None)
        if context:
            parts.append(json.dumps(context, ensure_ascii=False, separators=(",", ":"), default=str))
        if record.exc_info is not None:
            parts.append(
                getattr(
                    record,
                    "observability_exception",
                    _sanitize_value(self.formatException(record.exc_info)),
                )
            )
        return " ".join(str(part) for part in parts if part)


class DroppingQueueHandler(logging.handlers.QueueHandler):
    def __init__(self, log_queue: queue.Queue[logging.LogRecord]) -> None:
        super().__init__(log_queue)
        self.dropped_count = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped_count += 1


@dataclass(slots=True)
class LoggingRuntime:
    queue: queue.Queue[logging.LogRecord]
    queue_handler: DroppingQueueHandler
    listener: logging.handlers.QueueListener
    sink_handler: logging.Handler
    started_at: datetime = field(default_factory=utc_now)

    def shutdown(self) -> None:
        self.listener.stop()

    @property
    def dropped_records(self) -> int:
        return self.queue_handler.dropped_count


def configure_logging(
    level: str | int = "INFO",
    *,
    structured: bool = True,
    queue_size: int = 10_000,
    stream: Any | None = None,
    force: bool = True,
) -> LoggingRuntime:
    """Configure async queue logging with structured redaction."""

    global _ACTIVE_RUNTIME

    if _ACTIVE_RUNTIME is not None:
        previous = _ACTIVE_RUNTIME
        previous.shutdown()
        root_logger = logging.getLogger()
        if previous.queue_handler in root_logger.handlers:
            root_logger.removeHandler(previous.queue_handler)
        previous.sink_handler.close()
        _ACTIVE_RUNTIME = None

    root = logging.getLogger()
    root.setLevel(_coerce_level(level))
    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)

    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=max(1, int(queue_size)))
    sink_handler: logging.Handler = logging.StreamHandler(stream)
    sink_handler.setLevel(_coerce_level(level))
    sink_handler.setFormatter(StructuredJsonFormatter() if structured else SafeTextFormatter())

    queue_handler = DroppingQueueHandler(log_queue)
    queue_handler.setLevel(_coerce_level(level))
    # ContextRedactionFilter 加在 sink_handler (listener 线程跑) 而非 queue_handler
    # (主线程跑). _sanitize_value 递归深度 8 + regex redact 每个 dict value 是 CPU
    # 密集, 主线程跑会阻塞 event loop (实测 lag p50 569ms → 1ms, §7 必须遵守).
    sink_handler.addFilter(ContextRedactionFilter())

    listener = logging.handlers.QueueListener(
        log_queue,
        sink_handler,
        respect_handler_level=True,
    )
    root.addHandler(queue_handler)
    listener.start()

    runtime = LoggingRuntime(
        queue=log_queue,
        queue_handler=queue_handler,
        listener=listener,
        sink_handler=sink_handler,
    )
    _ACTIVE_RUNTIME = runtime
    return runtime


def shutdown_logging() -> None:
    global _ACTIVE_RUNTIME
    if _ACTIVE_RUNTIME is None:
        return
    runtime = _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME.shutdown()
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler is runtime.queue_handler:
            root.removeHandler(handler)
    runtime.sink_handler.close()
    _ACTIVE_RUNTIME = None


__all__ = [
    "ContextRedactionFilter",
    "LoggingRuntime",
    "SafeTextFormatter",
    "StructuredJsonFormatter",
    "bind_log_context",
    "clear_log_context",
    "configure_logging",
    "current_log_context",
    "log_context_scope",
    "shutdown_logging",
]
