"""``build_missed_opportunities`` 行为。

覆盖：
- 仅 ``accepted=false`` 决策（由 caller 过滤；本模块输入即假设已过滤）
- 决策 token_id == winner 时记 ``would_have_won`` + 正 PnL
- token_id != winner 时记 ``would_have_lost`` + 负 PnL
- 未结算决策 ``status=unsettled``，PnL=None
- 缺 entry_price 的决策 ``status=unscorable``
- 按 reason 聚合 + ``hypothetical_pnl_usdc`` 总和正负正确
- ``per_decision_usdc`` 改变 PnL 量级
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from polymarket_trader.app.missed_opportunities import build_missed_opportunities
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


BASE = datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc)


def _decision(
    *,
    record_id: str,
    condition_id: str,
    token_id: str | None,
    reason: str,
    entry_price: str | None,
    fair_value: str | None = "0.40",
) -> DecisionRecord:
    output: dict[str, Any] = {}
    if entry_price is not None:
        output["entry_price"] = entry_price
    if fair_value is not None:
        output["fair_value"] = fair_value
    return DecisionRecord(
        strategy_id="sports_tail",
        record_id=record_id,
        trace_id=f"trace-{record_id}",
        condition_id=condition_id,
        token_id=token_id,
        decision_input={},
        decision_output=output,
        accepted=False,
        reason=reason,
        created_at=BASE,
    )


def _settlement(*, condition_id: str, winner_token: str | None) -> AuditEvent:
    return AuditEvent(
        strategy_id="sports_tail",
        trace_id=f"trace-{condition_id}",
        event_id=f"settled-{condition_id}",
        event_title="market_settled",
        condition_id=condition_id,
        token_id=winner_token,
        status="ok",
        reason="manual_settlement",
        payload={"winning_token_id": winner_token, "settled_at": "2026-05-12T00:00:00+00:00"},
        created_at=BASE,
    )


def test_decision_token_matches_winner_yields_positive_pnl() -> None:
    decisions = (
        _decision(
            record_id="r1", condition_id="c1", token_id="t1",
            reason="risk_blocked", entry_price="0.40",
        ),
    )
    settlements = (_settlement(condition_id="c1", winner_token="t1"),)
    result = build_missed_opportunities(
        rejected_decisions=decisions,
        settlements=settlements,
        per_decision_usdc=Decimal("10"),
    )
    item = result["items"][0]
    assert item["status"] == "would_have_won"
    # size = 10/0.4 = 25 shares; PnL = (1 - 0.4) * 25 = 15
    assert Decimal(item["hypothetical_pnl_usdc"]) == Decimal("15")


def test_decision_token_misses_winner_yields_negative_pnl() -> None:
    decisions = (
        _decision(
            record_id="r1", condition_id="c1", token_id="t1",
            reason="risk_blocked", entry_price="0.40",
        ),
    )
    settlements = (_settlement(condition_id="c1", winner_token="t-other"),)
    result = build_missed_opportunities(
        rejected_decisions=decisions,
        settlements=settlements,
        per_decision_usdc=Decimal("10"),
    )
    item = result["items"][0]
    assert item["status"] == "would_have_lost"
    # size = 25 shares; PnL = (0 - 0.4) * 25 = -10
    assert Decimal(item["hypothetical_pnl_usdc"]) == Decimal("-10")


def test_unsettled_decisions_carry_pending_status() -> None:
    decisions = (
        _decision(
            record_id="r1", condition_id="c-pending", token_id="t1",
            reason="risk_blocked", entry_price="0.40",
        ),
    )
    result = build_missed_opportunities(rejected_decisions=decisions, settlements=())
    item = result["items"][0]
    assert item["status"] == "unsettled"
    assert item["hypothetical_pnl_usdc"] is None


def test_missing_entry_price_marked_unscorable() -> None:
    decisions = (
        _decision(
            record_id="r1", condition_id="c1", token_id="t1",
            reason="risk_blocked", entry_price=None,
        ),
    )
    settlements = (_settlement(condition_id="c1", winner_token="t1"),)
    result = build_missed_opportunities(rejected_decisions=decisions, settlements=settlements)
    item = result["items"][0]
    assert item["status"] == "unscorable"
    assert item["hypothetical_pnl_usdc"] is None


def test_by_reason_aggregates_pnl_and_counts() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", reason="risk_blocked", entry_price="0.40"),
        _decision(record_id="r2", condition_id="c2", token_id="t-loss", reason="risk_blocked", entry_price="0.40"),
        _decision(record_id="r3", condition_id="c3", token_id="t3", reason="no_signal", entry_price="0.50"),
    )
    settlements = (
        _settlement(condition_id="c1", winner_token="t1"),  # win
        _settlement(condition_id="c2", winner_token="t-winner"),  # lose
        _settlement(condition_id="c3", winner_token="t3"),  # win
    )
    result = build_missed_opportunities(
        rejected_decisions=decisions,
        settlements=settlements,
        per_decision_usdc=Decimal("10"),
    )
    by_reason = {row["reason"]: row for row in result["by_reason"]}
    assert by_reason["risk_blocked"]["decision_count"] == 2
    assert by_reason["risk_blocked"]["would_have_won_count"] == 1
    assert by_reason["risk_blocked"]["would_have_lost_count"] == 1
    # 15 + (-10) = 5
    assert Decimal(by_reason["risk_blocked"]["hypothetical_pnl_usdc"]) == Decimal("5")
    # no_signal: 10/0.5 = 20 shares, (1-0.5)*20 = 10
    assert Decimal(by_reason["no_signal"]["hypothetical_pnl_usdc"]) == Decimal("10")


def test_per_decision_usdc_scales_pnl() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", reason="x", entry_price="0.50"),
    )
    settlements = (_settlement(condition_id="c1", winner_token="t1"),)
    result_10 = build_missed_opportunities(
        rejected_decisions=decisions, settlements=settlements, per_decision_usdc=Decimal("10")
    )
    result_100 = build_missed_opportunities(
        rejected_decisions=decisions, settlements=settlements, per_decision_usdc=Decimal("100")
    )
    # 10x USDC → 10x shares → 10x PnL
    assert Decimal(result_100["items"][0]["hypothetical_pnl_usdc"]) == (
        Decimal(result_10["items"][0]["hypothetical_pnl_usdc"]) * Decimal("10")
    )


def test_totals_aggregate_across_reasons() -> None:
    decisions = (
        _decision(record_id="r1", condition_id="c1", token_id="t1", reason="r1", entry_price="0.40"),
        _decision(record_id="r2", condition_id="c2", token_id="t-loss", reason="r2", entry_price="0.40"),
    )
    settlements = (
        _settlement(condition_id="c1", winner_token="t1"),
        _settlement(condition_id="c2", winner_token="t-winner"),
    )
    result = build_missed_opportunities(
        rejected_decisions=decisions, settlements=settlements, per_decision_usdc=Decimal("10")
    )
    totals = result["totals"]
    assert totals["decision_count"] == 2
    assert totals["settled_count"] == 2
    assert totals["would_have_won_count"] == 1
    assert totals["would_have_lost_count"] == 1
    # 15 - 10 = 5
    assert Decimal(totals["hypothetical_pnl_usdc"]) == Decimal("5")
