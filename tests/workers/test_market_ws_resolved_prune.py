"""WS market_resolved 即时 prune 行为约束 (task #85)。

关键不变量:
1. polymarket WS 推 market_resolved → registry.mark_resolved (改 status 到 RESOLVED).
2. 账户无敞口 → 立即 registry.remove_market 触发 prune callback 链.
3. 账户有敞口 (持仓/挂单) → 跳过 prune, 留给 reconcile 走完整结算路径.
4. account_snapshot_provider 抛异常 / 返回 None → 安全降级 (不 prune, 不挂).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.order import Order, OrderSide, OrderStatus, OrderType
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.workers.market_ws.worker import MarketWsWorker


def _make_market(condition_id: str = "cid-1", token_ids: tuple[str, ...] = ("tk-yes", "tk-no")) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=f"slug-{condition_id}",
        event_slug=f"event-{condition_id}",
        outcomes=tuple(
            MarketOutcome(token_id=tid, outcome=("YES" if i == 0 else "NO"))
            for i, tid in enumerate(token_ids)
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _empty_account() -> AccountSnapshot:
    return AccountSnapshot()


def _account_with_position(market: Market) -> AccountSnapshot:
    position = Position(
        strategy_id="test",
        condition_id=market.condition_id,
        token_id=market.outcomes[0].token_id,
        market_slug=market.market_slug,
        shares=Decimal("100"),
        cost_usdc=Decimal("50"),
    )
    return AccountSnapshot(positions=(position,))


def _account_with_open_order(market: Market) -> AccountSnapshot:
    order = Order(
        strategy_id="test",
        order_id="o-1",
        condition_id=market.condition_id,
        token_id=market.outcomes[0].token_id,
        market_slug=market.market_slug,
        side=OrderSide.BUY,
        order_type=OrderType.GTC,
        price=Decimal("0.5"),
        size_shares=Decimal("10"),
        status=OrderStatus.OPEN,
    )
    return AccountSnapshot(open_orders=(order,))


@pytest.fixture
def registry() -> MarketRegistry:
    return MarketRegistry()


def test_resolved_no_exposure_triggers_remove(registry: MarketRegistry) -> None:
    """无敞口 + WS resolved → registry.remove_market 被调用."""
    market = _make_market()
    registry.upsert(market)
    assert registry.get_by_condition_id(market.condition_id) is not None

    prune_callbacks_called: list[tuple[str, tuple[str, ...]]] = []
    registry.register_prune_callback(lambda cid, tokens: prune_callbacks_called.append((cid, tokens)))

    worker = MarketWsWorker(
        registry=registry,
        account_snapshot_provider=_empty_account,
    )
    worker.track_market(market)

    worker._maybe_prune_on_resolved(market)

    # 验证: market 被 remove, prune callback 被触发
    assert registry.get_by_condition_id(market.condition_id) is None
    assert len(prune_callbacks_called) == 1
    assert prune_callbacks_called[0][0] == market.condition_id


def test_resolved_with_position_does_not_prune(registry: MarketRegistry) -> None:
    """有持仓 → 跳过 prune (留给 reconcile)."""
    market = _make_market()
    registry.upsert(market)

    callbacks_called: list[str] = []
    registry.register_prune_callback(lambda cid, _tokens: callbacks_called.append(cid))

    worker = MarketWsWorker(
        registry=registry,
        account_snapshot_provider=lambda: _account_with_position(market),
    )
    worker._maybe_prune_on_resolved(market)

    # 验证: market 仍在 registry, 无 prune callback 触发
    assert registry.get_by_condition_id(market.condition_id) is not None
    assert callbacks_called == []


def test_resolved_with_open_order_does_not_prune(registry: MarketRegistry) -> None:
    """有挂单 → 跳过 prune."""
    market = _make_market()
    registry.upsert(market)

    callbacks_called: list[str] = []
    registry.register_prune_callback(lambda cid, _tokens: callbacks_called.append(cid))

    worker = MarketWsWorker(
        registry=registry,
        account_snapshot_provider=lambda: _account_with_open_order(market),
    )
    worker._maybe_prune_on_resolved(market)

    assert registry.get_by_condition_id(market.condition_id) is not None
    assert callbacks_called == []


def test_resolved_without_provider_skips(registry: MarketRegistry) -> None:
    """无 provider (启动期/测试) → 退化到 reconcile 周期 prune, 不报错."""
    market = _make_market()
    registry.upsert(market)
    worker = MarketWsWorker(registry=registry)  # 无 account_snapshot_provider
    worker._maybe_prune_on_resolved(market)
    # 仍在 registry
    assert registry.get_by_condition_id(market.condition_id) is not None


def test_resolved_provider_returns_none_skips(registry: MarketRegistry) -> None:
    """provider 返回 None → 保守跳过, 不 prune."""
    market = _make_market()
    registry.upsert(market)
    worker = MarketWsWorker(
        registry=registry,
        account_snapshot_provider=lambda: None,
    )
    worker._maybe_prune_on_resolved(market)
    assert registry.get_by_condition_id(market.condition_id) is not None


def test_resolved_provider_raises_is_swallowed(registry: MarketRegistry) -> None:
    """provider 抛异常 → 安全降级, 不挂 worker."""
    market = _make_market()
    registry.upsert(market)

    def _boom():
        raise RuntimeError("simulated")

    worker = MarketWsWorker(
        registry=registry,
        account_snapshot_provider=_boom,
    )
    # 不应抛
    worker._maybe_prune_on_resolved(market)
    # 仍在 registry (安全退化)
    assert registry.get_by_condition_id(market.condition_id) is not None
