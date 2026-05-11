"""把订单簿撮合 + fee 计算 + 账本更新组装成 ``OrderExecutionResponse``。

设计要点：
- 沙箱不破坏 §3 的"OrderExecutor 唯一下单出口"——本模块只是 client 实现细节；
- BUY 一律按 taker / FAK 模拟（策略侧禁止长期 resting BUY，§3）；
- SELL GTC 返回 LIVE（资金/仓位都不动），与生产 limit SELL 行为一致；
- SELL FAK 走 taker 撮合；
- 撮合不到任何份额返回 NO_FILL；超出可用流动性返回 PARTIAL_FILL。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from typing import Literal

from polymarket_trader.domain.fees import FeeQuote, calculate_trade_fee
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.order import OrderResultStatus, OrderSide, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)

from .orderbook_matcher import MatchResult, match_taker_buy, match_taker_sell
from .state import PaperVirtualLedger

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    """沙箱单次提交的完整结果，便于上层组装审计 payload。"""

    response: OrderExecutionResponse
    match_result: MatchResult | None
    fee_quote: FeeQuote | None


def simulate_fill(
    request: OrderExecutionRequest,
    *,
    market: Market | None,
    orderbook: OrderbookSnapshot | None,
    ledger: PaperVirtualLedger,
) -> SimulationOutcome:
    """模拟一次订单提交。"""

    if request.action != "submit":
        return _passthrough_response(
            status=OrderResultStatus.UNKNOWN_TIMEOUT,
            reason=f"paper_simulate_unsupported_action_{request.action}",
        )

    if request.side == OrderSide.BUY:
        return _simulate_buy(request, market=market, orderbook=orderbook, ledger=ledger)
    if request.side == OrderSide.SELL:
        return _simulate_sell(request, market=market, orderbook=orderbook, ledger=ledger)

    return _passthrough_response(
        status=OrderResultStatus.REJECTED,
        reason="paper_simulate_missing_side",
    )


def _simulate_buy(
    request: OrderExecutionRequest,
    *,
    market: Market | None,
    orderbook: OrderbookSnapshot | None,
    ledger: PaperVirtualLedger,
) -> SimulationOutcome:
    amount_usdc = _decimal(request.amount_usdc)
    limit_price = _optional_decimal(request.price)
    if amount_usdc <= _ZERO:
        return _passthrough_response(
            status=OrderResultStatus.REJECTED,
            reason="paper_buy_zero_amount",
        )

    match = match_taker_buy(orderbook, amount_usdc, limit_price)
    if not match.is_filled:
        return SimulationOutcome(
            response=OrderExecutionResponse(
                status=OrderResultStatus.NO_FILL,
                order_id=f"paper-buy-nofill-{request.trace_id}",
                matched_shares=_ZERO,
                remaining_shares=_ZERO,
                spent_usdc=_ZERO,
                raw_response=_buy_raw(match, fee_quote=None),
                reason="paper_buy_no_liquidity",
            ),
            match_result=match,
            fee_quote=None,
        )

    fee_rate_bps = _resolve_fee_rate_bps(market)
    # 逐档求 fee：Polymarket 公式含 min(price, 1-price) 在 0.5 附近非线性，
    # 跨档撮合时按 avg_price 单次估算会偏离逐档求和；故按 consumed_levels 累加。
    fee_quote = _aggregate_fees(
        match=match,
        side="buy",
        fee_rate_bps=fee_rate_bps,
        fees_enabled=None if market is None else market.fees_enabled,
        price_source="best_ask",
    )
    fee_shares = fee_quote.fee_shares or _ZERO
    ledger.apply_buy_fill(
        token_id=request.token_id,
        gross_spent_usdc=match.gross_notional_usdc,
        gross_filled_shares=match.filled_shares,
        fee_usdc=fee_quote.fee_usdc,
        fee_shares=fee_shares,
    )

    status = OrderResultStatus.FULL_FILL if match.is_full_fill else OrderResultStatus.PARTIAL_FILL
    return SimulationOutcome(
        response=OrderExecutionResponse(
            status=status,
            order_id=f"paper-buy-{request.trace_id}",
            trade_id=f"paper-trade-{request.trace_id}",
            # matched_shares 保持 gross 与生产语义一致（fee 在 share 维度由 Polymarket
            # data API reconcile 反映到仓位真相，不在 OrderResult 链上扣减）；沙箱
            # 的净持仓真相在 ``ledger.positions``。
            matched_shares=match.filled_shares,
            remaining_shares=_ZERO,
            spent_usdc=match.gross_notional_usdc,
            raw_response=_buy_raw(match, fee_quote=fee_quote),
            reason="paper_buy_full_fill" if status == OrderResultStatus.FULL_FILL else "paper_buy_partial_fill",
        ),
        match_result=match,
        fee_quote=fee_quote,
    )


def _simulate_sell(
    request: OrderExecutionRequest,
    *,
    market: Market | None,
    orderbook: OrderbookSnapshot | None,
    ledger: PaperVirtualLedger,
) -> SimulationOutcome:
    size_shares = _decimal(request.size_shares)
    limit_price = _optional_decimal(request.price)
    if size_shares <= _ZERO:
        return _passthrough_response(
            status=OrderResultStatus.REJECTED,
            reason="paper_sell_zero_size",
        )

    if request.order_type == OrderType.GTC:
        return SimulationOutcome(
            response=OrderExecutionResponse(
                status=OrderResultStatus.LIVE,
                order_id=f"paper-sell-live-{request.trace_id}",
                matched_shares=_ZERO,
                remaining_shares=size_shares,
                spent_usdc=_ZERO,
                raw_response={
                    "virtual": True,
                    "side": "SELL",
                    "order_type": "GTC",
                    "limit_price": str(limit_price) if limit_price is not None else None,
                    "size_shares": str(size_shares),
                },
                reason="paper_sell_live",
            ),
            match_result=None,
            fee_quote=None,
        )

    match = match_taker_sell(orderbook, size_shares, limit_price)
    if not match.is_filled:
        return SimulationOutcome(
            response=OrderExecutionResponse(
                status=OrderResultStatus.NO_FILL,
                order_id=f"paper-sell-nofill-{request.trace_id}",
                matched_shares=_ZERO,
                remaining_shares=size_shares,
                spent_usdc=_ZERO,
                raw_response=_sell_raw(match, fee_quote=None),
                reason="paper_sell_no_liquidity",
            ),
            match_result=match,
            fee_quote=None,
        )

    fee_rate_bps = _resolve_fee_rate_bps(market)
    fee_quote = _aggregate_fees(
        match=match,
        side="sell",
        fee_rate_bps=fee_rate_bps,
        fees_enabled=None if market is None else market.fees_enabled,
        price_source="best_bid",
    )
    ledger.apply_sell_fill(
        token_id=request.token_id,
        gross_received_usdc=match.gross_notional_usdc,
        gross_filled_shares=match.filled_shares,
        fee_usdc=fee_quote.fee_usdc,
    )

    status = OrderResultStatus.FULL_FILL if match.is_full_fill else OrderResultStatus.PARTIAL_FILL
    return SimulationOutcome(
        response=OrderExecutionResponse(
            status=status,
            order_id=f"paper-sell-{request.trace_id}",
            trade_id=f"paper-sell-trade-{request.trace_id}",
            matched_shares=match.filled_shares,
            remaining_shares=match.unfilled_size_shares,
            # spent_usdc 在 SELL 语义下表示"成交名义额"（与 order_result_builder
            # 中 ``price × matched_shares`` 一致）；fee 不在此扣减，由 ledger 独立累计。
            spent_usdc=match.gross_notional_usdc,
            raw_response=_sell_raw(match, fee_quote=fee_quote),
            reason="paper_sell_full_fill" if status == OrderResultStatus.FULL_FILL else "paper_sell_partial_fill",
        ),
        match_result=match,
        fee_quote=fee_quote,
    )


def _aggregate_fees(
    *,
    match: MatchResult,
    side: Literal["buy", "sell"],
    fee_rate_bps: int | None,
    fees_enabled: bool | None,
    price_source: Literal["best_ask", "best_bid"],
) -> FeeQuote:
    """对 ``consumed_levels`` 逐档计 fee 并求和。

    Polymarket fee 公式含 ``min(price, 1-price)`` 在 0.5 附近非线性；多档跨 0.5
    时按 avg_price 单次估算会偏离逐档求和。本函数严格按生产语义还原。
    """

    total_fee_usdc = _ZERO
    total_fee_shares = _ZERO
    has_share_fee = False
    fee_rate_bps_used = 0
    for level in match.consumed_levels:
        quote = calculate_trade_fee(
            price=level.price,
            size_shares=level.shares,
            side=side,
            fee_rate_bps=fee_rate_bps,
            fees_enabled=fees_enabled,
            liquidity_role="taker",
            price_source=price_source,
        )
        total_fee_usdc += quote.fee_usdc
        if quote.fee_shares is not None:
            total_fee_shares += quote.fee_shares
            has_share_fee = True
        fee_rate_bps_used = quote.fee_rate_bps
    return FeeQuote(
        price=match.avg_price or _ZERO,
        size_shares=match.filled_shares,
        fee_rate_bps=fee_rate_bps_used,
        fee_usdc=total_fee_usdc,
        fee_shares=total_fee_shares if has_share_fee else None,
        charged_in="shares" if side == "buy" else "usdc",
        side=side,
        liquidity_role="taker",
        price_source=price_source,
    )


def _resolve_fee_rate_bps(market: Market | None) -> int | None:
    """与 ``domain/fees.py::_resolve_effective_taker_fee_rate_bps`` 同语义。"""

    if market is None:
        return None
    if market.fees_enabled is False:
        return 0
    if market.fee_rate_bps is not None:
        return market.fee_rate_bps
    if market.taker_base_fee_bps is not None:
        return market.taker_base_fee_bps
    return None


def _decimal(value: Decimal | str | None) -> Decimal:
    if value is None:
        return _ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _optional_decimal(value: Decimal | str | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _passthrough_response(*, status: OrderResultStatus, reason: str) -> SimulationOutcome:
    return SimulationOutcome(
        response=OrderExecutionResponse(status=status, reason=reason, raw_response={"virtual": True, "reason": reason}),
        match_result=None,
        fee_quote=None,
    )


def _buy_raw(match: MatchResult, *, fee_quote: FeeQuote | None) -> dict[str, object]:
    return {
        "virtual": True,
        "side": "BUY",
        "filled_shares": str(match.filled_shares),
        "avg_price": None if match.avg_price is None else str(match.avg_price),
        "gross_spent_usdc": str(match.gross_notional_usdc),
        "unfilled_amount_usdc": str(match.unfilled_amount_usdc),
        "consumed_levels": [
            {"price": str(level.price), "shares": str(level.shares), "notional_usdc": str(level.notional_usdc)}
            for level in match.consumed_levels
        ],
        "fee_usdc": None if fee_quote is None else str(fee_quote.fee_usdc),
        "fee_shares": None if fee_quote is None or fee_quote.fee_shares is None else str(fee_quote.fee_shares),
        "fee_rate_bps": None if fee_quote is None else fee_quote.fee_rate_bps,
    }


def _sell_raw(match: MatchResult, *, fee_quote: FeeQuote | None) -> dict[str, object]:
    return {
        "virtual": True,
        "side": "SELL",
        "filled_shares": str(match.filled_shares),
        "avg_price": None if match.avg_price is None else str(match.avg_price),
        "gross_received_usdc": str(match.gross_notional_usdc),
        "unfilled_size_shares": str(match.unfilled_size_shares),
        "consumed_levels": [
            {"price": str(level.price), "shares": str(level.shares), "notional_usdc": str(level.notional_usdc)}
            for level in match.consumed_levels
        ],
        "fee_usdc": None if fee_quote is None else str(fee_quote.fee_usdc),
        "fee_rate_bps": None if fee_quote is None else fee_quote.fee_rate_bps,
    }
