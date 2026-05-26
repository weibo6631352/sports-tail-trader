"""通用辅助：从 DecisionContext.metadata 中读 Decimal / 文本；Fill 金额计算；
决策元数据投影（enrich_decision / build_strategy_summary）；tick_size 解析；
价格 tick 对齐。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, ROUND_FLOOR
from typing import TYPE_CHECKING, Any, Mapping

from polymarket_trader.domain.events import Fill
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.decisions import DecisionContext, DecisionKind, StrategySummary, TradingDecision

if TYPE_CHECKING:
    from polymarket_trader.domain.market import Market

# Polymarket 标准最小 tick；仅在 orderbook 和 market 均未报告 tick_size 时兜底。
_TICK_SIZE_FALLBACK = Decimal("0.01")


def align_price_to_tick(price: Decimal, *, tick_size: Decimal | None) -> Decimal:
    """把策略目标价格向下对齐到交易所允许的 tick。

    卖出退出价是"目标上限"——当市场只支持 0.01 tick 时，0.995 应落到 0.99；
    tick 缺失或异常时保持原价，由框架风控继续审计。
    """

    if tick_size is None or tick_size <= Decimal("0"):
        return price
    units = (price / tick_size).to_integral_value(rounding=ROUND_FLOOR)
    if units <= 0:
        return price
    return units * tick_size


def cap_price_to_clob_limit(price: Decimal, *, tick_size: Decimal | None = None) -> Decimal:
    """把目标价限制在 Polymarket CLOB 当前 tick 接受的最高价格内。"""

    effective_tick = tick_size if tick_size is not None and tick_size > Decimal("0") else _TICK_SIZE_FALLBACK
    return min(price, Decimal("1") - effective_tick)


def effective_tick_size(context: DecisionContext) -> Decimal | None:
    """优先 orderbook.tick_size，次 market.tick_size，否则 None（caller 自决兜底）。"""

    if context.orderbook is not None and context.orderbook.tick_size is not None:
        return context.orderbook.tick_size
    if context.market is not None:
        return context.market.tick_size
    return None


def resolve_tick_size(
    orderbook: OrderbookSnapshot | None,
    market: "Market | None" = None,
) -> Decimal:
    """优先取 orderbook.tick_size，次取 market.tick_size，最後兜底 0.01。"""

    if orderbook is not None and orderbook.tick_size is not None and orderbook.tick_size > Decimal("0"):
        return orderbook.tick_size
    if market is not None and market.tick_size is not None and market.tick_size > Decimal("0"):
        return market.tick_size
    return _TICK_SIZE_FALLBACK


def bid_plus_tick_fallback_ask(
    orderbook: OrderbookSnapshot | None,
    tick_size: Decimal | None,
) -> Decimal | None:
    """missing_best_ask 时用 ``best_bid + tick_size`` 估算 fallback ask。

    返回 None 表示 fallback 不可用（无 bid / 无 tick / 估算价越界）。返回估算价时
    调用方应把执行权限降级为 RECORD_ONLY——估算价只让 evaluator 跑出 fair_value
    与"理论可成交价"的对比，下单仍需要真实 ask 流动性。

    覆盖 outright + tail 两条评估路径——missing_best_ask 是单场盘口最大占比
    拒绝原因（实测 single_game 96%+），让两条路径都能用同一份 fallback 语义。
    """

    if orderbook is None or tick_size is None or tick_size <= Decimal("0"):
        return None
    best_bid = orderbook.best_bid
    if best_bid is None or best_bid <= Decimal("0"):
        return None
    fallback_ask = best_bid + tick_size
    if fallback_ask <= Decimal("0") or fallback_ask >= Decimal("1"):
        return None
    return fallback_ask


def bid_plus_tick_fallback_metadata(
    *,
    orderbook: OrderbookSnapshot,
    tick_size: Decimal,
    fallback_ask: Decimal,
) -> dict[str, str]:
    """统一 outright + tail 两条路径 RECORD_ONLY decision 的 fallback metadata。

    调用方已通过 ``bid_plus_tick_fallback_ask`` 拿到非 None 的 ``fallback_ask``，
    这里只负责拼成 decision_records 用于事后校准的四字段写入块——抽出来避免
    两个 callsite 字段名 / 字符串化方式漂移。
    """

    return {
        "best_ask_fallback": "bid_plus_tick",
        "best_bid": str(orderbook.best_bid),
        "tick_size": str(tick_size),
        "fallback_ask": str(fallback_ask),
    }


def fill_notional_usdc(fill: Fill) -> Decimal:
    """Fill の約定名義金額（USDC）を返す。notional_usdc があればそれを使い、なければ price×size。"""
    if fill.notional_usdc is not None:
        try:
            return Decimal(str(fill.notional_usdc))
        except Exception:
            return Decimal("0")
    if fill.price is None or fill.size is None:
        return Decimal("0")
    try:
        return fill.price * fill.size
    except Exception:
        return Decimal("0")


def decimal_from_metadata(value: object) -> Decimal | None:
    """metadata 中的价格/金额文本转 Decimal；None 表示缺失或解析失败。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def enrich_decision(decision: TradingDecision, *, default_kind: DecisionKind) -> TradingDecision:
    """把策略私有 metadata 投影成 framework 中性的 StrategySummary / decision_kind / intent_tags。

    策略 metadata 仍然透传 audit，但 framework 只读强类型字段。
    """
    metadata = dict(decision.metadata or {})
    is_scale_in = metadata.get("opportunity_type") == "scale_in_advantage"
    decision_kind = decision.decision_kind if decision.decision_kind is not None else (
        DecisionKind.SCALE_IN if is_scale_in else default_kind
    )
    intent_tags = decision.intent_tags if decision.intent_tags else (
        frozenset({"scale_in"}) if is_scale_in else frozenset()
    )
    summary = decision.summary if decision.summary is not None else _build_strategy_summary(metadata)
    return replace(decision, decision_kind=decision_kind, intent_tags=intent_tags, summary=summary)


def _build_strategy_summary(metadata: Mapping[str, Any]) -> StrategySummary:
    live_game = (
        metadata.get("live_game") if isinstance(metadata.get("live_game"), Mapping) else {}
    )
    home = live_game.get("home_name") or ""
    away = live_game.get("away_name") or ""
    period = live_game.get("period") or ""
    label_parts = [str(p).strip() for p in (home, "vs" if home and away else "", away, period) if str(p).strip()]
    label = " ".join(label_parts)
    return StrategySummary(
        action=str(metadata.get("tail_action") or ""),
        reason=str(metadata.get("tail_reason") or ""),
        label=label,
        market_type=str(metadata.get("market_type") or ""),
        side=str(metadata.get("side") or ""),
        line=decimal_from_metadata(metadata.get("line")),
        best_ask=decimal_from_metadata(metadata.get("best_ask")),
        observed_at=None,
        manual_confirmed=bool(metadata.get("manual_confirmed")),
        confirmed_by=str(metadata.get("confirmed_by") or ""),
        confirm_reason=str(metadata.get("confirm_reason") or ""),
        extras={
            "league": live_game.get("league"),
            "home_name": live_game.get("home_name"),
            "away_name": live_game.get("away_name"),
            "period": live_game.get("period"),
            "observed_at": live_game.get("observed_at"),
            "game_status": metadata.get("game_status") or live_game.get("status"),
            "total_score": metadata.get("total_score"),
            "seconds_remaining": metadata.get("seconds_remaining"),
            "execution_permission": metadata.get("execution_permission"),
            "market_family": metadata.get("market_family"),
            "risk_reason": metadata.get("risk_reason"),
        },
    )


def _metadata_decimal(context: DecisionContext, *keys: str) -> Decimal | None:
    """按优先顺序从 metadata 中读取十进制数值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is None:
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return None
    return None


def _metadata_text(context: DecisionContext, *keys: str) -> str | None:
    """按优先顺序从 metadata 中读取文本值。"""

    for key in keys:
        value = context.metadata.get(key)
        if value is not None:
            return str(value)
    return None
