"""定期扫描已结算市场，发 ``MARKET_SETTLED`` 事件给 calibration 端点。

工作方式：
1. 对仓位表里所有 ``shares > 0`` 的市场（含已 redeemable 的），按 ``condition_id``
   去重；
2. 跳过那些 audit_events 已经有 ``market_settled`` 事件的 condition_id，避免
   重复发；
3. 通过 ``GammaClient.get_market(condition_id)`` 拉最新市场状态——``closed``
   或 ``outcomePrices=[1, 0]/[0, 1]`` 视为已结算，winning_token_id 从
   ``outcomePrices`` 推断；
4. 把结果作为 ``DomainEvent(MARKET_SETTLED)`` 投到 outbox，由 persistence
   worker 落 audit_events。

这条链路 P3 优先级——失败、超时、抓不到 outcome 一律静默，下一轮再试。
目的是把 calibration / Brier score 从"需要运维手工 POST"升级成"自动跟踪"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

from polymarket_trader.domain.events import (
    DomainEvent,
    DomainEventType,
    OutboxPriority,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettlementScanResult:
    scanned: int
    skipped_already_settled: int
    detected: int
    failed_lookups: int


@dataclass(frozen=True, slots=True)
class _ResolvedMarket:
    condition_id: str
    winning_token_id: str | None
    winning_outcome: str | None
    closed: bool


GammaMarketFetcher = Callable[[str], Awaitable[Any]]
PositionsProvider = Callable[[], Iterable[Any]]
AuditEventsQuery = Callable[..., Awaitable[Any]]
EventBus = Any  # 与 main.RuntimeComponents.event_bus 一致


class SettlementScannerService:
    """周期性结算检测——非交易热路径，调度器驱动。"""

    def __init__(
        self,
        *,
        gamma_market_fetcher: GammaMarketFetcher,
        positions_provider: PositionsProvider,
        audit_events_query: AuditEventsQuery,
        event_bus: EventBus,
        max_markets_per_run: int = 50,
    ) -> None:
        self._fetch_market = gamma_market_fetcher
        self._positions_provider = positions_provider
        self._audit_events_query = audit_events_query
        self._event_bus = event_bus
        self._max_markets_per_run = max(1, max_markets_per_run)

    async def run_once(self) -> SettlementScanResult:
        positions = tuple(self._positions_provider() or ())
        condition_ids: list[str] = []
        seen: set[str] = set()
        for position in positions:
            cid = getattr(position, "condition_id", None)
            if not cid or cid in seen:
                continue
            shares = getattr(position, "shares", None)
            if shares is None or shares == Decimal("0"):
                continue
            seen.add(cid)
            condition_ids.append(cid)
            if len(condition_ids) >= self._max_markets_per_run:
                break

        skipped = 0
        detected = 0
        failed = 0
        for cid in condition_ids:
            try:
                already = await self._has_settlement_event(cid)
            except Exception:
                logger.warning(
                    "settlement_scanner.audit_query_failed",
                    extra={"condition_id": cid},
                    exc_info=True,
                )
                failed += 1
                continue
            if already:
                skipped += 1
                continue
            try:
                resolved = await self._lookup_resolution(cid)
            except Exception:
                logger.info(
                    "settlement_scanner.gamma_lookup_failed",
                    extra={"condition_id": cid},
                    exc_info=True,
                )
                failed += 1
                continue
            if resolved is None or not resolved.closed:
                continue
            await self._publish_settlement(resolved)
            detected += 1
        return SettlementScanResult(
            scanned=len(condition_ids),
            skipped_already_settled=skipped,
            detected=detected,
            failed_lookups=failed,
        )

    async def _has_settlement_event(self, condition_id: str) -> bool:
        page = await self._audit_events_query(
            limit=1,
            offset=0,
            event_title=DomainEventType.MARKET_SETTLED.value,
            condition_id=condition_id,
        )
        items = page.items if hasattr(page, "items") else page.get("items", ())
        return bool(items)

    async def _lookup_resolution(self, condition_id: str) -> _ResolvedMarket | None:
        payload = await self._fetch_market(condition_id)
        if payload is None:
            return None
        return _resolve_from_gamma_payload(condition_id, payload)

    async def _publish_settlement(self, resolved: _ResolvedMarket) -> None:
        try:
            await self._event_bus.publish(
                OutboxPriority.P3,
                DomainEvent(
                    trace_id=f"settlement-scan-{uuid4().hex}",
                    event_type=DomainEventType.MARKET_SETTLED,
                    event_id=f"settlement:{resolved.condition_id}",
                    condition_id=resolved.condition_id,
                    token_id=resolved.winning_token_id,
                    reason="auto_settlement_detected",
                    payload={
                        "winning_token_id": resolved.winning_token_id,
                        "winning_outcome": resolved.winning_outcome,
                        "settled_at": datetime.now(timezone.utc).isoformat(),
                        "source": "gamma_scanner",
                        "operator": "scheduler",
                    },
                ),
            )
        except Exception:
            logger.info(
                "settlement_scanner.publish_failed",
                extra={"condition_id": resolved.condition_id},
                exc_info=True,
            )


def _resolve_from_gamma_payload(condition_id: str, payload: Any) -> _ResolvedMarket | None:
    """从 gamma 市场 payload 推断结算结果。

    Gamma 在结算后会把 ``outcomePrices`` 设成 ``["1", "0"]`` / ``["0", "1"]``；
    ``closed`` 也会被置为 True。token_ids 与 outcomes 顺序一致。
    """

    raw = getattr(payload, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    closed = bool(getattr(payload, "closed", False))
    if not closed:
        return None
    outcome_prices = _coerce_outcome_prices(raw)
    if outcome_prices is None:
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    winning_idx = _winning_index(outcome_prices)
    if winning_idx is None:
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    outcomes = getattr(payload, "outcomes", ())
    if winning_idx >= len(outcomes):
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    outcome = outcomes[winning_idx]
    return _ResolvedMarket(
        condition_id=condition_id,
        winning_token_id=getattr(outcome, "token_id", None),
        winning_outcome=getattr(outcome, "outcome", None),
        closed=True,
    )


def _coerce_outcome_prices(raw: Mapping[str, Any]) -> tuple[Decimal, ...] | None:
    raw_prices = raw.get("outcomePrices") or raw.get("outcome_prices")
    if raw_prices is None:
        return None
    if isinstance(raw_prices, str):
        # Gamma 偶尔返回 stringified JSON list "[\"1\", \"0\"]"
        import json

        try:
            raw_prices = json.loads(raw_prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw_prices, (list, tuple)):
        return None
    parsed: list[Decimal] = []
    for value in raw_prices:
        try:
            parsed.append(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            return None
    return tuple(parsed)


def _winning_index(prices: tuple[Decimal, ...]) -> int | None:
    """选 outcomePrice ≈ 1 的那个；至少 0.9 才视为明确胜者。"""

    for idx, price in enumerate(prices):
        if price >= Decimal("0.9"):
            return idx
    return None
