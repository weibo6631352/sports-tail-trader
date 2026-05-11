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


@dataclass
class _StubPage:
    items: tuple[Any, ...]


async def _empty_audit_query(**kwargs: Any) -> _StubPage:
    return _StubPage(items=())


async def _seeded_audit_query(condition_ids: set[str], **kwargs: Any) -> _StubPage:
    cid = kwargs.get("condition_id")
    if cid in condition_ids:
        return _StubPage(items=("placeholder",))
    return _StubPage(items=())


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
    service = SettlementScannerService(
        gamma_market_fetcher=lambda cid: asyncio.sleep(0),
        positions_provider=lambda: (_StubPosition("c1", Decimal("0")),),
        audit_events_query=_empty_audit_query,
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.scanned == 0
    assert result.detected == 0
    assert bus.published == []


def test_scanner_skips_already_settled() -> None:
    bus = _SpyEventBus()
    settled = {"c1"}

    async def _audit(**kwargs: Any) -> _StubPage:
        return await _seeded_audit_query(settled, **kwargs)

    async def _fetch_should_not_be_called(cid: str) -> Any:  # pragma: no cover
        raise AssertionError("should not fetch already-settled market")

    service = SettlementScannerService(
        gamma_market_fetcher=_fetch_should_not_be_called,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        audit_events_query=_audit,
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.scanned == 1
    assert result.skipped_already_settled == 1
    assert result.detected == 0
    assert bus.published == []


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
        gamma_market_fetcher=_fetch,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        audit_events_query=_empty_audit_query,
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
        gamma_market_fetcher=_fetch,
        positions_provider=lambda: (_StubPosition("c1", Decimal("10")),),
        audit_events_query=_empty_audit_query,
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.failed_lookups == 1
    assert result.detected == 0
    assert bus.published == []


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
        gamma_market_fetcher=_fetch,
        positions_provider=lambda: positions,
        audit_events_query=_empty_audit_query,
        event_bus=bus,
    )
    result = asyncio.run(service.run_once())
    assert result.scanned == 2  # c1 / c2
    assert fetch_count == 2
