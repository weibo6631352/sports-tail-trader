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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Mapping
from uuid import uuid4

if TYPE_CHECKING:
    from polymarket_trader.domain.position import Position
    from polymarket_trader.infra.polymarket.schemas import GammaMarketDTO
    from polymarket_trader.runtime.account_state import AccountStateStore

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


GammaMarketByConditionLookup = Callable[[str], Awaitable["GammaMarketDTO | None"]]
PositionsProvider = Callable[[], Iterable["Position"]]
EventBus = Any  # 与 main.RuntimeComponents.event_bus 一致


class SettlementScannerService:
    """周期性结算检测——非交易热路径，调度器驱动。"""

    def __init__(
        self,
        *,
        gamma_market_by_condition: GammaMarketByConditionLookup,
        positions_provider: PositionsProvider,
        event_bus: EventBus,
        account_state_store: "AccountStateStore | None" = None,
        max_markets_per_run: int = 50,
    ) -> None:
        # Gamma ``/markets/{id}`` 用的是 Polymarket 内部 id 而不是 condition_id；
        # 必须用 query 端 ``condition_ids`` 过滤拉单条市场。caller 注入做了这层
        # 转换的 callable，本服务不依赖 GammaClient 的具体形状。
        self._lookup_by_condition = gamma_market_by_condition
        self._positions_provider = positions_provider
        self._event_bus = event_bus
        # 可选 account_state_store：检测到 resolved 时把胜负结果回写到对应 Position，
        # 这样 redeemable / cur_price / settled_zero_value 不再等 Polymarket data
        # API 返回（data API 对老 NEG_RISK 死链经常 redeemable=null），UI 可立即
        # 按官方做法过滤掉价值 0 的"僵尸持仓"。
        self._account_state_store = account_state_store
        self._max_markets_per_run = max(1, max_markets_per_run)
        # 本进程内已经发过 MARKET_SETTLED 事件的 condition_id；重启会清空，
        # 重复发的 event_id 用 settlement:{cid} 由 outbox 兜底去重。
        # 注：不再查 DB audit_events 做幂等——DB 仅审计、不做运行时数据源。
        self._published_settlements: set[str] = set()

    async def run_once(self) -> SettlementScanResult:
        positions = tuple(self._positions_provider() or ())
        condition_ids: list[str] = []
        seen: set[str] = set()
        for position in positions:
            cid = position.condition_id
            if not cid or cid in seen:
                continue
            if position.shares == Decimal("0"):
                continue
            seen.add(cid)
            condition_ids.append(cid)
            if len(condition_ids) >= self._max_markets_per_run:
                break

        skipped = 0
        detected = 0
        failed = 0
        for cid in condition_ids:
            if cid in self._published_settlements:
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
            self._enrich_account_positions(resolved)
            self._published_settlements.add(cid)
            detected += 1
        return SettlementScanResult(
            scanned=len(condition_ids),
            skipped_already_settled=skipped,
            detected=detected,
            failed_lookups=failed,
        )

    async def _lookup_resolution(self, condition_id: str) -> _ResolvedMarket | None:
        payload = await self._lookup_by_condition(condition_id)
        if payload is None:
            return None
        return _resolve_from_gamma_payload(condition_id, payload)

    def _enrich_account_positions(self, resolved: _ResolvedMarket) -> None:
        """把 gamma 结算结果回写到 AccountStateStore 内的对应 Position。"""

        if self._account_state_store is None:
            return
        if resolved.winning_token_id is None:
            return
        snapshot = self._account_state_store.snapshot()
        for pos in snapshot.positions:
            if pos.condition_id != resolved.condition_id:
                continue
            if pos.shares <= Decimal("0"):
                continue
            updated = apply_outcome_to_position(pos, winning_token_id=resolved.winning_token_id)
            if updated is not None:
                self._account_state_store.upsert_position(updated)

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


def apply_outcome_to_position(position: "Position", *, winning_token_id: str) -> "Position | None":
    """根据胜方 token_id 把 position 标成可赎回 + 重算 MTM 字段。

    返回新的 Position（不可变）：
    - 胜方 (token_id == winning_token_id)：redeemable=True、cur_price=1、
      current_value=shares、cash_pnl=current_value-cost。
    - 输方：redeemable=True、cur_price=0、current_value=0、cash_pnl=-cost
      → 由此 Position.settled_zero_value 自动为 True（property 判定的依据）。

    shares <= 0 返回 None（无意义）。供 settlement_scanner（账户态 enrich）
    和 reconcile authority_refresher（拉到 redeemable=True 持仓即时 enrich）
    共用——两路都靠 gamma outcomePrices 派定胜负，逻辑必须一致。
    """

    if position.shares <= Decimal("0"):
        return None
    is_winner = position.token_id == winning_token_id
    cur_price = Decimal("1") if is_winner else Decimal("0")
    current_value = position.shares * cur_price
    cash_pnl: Decimal | None
    percent_pnl: Decimal | None
    if position.cost_usdc > Decimal("0"):
        cash_pnl = current_value - position.cost_usdc
        percent_pnl = cash_pnl / position.cost_usdc * Decimal("100")
    else:
        cash_pnl = None
        percent_pnl = None
    from dataclasses import replace

    return replace(
        position,
        redeemable=True,
        cur_price=cur_price,
        current_value=current_value,
        cash_pnl=cash_pnl,
        percent_pnl=percent_pnl,
    )


def _resolve_from_gamma_payload(condition_id: str, payload: "GammaMarketDTO") -> _ResolvedMarket | None:
    """从 gamma 市场 payload 推断结算结果。

    Gamma 在结算后会把 ``outcomePrices`` 设成 ``["1", "0"]`` / ``["0", "1"]``；
    ``closed`` 也会被置为 True。token_ids 与 outcomes 顺序一致。
    """

    if not isinstance(payload.raw, Mapping):
        return None
    if not payload.closed:
        return None
    outcome_prices = _coerce_outcome_prices(payload.raw)
    if outcome_prices is None:
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    winning_idx = _winning_index(outcome_prices)
    if winning_idx is None:
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    outcomes = payload.outcomes
    if winning_idx >= len(outcomes):
        return _ResolvedMarket(condition_id=condition_id, winning_token_id=None, winning_outcome=None, closed=True)
    outcome = outcomes[winning_idx]
    return _ResolvedMarket(
        condition_id=condition_id,
        winning_token_id=outcome.token_id,
        winning_outcome=outcome.outcome,
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
