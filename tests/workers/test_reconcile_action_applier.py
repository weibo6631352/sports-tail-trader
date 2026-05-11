"""ReconcileActionApplier 骨架契约测试。

此前 workers/reconcile/ 三个子模块共用 test_reconcile_authority_policy.py 一个测试文件，
ReconcileActionApplier 没有独立测试。本文件覆盖 6 类 action_type 的 dispatch + cancel
状态修正路径 + 类型保护 + service 必要性。

submit / replace 路径状态修正逻辑复杂（涉及 AccountStateProjector + position open/pending
shares 重算 + replace 时机敏感的 order id 切换），后续可以独立扩充。
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.app.reconcile_service import ReconcileAction, ReconcileActionType
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    OrderResult,
    OrderResultStatus,
    OrderSide,
    OrderType,
)
from polymarket_trader.domain.position import Position
from polymarket_trader.workers.reconcile.action_applier import ReconcileActionApplier


class _StubAccountStateStore:
    """记录 resume_market / remove_order / upsert_position 调用。"""

    def __init__(self) -> None:
        self.resumed_markets: list[str] = []
        self.removed_orders: list[str] = []
        self.upserted_positions: list[Position] = []

    def resume_market(self, condition_id: str) -> None:
        self.resumed_markets.append(condition_id)

    def remove_order(self, order_id: str) -> None:
        self.removed_orders.append(order_id)

    def upsert_position(self, position: Position) -> None:
        self.upserted_positions.append(position)


class _StubReview:
    def __init__(self, *, submitted: bool, order_result: OrderResult | None) -> None:
        self.submitted = submitted
        self.order_result = order_result


class _StubTradingService:
    def __init__(self, *, cancel_status: OrderResultStatus = OrderResultStatus.CANCELLED) -> None:
        self.cancel_status = cancel_status
        self.cancel_calls: list[CancelOrderIntent] = []

    async def cancel(self, intent: CancelOrderIntent) -> _StubReview:
        self.cancel_calls.append(intent)
        result = OrderResult(
            strategy_id="sports_tail",
            trace_id=intent.trace_id,
            condition_id=intent.condition_id,
            token_id=intent.token_id,
            status=self.cancel_status,
            order_id=intent.order_id,
            side=OrderSide.BUY,
            order_type=OrderType.GTC,
            price=Decimal("0.5"),
        )
        return _StubReview(submitted=True, order_result=result)


def _cancel_intent() -> CancelOrderIntent:
    return CancelOrderIntent(
        strategy_id="sports_tail",
        trace_id="trace-recon-1",
        condition_id="cond-1",
        token_id="tok-1",
        order_id="exchange-order-id-7",
        reason="reconcile_cancel_open_buy",
    )


def _cancel_action(intent: CancelOrderIntent | None) -> ReconcileAction:
    return ReconcileAction(
        action_type=ReconcileActionType.CANCEL_ORDER,
        trace_id="trace-recon-1",
        condition_id="cond-1",
        token_id="tok-1",
        market_slug="slug-1",
        reason="reconcile_cancel_open_buy",
        source_order_id="exchange-order-id-7",
        source_order_side=OrderSide.BUY,
        target_size_shares=Decimal("0"),
        intent=intent,
    )


def _empty_account() -> AccountSnapshot:
    return AccountSnapshot(positions=(), allow_new_entries=True)


def _account_with_position(*, condition_id: str = "cond-1", token_id: str = "tok-1") -> AccountSnapshot:
    return AccountSnapshot(
        positions=(
            Position(
                strategy_id="sports_tail",
                condition_id=condition_id,
                token_id=token_id,
                shares=Decimal("3"),
                cost_usdc=Decimal("1.50"),
                open_buy_shares=Decimal("2"),
                pending_buy_shares=Decimal("2"),
            ),
        ),
        allow_new_entries=True,
    )


def _market() -> Any:
    """ReconcileActionApplier.apply 接 Market，但 CANCEL/RESUME/PAUSE 路径不消费它。
    占位的 _SimpleNamespace 即可——只需要存在不被 None check 误判。"""
    from types import SimpleNamespace

    return SimpleNamespace(condition_id="cond-1", market_slug="slug-1")


# ============================================================
# Dispatch：6 类 action 各自路由到正确分支
# ============================================================

def test_pause_trading_action_is_noop() -> None:
    """PAUSE_TRADING action 不触发任何 service / store 调用——pause 通过 supervisor 路径。"""
    store = _StubAccountStateStore()
    service = _StubTradingService()
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=service,  # type: ignore[arg-type]
        account_state_store=store,
    )
    action = ReconcileAction(
        action_type=ReconcileActionType.PAUSE_TRADING,
        trace_id="t",
        condition_id="cond-1",
        token_id=None,
        market_slug="slug-1",
        reason="r",
    )
    asyncio.run(applier.apply(action, _market(), _empty_account()))
    assert service.cancel_calls == []
    assert store.removed_orders == []
    assert store.resumed_markets == []


def test_resume_trading_action_calls_account_state_resume_market() -> None:
    store = _StubAccountStateStore()
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=None,
        account_state_store=store,
    )
    action = ReconcileAction(
        action_type=ReconcileActionType.RESUME_TRADING,
        trace_id="t",
        condition_id="cond-1",
        token_id=None,
        market_slug="slug-1",
        reason="r",
    )
    asyncio.run(applier.apply(action, _market(), _empty_account()))
    assert store.resumed_markets == ["cond-1"]


def test_cancel_action_dispatches_to_trading_service_cancel() -> None:
    store = _StubAccountStateStore()
    service = _StubTradingService(cancel_status=OrderResultStatus.CANCELLED)
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=service,  # type: ignore[arg-type]
        account_state_store=store,
    )
    asyncio.run(applier.apply(_cancel_action(_cancel_intent()), _market(), _empty_account()))
    assert len(service.cancel_calls) == 1
    assert service.cancel_calls[0].order_id == "exchange-order-id-7"


def test_cancel_success_clears_open_buy_shares_in_account_state() -> None:
    """CANCEL 成功 + source_order_side=BUY → remove_order + upsert_position(open_buy_shares=0)。"""
    store = _StubAccountStateStore()
    service = _StubTradingService(cancel_status=OrderResultStatus.CANCELLED)
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=service,  # type: ignore[arg-type]
        account_state_store=store,
    )
    snapshot = _account_with_position()
    asyncio.run(applier.apply(_cancel_action(_cancel_intent()), _market(), snapshot))
    assert store.removed_orders == ["exchange-order-id-7"]
    # cancel 后 position 的 open_buy_shares 和 pending_buy_shares 都应清零
    assert len(store.upserted_positions) == 1
    updated = store.upserted_positions[0]
    assert updated.open_buy_shares == Decimal("0")
    assert updated.pending_buy_shares == Decimal("0")


def test_cancel_skips_state_update_when_executor_rejects() -> None:
    """CANCEL 但 executor 返回 REJECTED → 不删 order，也不动 position（保留旧状态等下一轮）。"""
    store = _StubAccountStateStore()
    service = _StubTradingService(cancel_status=OrderResultStatus.REJECTED)
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=service,  # type: ignore[arg-type]
        account_state_store=store,
    )
    snapshot = _account_with_position()
    asyncio.run(applier.apply(_cancel_action(_cancel_intent()), _market(), snapshot))
    assert store.removed_orders == []
    assert store.upserted_positions == []


# ============================================================
# 类型保护 + 依赖必要性
# ============================================================

def test_cancel_with_wrong_intent_type_raises_typeerror() -> None:
    """CANCEL action 但 intent 是 BuyOrderIntent → TypeError 防误用。"""
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=_StubTradingService(),  # type: ignore[arg-type]
        account_state_store=_StubAccountStateStore(),
    )
    wrong_intent = BuyOrderIntent(
        strategy_id="sports_tail",
        trace_id="t",
        condition_id="cond-1",
        token_id="tok-1",
        price=Decimal("0.5"),
        amount_usdc=Decimal("3"),
    )
    action = _cancel_action(wrong_intent)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        asyncio.run(applier.apply(action, _market(), _empty_account()))


def test_cancel_without_trading_service_raises_runtimeerror() -> None:
    """trading_service=None 时执行 CANCEL → RuntimeError，防止静默失败让 reconcile 误以为对齐。"""
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=None,
        account_state_store=_StubAccountStateStore(),
    )
    with pytest.raises(RuntimeError, match="trading_service"):
        asyncio.run(applier.apply(_cancel_action(_cancel_intent()), _market(), _empty_account()))


def test_unsupported_action_type_raises_runtimeerror() -> None:
    """添加新 action_type 但忘记加 dispatch 分支 → RuntimeError 强制失败而非静默跳过。"""
    applier = ReconcileActionApplier(
        strategy_id="sports_tail",
        trading_service=_StubTradingService(),  # type: ignore[arg-type]
        account_state_store=_StubAccountStateStore(),
    )

    class _FakeActionType:
        value = "unsupported_fake"

    action = ReconcileAction(
        action_type=_FakeActionType(),  # type: ignore[arg-type]
        trace_id="t",
        condition_id="cond-1",
        token_id=None,
        market_slug="slug-1",
        reason="r",
    )
    with pytest.raises(RuntimeError, match="unsupported reconcile action"):
        asyncio.run(applier.apply(action, _market(), _empty_account()))


def test_empty_strategy_id_raises_at_construction() -> None:
    """strategy_id="" → ValueError，避免无归属审计记录。"""
    with pytest.raises(ValueError):
        ReconcileActionApplier(
            strategy_id="",
            trading_service=None,
            account_state_store=None,
        )
