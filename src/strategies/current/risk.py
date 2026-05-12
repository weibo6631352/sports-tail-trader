"""体育扫尾策略级风险限制。

这里处理“同一比赛、同一联赛、当日新增预算、连续亏损暂停”等业务风险。
框架级账户余额、单笔金额、订单状态和执行器门禁仍由 `RiskManager` 负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from polymarket_trader.domain.allocation import current_exposure_usdc
from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api.context import AccountSnapshotView

from strategies.current.allocation import AllocationMarketSnapshot
from strategies.current.config import CurrentStrategyConfig
from strategies.current.trading.helpers import fill_notional_usdc


@dataclass(frozen=True, slots=True)
class SportsRiskDecision:
    """体育扫尾策略级风险判断结果。"""

    passed: bool
    reason: str = "passed"
    metadata: Mapping[str, object] | None = None


def check_tail_entry_risk(
    config: CurrentStrategyConfig,
    *,
    market: Market,
    token_id: str,
    buy_budget_usdc: Decimal,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
    metadata: Mapping[str, object],
    account_snapshot: object | None = None,
    now: datetime | None = None,
    bankroll_usdc: Decimal = Decimal("0"),
) -> SportsRiskDecision:
    """检查体育扫尾业务风险上限（相关性硬上限）。

    与 Kelly 单市场 cap 互补：Kelly 控制单笔 sizing；这里控制"同事件 / 同联赛 /
    单日"的相关性敞口。caps = ``max(bankroll × fraction, min_floor_usdc)``，
    bankroll 涨大时同步放大；bankroll 极小时 floor 接管避免 cap=0 全拒。
    """

    risk_metadata = _base_metadata(
        market=market,
        token_id=token_id,
        candidate_snapshots=candidate_snapshots,
        buy_budget_usdc=buy_budget_usdc,
        metadata=metadata,
    )
    if buy_budget_usdc <= Decimal("0"):
        return SportsRiskDecision(passed=True, metadata=risk_metadata)

    consecutive_losses = _int_metadata(
        metadata,
        "tail_consecutive_losses",
        "consecutive_losses",
        default=0,
    )
    risk_metadata["consecutive_losses"] = consecutive_losses
    if config.tail_max_consecutive_losses >= 0 and consecutive_losses >= config.tail_max_consecutive_losses:
        return _reject("consecutive_loss_pause", risk_metadata)

    event_cap = _bankroll_fraction_cap(
        bankroll_usdc=bankroll_usdc,
        fraction=config.tail_max_event_exposure_fraction,
        min_floor_usdc=config.tail_max_event_exposure_min_floor_usdc,
    )
    event_exposure = _event_exposure_usdc(market, candidate_snapshots)
    event_after = event_exposure + buy_budget_usdc
    risk_metadata["event_exposure_usdc"] = str(event_exposure)
    risk_metadata["event_exposure_after_usdc"] = str(event_after)
    risk_metadata["max_event_exposure_usdc"] = str(event_cap)
    risk_metadata["max_event_exposure_fraction"] = str(config.tail_max_event_exposure_fraction)
    if event_after > event_cap:
        return _reject("event_exposure_limit", risk_metadata)

    league_cap = _bankroll_fraction_cap(
        bankroll_usdc=bankroll_usdc,
        fraction=config.tail_max_league_exposure_fraction,
        min_floor_usdc=config.tail_max_league_exposure_min_floor_usdc,
    )
    league = _league_key(market, metadata)
    league_exposure = _league_exposure_usdc(league, candidate_snapshots)
    league_after = league_exposure + buy_budget_usdc
    risk_metadata["league"] = league
    risk_metadata["league_exposure_usdc"] = str(league_exposure)
    risk_metadata["league_exposure_after_usdc"] = str(league_after)
    risk_metadata["max_league_exposure_usdc"] = str(league_cap)
    risk_metadata["max_league_exposure_fraction"] = str(config.tail_max_league_exposure_fraction)
    if league_after > league_cap:
        return _reject("league_exposure_limit", risk_metadata)

    daily_cap = _bankroll_fraction_cap(
        bankroll_usdc=bankroll_usdc,
        fraction=config.tail_max_daily_entry_fraction,
        min_floor_usdc=config.tail_max_daily_entry_min_floor_usdc,
    )
    daily_entry_usdc = _daily_entry_usdc(
        metadata,
        account_snapshot=account_snapshot,
        candidate_snapshots=candidate_snapshots,
        focus_market=market,
        now=now,
    )
    daily_after = daily_entry_usdc + buy_budget_usdc
    risk_metadata["daily_entry_usdc"] = str(daily_entry_usdc)
    risk_metadata["daily_entry_after_usdc"] = str(daily_after)
    risk_metadata["max_daily_entry_usdc"] = str(daily_cap)
    risk_metadata["max_daily_entry_fraction"] = str(config.tail_max_daily_entry_fraction)
    if daily_after > daily_cap:
        return _reject("daily_entry_limit", risk_metadata)

    risk_metadata["risk_reason"] = "passed"
    return SportsRiskDecision(passed=True, metadata=risk_metadata)


def _bankroll_fraction_cap(
    *,
    bankroll_usdc: Decimal,
    fraction: Decimal,
    min_floor_usdc: Decimal,
) -> Decimal:
    """fraction × bankroll 与绝对 floor 取较大者。bankroll 极小时 floor 接管。"""

    fraction_cap = bankroll_usdc * fraction if bankroll_usdc > Decimal("0") else Decimal("0")
    return max(fraction_cap, min_floor_usdc)


def _reject(reason: str, metadata: dict[str, object]) -> SportsRiskDecision:
    metadata["risk_reason"] = reason
    return SportsRiskDecision(passed=False, reason=reason, metadata=metadata)


def _base_metadata(
    *,
    market: Market,
    token_id: str,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
    buy_budget_usdc: Decimal,
    metadata: Mapping[str, object],
) -> dict[str, object]:
    return {
        "risk_reason": "pending",
        "risk_condition_id": market.condition_id,
        "risk_token_id": token_id,
        "risk_event_key": _event_key(market),
        "risk_candidate_count": len(candidate_snapshots),
        "risk_buy_budget_usdc": str(buy_budget_usdc),
        "live_source": _nested_text(metadata, "live_game", "source"),
        "live_source_event_id": _nested_text(metadata, "live_game", "source_event_id"),
    }


def _event_exposure_usdc(
    market: Market,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
) -> Decimal:
    event_key = _event_key(market)
    total = Decimal("0")
    seen: set[tuple[str, str]] = set()
    for snapshot in candidate_snapshots:
        if _event_key(snapshot.market) != event_key:
            continue
        key = (snapshot.condition_id, snapshot.token_id)
        if key in seen:
            continue
        seen.add(key)
        total += current_exposure_usdc(snapshot.position, snapshot.open_orders)
    return total


def _league_exposure_usdc(
    league: str,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
) -> Decimal:
    total = Decimal("0")
    seen: set[tuple[str, str]] = set()
    for snapshot in candidate_snapshots:
        if _market_league_key(snapshot.market) != league:
            continue
        key = (snapshot.condition_id, snapshot.token_id)
        if key in seen:
            continue
        seen.add(key)
        total += current_exposure_usdc(snapshot.position, snapshot.open_orders)
    return total


def _daily_entry_usdc(
    metadata: Mapping[str, object],
    *,
    account_snapshot: AccountSnapshotView | None,
    candidate_snapshots: tuple[AllocationMarketSnapshot, ...],
    focus_market: Market,
    now: datetime | None,
) -> Decimal:
    """返回体育扫尾当日 BUY 成交额。

    运行时如果有显式 metadata，以它为准；否则从账户快照的 fills 中统计
    当前候选集合对应的 BUY 成交。这样自动路径不需要额外手工字段也能生效，
    管理台仍可通过 metadata 写入外部清算口径。
    """

    explicit = _decimal_metadata(
        metadata,
        "tail_daily_entry_usdc",
        "daily_entry_usdc",
        default=None,
    )
    if explicit is not None:
        return explicit
    fills = account_snapshot.fills if account_snapshot is not None else ()
    if not fills:
        return Decimal("0")

    today = _utc_date(now)
    tail_condition_ids = {focus_market.condition_id}
    tail_condition_ids.update(snapshot.condition_id for snapshot in candidate_snapshots)
    total = Decimal("0")
    for fill in fills:
        if fill.condition_id not in tail_condition_ids:
            continue
        if (fill.side or "").upper() != "BUY":
            continue
        fill_time = fill.confirmed_at or fill.created_at
        if _utc_date(fill_time) != today:
            continue
        total += fill_notional_usdc(fill)
    return total


def _utc_date(value: datetime | None) -> object:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date()


def _event_key(market: Market) -> str:
    return market.event_slug or market.event_id or market.condition_id


def _league_key(market: Market, metadata: Mapping[str, object]) -> str:
    game_league = _nested_text(metadata, "live_game", "league")
    if game_league:
        return game_league.lower()
    return _market_league_key(market)


def _market_league_key(market: Market) -> str:
    for value in (*market.tags, market.category):
        if value is None:
            continue
        text = str(value).strip().lower()
        if text and text != "sports":
            return text
    return "unknown"


def _nested_text(metadata: Mapping[str, object], root_key: str, field_key: str) -> str | None:
    root = metadata.get(root_key)
    if not isinstance(root, Mapping):
        return None
    value = root.get(field_key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_metadata(
    metadata: Mapping[str, object],
    *keys: str,
    default: Decimal | None,
) -> Decimal | None:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (ValueError, TypeError, InvalidOperation):
            continue
    return default


def _int_metadata(
    metadata: Mapping[str, object],
    *keys: str,
    default: int,
) -> int:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (ValueError, TypeError):
            continue
    return default
