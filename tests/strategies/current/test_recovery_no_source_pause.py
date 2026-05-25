from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.extension_api.context import ExtensionContext
from polymarket_trader.extension_api.lifecycle import (
    LifecycleEnvelope,
    LifecycleEvent,
    SubscriptionHandle,
)
from polymarket_trader.extension_api.ports import ExtensionPorts
from strategies.current.config import CurrentStrategyConfig
from strategies.current.identity import STRATEGY_ID
from strategies.current.strategy import CurrentStrategy

# 固定 envelope occurred_at；recovery 逻辑不读 occurred_at，但稳定值便于回放。
_FIXED_NOW = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FakeLifecycleBus:
    """记录订阅回调，并允许测试手动触发 envelope。"""

    subscribers: dict[LifecycleEvent, list] = None

    def __post_init__(self) -> None:
        self.subscribers = {}

    def subscribe(self, event, callback) -> SubscriptionHandle:
        self.subscribers.setdefault(event, []).append(callback)
        return SubscriptionHandle(subscription_id=len(self.subscribers[event]))

    def unsubscribe(self, handle) -> None:
        pass

    def publish_now(self, event: LifecycleEvent, payload: Mapping[str, Any]) -> None:
        for cb in self.subscribers.get(event, ()):
            result = cb(LifecycleEnvelope(event=event, occurred_at=_FIXED_NOW, payload=payload))
            if asyncio.iscoroutine(result):
                asyncio.get_event_loop().run_until_complete(result)


def _single_game_market() -> Market:
    return Market(
        condition_id="nba-game-1",
        market_slug="nba-bos-vs-nyk-moneyline-2026-04-29",
        market_question="Celtics vs Knicks moneyline",
        event_title="Celtics vs Knicks",
        event_slug="nba-bos-vs-nyk-2026-04-29",
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="bos", outcome="Celtics"),
            MarketOutcome(token_id="nyk", outcome="Knicks"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _context(market: Market) -> ExtensionContext:
    return ExtensionContext(
        trace_id="t",
        strategy_id=STRATEGY_ID,
        market=market,
        now=datetime(2026, 5, 11, tzinfo=timezone.utc),
        quant_trigger_kind="reconcile_cycle",
    )


def test_recovery_pauses_single_game_when_no_feasible_source_published() -> None:
    bus = _FakeLifecycleBus()
    strategy = CurrentStrategy(
        config=CurrentStrategyConfig(),
        ports=ExtensionPorts(lifecycle=bus),
    )
    market = _single_game_market()

    # 先确认没有暂停信号时不会主动停。
    decision = strategy.quant_decide(_context(market))
    assert decision.pause_trading is False

    asyncio.run(_fire(bus, LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE, {
        "observed_at": "2026-05-11T12:00:00+00:00",
        "source_statuses": [
            {"source": "espn", "health": "failed"},
            {"source": "sofascore", "health": "failed"},
        ],
    }))

    decision = strategy.quant_decide(_context(market))
    assert decision.pause_trading is True
    assert decision.pause_reason == "sports_live_state_no_source"


def test_recovery_resumes_when_any_source_recovers() -> None:
    bus = _FakeLifecycleBus()
    strategy = CurrentStrategy(
        config=CurrentStrategyConfig(),
        ports=ExtensionPorts(lifecycle=bus),
    )
    market = _single_game_market()

    asyncio.run(_fire(bus, LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE, {
        "observed_at": "2026-05-11T12:00:00+00:00",
        "source_statuses": [{"source": "espn", "health": "failed"}],
    }))
    assert strategy.quant_decide(_context(market)).pause_trading is True

    # 同事件名再次发布，但这次至少一个源恢复 → 清掉暂停标志。
    asyncio.run(_fire(bus, LifecycleEvent.LIVE_STATE_NO_FEASIBLE_SOURCE, {
        "observed_at": "2026-05-11T12:05:00+00:00",
        "source_statuses": [
            {"source": "espn", "health": "failed"},
            {"source": "sofascore", "health": "success_with_live_data"},
        ],
    }))
    assert strategy.quant_decide(_context(market)).pause_trading is False


async def _fire(bus: _FakeLifecycleBus, event: LifecycleEvent, payload: dict) -> None:
    envelope = LifecycleEnvelope(event=event, occurred_at=_FIXED_NOW, payload=payload)
    for cb in bus.subscribers.get(event, ()):
        await cb(envelope)
