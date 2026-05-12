"""ReconcileAuthorityRefresher 骨架契约测试。

此前 reconcile/ 子模块只共享 test_reconcile_authority_policy.py 覆盖 policy 决策面，
ReconcileAuthorityRefresher 814 行没有独立测试。本文件覆盖：

- AuthoritativeRefreshSummary 的核心计数（market_count / refreshed_markets / refreshed_orderbooks
  / refreshed_fee_rates / user_refresh_enabled / refreshed_balance / refreshed_allowance）；
- 失败兜底（gamma 抛 TimeoutError → AuthoritativeRefreshFailure 收集而不中断流程）；
- condition_ids scope 过滤是否如约只刷指定市场；
- 各依赖缺失时的早退（registry / account_state / clob_client / data_client）；
- 构造期 strategy_id 为空的 ValueError 防呆。

注：测试只断"行为可观察"的外部副作用（gamma 调用了哪些 slug、registry / market_ws 是否被
upsert / track、summary 计数值），不窥探 refresher 私有状态。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.registry import MarketRegistry, MarketRegistrySnapshot
from polymarket_trader.workers.reconcile.authority_refresher import (
    ReconcileAuthorityRefresher,
)


# ============================================================
# Stub clients / DTOs
# ============================================================


class _StubGammaCandidate:
    def __init__(self, market: Market, *, clob_enabled: bool = True) -> None:
        self.condition_id = market.condition_id
        self.market_slug = market.market_slug
        self.clob_enabled = clob_enabled
        self.outcomes = market.outcomes
        self._market = market

    def to_market(self) -> Market:
        return self._market


class _StubGammaClient:
    """记录 list_markets slug 调用，按 slug→Market 返回候选。"""

    def __init__(self, markets_by_slug: dict[str, Market] | None = None) -> None:
        self.markets_by_slug = markets_by_slug or {}
        self.slugs_called: list[str | None] = []

    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[_StubGammaCandidate, ...]:
        self.slugs_called.append(slug)
        if slug is not None and slug in self.markets_by_slug:
            return (_StubGammaCandidate(self.markets_by_slug[slug]),)
        return ()


class _TimeoutGammaClient:
    """模拟 gamma list_markets 永远超时，验证 refresher 不向上抛而是收集 failure。"""

    def __init__(self) -> None:
        self.calls = 0

    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        tag: str | None = None,
        slug: str | None = None,
        limit: int = 100,
        offset: int = 0,
        timeout_s: float | None = None,
    ) -> tuple[Any, ...]:
        self.calls += 1
        # _await_authority 会用 asyncio.wait_for 包装，这里直接抛 TimeoutError 走 timeout 分支。
        raise TimeoutError("simulated gamma timeout")


class _StubOrderbookDTO:
    def __init__(self, snapshot: OrderbookSnapshot) -> None:
        self._snapshot = snapshot

    def to_snapshot(self) -> OrderbookSnapshot:
        return self._snapshot


class _StubBalance:
    def __init__(self, balance: Decimal, allowance: Decimal) -> None:
        self.balance_usdc = balance
        self.allowance_usdc = allowance


class _StubClobClient:
    """带 has_auth_client，可选返回 orderbook / fee_rate / balance。

    用 hasattr 检查（refresher 用 hasattr 判 get_orderbook / get_fee_rate），
    所以未配置时对应方法应是没有定义而不是返回 None。这里通过实例属性删除来模拟。
    """

    has_auth_client = True

    def __init__(
        self,
        *,
        orderbook_by_token: dict[str, OrderbookSnapshot] | None = None,
        fee_rate: int | None = None,
        balance: _StubBalance | None = None,
        open_orders: tuple[Any, ...] = (),
        fills: tuple[Any, ...] = (),
    ) -> None:
        self._orderbook_by_token = orderbook_by_token or {}
        self._fee_rate = fee_rate
        self._balance = balance
        self._open_orders = open_orders
        self._fills = fills
        self.orderbook_calls: list[str] = []
        self.fee_rate_calls: list[str] = []

    async def get_orderbook(
        self,
        token_id: str,
        *,
        market_slug: str | None = None,
        condition_id: str | None = None,
    ) -> _StubOrderbookDTO | None:
        self.orderbook_calls.append(token_id)
        snap = self._orderbook_by_token.get(token_id)
        if snap is None:
            return None
        return _StubOrderbookDTO(snap)

    async def get_fee_rate(self, token_id: str) -> int | None:
        self.fee_rate_calls.append(token_id)
        return self._fee_rate

    async def list_open_orders(self) -> tuple[Any, ...]:
        return self._open_orders

    async def list_fills(self) -> tuple[Any, ...]:
        return self._fills

    async def get_balance_allowance(self) -> _StubBalance | None:
        return self._balance


# ============================================================
# Helpers
# ============================================================


def _market(index: int, *, fee_rate_bps: int | None = None) -> Market:
    market = Market(
        condition_id=f"condition-{index}",
        market_slug=f"market-{index}",
        event_slug=f"market-{index}",
        outcomes=(
            MarketOutcome(token_id=f"token-{index}-yes", outcome="YES"),
            MarketOutcome(token_id=f"token-{index}-no", outcome="NO"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
        taker_base_fee_bps=fee_rate_bps,
    )
    return market


def _orderbook(token_id: str) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        token_id=token_id,
        condition_id=None,
        market_slug=None,
        best_bid=Decimal("0.4"),
        best_ask=Decimal("0.5"),
        bids=(PriceLevel(price=Decimal("0.4"), size=Decimal("10")),),
        asks=(PriceLevel(price=Decimal("0.5"), size=Decimal("10")),),
        received_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        tick_size=Decimal("0.01"),
    )


# ============================================================
# 构造期防呆
# ============================================================


def test_empty_strategy_id_raises_at_construction() -> None:
    """strategy_id="" → ValueError，避免无归属的权威刷新审计。"""
    with pytest.raises(ValueError):
        ReconcileAuthorityRefresher(strategy_id="")


# ============================================================
# refresh() 主路径：summary 计数 + 副作用
# ============================================================


def test_refresh_without_any_dependencies_returns_empty_summary() -> None:
    """无 registry / account_state / 任何 client → market_count=0、user_refresh_enabled=False。"""
    refresher = ReconcileAuthorityRefresher(strategy_id="sports_tail")
    summary = asyncio.run(refresher.refresh(trace_id="trace-empty"))

    assert summary.trace_id == "trace-empty"
    assert summary.market_count == 0
    assert summary.refreshed_markets == 0
    assert summary.refreshed_orderbooks == 0
    assert summary.refreshed_fee_rates == 0
    assert summary.user_refresh_enabled is False
    assert summary.refreshed_balance is False
    assert summary.refreshed_allowance is False
    assert summary.failures == ()


def test_refresh_with_scoped_condition_ids_refreshes_only_target_market() -> None:
    """传入 condition_ids 时只刷指定 market 且无需 account_state 暴露过滤。"""
    target = _market(1)
    other = _market(2)
    refreshed_target = target.with_trading_status(TradingStatus.ELIGIBLE)
    gamma = _StubGammaClient(markets_by_slug={target.market_slug: refreshed_target})
    registry = MarketRegistry()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((target, other)),
        registry=registry,
        gamma_client=gamma,
    )

    summary = asyncio.run(
        refresher.refresh(trace_id="trace-scope", condition_ids=(target.condition_id,))
    )

    # 只查了 target 的 slug，不应碰 other
    assert target.market_slug in gamma.slugs_called
    assert other.market_slug not in gamma.slugs_called
    assert summary.market_count == 1
    assert summary.refreshed_markets == 1


def test_refresh_collects_failure_when_gamma_call_times_out() -> None:
    """gamma 超时 → AuthoritativeRefreshFailure(reason='timeout') 收集，refresh 不向上抛。"""
    market = _market(1)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    gamma = _TimeoutGammaClient()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=gamma,
        authority_call_timeout_s=0.01,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-fail"))

    assert summary.refreshed_markets == 0
    assert gamma.calls >= 1
    failure_reasons = {failure.reason for failure in summary.failures}
    assert "timeout" in failure_reasons


def test_refresh_applies_orderbook_snapshot_when_clob_returns_book() -> None:
    """clob.get_orderbook 返回 DTO → summary.refreshed_orderbooks 计入 token 数。"""
    market = _market(1, fee_rate_bps=10)  # 已有 fee_rate_bps，跳过 fee_rate 拉取
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    orderbook_yes = _orderbook(market.token_ids[0])
    orderbook_no = _orderbook(market.token_ids[1])
    clob = _StubClobClient(
        orderbook_by_token={
            market.token_ids[0]: orderbook_yes,
            market.token_ids[1]: orderbook_no,
        }
    )
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        clob_client=clob,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-book"))

    # 两个 token 都调过 orderbook，summary 计入 2
    assert sorted(clob.orderbook_calls) == sorted(list(market.token_ids))
    assert summary.refreshed_orderbooks == 2
    assert clob.fee_rate_calls == []  # 已有 fee_rate，不应再拉


def test_refresh_pulls_fee_rate_when_market_lacks_fee_metadata() -> None:
    """市场缺少 fee_rate_bps / taker_base_fee_bps → 触发 clob.get_fee_rate 且计 refreshed_fee_rates。"""
    market = _market(1)  # 没有 fee_rate
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    clob = _StubClobClient(fee_rate=25)
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        clob_client=clob,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-fee"))

    assert clob.fee_rate_calls != []
    assert summary.refreshed_fee_rates == 1


def test_refresh_account_returns_disabled_when_no_user_client_configured() -> None:
    """没有 trading / data / clob client → user_refresh_enabled=False，不发请求。"""
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(()),
    )
    summary = asyncio.run(
        refresher.refresh_account(trace_id="trace-no-user", markets=())
    )

    assert summary.user_refresh_enabled is False
    assert summary.refreshed_balance is False
    assert summary.refreshed_allowance is False


def test_refresh_account_persists_balance_into_account_state_store() -> None:
    """clob.get_balance_allowance 返回 → account_state_store.update_balances 调用。

    可观察侧：summary.refreshed_balance / refreshed_allowance 都为 True；
    AccountStateStore.snapshot() 的 balance_usdc / allowance_usdc 被写入。
    """
    account_state = AccountStateStore()
    clob = _StubClobClient(balance=_StubBalance(Decimal("100"), Decimal("50")))
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot(()),
        account_state_store=account_state,
        clob_client=clob,
    )

    summary = asyncio.run(refresher.refresh_account(trace_id="trace-bal", markets=()))

    assert summary.user_refresh_enabled is True
    assert summary.refreshed_balance is True
    assert summary.refreshed_allowance is True
    snapshot = account_state.snapshot()
    assert snapshot.balance_usdc == Decimal("100")
    assert snapshot.allowance_usdc == Decimal("50")


def test_refresh_without_account_state_skips_exposure_filter_and_makes_no_gamma_call() -> None:
    """没有 account_state_store 时 _market_authority_targets 返回空（unscoped 路径）。

    没有 account 暴露视角时，不应贸然刷 registry 中所有 market 的 gamma，
    避免 idle market 浪费授权调用。
    """
    market = _market(1)
    gamma = _StubGammaClient()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        gamma_client=gamma,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-no-state"))

    assert gamma.slugs_called == []
    assert summary.market_count == 1
    assert summary.refreshed_markets == 0


def test_refresh_market_authority_disabled_skips_all_gamma_calls_even_with_exposure() -> None:
    """refresh_market_authority=False 时即便有 exposure 也不发 gamma 请求。

    这条路径是 'discovery 触发的 reconcile' 场景：账户态已经新鲜，不需要再补市场元数据。
    """
    market = _market(1)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )
    gamma = _StubGammaClient()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=gamma,
    )

    summary = asyncio.run(
        refresher.refresh(trace_id="trace-skip", refresh_market_authority=False)
    )

    assert gamma.slugs_called == []
    assert summary.market_count == 1


def test_refresh_collects_failure_when_gamma_raises_general_exception() -> None:
    """gamma list_markets 抛非 TimeoutError 的一般异常 → AuthoritativeRefreshFailure(reason='exception') 收集，
    refresh 不向上抛异常。覆盖 authority_refresher.py 中 except Exception 分支。"""
    market = _market(1)
    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("1"),
            cost_usdc=Decimal("0.5"),
        )
    )

    class _ErrorGammaClient:
        async def list_markets(self, **kwargs) -> tuple:
            raise ValueError("simulated_gamma_error")

    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=_ErrorGammaClient(),
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-general-fail"))

    assert summary.refreshed_markets == 0
    failure_reasons = {failure.reason for failure in summary.failures}
    assert "exception" in failure_reasons
