from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from polymarket_trader.domain.orderbook import PriceLevel

_FEE_RATE_DENOMINATOR = Decimal("1000")


def decimal_value(value: Any | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    return Decimal(text)


def first_value(mapping: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


def nested_mapping(mapping: Mapping[str, Any], *keys: str) -> Mapping[str, Any] | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def bool_value(value: Any | None) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def bps_value(value: Any | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        return None
    if numeric == numeric.to_integral_value():
        return int(numeric)
    if abs(numeric) < Decimal("1"):
        return int((numeric * Decimal("10000")).to_integral_value())
    return int(numeric.to_integral_value())


def fee_rate_units(value: Any | None) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        return None
    if numeric < Decimal("0"):
        return None
    if numeric < Decimal("1"):
        return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())
    if numeric == numeric.to_integral_value():
        return int(numeric)
    return int((numeric * _FEE_RATE_DENOMINATOR).to_integral_value())


def datetime_value(value: Any | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = Decimal(text)
    except InvalidOperation:
        numeric = None
    if numeric is not None:
        timestamp = float(numeric)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def extract_token_id(message: Mapping[str, Any]) -> str | None:
    value = first_value(
        message,
        "token_id",
        "tokenId",
        "asset_id",
        "assetId",
        "market_token_id",
        "winning_asset_id",
    )
    return None if value is None else str(value)


def extract_sequence(message: Mapping[str, Any]) -> int | None:
    value = first_value(message, "sequence", "seq", "version")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_levels(value: Any) -> tuple[PriceLevel, ...]:
    if not isinstance(value, list):
        return ()
    levels: list[PriceLevel] = []
    for item in value:
        if isinstance(item, Mapping):
            price = decimal_value(first_value(item, "price", "p"))
            size = decimal_value(first_value(item, "size", "quantity", "qty", "amount"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = decimal_value(item[0])
            size = decimal_value(item[1])
        else:
            continue
        if price is None or size is None:
            continue
        levels.append(PriceLevel(price=price, size=size))
    return tuple(levels)


def message_type(message: Mapping[str, Any]) -> str:
    value = first_value(message, "event_type", "message_type", "channel_event", "event", "type", "action")
    return "" if value is None else str(value).strip().lower()


def extract_token_candidates(message: Mapping[str, Any]) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()
    direct = extract_token_id(message)
    if direct is not None and direct not in seen:
        candidates.append(direct)
        seen.add(direct)
    for key in ("assets_ids", "clob_token_ids"):
        value = message.get(key)
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            text = str(item).strip()
            if text and text not in seen:
                candidates.append(text)
                seen.add(text)
    return tuple(candidates)


def best_price(levels: tuple[PriceLevel, ...]) -> Decimal | None:
    """bids 中"真实"best_bid: 排除 MM 天花板单 $0.99 (极远端接 SELL 的挂单)。

    实盘 case: 同名 fuj-iwa bids 含 $0.99 size=大量 天花板单 → max(price)
    取 $0.99 错。真 best_bid 应排除 > 0.95 的高位 bids (远端 MM 挂单)。

    与 worst_ask 对称: best_bid 在 0.01-0.95 区间合理范围。
    """

    if not levels:
        return None
    # 排除 ≥ 1-tick 的天花板单(MM 机器人 $0.99 接 SELL 的远端挂单)。
    # Polymarket tick 0.01,真 best_bid 在 (0.01, 0.99) 区间(含端点取实际数据)。
    real_prices = [level.price for level in levels if level.price < Decimal("0.99")]
    if not real_prices:
        # 所有 bid 都 ≥ 0.99? 退到 max (极接近 settle 状态)。
        return max(level.price for level in levels)
    return max(real_prices)


def worst_ask(levels: tuple[PriceLevel, ...]) -> Decimal | None:
    """asks 中"真实"best_ask: 排除地板/天花板挂单($0.01-$0.02 极远端 MM 单)。

    实盘 bug case: j2100-fuj-iwa Fuj-YES asks 数组含 $0.01 size=4207 地板单 +
    真挂单 $0.97-$0.99 各 5-30 万 size。`min(price)` 取到 $0.01 (天花板单),
    导致 best_ask=$0.01 严重错,污染 microprice / odds_gap edge / reprice
    等所有下游决策。

    实际 best_ask 应为 asks 中**最低且不是地板单**的价位。简单守卫:
    排除 < 0.05 的价 (Polymarket tick 0.01,< 0.05 几乎都是 MM 地板单)。
    所有真实 best_ask 在 0.05-0.99 区间。
    """

    if not levels:
        return None
    # 排除 ≤ tick_size 的极端地板单(MM 机器人 $0.001-$0.01 兜底单,
    # 实际不该当作真 best_ask)。Polymarket tick 0.01 是常见值,$0.02 起算真挂单。
    real_prices = [level.price for level in levels if level.price > Decimal("0.01")]
    if not real_prices:
        # 所有 ask 都 ≤ 0.01? 退到 min 保守(可能真极冷市场)。
        return min(level.price for level in levels)
    return min(real_prices)
