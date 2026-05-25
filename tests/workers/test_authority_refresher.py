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


def test_refresh_never_fetches_orderbook_rest_ws_is_sole_source_of_truth() -> None:
    """reconcile 不再拉 /book REST——盘口由 market_ws push 维护，
    refreshed_orderbooks 永远 0。WS 漏推由 worker.py sequence_gap repair 兜底。"""
    market = _market(1, fee_rate_bps=10)
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
    clob = _StubClobClient(
        orderbook_by_token={
            market.token_ids[0]: _orderbook(market.token_ids[0]),
            market.token_ids[1]: _orderbook(market.token_ids[1]),
        }
    )
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        clob_client=clob,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-no-book"))

    # 不再调 /book，refreshed_orderbooks 始终为 0
    assert clob.orderbook_calls == []
    assert summary.refreshed_orderbooks == 0


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


# ============================================================
# 死链回收：auto_quarantine_dead_market + 无敞口 → 从 registry remove
# ============================================================


def test_refresh_skips_inline_account_fetch_when_external_polling_enabled() -> None:
    """refresh_account_inline=False 时 refresh() 主路径不再触发 data/clob fetch。

    UserAccountPoller 独立轮询时这个开关被打开；保证 reconcile 主循环不抢
    AccountStateStore，跟 paper_balance_syncer 的 race 一样的语义被一般化。
    """

    market = _market(1)

    class _SpyData:
        has_auth_client = True
        calls = 0

        async def list_positions(self) -> tuple[Any, ...]:
            type(self).calls += 1
            return ()

    class _SpyClob:
        has_auth_client = True
        balance_calls = 0
        orders_calls = 0
        fills_calls = 0

        async def get_orderbook(self, *args: Any, **kwargs: Any) -> Any:
            return None

        async def get_fee_rate(self, *args: Any, **kwargs: Any) -> int | None:
            return None

        async def list_open_orders(self) -> tuple[Any, ...]:
            type(self).orders_calls += 1
            return ()

        async def list_fills(self) -> tuple[Any, ...]:
            type(self).fills_calls += 1
            return ()

        async def get_balance_allowance(self) -> Any:
            type(self).balance_calls += 1
            return None

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
    spy_data = _SpyData()
    spy_clob = _SpyClob()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=_StubGammaClient(markets_by_slug={market.market_slug: market}),
        clob_client=spy_clob,
        data_client=spy_data,
        refresh_account_inline=False,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-no-inline"))

    # refresh() 主路径没触达任何用户态 client
    assert summary.user_refresh_enabled is False
    assert _SpyData.calls == 0
    assert _SpyClob.balance_calls == 0
    assert _SpyClob.orders_calls == 0
    assert _SpyClob.fills_calls == 0

    # refresh_account 仍可被 UserAccountPoller 直接调用——验证这条路径活着
    asyncio.run(refresher.refresh_account(trace_id="trace-direct", markets=()))
    assert _SpyData.calls == 1
    assert _SpyClob.balance_calls == 1


def test_fetch_gamma_market_skips_network_when_snapshot_store_has_fresh_entry() -> None:
    """gamma_snapshot_store 命中 → refresh_market_authority 完全不调 list_markets。

    收益验证：discovery 每 0.5s 已经把 /events.markets 的完整 DTO 写进 store，
    reconcile 不必再为每个 tracked market 单调 /markets——节省 90% gamma 调用。
    """
    from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore

    market = _market(1)

    class _StubCachedDTO:
        condition_id = market.condition_id
        clob_enabled = True
        raw: dict[str, Any] = {}

        def to_market(self) -> Market:
            return market.with_trading_status(TradingStatus.ELIGIBLE)

    store = GammaMarketSnapshotStore()
    store.upsert(_StubCachedDTO())

    class _FailingGamma:
        """如果 refresher 触达网络就立即失败——cache 命中场景不该走到这里。"""

        async def list_markets(self, **kwargs: Any) -> tuple[Any, ...]:
            raise AssertionError("snapshot cache hit should bypass /markets call")

        async def get_market_by_condition_id(
            self, condition_id: str, *, timeout_s: float | None = None
        ) -> Any | None:
            raise AssertionError("snapshot cache hit should bypass /markets call")

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
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=_FailingGamma(),
        gamma_snapshot_store=store,
    )

    summary = asyncio.run(refresher.refresh(trace_id="trace-cache-hit"))

    # cache 命中，gamma 没爆 → refreshed_markets 计数照常
    assert summary.refreshed_markets == 1
    assert all(f.component != "gamma" for f in summary.failures)


def test_fetch_gamma_market_falls_back_to_network_when_snapshot_stale_or_missing() -> None:
    """snapshot 缺失（或 stale）时退回 /markets?slug= 直查——孤儿 condition 兜底。"""
    from polymarket_trader.runtime.gamma_snapshot_store import GammaMarketSnapshotStore

    market = _market(1)
    refreshed = market.with_trading_status(TradingStatus.ELIGIBLE)
    gamma = _StubGammaClient(markets_by_slug={market.market_slug: refreshed})
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
    empty_store = GammaMarketSnapshotStore()  # 没有 cache
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        gamma_client=gamma,
        gamma_snapshot_store=empty_store,
    )

    asyncio.run(refresher.refresh(trace_id="trace-cache-miss"))

    # cache miss → 仍然打 list_markets fallback
    assert market.market_slug in gamma.slugs_called


def test_fetched_redeemable_position_is_enriched_via_gamma_outcome_before_store_write() -> None:
    """data API 返回 redeemable=True + cur_price=None 的持仓，refresh_account 内
    会用 gamma outcomePrices 派定胜负后再写 AccountStateStore——避免在 5-min
    settlement_scanner 跑之前 UI 显示错误"可赎回 + 按 cost 算名义"状态。"""

    win_market = _market(1)
    lose_market = _market(2)
    win_token = win_market.token_ids[0]
    lose_token = lose_market.token_ids[1]  # 我们持的是输方

    class _SettledCandidate:
        """带 outcomePrices 的 stub，让 _resolve_from_gamma_payload 能派定胜负。"""

        def __init__(self, market: Market, winning_idx: int) -> None:
            outcome_prices = ["1", "0"] if winning_idx == 0 else ["0", "1"]
            self.raw = {"outcomePrices": outcome_prices}
            self.outcomes = market.outcomes
            self.closed = True
            self._market = market
            self.condition_id = market.condition_id
            self.market_slug = market.market_slug
            self.clob_enabled = True

        def to_market(self) -> Market:
            return self._market

    class _GammaSettled:
        async def list_markets(self, **kwargs: Any) -> tuple[Any, ...]:
            return ()

        async def get_market_by_condition_id(
            self, condition_id: str, *, timeout_s: float | None = None
        ) -> Any | None:
            if condition_id == win_market.condition_id:
                return _SettledCandidate(win_market, winning_idx=0)
            if condition_id == lose_market.condition_id:
                return _SettledCandidate(lose_market, winning_idx=0)
            return None

    class _DataWithRedeemable:
        """data API stub：把 redeemable=True 标在两个持仓上，cur_price 不设。"""

        has_auth_client = True

        async def list_positions(self) -> tuple[Any, ...]:
            class _P:
                def __init__(self, market: Market, token_id: str) -> None:
                    self._market = market
                    self._token = token_id

                def to_position(self, *, strategy_id: str) -> Position:
                    return Position(
                        strategy_id=strategy_id,
                        condition_id=self._market.condition_id,
                        token_id=self._token,
                        shares=Decimal("10"),
                        cost_usdc=Decimal("3"),
                        cur_price=None,
                        current_value=None,
                        redeemable=True,
                    )

            return (_P(win_market, win_token), _P(lose_market, lose_token))

    account_state = AccountStateStore()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((win_market, lose_market)),
        account_state_store=account_state,
        data_client=_DataWithRedeemable(),
        gamma_client=_GammaSettled(),
    )

    asyncio.run(
        refresher.refresh_account(trace_id="trace-enrich", markets=(win_market, lose_market))
    )

    snap = account_state.snapshot()
    by_cid = {p.condition_id: p for p in snap.positions}
    winner = by_cid[win_market.condition_id]
    loser = by_cid[lose_market.condition_id]

    # 胜方：cur_price=1，cash_pnl=10*1 - 3 = 7
    assert winner.cur_price == Decimal("1")
    assert winner.current_value == Decimal("10")
    assert winner.cash_pnl == Decimal("7")
    assert winner.settled_zero_value is False

    # 输方：cur_price=0，cash_pnl=-3，settled_zero_value=True
    assert loser.cur_price == Decimal("0")
    assert loser.current_value == Decimal("0")
    assert loser.cash_pnl == Decimal("-3")
    assert loser.settled_zero_value is True


def test_paper_mode_skips_user_account_refresh_to_avoid_paper_ledger_race() -> None:
    """paper_mode=True 时 refresh_account 直接早退、不调任何 user-side fetch、
    不触碰 AccountStateStore——避免与 paper_balance_syncer 抢同一份状态。"""

    market = _market(1)
    account_state = AccountStateStore()
    # 预置 paper-side 持仓（模拟 paper_balance_syncer 刚写过）
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=market.condition_id,
            token_id=market.token_ids[0],
            shares=Decimal("100"),  # paper 持仓
            cost_usdc=Decimal("50"),
        )
    )

    class _GhostDataClient:
        """data API 会返回 24 个旧链上仓位——paper 模式下不应被调用。"""

        has_auth_client = True
        called = False

        async def list_positions(self) -> tuple[Any, ...]:
            type(self).called = True  # pragma: no cover - 应该不调用
            raise AssertionError("paper mode must not call data_client.list_positions")

    ghost = _GhostDataClient()
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((market,)),
        account_state_store=account_state,
        data_client=ghost,
        paper_mode=True,
    )

    summary = asyncio.run(refresher.refresh_account(trace_id="trace-paper", markets=(market,)))

    assert summary.user_refresh_enabled is False
    assert summary.refreshed_positions == 0
    assert _GhostDataClient.called is False
    # paper 持仓未被覆盖
    snap = account_state.snapshot()
    assert len(snap.positions) == 1
    assert snap.positions[0].shares == Decimal("100")


def test_quarantined_market_without_exposure_is_pruned_from_registry() -> None:
    """已 quarantine 且无任何持仓/挂单 → refresh 时直接从 registry 删，不再永久挂 PAUSED。"""
    dead = _market(1).with_trading_status(
        TradingStatus.PAUSED, reject_reason="auto_quarantine_dead_market"
    )
    registry = MarketRegistry()
    registry.upsert(dead)
    assert registry.get_by_condition_id(dead.condition_id) is not None

    account_state = AccountStateStore()  # 无 position 无 open order
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((dead,)),
        account_state_store=account_state,
        registry=registry,
    )

    asyncio.run(
        refresher.refresh(trace_id="trace-prune", condition_ids=(dead.condition_id,))
    )

    assert registry.get_by_condition_id(dead.condition_id) is None


def test_quarantined_market_with_shares_is_kept_paused_not_pruned() -> None:
    """已 quarantine 但持仓 shares>0 → 保留 PAUSED 簿记，不能 prune（可能等结算/redeem）。"""
    dead = _market(1).with_trading_status(
        TradingStatus.PAUSED, reject_reason="auto_quarantine_dead_market"
    )
    registry = MarketRegistry()
    registry.upsert(dead)

    account_state = AccountStateStore()
    account_state.upsert_position(
        Position(
            strategy_id="sports_tail",
            condition_id=dead.condition_id,
            token_id=dead.token_ids[0],
            shares=Decimal("3.5"),
            cost_usdc=Decimal("1.0"),
        )
    )
    refresher = ReconcileAuthorityRefresher(
        strategy_id="sports_tail",
        registry_snapshot_provider=lambda: MarketRegistrySnapshot((dead,)),
        account_state_store=account_state,
        registry=registry,
    )

    asyncio.run(
        refresher.refresh(trace_id="trace-keep", condition_ids=(dead.condition_id,))
    )

    kept = registry.get_by_condition_id(dead.condition_id)
    assert kept is not None
    assert kept.trading_status == TradingStatus.PAUSED
