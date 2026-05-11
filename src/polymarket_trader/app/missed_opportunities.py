"""被风控/策略拒绝的决策事后模拟。

回答："如果当时下单了，结算后会赚还是亏？" —— 配合 ``risk_rejections`` 端点
判断风控阈值是否过严。

模拟规则（保守版）：
- 仅对 ``accepted=false`` 的决策；
- 需要 ``decision_output`` 含 ``fair_value`` 或 ``entry_price_cap`` / ``entry_price``
  这类预期入场价；缺失则跳过；
- 假设以 ``entry_price`` 全额入场（用 ``per_decision_usdc`` 默认 10 USDC 作仓位
  规模），市场结算后赢家 token 价值 1.0、输家 0.0；
- ``hypothetical_pnl_usdc = (settled_value - entry_price) * size_shares``，其中
  ``size_shares = per_decision_usdc / entry_price``；
- 没有结算的决策不计入"已模拟"，但会作为 ``pending_unsettled`` 暴露给前端。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from polymarket_trader.app.admin_serialization import decimal_text, jsonable
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.events import AuditEvent


_PER_DECISION_DEFAULT_USDC = Decimal("10")


@dataclass(frozen=True, slots=True)
class MissedOpportunityItem:
    record_id: str
    trace_id: str
    condition_id: str
    token_id: str | None
    market_slug: str | None
    created_at_iso: str | None
    reason: str | None
    entry_price: Decimal | None
    fair_value: Decimal | None
    winning_token_id: str | None
    settled_at: str | None
    hypothetical_pnl_usdc: Decimal | None
    status: str  # would_have_won | would_have_lost | unsettled | unscorable

    def as_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "trace_id": self.trace_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "market_slug": self.market_slug,
            "created_at": self.created_at_iso,
            "reason": self.reason,
            "entry_price": decimal_text(self.entry_price),
            "fair_value": decimal_text(self.fair_value),
            "winning_token_id": self.winning_token_id,
            "settled_at": self.settled_at,
            "hypothetical_pnl_usdc": decimal_text(self.hypothetical_pnl_usdc),
            "status": self.status,
        }


def build_missed_opportunities(
    *,
    rejected_decisions: Sequence[DecisionRecord],
    settlements: Sequence[AuditEvent],
    per_decision_usdc: Decimal = _PER_DECISION_DEFAULT_USDC,
) -> dict[str, Any]:
    """生成 missed-opportunity 列表 + 按 ``reason`` 聚合的事后盈利分组。"""

    settled_winners: dict[str, str | None] = {}
    settled_when: dict[str, str | None] = {}
    for event in settlements:
        if not event.condition_id:
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        settled_winners[event.condition_id] = (
            None if payload.get("winning_token_id") is None else str(payload["winning_token_id"])
        )
        settled_when[event.condition_id] = payload.get("settled_at")

    items: list[MissedOpportunityItem] = []
    for record in rejected_decisions:
        entry_price = _extract_decimal(
            record.decision_output, ("entry_price", "entry_price_cap", "price")
        )
        fair_value = _extract_decimal(record.decision_output, ("fair_value",))
        winner = settled_winners.get(record.condition_id)
        settled_at = settled_when.get(record.condition_id)
        pnl: Decimal | None = None
        status = "unsettled"
        if record.condition_id in settled_winners:
            if winner is None or entry_price is None or entry_price <= Decimal("0"):
                status = "unscorable"
            else:
                size_shares = per_decision_usdc / entry_price
                settled_value = (
                    Decimal("1") if record.token_id and record.token_id == winner else Decimal("0")
                )
                pnl = (settled_value - entry_price) * size_shares
                status = "would_have_won" if pnl > 0 else "would_have_lost"
        items.append(
            MissedOpportunityItem(
                record_id=record.record_id,
                trace_id=record.trace_id,
                condition_id=record.condition_id,
                token_id=record.token_id,
                market_slug=record.market_slug,
                created_at_iso=jsonable(record.created_at),
                reason=record.reason,
                entry_price=entry_price,
                fair_value=fair_value,
                winning_token_id=winner,
                settled_at=settled_at,
                hypothetical_pnl_usdc=pnl,
                status=status,
            )
        )

    by_reason = aggregate_by_reason(items)
    totals = aggregate_totals(items)
    return {
        "items": [item.as_payload() for item in items],
        "by_reason": by_reason,
        "totals": totals,
        "per_decision_usdc": str(per_decision_usdc),
    }


def aggregate_by_reason(items: Sequence[MissedOpportunityItem]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "decision_count": 0,
            "settled_count": 0,
            "would_have_won_count": 0,
            "would_have_lost_count": 0,
            "hypothetical_pnl_usdc": Decimal("0"),
        }
    )
    for item in items:
        bucket = grouped[item.reason or "(unspecified)"]
        bucket["decision_count"] += 1
        if item.status in ("would_have_won", "would_have_lost"):
            bucket["settled_count"] += 1
            bucket["hypothetical_pnl_usdc"] += item.hypothetical_pnl_usdc or Decimal("0")
        if item.status == "would_have_won":
            bucket["would_have_won_count"] += 1
        if item.status == "would_have_lost":
            bucket["would_have_lost_count"] += 1
    rows = []
    for reason, bucket in grouped.items():
        rows.append(
            {
                "reason": reason,
                "decision_count": bucket["decision_count"],
                "settled_count": bucket["settled_count"],
                "would_have_won_count": bucket["would_have_won_count"],
                "would_have_lost_count": bucket["would_have_lost_count"],
                "hypothetical_pnl_usdc": decimal_text(bucket["hypothetical_pnl_usdc"]),
            }
        )
    rows.sort(
        key=lambda row: Decimal(row["hypothetical_pnl_usdc"] or "0"),
        reverse=True,
    )
    return rows


def aggregate_totals(items: Sequence[MissedOpportunityItem]) -> dict[str, Any]:
    settled = [item for item in items if item.status in ("would_have_won", "would_have_lost")]
    win_count = sum(1 for item in settled if item.status == "would_have_won")
    total_pnl = sum(
        (item.hypothetical_pnl_usdc or Decimal("0") for item in settled), Decimal("0")
    )
    return {
        "decision_count": len(items),
        "settled_count": len(settled),
        "would_have_won_count": win_count,
        "would_have_lost_count": len(settled) - win_count,
        "hypothetical_pnl_usdc": decimal_text(total_pnl),
    }


def _extract_decimal(payload: Mapping[str, Any], keys: Sequence[str]) -> Decimal | None:
    for key in keys:
        value = payload.get(key) if isinstance(payload, Mapping) else None
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None
