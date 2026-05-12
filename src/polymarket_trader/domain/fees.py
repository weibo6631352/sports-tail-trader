from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, cast

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.orderbook import OrderbookSnapshot

TradeSide = Literal["buy", "sell"]
LiquidityRole = Literal["maker", "taker"]
ChargedIn = Literal["shares", "usdc"]
PriceSource = Literal["best_ask", "best_bid"]

_ZERO = Decimal("0")
_ONE = Decimal("1")
_FEE_QUANTUM = Decimal("0.00001")
# Polymarket documents fee rates like 0.03 while the fee-rate endpoint returns 30.
_POLYMARKET_FEE_RATE_DENOMINATOR = Decimal("1000")


@dataclass(frozen=True, slots=True)
class FeeQuote:
    price: Decimal
    size_shares: Decimal
    fee_rate_bps: int
    fee_usdc: Decimal
    fee_shares: Decimal | None
    charged_in: ChargedIn
    side: TradeSide
    liquidity_role: LiquidityRole
    price_source: PriceSource | None = None


@dataclass(frozen=True, slots=True)
class TakerFeePreview:
    basis_size_shares: Decimal
    fee_rate_bps: int
    buy: FeeQuote | None
    sell: FeeQuote | None


def calculate_trade_fee(
    *,
    price: Decimal,
    size_shares: Decimal,
    side: TradeSide,
    fee_rate_bps: int | None,
    fees_enabled: bool | None = True,
    liquidity_role: LiquidityRole = "taker",
    price_source: PriceSource | None = None,
) -> FeeQuote:
    normalized_side = _normalize_side(side)
    normalized_role = _normalize_liquidity_role(liquidity_role)
    normalized_price = _normalize_price(price)
    normalized_size = _normalize_size(size_shares)
    charged_in: ChargedIn = "shares" if normalized_side == "buy" else "usdc"

    effective_fee_rate_bps = 0
    if fees_enabled is not False and normalized_role == "taker" and fee_rate_bps is not None and fee_rate_bps > 0:
        effective_fee_rate_bps = fee_rate_bps

    fee_usdc = _round_fee_usdc(
        normalized_size
        * _bps_to_rate(effective_fee_rate_bps)
        * normalized_price
        * (_ONE - normalized_price)
    )
    fee_shares: Decimal | None = None
    if charged_in == "shares":
        fee_shares = _ZERO if normalized_price == _ZERO else _round_fee_shares(fee_usdc / normalized_price)

    return FeeQuote(
        price=normalized_price,
        size_shares=normalized_size,
        fee_rate_bps=effective_fee_rate_bps,
        fee_usdc=fee_usdc,
        fee_shares=fee_shares,
        charged_in=charged_in,
        side=normalized_side,
        liquidity_role=normalized_role,
        price_source=price_source,
    )


def build_taker_fee_preview(
    *,
    market: Market,
    orderbook: OrderbookSnapshot | None,
    basis_size_shares: Decimal = Decimal("100"),
) -> TakerFeePreview | None:
    if orderbook is None:
        return None

    normalized_basis = _normalize_size(basis_size_shares)
    effective_fee_rate_bps = resolve_taker_fee_rate_bps(market)
    if effective_fee_rate_bps is None:
        return None

    buy_quote = None
    if orderbook.best_ask is not None:
        buy_quote = calculate_trade_fee(
            price=orderbook.best_ask,
            size_shares=normalized_basis,
            side="buy",
            fee_rate_bps=effective_fee_rate_bps,
            fees_enabled=market.fees_enabled,
            liquidity_role="taker",
            price_source="best_ask",
        )

    sell_quote = None
    if orderbook.best_bid is not None:
        sell_quote = calculate_trade_fee(
            price=orderbook.best_bid,
            size_shares=normalized_basis,
            side="sell",
            fee_rate_bps=effective_fee_rate_bps,
            fees_enabled=market.fees_enabled,
            liquidity_role="taker",
            price_source="best_bid",
        )

    if buy_quote is None and sell_quote is None:
        return None

    return TakerFeePreview(
        basis_size_shares=normalized_basis,
        fee_rate_bps=effective_fee_rate_bps if market.fees_enabled is not False else 0,
        buy=buy_quote,
        sell=sell_quote,
    )


def resolve_taker_fee_rate_bps(market: Market | None) -> int | None:
    """决定一个市场该按多少 bps 收 taker fee。

    优先级：``fees_enabled=False`` → 0；``fee_rate_bps`` 覆盖值 → 用它；否则
    ``taker_base_fee_bps``；都没就 ``None``（caller 自行决定 fallback）。
    ``market is None`` 同样返回 ``None``——paper engine 偶尔会被传入未登记的市场。
    """

    if market is None:
        return None
    if market.fees_enabled is False:
        return 0
    if market.fee_rate_bps is not None:
        return market.fee_rate_bps
    if market.taker_base_fee_bps is not None:
        return market.taker_base_fee_bps
    return None


def _normalize_side(value: TradeSide) -> TradeSide:
    normalized = str(value).strip().lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError(f"unsupported trade side: {value!r}")
    return cast(TradeSide, normalized)


def _normalize_liquidity_role(value: LiquidityRole) -> LiquidityRole:
    normalized = str(value).strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError(f"unsupported liquidity role: {value!r}")
    return cast(LiquidityRole, normalized)


def _normalize_price(value: Decimal) -> Decimal:
    if value < _ZERO or value > _ONE:
        raise ValueError(f"price must be within [0, 1], got {value}")
    return value


def _normalize_size(value: Decimal) -> Decimal:
    if value < _ZERO:
        raise ValueError(f"size_shares must be >= 0, got {value}")
    return value


def _bps_to_rate(value: int) -> Decimal:
    if value <= 0:
        return _ZERO
    return Decimal(value) / _POLYMARKET_FEE_RATE_DENOMINATOR


def _round_fee_usdc(value: Decimal) -> Decimal:
    return value.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_UP)


def _round_fee_shares(value: Decimal) -> Decimal:
    return value.quantize(_FEE_QUANTUM, rounding=ROUND_HALF_UP)


__all__ = [
    "ChargedIn",
    "FeeQuote",
    "LiquidityRole",
    "PriceSource",
    "TakerFeePreview",
    "TradeSide",
    "build_taker_fee_preview",
    "calculate_trade_fee",
]
