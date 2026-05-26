"""仓位 PnL 按维度分解（只读聚合）。

回答"哪个 workflow / market / category / outcome 是赚钱主力，哪个在烧钱"。
当前 ``GET /portfolio`` 只给整体快照，``/positions`` 只给单仓位列表——
按维度 group_by 后才能定位资金效率瓶颈。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from polymarket_trader.serialization import decimal_text
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.position import Position


PnlGroupBy = str  # Literal[...] 但保持枚举透明，由 caller 校验


_VALID_GROUP_BY: frozenset[str] = frozenset(
    {
        "market_slug",
        "condition_id",
        "category",
        "outcome",
        "redeemable_status",
    }
)


@dataclass(frozen=True, slots=True)
class PnlBreakdownRow:
    group_key: str
    position_count: int
    total_realized_pnl_usdc: Decimal
    total_cash_pnl_usdc: Decimal
    total_current_value_usdc: Decimal
    total_cost_usdc: Decimal

    def as_payload(self) -> dict[str, object]:
        return {
            "group_key": self.group_key,
            "position_count": self.position_count,
            "realized_pnl_usdc": decimal_text(self.total_realized_pnl_usdc),
            "cash_pnl_usdc": decimal_text(self.total_cash_pnl_usdc),
            "current_value_usdc": decimal_text(self.total_current_value_usdc),
            "cost_usdc": decimal_text(self.total_cost_usdc),
        }


def is_valid_group_by(group_by: str) -> bool:
    return group_by in _VALID_GROUP_BY


def valid_group_by_values() -> tuple[str, ...]:
    return tuple(sorted(_VALID_GROUP_BY))


def build_pnl_breakdown(
    *,
    positions: Sequence[Position],
    markets_by_condition: Mapping[str, Market],
    group_by: str,
) -> tuple[PnlBreakdownRow, ...]:
    """对仓位按维度聚合 PnL；输出按 ``realized_pnl`` 降序排列。

    缺失 cash_pnl / realized_pnl 字段视为 0——避免把"已结算 + 未结算"的
    仓位完全排除。``current_value`` 默认 0（极少缺失），用于回答"还有多少
    资金在场"。
    """

    if not is_valid_group_by(group_by):
        raise ValueError(f"unsupported group_by: {group_by}")

    key_fn = _group_key_resolver(group_by, markets_by_condition)
    buckets: dict[str, _Bucket] = {}
    for position in positions:
        key = key_fn(position)
        bucket = buckets.setdefault(key, _Bucket())
        bucket.add(position)
    rows = [bucket.to_row(key) for key, bucket in buckets.items()]
    rows.sort(key=lambda row: row.total_realized_pnl_usdc, reverse=True)
    return tuple(rows)


class _Bucket:
    __slots__ = ("count", "realized", "cash", "current_value", "cost")

    def __init__(self) -> None:
        self.count = 0
        self.realized = Decimal("0")
        self.cash = Decimal("0")
        self.current_value = Decimal("0")
        self.cost = Decimal("0")

    def add(self, position: Position) -> None:
        self.count += 1
        self.realized += _zero_if_none(position.realized_pnl)
        self.cash += _zero_if_none(position.cash_pnl)
        self.current_value += _zero_if_none(position.current_value)
        self.cost += _zero_if_none(position.cost_usdc)

    def to_row(self, key: str) -> PnlBreakdownRow:
        return PnlBreakdownRow(
            group_key=key,
            position_count=self.count,
            total_realized_pnl_usdc=self.realized,
            total_cash_pnl_usdc=self.cash,
            total_current_value_usdc=self.current_value,
            total_cost_usdc=self.cost,
        )


def _group_key_resolver(
    group_by: str,
    markets_by_condition: Mapping[str, Market],
) -> Callable[[Position], str]:
    if group_by == "market_slug":
        return lambda p: p.market_slug or "(unknown)"
    if group_by == "condition_id":
        return lambda p: p.condition_id or "(unknown)"
    if group_by == "category":
        def _category(position: Position) -> str:
            market = markets_by_condition.get(position.condition_id)
            return (market.category if market is not None and market.category else "(uncategorized)")

        return _category
    if group_by == "outcome":
        def _outcome(position: Position) -> str:
            market = markets_by_condition.get(position.condition_id)
            if market is None:
                return "(unknown_market)"
            outcome = market.get_outcome_by_token_id(position.token_id)
            return outcome.outcome if outcome is not None else "(unknown_outcome)"

        return _outcome
    if group_by == "redeemable_status":
        def _status(position: Position) -> str:
            if position.redeemable is True:
                return "redeemable"
            if position.redeemable is False:
                return "open"
            return "unknown"

        return _status
    raise ValueError(f"unsupported group_by: {group_by}")


def _zero_if_none(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")


def aggregate_totals(rows: Iterable[PnlBreakdownRow]) -> dict[str, object]:
    """所有分组的合计——给前端总览。"""

    total_realized = Decimal("0")
    total_cash = Decimal("0")
    total_current_value = Decimal("0")
    total_cost = Decimal("0")
    position_count = 0
    for row in rows:
        total_realized += row.total_realized_pnl_usdc
        total_cash += row.total_cash_pnl_usdc
        total_current_value += row.total_current_value_usdc
        total_cost += row.total_cost_usdc
        position_count += row.position_count
    return {
        "position_count": position_count,
        "realized_pnl_usdc": decimal_text(total_realized),
        "cash_pnl_usdc": decimal_text(total_cash),
        "current_value_usdc": decimal_text(total_current_value),
        "cost_usdc": decimal_text(total_cost),
    }
