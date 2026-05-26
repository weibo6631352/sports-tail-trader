from __future__ import annotations

from typing import Any

from polymarket_trader.domain.order import OrderSide, OrderType
from polymarket_trader.infra.polymarket.order_execution_types import OrderExecutionRequest


def _text(value: Any | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def order_type_text(order_type: OrderType | str | None) -> str:
    if isinstance(order_type, OrderType):
        return order_type.value
    text = _text(order_type)
    return text or OrderType.GTC.value


def is_market_order_type(order_type: OrderType | str | None) -> bool:
    return order_type_text(order_type).upper() in {"FAK", "FOK"}


def is_limit_order_type(order_type: OrderType | str | None) -> bool:
    return order_type_text(order_type).upper() in {"GTC", "GTD"}


def build_signed_order(
    *,
    client: Any,
    exported: dict[str, Any],
    request: OrderExecutionRequest,
) -> Any:
    # py-clob-client-v2 的 order_args/market_order_args 强制要求 float。
    # workflow 层（round_to_tick）在构造请求前已将价格/数量对齐到 tick_size（≥ 0.01），
    # 因此 float 转换引入的精度误差（≤ 1 ULP ≈ 1e-15）远小于最小 tick 粒度。
    requested_order_type = order_type_text(request.order_type).upper()
    order_type_enum = exported["order_type"]
    if not hasattr(order_type_enum, requested_order_type):
        raise ValueError(f"order type {requested_order_type!r} not supported by clob client")
    official_order_type = getattr(order_type_enum, requested_order_type)

    if request.side == OrderSide.BUY:
        if request.amount_usdc is None:
            raise ValueError("BUY order requires amount_usdc")
        if is_market_order_type(request.order_type):
            order_args = exported["market_order_args"](
                token_id=request.token_id,
                amount=float(request.amount_usdc),
                side=request.side.value,
                price=0 if request.price is None else float(request.price),
                order_type=official_order_type,
            )
            return client.create_market_order(order_args)
        if not is_limit_order_type(request.order_type):
            raise ValueError(f"unsupported BUY order type: {requested_order_type}")
        if request.price is None or request.price <= 0:
            raise ValueError("BUY limit order requires price")
        order_args = exported["order_args"](
            token_id=request.token_id,
            price=float(request.price),
            size=float(request.amount_usdc / request.price),
            side=request.side.value,
        )
        return client.create_order(order_args)

    if request.side == OrderSide.SELL:
        if request.price is None or request.size_shares is None:
            raise ValueError("SELL order requires price and size_shares")
        if is_market_order_type(request.order_type):
            order_args = exported["market_order_args"](
                token_id=request.token_id,
                amount=float(request.size_shares),
                side=request.side.value,
                price=float(request.price),
                order_type=official_order_type,
            )
            return client.create_market_order(order_args)
        if not is_limit_order_type(request.order_type):
            raise ValueError(f"unsupported SELL order type: {requested_order_type}")
        order_args = exported["order_args"](
            token_id=request.token_id,
            price=float(request.price),
            size=float(request.size_shares),
            side=request.side.value,
        )
        return client.create_order(order_args)

    raise ValueError(f"unsupported order side for signing: {request.side!r}")
