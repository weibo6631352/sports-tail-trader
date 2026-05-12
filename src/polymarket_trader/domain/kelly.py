"""Kelly 仓位 sizing 纯函数（Polymarket 二元市场）。

约定（重要）
============

``price_c`` 和 ``fair_value_p`` 永远指**正在买入的那个 token**——无论它是 YES 还是 NO 端。
``side`` 只是审计标签，不影响公式：

* BUY YES：c = YES token 价格；p = 我方估计的 YES 真概率
* BUY NO ：c = NO  token 价格；p = 我方估计的 NO  真概率（即 1 - p_yes_true）

公式
====

无 side 分支，统一：::

    edge   = p - c
    denom  = 1 - c
    f*     = edge / denom
    stake  = bankroll × κ_eff × max(0, f*)
    stake  = min(stake, bankroll × max_position_fraction, liquidity_usdc)

``κ_eff = kelly_fraction × prob_confidence``——quarter Kelly + 模型不确定性收缩。

费用扣减
=========

Polymarket 公式：``fee_per_share = (rate_bps / 1000) × price × (1 - price)``。
``edge_net = edge - fee_per_share``，先扣再算 f*。fees_enabled=False 或 maker
路径时 fee_rate_bps=0。

最小下单兼容
=============

Polymarket ``min_order_size`` 是 share 维度，不同市场不同。effective_min_stake =
max(框架硬下限 ``min_stake_usdc``, ``market_min_order_size_shares × price``)。
若 Kelly 推荐 stake < effective_min_stake：

* ``allow_round_up_to_market_min=True`` 且 round-up 后 ≤
  ``position_cap × round_up_max_overbet_ratio`` → 凑齐 stake = effective_min_stake，
  capped_by=``rounded_up_to_market_min``，``is_round_up_overbet=True``（审计标记）。
* 否则 reject ``bankroll_too_small_for_market_min``。

Domain 约束（CLAUDE.md §3）
==========================

纯函数；无 IO / 副作用 / 浮点；金额价格走 Decimal。
KellyStake 携带完整审计字段，便于 RiskManager / Allocation 落审计事件，
不依赖外部上下文重新计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

KellySide = Literal["BUY_YES", "BUY_NO"]

_ZERO = Decimal("0")
_ONE = Decimal("1")
# Polymarket 主流市场 tick=0.01；少数 0.001 但 Kelly 不为这种边缘 tick 单独建模。
_PRICE_MIN = Decimal("0.01")
_PRICE_MAX = Decimal("0.99")
# Polymarket fee rate 单位陷阱：参数 / 列名都叫 ``fee_rate_bps`` 但**实际是
# per-mille (denominator=1000)**——3% 费率存为 ``30``。与 ``domain/fees.py``
# 和 ``infra/polymarket/schemas/_helpers.py:_coerce_fee_rate_units`` 一致。
# 真 bps（per-10000）会让 fee 少扣 10×，造成 Kelly 虚假 edge。改名风险大
# (跨模块 ripple)，所以 docstring + comment 强标注。
_FEE_RATE_DENOMINATOR = Decimal("1000")


@dataclass(frozen=True, slots=True)
class KellyStake:
    """Kelly 公式的完整决策快照——既是 sizing 输出也是审计字段。"""

    stake_usdc: Decimal
    f_star: Decimal
    edge: Decimal              # 已扣手续费
    edge_gross: Decimal        # 未扣手续费（p - c 或 c - p）
    fee_per_share_usdc: Decimal
    fair_value_p: Decimal
    price_c: Decimal
    side: KellySide
    effective_kelly_fraction: Decimal  # κ × prob_confidence
    effective_min_stake_usdc: Decimal
    capped_by: str | None = None
    is_round_up_overbet: bool = False
    reject_reason: str | None = None


def kelly_stake(
    *,
    bankroll_usdc: Decimal,
    price_c: Decimal,
    fair_value_p: Decimal,
    side: KellySide = "BUY_YES",
    kelly_fraction: Decimal,
    prob_confidence: Decimal = _ONE,
    max_position_fraction: Decimal,
    min_edge: Decimal,
    min_stake_usdc: Decimal,
    market_min_order_size_shares: Decimal | None = None,
    fee_rate_bps: int = 0,
    fees_enabled: bool = True,
    liquidity_usdc: Decimal | None = None,
    allow_round_up_to_market_min: bool = True,
    round_up_max_overbet_ratio: Decimal = _ONE,
) -> KellyStake:
    """计算单市场 Kelly 仓位。详细公式见模块 docstring。"""

    effective_kappa = kelly_fraction * prob_confidence

    # 早拒：参数 / 价格 / 概率超出合法域。snapshot 上 effective_min_stake 用
    # market_min（若给）或 min_stake_usdc 兜底，方便审计原因清晰。
    pre_min_stake = _effective_min_stake(
        min_stake_usdc=min_stake_usdc,
        market_min_order_size_shares=market_min_order_size_shares,
        price_c=price_c,
    )
    if bankroll_usdc <= _ZERO:
        return _reject_with_zero_metrics(
            price_c=price_c,
            fair_value_p=fair_value_p,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reason="bankroll_non_positive",
        )
    if price_c < _PRICE_MIN or price_c > _PRICE_MAX:
        return _reject_with_zero_metrics(
            price_c=price_c,
            fair_value_p=fair_value_p,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reason="price_out_of_range",
        )
    if fair_value_p < _ZERO or fair_value_p > _ONE:
        return _reject_with_zero_metrics(
            price_c=price_c,
            fair_value_p=fair_value_p,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reason="fair_value_out_of_range",
        )
    if prob_confidence < _ZERO or prob_confidence > _ONE:
        return _reject_with_zero_metrics(
            price_c=price_c,
            fair_value_p=fair_value_p,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reason="prob_confidence_out_of_range",
        )

    # 公式 side-agnostic：(c, p) 都指被买入 token 的价格 / 真概率。
    edge_gross = fair_value_p - price_c
    denom = _ONE - price_c

    fee_per_share = _fee_per_share_usdc(
        price_c=price_c,
        fee_rate_bps=fee_rate_bps,
        fees_enabled=fees_enabled,
    )
    edge_net = edge_gross - fee_per_share

    if denom <= _ZERO:
        # 端点兜底（理论上 _PRICE_MIN/MAX 已挡）
        return KellyStake(
            stake_usdc=_ZERO,
            f_star=_ZERO,
            edge=edge_net,
            edge_gross=edge_gross,
            fee_per_share_usdc=fee_per_share,
            fair_value_p=fair_value_p,
            price_c=price_c,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reject_reason="price_out_of_range",
        )

    f_star = edge_net / denom
    if edge_net < min_edge or f_star <= _ZERO:
        return KellyStake(
            stake_usdc=_ZERO,
            f_star=f_star,
            edge=edge_net,
            edge_gross=edge_gross,
            fee_per_share_usdc=fee_per_share,
            fair_value_p=fair_value_p,
            price_c=price_c,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reject_reason="edge_below_min",
        )

    raw_stake = bankroll_usdc * effective_kappa * f_star
    capped_by: str | None = None

    position_cap = bankroll_usdc * max_position_fraction
    stake = raw_stake
    if stake > position_cap:
        stake = position_cap
        capped_by = "max_position_fraction"
    if liquidity_usdc is not None and liquidity_usdc >= _ZERO and stake > liquidity_usdc:
        stake = liquidity_usdc
        capped_by = "liquidity"

    effective_min_stake = pre_min_stake
    is_round_up_overbet = False

    if stake < effective_min_stake:
        # Kelly 推荐 stake < market min。看是否允许凑齐到 market min。
        if not allow_round_up_to_market_min:
            return KellyStake(
                stake_usdc=_ZERO,
                f_star=f_star,
                edge=edge_net,
                edge_gross=edge_gross,
                fee_per_share_usdc=fee_per_share,
                fair_value_p=fair_value_p,
                price_c=price_c,
                side=side,
                effective_kelly_fraction=effective_kappa,
                effective_min_stake_usdc=effective_min_stake,
                capped_by=capped_by,
                reject_reason="kelly_below_market_min_no_round_up",
            )
        # 凑齐后是否超 cap × tolerance？
        round_up_ceiling = position_cap * round_up_max_overbet_ratio
        if liquidity_usdc is not None and liquidity_usdc >= _ZERO:
            round_up_ceiling = min(round_up_ceiling, liquidity_usdc)
        if effective_min_stake > round_up_ceiling:
            return KellyStake(
                stake_usdc=_ZERO,
                f_star=f_star,
                edge=edge_net,
                edge_gross=edge_gross,
                fee_per_share_usdc=fee_per_share,
                fair_value_p=fair_value_p,
                price_c=price_c,
                side=side,
                effective_kelly_fraction=effective_kappa,
                effective_min_stake_usdc=effective_min_stake,
                capped_by=capped_by,
                reject_reason="bankroll_too_small_for_market_min",
            )
        stake = effective_min_stake
        capped_by = "rounded_up_to_market_min"
        is_round_up_overbet = stake > raw_stake

    return KellyStake(
        stake_usdc=stake,
        f_star=f_star,
        edge=edge_net,
        edge_gross=edge_gross,
        fee_per_share_usdc=fee_per_share,
        fair_value_p=fair_value_p,
        price_c=price_c,
        side=side,
        effective_kelly_fraction=effective_kappa,
        effective_min_stake_usdc=effective_min_stake,
        capped_by=capped_by,
        is_round_up_overbet=is_round_up_overbet,
        reject_reason=None,
    )


def implied_fair_value_from_price_cap(
    price_cap: Decimal,
    *,
    min_edge_required: Decimal,
) -> Decimal | None:
    """tail 路径反推 implied fair_value：``cap = fair × (1 - edge_required)``。

    现状策略只配 ``tail_*_max_entry_price``（愿意买入的最高价）+ ``min_edge_bps``，
    隐含了"心里的 fair_value"。把这层信念显式化为 Kelly 公式吃的 ``p``，避免
    Kelly 链路双口径。

    ``min_edge_required >= 1`` 没有数学意义；返回 None 让调用侧降级 record-only。
    """

    if min_edge_required >= _ONE or min_edge_required < _ZERO:
        return None
    fair_value = price_cap / (_ONE - min_edge_required)
    if fair_value <= _ZERO:
        return None
    if fair_value > _ONE:
        return _ONE
    return fair_value


def _fee_per_share_usdc(
    *,
    price_c: Decimal,
    fee_rate_bps: int,
    fees_enabled: bool,
) -> Decimal:
    """Polymarket 每股手续费：``(rate / 1000) × c × (1 - c)``。

    ``fee_rate_bps`` 名字误导——实际单位是 **per-mille**（见 ``_FEE_RATE_DENOMINATOR``
    顶部注释）。与 ``domain/fees.py:calculate_trade_fee`` 同口径。
    """

    if not fees_enabled or fee_rate_bps <= 0:
        return _ZERO
    if price_c <= _ZERO or price_c >= _ONE:
        return _ZERO
    return (Decimal(fee_rate_bps) / _FEE_RATE_DENOMINATOR) * price_c * (_ONE - price_c)


def _effective_min_stake(
    *,
    min_stake_usdc: Decimal,
    market_min_order_size_shares: Decimal | None,
    price_c: Decimal,
) -> Decimal:
    floor = min_stake_usdc
    if market_min_order_size_shares is not None and market_min_order_size_shares > _ZERO and price_c > _ZERO:
        market_min_usdc = market_min_order_size_shares * price_c
        if market_min_usdc > floor:
            return market_min_usdc
    return floor


def _reject_with_zero_metrics(
    *,
    price_c: Decimal,
    fair_value_p: Decimal,
    side: KellySide,
    effective_kelly_fraction: Decimal,
    effective_min_stake_usdc: Decimal,
    reason: str,
) -> KellyStake:
    return KellyStake(
        stake_usdc=_ZERO,
        f_star=_ZERO,
        edge=_ZERO,
        edge_gross=_ZERO,
        fee_per_share_usdc=_ZERO,
        fair_value_p=fair_value_p,
        price_c=price_c,
        side=side,
        effective_kelly_fraction=effective_kelly_fraction,
        effective_min_stake_usdc=effective_min_stake_usdc,
        reject_reason=reason,
    )


__all__ = [
    "KellySide",
    "KellyStake",
    "kelly_stake",
    "implied_fair_value_from_price_cap",
]
