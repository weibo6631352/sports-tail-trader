"""``SettlementScannerService`` 与 ``_resolve_from_gamma_payload`` 行为。

覆盖：
- 仓位为空时 scan 数为 0
- shares=0 的仓位被跳过
- 已经有 audit ``market_settled`` 事件的 condition_id 被跳过
- ``closed=True`` + ``outcomePrices=[1, 0]`` 推断出 winner=token0
- ``closed=False`` 不发事件
- gamma 抓取失败被 catch
- ``outcomePrices`` JSON-string 形式也能解析
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from polymarket_trader.app.settlement_scanner import (
    SettlementScannerService,
    _resolve_from_gamma_payload,
)


@dataclass
class _StubPosition:
    condition_id: str
    shares: Decimal


@dataclass
class _StubOutcome:
    token_id: str
    outcome: str


class _StubGammaPayload:
    def __init__(
        self,
        raw: dict[str, Any],
        outcomes: tuple[_StubOutcome, ...],
        closed: bool,
    ) -> None:
        self.raw = raw
        self.outcomes = outcomes
        self.closed = closed


class _SpyEventBus:
    def __init__(self) -> None:
        self.published: list[Any] = []

    async def publish(self, priority: Any, event: Any) -> None:
        self.published.append(event)


def test_resolve_payload_with_outcome_prices_picks_winner() -> None:
    payload = _StubGammaPayload(
        raw={"outcomePrices": ["1", "0"]},
        outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
        closed=True,
    )
    resolved = _resolve_from_gamma_payload("c1", payload)
    assert resolved is not None
    assert resolved.winning_token_id == "tok-yes"
    assert resolved.winning_outcome == "Yes"
    assert resolved.closed is True


def test_resolve_payload_with_stringified_json_prices() -> None:
    payload = _StubGammaPayload(
        raw={"outcomePrices": '["0", "1"]'},
        outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
        closed=True,
    )
    resolved = _resolve_from_gamma_payload("c1", payload)
    assert resolved is not None
    assert resolved.winning_token_id == "tok-no"


def test_resolve_payload_not_closed_returns_none() -> None:
    payload = _StubGammaPayload(
        raw={"outcomePrices": ["1", "0"]},
        outcomes=(),
        closed=False,
    )
    assert _resolve_from_gamma_payload("c1", payload) is None


def test_resolve_payload_closed_but_missing_prices_returns_closed_record() -> None:
    payload = _StubGammaPayload(raw={}, outcomes=(), closed=True)
    resolved = _resolve_from_gamma_payload("c1", payload)
    assert resolved is not None
    assert resolved.closed is True
    assert resolved.winning_token_id is None


def test_scanner_skips_positions_with_zero_shares() -> None:
    bus = _SpyEventBus()

    async def _noop_gamma(cid: str) -> None:
        return None

    service = SettlementScannerService(
        gamma_market_by_condition=_noop_gamma,
        positions_provider=lambda: (_StubPosition("c1", Decimal("0")),),
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.scanned == 0
    assert result.detected == 0
    assert bus.published == []


def test_scanner_dedupes_within_session_via_in_memory_set() -> None:
    """同一 service 实例的第二次扫描不再发同一 cid 的 MARKET_SETTLED。

    §3 强化：不查 DB audit_events 做幂等，靠内存 set + outbox event_id 兜底。
    """
    bus = _SpyEventBus()
    fetch_count = 0

    async def _fetch(cid: str) -> Any:
        nonlocal fetch_count
        fetch_count += 1
        return _StubGammaPayload(
            raw={"outcomePrices": ["1", "0"]},
            outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
            closed=True,
        )

    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        event_bus=bus,
    )
    first = asyncio.run(service.run_once())
    second = asyncio.run(service.run_once())
    assert first.detected == 1
    assert len(bus.published) == 1
    assert second.detected == 0
    assert second.skipped_already_settled == 1
    assert len(bus.published) == 1  # 第二轮不再 publish
    assert fetch_count == 1  # 第二轮也不再 gamma lookup


def test_scanner_emits_settlement_event_for_resolved_market() -> None:
    bus = _SpyEventBus()
    payload = _StubGammaPayload(
        raw={"outcomePrices": ["1", "0"]},
        outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
        closed=True,
    )

    async def _fetch(cid: str) -> Any:
        return payload

    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.detected == 1
    assert len(bus.published) == 1
    event = bus.published[0]
    assert event.event_type.value == "market_settled"
    assert event.payload["winning_token_id"] == "tok-yes"
    assert event.event_id == "settlement:c1"  # 幂等键


def test_scanner_silent_on_fetch_failure() -> None:
    bus = _SpyEventBus()

    async def _fetch(cid: str) -> Any:
        raise RuntimeError("upstream down")

    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.failed_lookups == 1
    assert result.detected == 0
    assert bus.published == []


def test_scanner_enriches_winning_position_with_redeemable_and_price_one() -> None:
    """检测到 resolved → AccountStateStore 中胜方 Position 被标 redeemable=True / cur_price=1。"""

    from polymarket_trader.domain.position import Position
    from polymarket_trader.runtime.account_state import AccountStateStore

    account_state = AccountStateStore()
    winner = Position(
        strategy_id="sports_tail",
        condition_id="c-win",
        token_id="tok-yes",
        shares=Decimal("10"),
        cost_usdc=Decimal("4"),
    )
    account_state.upsert_position(winner)
    bus = _SpyEventBus()
    payload = _StubGammaPayload(
        raw={"outcomePrices": ["1", "0"]},
        outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
        closed=True,
    )

    async def _fetch(cid: str) -> Any:
        return payload

    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: (_StubPosition("c-win", Decimal("10")),),
        event_bus=bus,
        account_state_store=account_state,
    )
    asyncio.run(service.run_once())

    enriched = next(
        p for p in account_state.snapshot().positions if p.condition_id == "c-win"
    )
    assert enriched.redeemable is True
    assert enriched.cur_price == Decimal("1")
    assert enriched.current_value == Decimal("10")
    assert enriched.cash_pnl == Decimal("6")  # 10 - 4
    assert enriched.settled_zero_value is False  # winner 有价值


def test_scanner_enriches_losing_position_to_settled_zero_value() -> None:
    """输方 Position 被标 redeemable=True / cur_price=0 → settled_zero_value=True。"""

    from polymarket_trader.domain.position import Position
    from polymarket_trader.runtime.account_state import AccountStateStore

    account_state = AccountStateStore()
    loser = Position(
        strategy_id="sports_tail",
        condition_id="c-lose",
        token_id="tok-no",
        shares=Decimal("11232"),
        cost_usdc=Decimal("11"),
    )
    account_state.upsert_position(loser)
    payload = _StubGammaPayload(
        raw={"outcomePrices": ["1", "0"]},  # yes 赢，我们持的是 no
        outcomes=(_StubOutcome("tok-yes", "Yes"), _StubOutcome("tok-no", "No")),
        closed=True,
    )

    async def _fetch(cid: str) -> Any:
        return payload

    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: (_StubPosition("c-lose", Decimal("11232")),),
        event_bus=_SpyEventBus(),
        account_state_store=account_state,
    )
    asyncio.run(service.run_once())

    enriched = next(
        p for p in account_state.snapshot().positions if p.condition_id == "c-lose"
    )
    assert enriched.redeemable is True
    assert enriched.cur_price == Decimal("0")
    assert enriched.current_value == Decimal("0")
    assert enriched.cash_pnl == Decimal("-11")
    assert enriched.settled_zero_value is True


def test_scanner_dedupes_condition_ids_across_multiple_token_positions() -> None:
    bus = _SpyEventBus()
    fetch_count = 0

    async def _fetch(cid: str) -> Any:
        nonlocal fetch_count
        fetch_count += 1
        return _StubGammaPayload(raw={}, outcomes=(), closed=False)

    positions = (
        _StubPosition("c1", Decimal("10")),
        _StubPosition("c1", Decimal("5")),  # 同 condition_id 不同 token
        _StubPosition("c2", Decimal("3")),
    )
    service = SettlementScannerService(
        gamma_market_by_condition=_fetch,
        positions_provider=lambda: positions,
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.scanned == 2  # c1 / c2
    assert fetch_count == 2
