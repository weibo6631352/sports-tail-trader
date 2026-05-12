from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from polymarket_trader.app.trading_decision_service import TradingDecisionService
from polymarket_trader.app.virtual_paper_trading import run_virtual_paper_trade
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.orderbook import OrderbookSnapshot, PriceLevel
from polymarket_trader.runtime.account_state import AccountStateStore
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry

from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy


def test_virtual_paper_trade_uses_real_runtime_and_only_virtualizes_final_submit() -> None:
    runtime = _runtime_with_real_like_candidate()

    result = asyncio.run(run_virtual_paper_trade(runtime))

    assert result["status"] == "ok"
    assert result["data_source"] == "real_runtime"
    assert result["execution"] == "paper_submit_only"
    assert result["virtual_boundary"] == "order_submission_payment"
    assert result["signing"]["source"] == "real_trading_client"
    assert len(runtime.trading_client.signed_requests) == 2
    assert [request["phase"] for request in result["order_requests"]] == [
        "sign",
        "submit",
        "sign",
        "submit",
    ]
    assert [request["virtual"] for request in result["order_requests"]] == [
        False,
        True,
        False,
        True,
    ]
    assert result["summary"]["entry_order_status"] == "full_fill"
    assert result["summary"]["follow_up_count"] == 1
    assert result["summary"]["follow_up_price"] == "0.99"
    assert result["summary"]["follow_up_order_status"] == "live"
    assert result["opportunity_funnel"]["auto_execute_count"] == 1
    assert result["rejection_summary"]["total"] == 0
    assert result["paper_pnl"]["basis"] == "entry_fill_vs_follow_up_limit_net_after_taker_fees"
    assert result["paper_pnl"]["realized"] is False
    assert result["paper_pnl"]["profitable"] is True
    # Kelly sizing：bankroll=10, kelly_max_position_fraction=1.0 理论可全仓，但
    # strategies/current 的 prob_confidence 现在动态收缩（按 ask_depth / spread）
    # + tail_max_event_exposure_min_floor_usdc=5 接管 event cap；实际 stake 取决于
    # 测试 orderbook 的 spread 和 depth。本用例只断言"projected_net_pnl > 0"——
    # 验证 paper trade 走通 + Kelly→fill→exit pnl 计算闭环，不绑定具体 sizing 数值。
    assert Decimal(result["paper_pnl"]["projected_net_pnl_usdc"]) > Decimal("0")
    # 测试用 Market 未配置 fee_rate_bps，所以 fee 为 0；Decimal("0").quantize 序列化为 "0E-18"
    assert Decimal(result["paper_pnl"]["fees_paid_usdc"]) == Decimal("0")


def test_virtual_paper_trade_does_not_fabricate_trade_without_live_metadata() -> None:
    runtime = _runtime_with_real_like_candidate()
    runtime.entry_metadata_store = EntryMetadataStore()

    result = asyncio.run(run_virtual_paper_trade(runtime))

    assert result["status"] == "no_trade"
    assert result["data_source"] == "real_runtime"
    assert result["execution"] == "not_submitted"
    assert runtime.trading_client.signed_requests == []
    assert result["opportunity_funnel"]["auto_execute_count"] == 0
    assert result["rejection_summary"]["by_reason"]["missing_live_game_state"] == 1
    assert result["rejection_summary"]["by_stage"]["plan"] == 1


def test_virtual_paper_trade_prioritizes_live_rejection_over_scheduled_single_game() -> None:
    runtime = _runtime_with_scheduled_and_live_single_game_rejections()

    result = asyncio.run(run_virtual_paper_trade(runtime))

    assert result["status"] == "no_trade"
    assert result["selection"]["market_slug"] == "nhl-live-tail-2026-04-28-total-10pt5"
    assert result["reason"] == "outcome_not_locked"


# === F-3 显式 market 找不到时不静默 fallback ===

def test_explicit_market_slug_not_found_returns_not_found_without_fallback() -> None:
    runtime = _runtime_with_real_like_candidate()

    result = asyncio.run(
        run_virtual_paper_trade(runtime, market_slug="does-not-exist-anywhere-9999")
    )

    assert result["status"] == "no_trade"
    assert result["reason"] == "explicit_market_not_found"
    # selection 必须明确给出"用户请求了什么"，便于分析师对照
    selection = result["selection"]
    assert selection["metadata"]["requested_market_slug"] == "does-not-exist-anywhere-9999"
    # 不能 fallback：market_slug / condition_id 不能是注册表里的真实市场
    assert selection["market_slug"] != "nhl-tb-mon-total-4-5"
    # 没有真实订单签名发生
    assert runtime.trading_client.signed_requests == []


def test_explicit_condition_id_not_found_returns_not_found_without_fallback() -> None:
    runtime = _runtime_with_real_like_candidate()

    result = asyncio.run(
        run_virtual_paper_trade(runtime, condition_id="0xdeadbeefnotaregisteredcondition")
    )

    assert result["status"] == "no_trade"
    assert result["reason"] == "explicit_market_not_found"
    assert result["selection"]["metadata"]["requested_condition_id"] == "0xdeadbeefnotaregisteredcondition"


def test_no_explicit_request_still_falls_back_to_registry_scan() -> None:
    # 三字段全 None 是 documented behavior：扫全部市场。F-3 修复不应影响这条路径。
    runtime = _runtime_with_real_like_candidate()

    result = asyncio.run(run_virtual_paper_trade(runtime))

    # 注册表里有一个 candidate，仍应跑通模拟
    assert result["status"] == "ok"


class _MarketWs:
    def __init__(self, snapshots: dict[str, OrderbookSnapshot]) -> None:
        self._snapshots = snapshots

    def snapshot(self, token_id: str) -> OrderbookSnapshot | None:
        return self._snapshots.get(token_id)


class _FakeTradingClient:
    """只实现真实签名阶段需要的最小生产客户端接口。"""

    def __init__(self) -> None:
        self.signed_requests: list[Any] = []

    def create_signed_order(self, request: Any) -> dict[str, Any]:
        self.signed_requests.append(request)
        return {"signed": True, "idempotency_key": request.idempotency_key}


def _runtime_with_real_like_candidate() -> SimpleNamespace:
    observed_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    market = Market(
        condition_id="totals-condition",
        market_slug="nhl-tb-mon-total-4-5",
        market_question="TB vs MON total over/under 4.5",
        event_title="TB vs MON",
        event_slug="nhl-tb-mon",
        category="Sports",
        tags=("NHL",),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    orderbook = OrderbookSnapshot(
        token_id="over",
        best_bid=Decimal("0.97"),
        best_ask=Decimal("0.98"),
        bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
        asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
        received_at=observed_at,
        market_slug=market.market_slug,
        condition_id=market.condition_id,
        tick_size=Decimal("0.01"),
    )
    registry = MarketRegistry()
    registry.upsert(market)
    market_ws = _MarketWs({"over": orderbook})
    account_state = AccountStateStore()
    account_state.update_balances(balance_usdc=Decimal("10"), allowance_usdc=Decimal("10"))
    account_state.mark_user_ws_connected(True)
    account_state.mark_reconciled(observed_at)
    metadata_store = EntryMetadataStore()
    metadata_store.upsert(
        condition_id=market.condition_id,
        source="unit_test_real_source",
        metadata={
            "live_game": {
                "league": "NHL",
                "home_name": "TB",
                "away_name": "MON",
                "home_score": 3,
                "away_score": 2,
                "period": "P3",
                "seconds_remaining": 420,
                "status": "live",
                "observed_at": observed_at.isoformat(),
                "source": "unit_test_real_source",
            },
        },
    )
    return SimpleNamespace(
        settings=SimpleNamespace(
            # Kelly sizing 框架替代旧 max_order/market/total/open_orders 静态上限。
            # bankroll = portfolio_budget = 10 USDC；单仓 fraction=1.0 让单笔可用满额，
            # 重现旧测试单 BUY 用满 10 USDC 的预期 PnL；min_edge=0 让小 edge 也通过；
            # round_up=True 允许 Kelly 推荐 stake < market.min_order 时凑齐到 cap。
            portfolio_budget_usdc=Decimal("10"),
            kelly_fraction=Decimal("1"),
            kelly_max_position_fraction=Decimal("1"),
            kelly_min_edge=Decimal("0"),
            kelly_min_stake_usdc=Decimal("1"),
            kelly_allow_round_up_to_market_min=True,
            kelly_round_up_max_overbet_ratio=Decimal("1"),
            kelly_drawdown_halt_fraction=Decimal("0"),
            order_retry_limit=2,
        ),
        registry=registry,
        market_ws_worker=market_ws,
        account_state_store=account_state,
        entry_metadata_store=metadata_store,
        trading_decision_service=TradingDecisionService(
            strategy_id="sports_tail",
            extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
            registry=registry,
            orderbook_reader=market_ws.snapshot,
        ),
        trading_client=_FakeTradingClient(),
    )


def _runtime_with_mixed_esports_and_scheduled_single_game() -> SimpleNamespace:
    runtime = _runtime_with_real_like_candidate()
    esports_market = Market(
        condition_id="esports-condition",
        market_slug="lol-hle1-ns-2026-04-22-game2-kill-over-33pt5",
        market_question="LoL: Hanwha Life Esports vs Nongshim Red Force game 2 kill over 33.5",
        event_title="LoL: Hanwha Life Esports vs Nongshim Red Force (BO3)",
        event_slug="lol-hle1-ns-2026-04-22",
        category="Sports",
        tags=("Esports", "league of legends"),
        outcomes=(
            MarketOutcome(token_id="esports-over", outcome="Over"),
            MarketOutcome(token_id="esports-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    single_game = Market(
        condition_id="single-game-condition",
        market_slug="nhl-ana-edm-2026-04-28-total-5pt5",
        market_question="Ducks vs. Oilers total over/under 5.5",
        event_title="Ducks vs. Oilers",
        event_slug="nhl-ana-edm-2026-04-28",
        category="Sports",
        tags=("NHL", "Hockey"),
        outcomes=(
            MarketOutcome(token_id="single-over", outcome="Over"),
            MarketOutcome(token_id="single-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    observed_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    registry = MarketRegistry()
    registry.upsert(esports_market)
    registry.upsert(single_game)
    runtime.registry = registry
    runtime.market_ws_worker = _MarketWs(
        {
            "esports-over": OrderbookSnapshot(
                token_id="esports-over",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=esports_market.market_slug,
                condition_id=esports_market.condition_id,
                tick_size=Decimal("0.001"),
            ),
            "esports-under": OrderbookSnapshot(
                token_id="esports-under",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=esports_market.market_slug,
                condition_id=esports_market.condition_id,
                tick_size=Decimal("0.001"),
            ),
            "single-over": OrderbookSnapshot(
                token_id="single-over",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=single_game.market_slug,
                condition_id=single_game.condition_id,
                tick_size=Decimal("0.01"),
            ),
            "single-under": OrderbookSnapshot(
                token_id="single-under",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=single_game.market_slug,
                condition_id=single_game.condition_id,
                tick_size=Decimal("0.01"),
            ),
        }
    )
    runtime.trading_decision_service = TradingDecisionService(
        strategy_id="sports_tail",
        extension_hooks=CurrentStrategy(config=CurrentStrategyConfig()).hooks,
        registry=registry,
        orderbook_reader=runtime.market_ws_worker.snapshot,
    )
    runtime.entry_metadata_store = EntryMetadataStore()
    runtime.entry_metadata_store.upsert(
        condition_id=single_game.condition_id,
        source="unit_test_real_source",
        metadata={
            "live_game": {
                "league": "NHL",
                "home_name": "Oilers",
                "away_name": "Ducks",
                "home_score": 0,
                "away_score": 0,
                "period": "FUT",
                "seconds_remaining": None,
                "status": "scheduled",
                "observed_at": observed_at.isoformat(),
                "source": "unit_test_real_source",
            },
        },
    )
    return runtime


def _runtime_with_scheduled_and_live_single_game_rejections() -> SimpleNamespace:
    runtime = _runtime_with_mixed_esports_and_scheduled_single_game()
    observed_at = datetime(2026, 4, 27, tzinfo=timezone.utc)
    live_market = Market(
        condition_id="live-single-game-condition",
        market_slug="nhl-live-tail-2026-04-28-total-10pt5",
        market_question="Rangers vs. Devils total over/under 10.5",
        event_title="Rangers vs. Devils",
        event_slug="nhl-live-tail-2026-04-28",
        category="Sports",
        tags=("NHL", "Hockey"),
        outcomes=(
            MarketOutcome(token_id="live-over", outcome="Over"),
            MarketOutcome(token_id="live-under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    runtime.registry.upsert(live_market)
    runtime.market_ws_worker._snapshots.update(
        {
            "live-over": OrderbookSnapshot(
                token_id="live-over",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=live_market.market_slug,
                condition_id=live_market.condition_id,
                tick_size=Decimal("0.01"),
            ),
            "live-under": OrderbookSnapshot(
                token_id="live-under",
                best_bid=Decimal("0.97"),
                best_ask=Decimal("0.98"),
                bids=(PriceLevel(price=Decimal("0.97"), size=Decimal("20")),),
                asks=(PriceLevel(price=Decimal("0.98"), size=Decimal("20")),),
                received_at=observed_at,
                market_slug=live_market.market_slug,
                condition_id=live_market.condition_id,
                tick_size=Decimal("0.01"),
            ),
        }
    )
    runtime.entry_metadata_store.upsert(
        condition_id=live_market.condition_id,
        source="unit_test_real_source",
        metadata={
            "live_game": {
                "league": "NHL",
                "home_name": "Rangers",
                "away_name": "Devils",
                "home_score": 3,
                "away_score": 2,
                "period": "P3",
                "seconds_remaining": 420,
                "status": "live",
                "observed_at": observed_at.isoformat(),
                "source": "unit_test_real_source",
            },
        },
    )
    return runtime
