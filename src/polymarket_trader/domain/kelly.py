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

# Polymarket tick reality：主流 0.01，少数 prop market 0.001。Kelly 公式需要的
# 价格 boundary = max(tick, 0.0001 floor)；caller 传 ``market_tick_size`` 时按
# tick 做端点，避免 0.001-tick 市场被一刀切归类 ``price_out_of_range``。
_DEFAULT_PRICE_FLOOR = Decimal("0.0001")


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
    market_tick_size: Decimal | None = None,
) -> KellyStake:
    """计算单市场 Kelly 仓位。详细公式见模块 docstring。

    ``market_tick_size`` 可选——按市场 tick 动态调整价格端点（C11）。0.01 tick
    市场端点 [0.01, 0.99]；0.001 tick 市场 [0.001, 0.999]。缺省按硬编码
    [_PRICE_MIN, _PRICE_MAX] = [0.01, 0.99] 兜底。
    """

    effective_kappa = kelly_fraction * prob_confidence

    # 早拒：参数 / 价格 / 概率超出合法域。snapshot 上 effective_min_stake 用
    # market_min（若给）或 min_stake_usdc 兜底，方便审计原因清晰。
    pre_min_stake = _effective_min_stake(
        min_stake_usdc=min_stake_usdc,
        market_min_order_size_shares=market_min_order_size_shares,
        price_c=price_c,
    )
    # tick-aware 价格端点：caller 传 market_tick_size → [tick, 1-tick]；缺省 [0.01, 0.99]。
    if market_tick_size is not None and market_tick_size > _ZERO:
        price_min = max(market_tick_size, _DEFAULT_PRICE_FLOOR)
        price_max = _ONE - market_tick_size
    else:
        price_min = _PRICE_MIN
        price_max = _PRICE_MAX
    if bankroll_usdc <= _ZERO:
        return _reject_with_zero_metrics(
            price_c=price_c,
            fair_value_p=fair_value_p,
            side=side,
            effective_kelly_fraction=effective_kappa,
            effective_min_stake_usdc=pre_min_stake,
            reason="bankroll_non_positive",
        )
    if price_c < price_min or price_c > price_max:
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

    # 单笔硬上限——RiskManager 同款公式（``effective_position_cap_usdc``），保持双侧
    # 闸门口径绝对一致。round_up 路径的 overbet ratio 不在 raw 截断里用，只在
    # 凑齐分支用——见下方 round-up 处理。
    position_cap = effective_position_cap_usdc(
        bankroll_usdc=bankroll_usdc,
        max_position_fraction=max_position_fraction,
    )
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


def effective_position_cap_usdc(
    *,
    bankroll_usdc: Decimal,
    max_position_fraction: Decimal,
    round_up_max_overbet_ratio: Decimal = _ONE,
) -> Decimal:
    """Kelly engine 与 RiskManager 共用的单笔上限计算——避免"两边各算一次"漂移。

    ``effective_cap = bankroll × max_position_fraction × max(1, overbet_ratio)``。
    overbet_ratio 默认 1（不放宽）；> 1 时让风控同步接受 round-up 路径的 over-bet。
    """

    base_cap = bankroll_usdc * max_position_fraction
    if round_up_max_overbet_ratio > _ONE:
        return base_cap * round_up_max_overbet_ratio
    return base_cap


@dataclass(frozen=True, slots=True)
class KellyExitSignal:
    """Kelly 反向 sizing：持仓途中 edge 翻转时给出半止盈 / 反向缩仓建议。

    应用场景：开仓时 fair=0.50, c=0.30 → 看多。持仓中市场涨到 c=0.55，我方
    fair 不变 → edge 反转（c > p）。继续持有 = 负 Kelly。该函数返回应卖出的
    shares 比例（``sell_fraction`` ∈ [0,1]）。0 表示继续持有，1 表示全部退出。

    简化模型：``sell_fraction = clip((c - p) / (1 - p), 0, 1)``——Kelly 在反向
    edge 时的对称解。当前未自动接入策略 decide_exit；调用时机由策略层决定。
    """

    sell_fraction: Decimal
    reverse_edge: Decimal
    reason: str


def kelly_exit_signal(
    *,
    current_price_c: Decimal,
    fair_value_p: Decimal,
    min_reverse_edge: Decimal = Decimal("0.02"),
) -> KellyExitSignal:
    """Kelly exit signal——edge 翻转时建议卖出比例。详见 KellyExitSignal docstring。"""

    reverse_edge = current_price_c - fair_value_p
    if reverse_edge < min_reverse_edge:
        return KellyExitSignal(
            sell_fraction=_ZERO,
            reverse_edge=reverse_edge,
            reason="hold",
        )
    denom = _ONE - fair_value_p
    if denom <= _ZERO:
        return KellyExitSignal(sell_fraction=_ONE, reverse_edge=reverse_edge, reason="full_exit")
    raw = reverse_edge / denom
    if raw >= _ONE:
        return KellyExitSignal(sell_fraction=_ONE, reverse_edge=reverse_edge, reason="full_exit")
    return KellyExitSignal(sell_fraction=raw, reverse_edge=reverse_edge, reason="partial_exit")


def apply_vol_scaling(
    stake_usdc: Decimal,
    *,
    realized_vol: Decimal,
    baseline_vol: Decimal,
) -> Decimal:
    """vol-scaling: 高波动市场缩仓——``stake' = stake × min(1, baseline/realized)²``。

    经验法则：σ 翻倍 → stake 减到 1/4。极薄盘口 / 大新闻事件下波动飙升时
    Kelly 公式假设的 p 估计更不可靠，应额外缩仓。strategy 可调，default 不接入。
    """

    if realized_vol <= _ZERO or baseline_vol <= _ZERO:
        return stake_usdc
    if realized_vol <= baseline_vol:
        return stake_usdc
    factor = baseline_vol / realized_vol
    return stake_usdc * factor * factor


def apply_settlement_discount(
    stake_usdc: Decimal,
    *,
    settlement_seconds: int,
    annualized_rate: Decimal,
) -> Decimal:
    """长持仓的资金占用机会成本折现：stake' = stake × (1 - r × T_year)。

    简化模型（线性近似 exp(-rT)）。outright family 长持仓（数月）应折，tail
    （分钟级）影响可忽略。strategy 可调，default 不接入。
    """

    if settlement_seconds <= 0 or annualized_rate <= _ZERO:
        return stake_usdc
    seconds_per_year = Decimal("31536000")  # 365 × 86400
    discount = annualized_rate * Decimal(settlement_seconds) / seconds_per_year
    if discount >= _ONE:
        return _ZERO
    return stake_usdc * (_ONE - discount)


def apply_dispute_premium(
    edge: Decimal,
    *,
    premium_bps: int,
) -> Decimal:
    """争议性市场（如 ambiguous resolution）edge 扣除——要求 N bps 额外补偿。

    Polymarket 历史上有 UMA dispute 案（如 Khamenei 案）。strategy 标记的
    high-dispute 市场应在原 edge 基础上扣 100-300 bps。default 不接入。
    """

    if premium_bps <= 0:
        return edge
    return edge - Decimal(premium_bps) / Decimal("10000")


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
    "effective_position_cap_usdc",
    "KellyExitSignal",
    "kelly_exit_signal",
    "apply_vol_scaling",
    "apply_settlement_discount",
    "apply_dispute_premium",
    "implied_fair_value_from_price_cap",
]
