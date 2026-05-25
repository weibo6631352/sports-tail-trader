"""下单前的强制门禁（CLAUDE.md §3）。

只保留 4 道闸——其他本地预校验全部交给 Polymarket CLOB 兜底
（tick / min size / notional / liquidity 等 CLOB 会同步拒回）。本地只守
**4 类资金安全相关的硬约束**，避免 RTT 风险或 CLOB 不会拒的危险动作：

1. ``market_state_gate``     — 不向 resolved/cancelled/archived/inactive 市场下单
2. ``bankroll_total_gate``   — 已投 + 本笔 ≤ bankroll（防超买）
3. ``balance_gate``/``allowance_gate`` — USDC 余额 / allowance 够（含估算 fee）
4. ``buy_order_type_gate``   — BUY 必须是 FAK 或 GTC+post_only（CLAUDE.md §3
   "买入侧不得保留长期 resting BUY order"，CLOB 本身不会拒 resting BUY）

实现是纯函数 + 无 IO，所有判断只读传入的内存快照——P0 路径不允许为了
下单临时打 REST 或查数据库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from polymarket_trader.domain.fees import calculate_trade_fee
from polymarket_trader.domain.market import Market, TradingStatus
from polymarket_trader.domain.order import Order, OrderIntent, OrderSide, OrderType
from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.domain.position import Position


@dataclass(frozen=True, slots=True)
class RiskCheck:
    name: str
    passed: bool
    reason: str = ""
    field: str | None = None
    value: object | None = None
    suggested_action: str = ""
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    passed: bool
    reason: str = ""
    trace_id: str = ""
    failed_field: str | None = None
    suggested_action: str = ""
    retryable: bool = False
    checks: tuple[RiskCheck, ...] = field(default_factory=tuple)


class RiskManager:
    """Mandatory gate before any order intent reaches the executor."""

    def check_order_intent(
        self,
        intent: OrderIntent,
        *,
        market: Market | None = None,
        orderbook: OrderbookSnapshot | None = None,
        position: Position | None = None,
        open_orders: Iterable[Order] = (),
        market_active: bool | None = None,
        market_open: bool | None = None,
        clob_enabled: bool | None = None,
        resolved: bool | None = None,
        cancelled: bool | None = None,
        archived: bool | None = None,
        balance_usdc: Decimal | None = None,
        allowance_usdc: Decimal | None = None,
        portfolio_total_invested_usdc: Decimal | None = None,
        bankroll_usdc: Decimal | None = None,
    ) -> RiskDecision:
        del open_orders, orderbook  # 留参数兼容 caller，但 4 道闸全部不用
        checks: list[RiskCheck] = []
        notional_usdc = _intent_notional_usdc(intent)
        portfolio_total = (
            Decimal("0")
            if portfolio_total_invested_usdc is None or portfolio_total_invested_usdc < Decimal("0")
            else portfolio_total_invested_usdc
        )

        decision = self._check_market_state(
            intent,
            checks,
            market=market,
            position=position,
            market_active=market_active,
            market_open=market_open,
            clob_enabled=clob_enabled,
            resolved=resolved,
            cancelled=cancelled,
            archived=archived,
        )
        if decision is not None:
            return decision

        decision = self._check_bankroll_total(
            intent,
            checks,
            notional_usdc=notional_usdc,
            portfolio_total_invested_usdc=portfolio_total,
            bankroll_usdc=bankroll_usdc,
        )
        if decision is not None:
            return decision

        decision = self._check_balance_and_allowance(
            intent,
            checks,
            market=market,
            notional_usdc=notional_usdc,
            balance_usdc=balance_usdc,
            allowance_usdc=allowance_usdc,
        )
        if decision is not None:
            return decision

        decision = self._check_buy_order_type(intent, checks)
        if decision is not None:
            return decision

        return RiskDecision(
            trace_id=intent.trace_id,
            passed=True,
            reason="passed",
            suggested_action="submit",
            retryable=False,
            checks=tuple(checks),
        )

    # ---- 1. market_state_gate -----------------------------------------

    def _check_market_state(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        market: Market | None,
        position: Position | None,
        market_active: bool | None,
        market_open: bool | None,
        clob_enabled: bool | None,
        resolved: bool | None,
        cancelled: bool | None,
        archived: bool | None,
    ) -> RiskDecision | None:
        (
            market_active,
            market_open,
            clob_enabled,
            resolved,
            cancelled,
            archived,
        ) = _resolve_market_flags(
            market=market,
            market_active=market_active,
            market_open=market_open,
            clob_enabled=clob_enabled,
            resolved=resolved,
            cancelled=cancelled,
            archived=archived,
        )

        # CANDIDATE 市场仅允许已持仓的减仓 SELL（恢复/补救路径），其它统一拒。
        candidate_position_reducing_sell = _is_candidate_position_reducing_sell(
            intent,
            market=market,
            position=position,
        )

        for name, passed, reason, field_name, suggested_action in (
            (
                "market_active_gate",
                market_active or candidate_position_reducing_sell,
                "market_not_active",
                "market.active",
                "refresh_snapshot",
            ),
            (
                "market_open_gate",
                market_open or candidate_position_reducing_sell,
                "market_not_open",
                "market.open",
                "refresh_snapshot",
            ),
            ("clob_gate", clob_enabled, "clob_disabled", "market.clob_enabled", "refresh_snapshot"),
            ("resolved_gate", not resolved, "market_resolved", "market.resolved", "reject"),
            ("cancelled_gate", not cancelled, "market_cancelled", "market.cancelled", "reject"),
            ("archived_gate", not archived, "market_archived", "market.archived", "reject"),
        ):
            checks.append(RiskCheck(name=name, passed=bool(passed), field=field_name, value=passed))
            if not passed:
                return self._fail(
                    trace_id=intent.trace_id,
                    checks=checks,
                    name=name,
                    reason=reason,
                    field=field_name,
                    suggested_action=suggested_action,
                    retryable=False,
                )

        if market is not None and market.trading_status != TradingStatus.ELIGIBLE:
            if candidate_position_reducing_sell:
                checks.append(
                    RiskCheck(
                        name="market_status_gate",
                        passed=True,
                        field="market.trading_status",
                        value={
                            "status": market.trading_status,
                            "allowed_for_position_reducing_sell": True,
                        },
                    )
                )
                return None
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="market_status_gate",
                reason="market_not_active",
                field="market.trading_status",
                value=market.trading_status,
                suggested_action="reject",
                retryable=False,
            )
        return None

    # ---- 2. bankroll_total_gate ---------------------------------------

    def _check_bankroll_total(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        notional_usdc: Decimal,
        portfolio_total_invested_usdc: Decimal,
        bankroll_usdc: Decimal | None,
    ) -> RiskDecision | None:
        """SELL/cancel/replace 不动预算；只对 BUY 做检查。"""

        if intent.side != OrderSide.BUY:
            return None
        if bankroll_usdc is None or bankroll_usdc <= Decimal("0"):
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="bankroll_gate",
                reason="bankroll_non_positive",
                field="bankroll_usdc",
                value={"bankroll_usdc": bankroll_usdc},
                suggested_action="fund_or_reduce_budget",
                retryable=False,
            )
        if portfolio_total_invested_usdc + notional_usdc > bankroll_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="bankroll_total_gate",
                reason="bankroll_overspent",
                field="portfolio.total_invested_usdc",
                value={
                    "portfolio_total_invested_usdc": portfolio_total_invested_usdc,
                    "notional_usdc": notional_usdc,
                    "bankroll_usdc": bankroll_usdc,
                },
                suggested_action="reduce_size",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="bankroll_total_gate",
                passed=True,
                field="portfolio.total_invested_usdc",
                value={
                    "portfolio_total_invested_usdc": portfolio_total_invested_usdc,
                    "notional_usdc": notional_usdc,
                    "bankroll_usdc": bankroll_usdc,
                },
            )
        )
        return None

    # ---- 3. balance + allowance gate ----------------------------------

    def _check_balance_and_allowance(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
        *,
        market: Market | None,
        notional_usdc: Decimal,
        balance_usdc: Decimal | None,
        allowance_usdc: Decimal | None,
    ) -> RiskDecision | None:
        if intent.side != OrderSide.BUY:
            return None
        estimated_fee_usdc = _estimated_buy_taker_fee_usdc(intent, market=market)
        required_usdc = notional_usdc + estimated_fee_usdc
        if balance_usdc is not None and required_usdc > balance_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="balance_gate",
                reason="balance_insufficient",
                field="account.balance_usdc",
                value={
                    "balance_usdc": balance_usdc,
                    "notional_usdc": notional_usdc,
                    "estimated_fee_usdc": estimated_fee_usdc,
                    "required_balance_usdc": required_usdc,
                },
                suggested_action="reduce_size",
                retryable=False,
            )
        if allowance_usdc is not None and required_usdc > allowance_usdc:
            return self._fail(
                trace_id=intent.trace_id,
                checks=checks,
                name="allowance_gate",
                reason="allowance_insufficient",
                field="account.allowance_usdc",
                value={
                    "allowance_usdc": allowance_usdc,
                    "notional_usdc": notional_usdc,
                    "estimated_fee_usdc": estimated_fee_usdc,
                    "required_allowance_usdc": required_usdc,
                },
                suggested_action="approve_or_reduce",
                retryable=False,
            )
        checks.append(
            RiskCheck(
                name="balance_gate",
                passed=True,
                field="account.balance_usdc",
                value={
                    "balance_usdc": balance_usdc,
                    "allowance_usdc": allowance_usdc,
                    "required_usdc": required_usdc,
                },
            )
        )
        return None

    # ---- 4. buy_order_type_gate ---------------------------------------

    def _check_buy_order_type(
        self,
        intent: OrderIntent,
        checks: list[RiskCheck],
    ) -> RiskDecision | None:
        """禁止 long-resting BUY：BUY 只允许 FAK 或 GTC+post_only。

        CLAUDE.md §3「买入侧不得保留长期 resting BUY order」。CLOB 本身不会
        拒 long-resting BUY，所以这是本地必守的强约束。
        """

        if intent.side != OrderSide.BUY:
            return None
        if intent.order_type == OrderType.FAK:
            checks.append(
                RiskCheck(
                    name="buy_order_type_gate",
                    passed=True,
                    field="intent.order_type",
                    value="FAK",
                )
            )
            return None
        if intent.order_type == OrderType.GTC and intent.post_only:
            checks.append(
                RiskCheck(
                    name="buy_order_type_gate",
                    passed=True,
                    field="intent.order_type",
                    value="GTC+post_only",
                )
            )
            return None
        return self._fail(
            trace_id=intent.trace_id,
            checks=checks,
            name="buy_order_type_gate",
            reason="resting_buy_not_allowed",
            field="intent.order_type",
            value={
                "order_type": intent.order_type.value if intent.order_type else None,
                "post_only": intent.post_only,
            },
            suggested_action="use_fak_or_post_only_gtc",
            retryable=False,
        )

    # ---- 失败构造 ------------------------------------------------------

    def _fail(
        self,
        *,
        trace_id: str,
        checks: list[RiskCheck],
        name: str,
        reason: str,
        field: str | None,
        suggested_action: str,
        retryable: bool,
        value: object | None = None,
    ) -> RiskDecision:
        checks.append(
            RiskCheck(
                name=name,
                passed=False,
                reason=reason,
                field=field,
                value=value,
                suggested_action=suggested_action,
                retryable=retryable,
            )
        )
        return RiskDecision(
            trace_id=trace_id,
            passed=False,
            reason=reason,
            failed_field=field,
            suggested_action=suggested_action,
            retryable=retryable,
            checks=tuple(checks),
        )


# ===== helpers =====


def _intent_notional_usdc(intent: OrderIntent) -> Decimal:
    if intent.notional_usdc is not None:
        return intent.notional_usdc
    if intent.amount_usdc is not None:
        return intent.amount_usdc
    if intent.size_shares is not None:
        return intent.price * intent.size_shares
    return Decimal("0")


def _is_candidate_position_reducing_sell(
    intent: OrderIntent,
    *,
    market: Market | None,
    position: Position | None,
) -> bool:
    """允许已有持仓在仅有本地候选快照时继续提交减仓 SELL。

    CANDIDATE 表示本地发现/恢复链路尚未拿到完整市场元数据，不等同于权威终态。
    该放行只适用于已持仓且 size 被持仓覆盖的 SELL；BUY 和无持仓 SELL 仍按
    常规 market gate 拒绝。
    """

    if intent.side != OrderSide.SELL:
        return False
    if market is None or market.trading_status != TradingStatus.CANDIDATE:
        return False
    if position is None:
        return False
    if position.condition_id != intent.condition_id or position.token_id != intent.token_id:
        return False
    size_shares = intent.size_shares or (
        intent.amount_usdc / intent.price if intent.amount_usdc and intent.price > 0 else Decimal("0")
    )
    return size_shares > Decimal("0") and position.shares >= size_shares


def _is_post_only_maker_buy(intent: OrderIntent) -> bool:
    return (
        intent.side == OrderSide.BUY
        and intent.order_type == OrderType.GTC
        and intent.post_only
    )


def _estimated_buy_taker_fee_usdc(intent: OrderIntent, *, market: Market | None) -> Decimal:
    if market is None or intent.side != OrderSide.BUY or _is_post_only_maker_buy(intent):
        return Decimal("0")
    fee_rate_bps = _effective_taker_fee_rate_bps(market)
    if fee_rate_bps is None or fee_rate_bps <= 0 or intent.amount_usdc is None or intent.price <= Decimal("0"):
        return Decimal("0")
    fee_quote = calculate_trade_fee(
        price=intent.price,
        size_shares=intent.amount_usdc / intent.price,
        side="buy",
        fee_rate_bps=fee_rate_bps,
        fees_enabled=market.fees_enabled,
        liquidity_role="taker",
    )
    return fee_quote.fee_usdc


def _effective_taker_fee_rate_bps(market: Market) -> int | None:
    if market.fees_enabled is False:
        return 0
    if market.fee_rate_bps is not None:
        return market.fee_rate_bps
    return market.taker_base_fee_bps


def _resolve_market_flags(
    *,
    market: Market | None,
    market_active: bool | None,
    market_open: bool | None,
    clob_enabled: bool | None,
    resolved: bool | None,
    cancelled: bool | None,
    archived: bool | None,
) -> tuple[bool, bool, bool, bool, bool, bool]:
    if market is not None:
        if market_active is None:
            market_active = market.trading_status == TradingStatus.ELIGIBLE
        if market_open is None:
            market_open = market.trading_status == TradingStatus.ELIGIBLE
        if resolved is None:
            resolved = market.trading_status == TradingStatus.RESOLVED
    if market is None and market_active is None and market_open is None:
        # 完全无市场信息时保守拒：避免假设安全通过。
        market_active = False
        market_open = False
        clob_enabled = False if clob_enabled is None else clob_enabled
    market_active = True if market_active is None else market_active
    market_open = True if market_open is None else market_open
    clob_enabled = True if clob_enabled is None else clob_enabled
    resolved = False if resolved is None else resolved
    cancelled = False if cancelled is None else cancelled
    archived = False if archived is None else archived
    return market_active, market_open, clob_enabled, resolved, cancelled, archived
