from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from itertools import count
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

MAX_RAW_RESPONSE_LENGTH = 4096
OUTBOX_RAW_RESPONSE_SUMMARY_LIMIT = 512
_MAX_SENSITIVE_DEPTH = 8
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
_KV_REDACT_PATTERN = re.compile(
    r"(?i)\b("
    r"api[_-]?key|apikey|authorization|bearer|cookie|mnemonic|passphrase|password|"
    r"private[_-]?key|seed(?:[_-]?phrase)?|secret|session[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|signature|token"
    r")\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_REDACT_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-+=/]+")
_UUID_SEQUENCE = count()


class DomainEventType(StrEnum):
    MARKET_DISCOVERED = "market_discovered"
    MARKET_UPDATED = "market_updated"
    MARKET_FILTERED_IN = "market_filtered_in"
    MARKET_FILTERED_OUT = "market_filtered_out"
    ORDERBOOK = "orderbook"
    ORDERBOOK_SNAPSHOT_UPDATED = "orderbook_snapshot_updated"
    ENTRY_SIGNAL_TRIGGERED = "entry_signal_triggered"
    MARKET_RESOLVED_OR_DISABLED = "market_resolved_or_disabled"
    RISK_CHECK_PASSED = "risk_check_passed"
    RISK_CHECK_FAILED = "risk_check_failed"
    ORDER_CREATED = "order_created"
    ORDER_SIGNED = "order_signed"
    ORDER_SUBMITTED = "order_submitted"
    ORDER_REJECTED = "order_rejected"
    ORDER_MATCHED = "order_matched"
    ORDER_NO_FILL = "order_no_fill"
    ORDER_PARTIALLY_FILLED = "order_partially_filled"
    BALANCE_UPDATED = "balance_updated"
    POSITION_UPDATED = "position_updated"
    ORDER_STATE_UPDATED = "order_state_updated"
    FILL_RECORDED = "fill_recorded"
    FOLLOW_UP_ORDER_SUBMITTED = "follow_up_order_submitted"
    ORDER_CANCEL_REQUESTED = "order_cancel_requested"
    ORDER_CANCELLED = "order_cancelled"
    REPLACE_ORDER_SUBMITTED = "replace_order_submitted"
    RECONCILE_STARTED = "reconcile_started"
    RECONCILE_DIFF_DETECTED = "reconcile_diff_detected"
    RECONCILE_APPLIED = "reconcile_applied"
    TRADING_PAUSED_FOR_MARKET = "trading_paused_for_market"
    TRADING_PAUSED = "trading_paused"
    TRADING_RESUMED = "trading_resumed"
    UNEXPECTED_RESTING_ORDER_DETECTED = "unexpected_resting_order_detected"
    TRADE_MINED = "trade_mined"
    TRADE_CONFIRMED = "trade_confirmed"
    DECISION_RECORDED = "decision_recorded"
    SPORTS_LIVE_STATE_RECORDED = "sports_live_state_recorded"
    SPORTS_LIVE_MATCH_GAP_RECORDED = "sports_live_match_gap_recorded"
    ALLOCATION_DECISION_RECORDED = "allocation_decision_recorded"
    RISK_REJECTION_RECORDED = "risk_rejection_recorded"
    MARKET_SETTLED = "market_settled"
    PARAMETER_OVERRIDE_APPLIED = "parameter_override_applied"
    # 操盘读 OrderbookDeltaStore.direction_signal 时落审计,记录窗口内 best bid/ask
    # delta + direction_score。供事后复盘"为什么这一刻判断买/卖压 → 决定加仓/退场"。
    ORDERBOOK_DIRECTION_QUERIED = "orderbook_direction_queried"
    RETRY = "retry"
    SKIPPED = "skipped"
    ERROR = "error"


class OutboxPriority(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P2 = "p2"
    P3 = "p3"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_datetime(value: datetime | str | None) -> datetime:
    value = value or _utc_now()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _utc_now()
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _redact_text(text: str, *, limit: int) -> str:
    redacted = _KV_REDACT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    redacted = _BEARER_REDACT_PATTERN.sub("Bearer [REDACTED]", redacted)
    return _truncate_text(redacted, limit)


def _is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def _sanitize_structure(value: Any, *, depth: int = 0, limit: int) -> Any:
    if value is None:
        return None
    if depth >= _MAX_SENSITIVE_DEPTH:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = _sanitize_structure(item, depth=depth + 1, limit=limit)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_structure(item, depth=depth + 1, limit=limit) for item in value]
    if isinstance(value, bytes):
        return _redact_text(value.decode("utf-8", errors="replace"), limit=limit)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return _redact_text(value, limit=limit)
        return _sanitize_structure(parsed, depth=depth + 1, limit=limit)
    if isinstance(value, (int, float, bool)):
        # 此路径仅处理 sanitize 后的 audit payload（如 raw_response_summary 的解析 JSON），
        # 不参与业务金额/价格判定——业务侧金额已在 domain 入口强制为 Decimal。这里允许
        # float 流过是为了忠实保留外部 API（Polymarket / 行情）原生 JSON number；
        # 在 audit 路径硬转 Decimal 反而会引入"看起来更精确实际不更精确"的伪精度。
        return value
    return _redact_text(str(value), limit=limit)


def sanitize_raw_response(
    raw_response: Any | None,
    *,
    max_length: int = MAX_RAW_RESPONSE_LENGTH,
) -> str | None:
    if raw_response is None:
        return None
    sanitized = _sanitize_structure(raw_response, limit=max_length)
    if sanitized is None:
        return None
    if isinstance(sanitized, str):
        return sanitized
    try:
        text = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    except TypeError:
        text = str(sanitized)
    return _truncate_text(text, max_length)


def _immutable_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not payload:
        return MappingProxyType({})
    sanitized = _sanitize_structure(dict(payload), limit=MAX_RAW_RESPONSE_LENGTH)
    if isinstance(sanitized, Mapping):
        return MappingProxyType(dict(sanitized))
    return MappingProxyType({"value": sanitized})


def _text_or_none(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    # 域内事件只保留稳定字段；外部协议细节进 payload/raw_response，避免契约扩散。
    trace_id: str
    event_type: DomainEventType | str
    event_id: str
    market_slug: str | None = None
    event_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    reason: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    # 队列去重 key：设置后 EventBus._trading_event_key 用此值合并同 key 的待处理事件，
    # 避免每次发布都占新槽位。None 表示使用 event_id（每次唯一）。
    merge_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _normalize_datetime(self.created_at))

    @property
    def name(self) -> str:
        return str(self.event_type)

    @property
    def occurred_at(self) -> datetime:
        return self.created_at


@dataclass(frozen=True, slots=True)
class DomainEvent(EventEnvelope):
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Fill:
    trace_id: str
    event_type: DomainEventType | str = DomainEventType.TRADE_CONFIRMED
    event_id: str = ""
    market_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    reason: str = ""
    created_at: datetime = field(default_factory=_utc_now)
    order_id: str | None = None
    trade_id: str | None = None
    side: str | None = None
    price: Decimal | None = None
    size: Decimal | None = None
    notional_usdc: Decimal | None = None
    status: str = "confirmed"
    confirmed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _normalize_datetime(self.created_at))
        if self.confirmed_at is not None:
            object.__setattr__(self, "confirmed_at", _normalize_datetime(self.confirmed_at))
        if not self.event_id:
            object.__setattr__(self, "event_id", f"fill-{next(_UUID_SEQUENCE):08d}-{uuid4().hex}")


@dataclass(frozen=True, slots=True, init=False)
class AuditEvent:
    trace_id: str
    event_id: str
    event_title: str
    market_slug: str | None = None
    event_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    outcome: str | None = None
    side: str | None = None
    order_type: str | None = None
    price: Any | None = None
    size: Any | None = None
    notional_usdc: Any | None = None
    order_id: str | None = None
    trade_id: str | None = None
    tx_hash: str | None = None
    status: str | None = None
    reason: str | None = None
    raw_response: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __init__(
        self,
        event_title: str | None = None,
        trace_id: str | None = None,
        created_at: datetime | None = None,
        payload: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        raw_response = fields.pop("raw_response", None)
        merged: dict[str, Any] = {}
        if payload:
            merged.update(dict(payload))
        merged.update(fields)

        event_title = event_title or merged.pop("event_title", None)
        if event_title is None:
            raise ValueError("AuditEvent requires event_title")
        if trace_id is None:
            trace_id = merged.pop("trace_id", None)
        if trace_id is None:
            raise ValueError("AuditEvent requires trace_id")
        # 历史调用方可能仍把 strategy_id 塞进 payload/fields——直接吞掉，
        # 避免 dataclass 未声明字段引发 KeyError；后续审计无此字段。
        merged.pop("strategy_id", None)

        created_at = _normalize_datetime(created_at or merged.pop("created_at", None))
        updated_at = merged.pop("updated_at", None) or created_at
        updated_at = _normalize_datetime(updated_at)
        if updated_at < created_at:
            updated_at = created_at

        payload_data = dict(payload) if payload else {}
        payload_data.update(merged)
        event_slug = _text_or_none(merged.pop("event_slug", None))

        event_fields = {
            "trace_id": trace_id,
            "event_id": merged.pop("event_id", None) or uuid4().hex,
            "event_title": str(event_title),
            "market_slug": merged.pop("market_slug", None),
            "event_slug": event_slug,
            "condition_id": merged.pop("condition_id", None),
            "token_id": merged.pop("token_id", None),
            "outcome": merged.pop("outcome", None),
            "side": merged.pop("side", None),
            "order_type": merged.pop("order_type", None),
            "price": merged.pop("price", None),
            "size": merged.pop("size", None),
            "notional_usdc": merged.pop("notional_usdc", None),
            "order_id": merged.pop("order_id", None),
            "trade_id": merged.pop("trade_id", None),
            "tx_hash": merged.pop("tx_hash", None),
            "status": merged.pop("status", None),
            "reason": merged.pop("reason", None),
            "raw_response": sanitize_raw_response(raw_response),
            "created_at": created_at,
            "updated_at": updated_at,
            "payload": _immutable_payload(payload_data),
        }

        for key, value in event_fields.items():
            object.__setattr__(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "event_id": self.event_id,
            "event_title": self.event_title,
            "market_slug": self.market_slug,
            "event_slug": self.event_slug,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "outcome": self.outcome,
            "side": self.side,
            "order_type": self.order_type,
            "price": self.price,
            "size": self.size,
            "notional_usdc": self.notional_usdc,
            "order_id": self.order_id,
            "trade_id": self.trade_id,
            "tx_hash": self.tx_hash,
            "status": self.status,
            "reason": self.reason,
            "raw_response": self.raw_response,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.payload)
        payload.update(self.to_dict())
        return payload

    def with_trace_id(self, trace_id: str) -> "AuditEvent":
        return AuditEvent(**{**self.to_payload(), "trace_id": trace_id})

    def with_updated_at(self, updated_at: datetime | None = None) -> "AuditEvent":
        return AuditEvent(**{**self.to_payload(), "updated_at": _normalize_datetime(updated_at)})

    def with_raw_response(self, raw_response: Any | None) -> "AuditEvent":
        return AuditEvent(**{**self.to_payload(), "raw_response": raw_response})


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    trace_id: str
    event_type: str
    idempotency_key: str
    event_id: str = field(default_factory=lambda: uuid4().hex)
    market_slug: str | None = None
    event_slug: str | None = None
    condition_id: str | None = None
    token_id: str | None = None
    reason: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    priority: int | str = 0
    retry_count: int = 0
    last_error: str | None = None
    raw_response_summary: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        raw_priority = self.priority
        if isinstance(raw_priority, str):
            text = raw_priority.strip().upper()
            if text.startswith("P") and text[1:].isdigit():
                priority = int(text[1:])
            else:
                priority = int(text)
        else:
            priority = raw_priority
        object.__setattr__(self, "priority", int(priority))
        object.__setattr__(self, "created_at", _normalize_datetime(self.created_at))
        if self.raw_response_summary is not None:
            object.__setattr__(
                self,
                "raw_response_summary",
                sanitize_raw_response(
                    self.raw_response_summary,
                    max_length=OUTBOX_RAW_RESPONSE_SUMMARY_LIMIT,
                ),
            )
        object.__setattr__(self, "payload", _immutable_payload(self.payload))
        if not self.event_id:
            object.__setattr__(self, "event_id", uuid4().hex)

    @property
    def is_critical(self) -> bool:
        return int(self.priority) <= 1

    @property
    def merge_key(self) -> str | None:
        if int(self.priority) < 2:
            return None
        parts = [self.event_type, self.market_slug or "", self.condition_id or "", self.token_id or ""]
        if not any(parts[1:]):
            return None
        return "|".join(parts)

    def with_retry(self, *, last_error: str | None = None) -> "OutboxEvent":
        return OutboxEvent(
            trace_id=self.trace_id,
            event_type=self.event_type,
            idempotency_key=self.idempotency_key,
            event_id=self.event_id,
            market_slug=self.market_slug,
            event_slug=self.event_slug,
            condition_id=self.condition_id,
            token_id=self.token_id,
            reason=self.reason,
            created_at=self.created_at,
            priority=self.priority,
            retry_count=self.retry_count + 1,
            last_error=last_error if last_error is not None else self.last_error,
            raw_response_summary=self.raw_response_summary,
            payload=self.payload,
        )

    def with_dead_letter(self, *, last_error: str | None = None) -> "OutboxEvent":
        return self.with_retry(last_error=last_error)
