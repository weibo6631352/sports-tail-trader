from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.app.admin_serialization import AdminSerializer
from polymarket_trader.app.trade_replay import TradeReplayFilters, build_trade_replay_records
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import AuditEvent, Fill
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.position import Position
from polymarket_trader.runtime.registry import MarketRegistry


def test_trade_replay_uses_data_position_pnl_and_audit_context() -> None:
    market = Market(
        condition_id="condition-1",
        market_slug="nba-nyk-bos-moneyline",
        event_title="Knicks vs Celtics",
        event_slug="knicks-celtics",
        outcomes=(MarketOutcome(token_id="home", outcome="NYK"),),
        trading_status=TradingStatus.RESOLVED,
    )
    registry = MarketRegistry()
    registry.upsert(market)
    serializer = AdminSerializer(
        account_snapshot_provider=lambda: AccountSnapshot(),
        registry_snapshot_provider=registry.snapshot,
        market_ws_snapshot=lambda _: None,
    )
    fills = (
        Fill(
            strategy_id="sports_tail",
            trace_id="trace-1",
            event_id="fill-buy",
            condition_id="condition-1",
            token_id="home",
            side="BUY",
            price=Decimal("0.80"),
            size=Decimal("10"),
            notional_usdc=Decimal("8"),
            confirmed_at=datetime(2026, 4, 27, tzinfo=timezone.utc),
        ),
        Fill(
            strategy_id="sports_tail",
            trace_id="trace-1",
            event_id="fill-sell",
            condition_id="condition-1",
            token_id="home",
            side="SELL",
            price=Decimal("0.95"),
            size=Decimal("5"),
            notional_usdc=Decimal("4.75"),
            confirmed_at=datetime(2026, 4, 27, 0, 1, tzinfo=timezone.utc),
        ),
    )
    position = Position(
        strategy_id="sports_tail",
        condition_id="condition-1",
        token_id="home",
        shares=Decimal("5"),
        cost_usdc=Decimal("4"),
        realized_pnl=Decimal("0.75"),
        cash_pnl=Decimal("1.65"),
        current_value=Decimal("4.9"),
        redeemable=True,
    )
    audit = AuditEvent(
        strategy_id="sports_tail",
        event_title="entry_candidate",
        trace_id="trace-1",
        condition_id="condition-1",
        token_id="home",
        reason="moneyline_late_lead",
        payload={
            "strategy_summary": {
                "action": "buy",
                "reason": "moneyline_late_lead",
            },
            "decision_kind": "entry_candidate",
            "strategy_payload": {
                "live_game": {"league": "NBA", "status": "live"},
                "live_match": {"source_event_id": "401705460"},
                "exit_plan": {"target_exit_price": "0.995"},
            },
        },
    )

    records = build_trade_replay_records(
        markets=(market,),
        orders=(),
        fills=fills,
        positions=(position,),
        audit_events=(audit,),
        serializer=serializer,
        filters=TradeReplayFilters(condition_id="condition-1", token_id="home"),
    )

    assert len(records) == 1
    record = records[0]
    assert record["settlement_status"] == "redeemable"
    assert record["pnl"]["source"] == "data_position"
    assert record["pnl"]["realized_pnl_usdc"] == "0.75"
    assert record["pnl"]["cash_pnl_usdc"] == "1.65"
    assert record["strategy_payload"]["live_game"]["league"] == "NBA"
    assert record["strategy_payload"]["exit_plan"]["target_exit_price"] == 0.995
    assert record["decision_kind"] == "entry_candidate"
    assert record["strategy_summary"]["reason"] == "moneyline_late_lead"
