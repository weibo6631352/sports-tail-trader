"""Edge 实现度（edge-realization）分析。

定价模型的核心检验：对每个 ``accepted=true`` 的决策，比较"预测 edge"
和"实际 per-share 兑现回报"。预测 edge 来自策略 hook 的 ``decision_output``
（``fair_value`` 和 ``entry_price_cap`` / ``entry_price``）；实际回报从
position 的 ``cash_pnl`` / ``realized_pnl`` 除以 ``cost_usdc`` 估算。

这是判断"定价模型对不对"最直接的指标——比 funnel / rejections 更上层。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from polymarket_trader.app.admin_serialization import decimal_text, jsonable
from polymarket_trader.domain.decisions import DecisionRecord
from polymarket_trader.domain.position import Position


_EDGE_BUCKET_EDGES_BPS: tuple[int, ...] = (50, 100, 200, 500, 1000)


@dataclass(frozen=True, slots=True)
class EdgeRealizationItem:
    record_id: str
    trace_id: str
    condition_id: str
    token_id: str | None
    market_slug: str | None
    created_at_iso: str | None
    fair_value: Decimal | None
    entry_price: Decimal | None
    predicted_edge_bps: Decimal | None
    cost_usdc: Decimal | None
    cash_pnl_usdc: Decimal | None
    realized_pnl_usdc: Decimal | None
    actual_return_bps: Decimal | None
    position_status: str  # open | redeemable | settled_zero | missing

    def as_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "trace_id": self.trace_id,
            "condition_id": self.condition_id,
            "token_id": self.token_id,
            "market_slug": self.market_slug,
            "created_at": self.created_at_iso,
            "fair_value": decimal_text(self.fair_value),
            "entry_price": decimal_text(self.entry_price),
            "predicted_edge_bps": decimal_text(self.predicted_edge_bps),
            "cost_usdc": decimal_text(self.cost_usdc),
            "cash_pnl_usdc": decimal_text(self.cash_pnl_usdc),
            "realized_pnl_usdc": decimal_text(self.realized_pnl_usdc),
            "actual_return_bps": decimal_text(self.actual_return_bps),
            "position_status": self.position_status,
        }


def build_edge_realization(
    *,
    decisions: Sequence[DecisionRecord],
    positions_by_key: Mapping[tuple[str, str], Position],
) -> tuple[EdgeRealizationItem, ...]:
    """对 ``accepted=true`` 决策构建 edge-realization 行集合。

    decisions 已由 caller 过滤为 accepted；本函数不再次过滤——保持职责单一。
    """

    items: list[EdgeRealizationItem] = []
    for record in decisions:
        fair_value = _extract_decimal(record.decision_output, ("fair_value",))
        entry_price = _extract_decimal(
            record.decision_output,
            ("entry_price", "entry_price_cap", "price"),
        )
        predicted_edge_bps = _predicted_edge_bps(fair_value, entry_price)
        position = (
            positions_by_key.get((record.condition_id, record.token_id))
            if record.token_id is not None
            else None
        )
        cost = None if position is None else position.cost_usdc
        cash = None if position is None else position.cash_pnl
        realized = None if position is None else position.realized_pnl
        actual_bps = _actual_return_bps(cost=cost, cash=cash, realized=realized)
        status = _position_status(position)
        items.append(
            EdgeRealizationItem(
                record_id=record.record_id,
                trace_id=record.trace_id,
                condition_id=record.condition_id,
                token_id=record.token_id,
                market_slug=record.market_slug,
                created_at_iso=jsonable(record.created_at),
                fair_value=fair_value,
                entry_price=entry_price,
                predicted_edge_bps=predicted_edge_bps,
                cost_usdc=cost,
                cash_pnl_usdc=cash,
                realized_pnl_usdc=realized,
                actual_return_bps=actual_bps,
                position_status=status,
            )
        )
    return tuple(items)


def aggregate_by_predicted_edge_buckets(
    items: Sequence[EdgeRealizationItem],
) -> tuple[dict[str, Any], ...]:
    """按预测 edge 分桶聚合实际回报；空桶仍返回 count=0。"""

    buckets: dict[str, list[EdgeRealizationItem]] = {label: [] for label in _bucket_labels()}
    for item in items:
        if item.predicted_edge_bps is None:
            buckets["(no_prediction)"].append(item)
            continue
        buckets[_bucket_for_edge(item.predicted_edge_bps)].append(item)
    out: list[dict[str, Any]] = []
    for label in _bucket_labels():
        rows = buckets.get(label, [])
        with_return = [r for r in rows if r.actual_return_bps is not None]
        out.append(
            {
                "bucket": label,
                "count": len(rows),
                "with_return_count": len(with_return),
                "mean_actual_return_bps": decimal_text(_mean_decimal([r.actual_return_bps for r in with_return])),
                "median_actual_return_bps": decimal_text(_median_decimal([r.actual_return_bps for r in with_return])),
                "win_rate": decimal_text(_win_rate(with_return)),
            }
        )
    return tuple(out)


def _bucket_labels() -> tuple[str, ...]:
    labels: list[str] = []
    prev = 0
    for edge in _EDGE_BUCKET_EDGES_BPS:
        labels.append(f"{prev}-{edge}bps")
        prev = edge
    labels.append(f">{prev}bps")
    labels.append("(no_prediction)")
    return tuple(labels)


def _bucket_for_edge(edge_bps: Decimal) -> str:
    prev = 0
    for edge in _EDGE_BUCKET_EDGES_BPS:
        if edge_bps < Decimal(edge):
            return f"{prev}-{edge}bps"
        prev = edge
    return f">{prev}bps"


def _predicted_edge_bps(fair_value: Decimal | None, entry_price: Decimal | None) -> Decimal | None:
    if fair_value is None or entry_price is None:
        return None
    if fair_value <= Decimal("0"):
        return None
    return ((fair_value - entry_price) / fair_value) * Decimal("10000")


def _actual_return_bps(
    *,
    cost: Decimal | None,
    cash: Decimal | None,
    realized: Decimal | None,
) -> Decimal | None:
    """优先用 realized_pnl（已平仓）；否则用 cash_pnl（含 MTM 浮动）。

    cost 缺失或为 0 时无法归一化——返回 None 避免除零或误读。
    """

    if cost is None or cost <= Decimal("0"):
        return None
    if realized is not None and realized != Decimal("0"):
        return (realized / cost) * Decimal("10000")
    if cash is not None:
        return (cash / cost) * Decimal("10000")
    return None


def _position_status(position: Position | None) -> str:
    if position is None:
        return "missing"
    if position.redeemable is True:
        return "settled_zero" if position.settled_zero_value else "redeemable"
    return "open"


def _extract_decimal(payload: Mapping[str, Any], keys: Sequence[str]) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


def _mean_decimal(values: Sequence[Decimal | None]) -> Decimal | None:
    filtered = [v for v in values if v is not None]
    if not filtered:
        return None
    return sum(filtered, Decimal("0")) / Decimal(len(filtered))


def _median_decimal(values: Sequence[Decimal | None]) -> Decimal | None:
    filtered = sorted(v for v in values if v is not None)
    if not filtered:
        return None
    n = len(filtered)
    mid = n // 2
    if n % 2 == 1:
        return filtered[mid]
    return (filtered[mid - 1] + filtered[mid]) / Decimal("2")


def _win_rate(items: Sequence[EdgeRealizationItem]) -> Decimal | None:
    if not items:
        return None
    wins = sum(1 for item in items if item.actual_return_bps is not None and item.actual_return_bps > Decimal("0"))
    return Decimal(wins) / Decimal(len(items))
