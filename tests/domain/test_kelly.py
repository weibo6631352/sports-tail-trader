"""Kelly sizing 纯函数测试。

覆盖矩阵：
* BUY YES / BUY NO 公式对称
* 早拒：bankroll≤0 / price 越界 / fair_value 越界 / prob_confidence 越界
* edge < min_edge 拒
* fee 抵消 edge 至不足 min_edge
* max_position_fraction 截断
* liquidity 截断
* market_min_order_size round-up（接受 / 超 cap reject）
* allow_round_up_to_market_min=False 时严格拒
* prob_confidence 缩 κ
* 端点价格被 _PRICE_MIN/MAX 挡
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_trader.domain.kelly import (
    KellyStake,
    implied_fair_value_from_price_cap,
    kelly_stake,
)

D = Decimal


def _base_kwargs(**overrides):
    base = dict(
        bankroll_usdc=D("100"),
        price_c=D("0.10"),
        fair_value_p=D("0.20"),
        side="BUY_YES",
        kelly_fraction=D("0.25"),
        prob_confidence=D("1"),
        max_position_fraction=D("0.10"),
        min_edge=D("0.02"),
        min_stake_usdc=D("1"),
        market_min_order_size_shares=None,
        fee_rate_bps=0,
        fees_enabled=True,
        liquidity_usdc=None,
        allow_round_up_to_market_min=True,
        round_up_max_overbet_ratio=D("1"),
    )
    base.update(overrides)
    return base


def test_buy_yes_basic_kelly_math():
    # bankroll=100, c=0.10, p=0.20 → edge=0.10, denom=0.90, f*=0.1111
    # κ=0.25, raw=100*0.25*0.1111=2.7778
    # position_cap=100*0.10=10; raw < cap so no cap
    result = kelly_stake(**_base_kwargs())
    assert result.reject_reason is None
    assert result.f_star == D("0.10") / D("0.90")
    assert result.edge == D("0.10")
    assert result.stake_usdc == pytest.approx(D("2.7778"), rel=D("0.001"))
    assert result.capped_by is None


def test_buy_no_uses_same_formula_as_buy_yes():
    # side 只是 audit 标签：(c, p) 永远指被买入的 token。
    # 例：BUY NO 端 @ 0.10（即 YES 端 0.90 上挂着，但策略买 NO），p_NO_true=0.20。
    # edge=0.10, denom=0.90, f*=0.111；κ=0.25, raw=100*0.25*0.111=2.78 < cap=10
    yes = kelly_stake(**_base_kwargs(side="BUY_YES", price_c=D("0.10"), fair_value_p=D("0.20")))
    no = kelly_stake(**_base_kwargs(side="BUY_NO", price_c=D("0.10"), fair_value_p=D("0.20")))
    assert yes.stake_usdc == no.stake_usdc
    assert yes.f_star == no.f_star
    assert no.side == "BUY_NO"  # 标签独立带出，公式相同


def test_buy_yes_zero_edge_rejected():
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.10")))
    assert result.reject_reason == "edge_below_min"
    assert result.stake_usdc == D("0")


def test_buy_yes_negative_edge_rejected():
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.05")))
    assert result.reject_reason == "edge_below_min"
    assert result.f_star <= D("0")


def test_edge_below_min_edge_rejected():
    # min_edge=0.02, gross edge=0.015 → 拒
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.115"), min_edge=D("0.02")))
    assert result.reject_reason == "edge_below_min"


def test_max_position_fraction_caps_stake():
    # bankroll=100, c=0.10, p=0.50 → edge=0.40, denom=0.90, f*=0.4444
    # κ=0.25, raw=100*0.25*0.4444=11.11 > position_cap=10
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.50")))
    assert result.reject_reason is None
    assert result.stake_usdc == D("10")
    assert result.capped_by == "max_position_fraction"


def test_liquidity_caps_stake_below_position_cap():
    # raw=11.11 → position_cap=10 → liquidity=3 → final=3
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.50"), liquidity_usdc=D("3")))
    assert result.stake_usdc == D("3")
    assert result.capped_by == "liquidity"


def test_bankroll_zero_rejected():
    result = kelly_stake(**_base_kwargs(bankroll_usdc=D("0")))
    assert result.reject_reason == "bankroll_non_positive"


def test_bankroll_negative_rejected():
    result = kelly_stake(**_base_kwargs(bankroll_usdc=D("-10")))
    assert result.reject_reason == "bankroll_non_positive"


def test_price_below_min_rejected():
    result = kelly_stake(**_base_kwargs(price_c=D("0.005"), fair_value_p=D("0.10")))
    assert result.reject_reason == "price_out_of_range"


def test_price_above_max_rejected():
    result = kelly_stake(**_base_kwargs(price_c=D("0.999"), fair_value_p=D("0.999")))
    assert result.reject_reason == "price_out_of_range"


def test_fair_value_negative_rejected():
    result = kelly_stake(**_base_kwargs(fair_value_p=D("-0.1")))
    assert result.reject_reason == "fair_value_out_of_range"


def test_fair_value_above_one_rejected():
    result = kelly_stake(**_base_kwargs(fair_value_p=D("1.1")))
    assert result.reject_reason == "fair_value_out_of_range"


def test_prob_confidence_out_of_range_rejected():
    result = kelly_stake(**_base_kwargs(prob_confidence=D("1.5")))
    assert result.reject_reason == "prob_confidence_out_of_range"


def test_prob_confidence_shrinks_kappa():
    # confidence=0.5 → effective κ=0.125
    # raw=100*0.125*0.1111=1.3889
    result = kelly_stake(
        **_base_kwargs(prob_confidence=D("0.5"))
    )
    assert result.effective_kelly_fraction == D("0.125")
    assert result.stake_usdc == pytest.approx(D("1.3889"), rel=D("0.001"))


def test_fee_reduces_edge_to_below_min_edge():
    # c=0.50, p=0.52 → gross edge=0.02
    # fee_rate_bps=200 (Polymarket "20 bps"=2%): fee_per_share=(200/1000)*0.50*0.50=0.05
    # edge_net = 0.02 - 0.05 = -0.03 → rejected by edge_below_min
    result = kelly_stake(
        **_base_kwargs(price_c=D("0.50"), fair_value_p=D("0.52"), fee_rate_bps=200)
    )
    assert result.reject_reason == "edge_below_min"
    assert result.edge_gross == D("0.02")
    assert result.fee_per_share_usdc == D("0.05")
    assert result.edge == D("-0.03")


def test_fee_partially_reduces_edge_still_passes():
    # c=0.10, p=0.30 → gross edge=0.20
    # fee_rate_bps=20 (0.2%): fee_per_share=(20/1000)*0.10*0.90=0.0018
    # edge_net = 0.20 - 0.0018 = 0.1982; still passes min_edge=0.02
    result = kelly_stake(
        **_base_kwargs(price_c=D("0.10"), fair_value_p=D("0.30"), fee_rate_bps=20)
    )
    assert result.reject_reason is None
    assert result.edge_gross == D("0.20")
    assert result.fee_per_share_usdc == pytest.approx(D("0.0018"), rel=D("0.001"))


def test_fees_disabled_skips_fee():
    result = kelly_stake(
        **_base_kwargs(fee_rate_bps=200, fees_enabled=False)
    )
    assert result.fee_per_share_usdc == D("0")


def test_market_min_round_up_within_cap_accepted():
    # bankroll=100, max_position_fraction=0.10 → cap=10
    # c=0.05, p=0.07 → edge=0.02, f*=0.0211
    # κ=0.25, raw=100*0.25*0.0211=0.526
    # market_min=5 shares → effective_min_stake = 5*0.05=0.25; raw>min so no round-up needed actually
    # Use p=0.052 → edge=0.002 below min_edge → reject
    # Use p=0.06: edge=0.01, below min_edge=0.02
    # So pick: c=0.10, p=0.13 → edge=0.03, f*=0.0333
    # raw=100*0.25*0.0333=0.833
    # market_min=5 shares → effective=5*0.10=0.50; raw>0.50 OK no round-up
    # Force round-up: market_min=20 shares → effective=20*0.10=2.0, raw=0.833 < 2.0
    # round-up to 2.0; 2.0 ≤ position_cap=10 ✓
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.10"),
            fair_value_p=D("0.13"),
            market_min_order_size_shares=D("20"),
        )
    )
    assert result.reject_reason is None
    assert result.stake_usdc == D("2")
    assert result.capped_by == "rounded_up_to_market_min"
    assert result.is_round_up_overbet is True
    assert result.effective_min_stake_usdc == D("2")


def test_market_min_round_up_exceeds_cap_rejected():
    # bankroll=10, max_position_fraction=0.10 → cap=1
    # c=0.20, p=0.23 → edge=0.03, f*=0.0375
    # raw=10*0.25*0.0375=0.094
    # market_min=20 shares → effective=20*0.20=4.0
    # round-up to 4 > cap=1 → reject bankroll_too_small_for_market_min
    result = kelly_stake(
        **_base_kwargs(
            bankroll_usdc=D("10"),
            price_c=D("0.20"),
            fair_value_p=D("0.23"),
            market_min_order_size_shares=D("20"),
        )
    )
    assert result.reject_reason == "bankroll_too_small_for_market_min"
    assert result.effective_min_stake_usdc == D("4")


def test_round_up_with_overbet_ratio_below_one():
    # bankroll=100, position_cap=10, overbet_ratio=0.5 → ceiling=5
    # market_min=60 shares → effective=60*0.10=6 > ceiling=5 → reject
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.10"),
            fair_value_p=D("0.13"),
            market_min_order_size_shares=D("60"),
            round_up_max_overbet_ratio=D("0.5"),
        )
    )
    assert result.reject_reason == "bankroll_too_small_for_market_min"


def test_round_up_disabled_strict_reject():
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.10"),
            fair_value_p=D("0.13"),
            market_min_order_size_shares=D("20"),
            allow_round_up_to_market_min=False,
        )
    )
    assert result.reject_reason == "kelly_below_market_min_no_round_up"


def test_round_up_when_kelly_already_meets_market_min():
    # raw=5, market_min=5 shares*1 USD → effective=5; stake meets it → no round-up needed
    # bankroll=100, c=1.00 invalid; pick c=0.50, p=0.95 → edge=0.45, f*=0.9
    # raw=100*0.25*0.9=22.5 > cap=10 → final=10
    # market_min=5 shares → effective=5*0.50=2.5; 10 >= 2.5 ✓
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.50"),
            fair_value_p=D("0.95"),
            market_min_order_size_shares=D("5"),
        )
    )
    assert result.reject_reason is None
    assert result.stake_usdc == D("10")
    assert result.is_round_up_overbet is False
    # capped_by 是 max_position_fraction 而非 round-up
    assert result.capped_by == "max_position_fraction"


def test_round_up_capped_by_liquidity():
    # raw=0.5, market_min=20 shares*0.10=2, liquidity=1 → ceiling=min(cap,liq)=1
    # round-up to 2 > 1 → reject
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.10"),
            fair_value_p=D("0.13"),
            market_min_order_size_shares=D("20"),
            liquidity_usdc=D("1"),
        )
    )
    assert result.reject_reason == "bankroll_too_small_for_market_min"


def test_min_stake_floor_used_when_no_market_min():
    # raw=0.5, no market_min, min_stake_usdc=1
    # round-up to 1 ≤ cap=10 ✓
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.10"),
            fair_value_p=D("0.13"),
        )
    )
    assert result.reject_reason is None
    assert result.stake_usdc == D("1")
    assert result.is_round_up_overbet is True
    assert result.effective_min_stake_usdc == D("1")


def test_market_min_overrides_lower_min_stake():
    # min_stake_usdc=0.5, market_min=5 shares*0.20=1.0 → effective=1.0 (取 max)
    result = kelly_stake(
        **_base_kwargs(
            price_c=D("0.20"),
            fair_value_p=D("0.30"),
            min_stake_usdc=D("0.5"),
            market_min_order_size_shares=D("5"),
        )
    )
    assert result.effective_min_stake_usdc == D("1")


def test_implied_fair_value_basic():
    # cap=0.10, edge_required=0.05 → fair = 0.10 / 0.95 ≈ 0.10526
    result = implied_fair_value_from_price_cap(D("0.10"), min_edge_required=D("0.05"))
    assert result is not None
    assert result == D("0.10") / D("0.95")


def test_implied_fair_value_clamped_to_one():
    # cap=0.99, edge=0.50 → fair=1.98 clamp to 1.0
    result = implied_fair_value_from_price_cap(D("0.99"), min_edge_required=D("0.50"))
    assert result == D("1")


def test_implied_fair_value_invalid_edge_returns_none():
    assert implied_fair_value_from_price_cap(D("0.10"), min_edge_required=D("1.5")) is None
    assert implied_fair_value_from_price_cap(D("0.10"), min_edge_required=D("-0.1")) is None


def test_implied_fair_value_zero_cap_returns_none():
    assert implied_fair_value_from_price_cap(D("0"), min_edge_required=D("0.05")) is None


def test_kelly_audit_fields_complete_on_accept():
    result = kelly_stake(**_base_kwargs())
    assert isinstance(result, KellyStake)
    assert result.f_star > D("0")
    assert result.edge_gross == D("0.10")
    assert result.fair_value_p == D("0.20")
    assert result.price_c == D("0.10")
    assert result.side == "BUY_YES"
    assert result.effective_kelly_fraction == D("0.25")
    assert result.effective_min_stake_usdc == D("1")


def test_kelly_audit_fields_complete_on_reject():
    result = kelly_stake(**_base_kwargs(fair_value_p=D("0.05")))
    assert result.reject_reason == "edge_below_min"
    # 仍然带完整审计上下文
    assert result.f_star <= D("0")
    assert result.fair_value_p == D("0.05")
    assert result.price_c == D("0.10")
    assert result.effective_kelly_fraction == D("0.25")


# C6: Kelly engine 与 RiskManager 的 effective_position_cap_usdc 一致性 contract test。
# 核心断言：Kelly engine 接受的任何 stake，RiskManager 的同款公式都不应拒绝
# （cap 漂移 = 双侧风控不一致）。
import pytest as _pytest_for_contract  # noqa: E402  - 测试结尾局部导入避免污染上面用例

from polymarket_trader.domain.kelly import effective_position_cap_usdc as _cap_fn  # noqa: E402


@_pytest_for_contract.mark.parametrize(
    "bankroll,fraction,ratio",
    [
        (D("100"), D("0.10"), D("1")),
        (D("100"), D("0.10"), D("3")),  # round-up over-bet
        (D("5.05"), D("0.10"), D("10")),  # 小 bankroll + 大 ratio（C 方案场景）
        (D("1000"), D("0.05"), D("1")),
        (D("50"), D("0.20"), D("2")),
    ],
)
def test_effective_cap_consistency_kelly_vs_risk(bankroll, fraction, ratio):
    """同 (bankroll, fraction, ratio) 输入 Kelly engine 与 RiskManager 同款公式
    必产同 stake 上限。这是双侧门禁不漂移的硬保证。"""

    cap_engine = _cap_fn(
        bankroll_usdc=bankroll,
        max_position_fraction=fraction,
    )
    cap_risk = _cap_fn(
        bankroll_usdc=bankroll,
        max_position_fraction=fraction,
        round_up_max_overbet_ratio=ratio,
    )
    # 没有 round-up 时 base_cap = bankroll × fraction
    assert cap_engine == bankroll * fraction
    # 有 round-up 时 effective = base × ratio
    if ratio > D("1"):
        assert cap_risk == bankroll * fraction * ratio
    else:
        assert cap_risk == cap_engine


def test_kelly_stake_accepts_001_tick_market():
    """C11: ``market_tick_size=0.001`` 时端点 [0.001, 0.999]，price=0.005 应通过。"""

    result = kelly_stake(
        bankroll_usdc=D("100"),
        price_c=D("0.005"),
        fair_value_p=D("0.02"),
        side="BUY_YES",
        kelly_fraction=D("0.25"),
        prob_confidence=D("1"),
        max_position_fraction=D("0.10"),
        min_edge=D("0.01"),
        min_stake_usdc=D("1"),
        market_tick_size=D("0.001"),
    )
    assert result.reject_reason is None
    assert result.stake_usdc > D("0")


def test_kelly_stake_rejects_below_001_tick():
    result = kelly_stake(
        bankroll_usdc=D("100"),
        price_c=D("0.0005"),  # < 0.001 tick
        fair_value_p=D("0.02"),
        side="BUY_YES",
        kelly_fraction=D("0.25"),
        prob_confidence=D("1"),
        max_position_fraction=D("0.10"),
        min_edge=D("0.01"),
        min_stake_usdc=D("1"),
        market_tick_size=D("0.001"),
    )
    assert result.reject_reason == "price_out_of_range"


def test_apply_vol_scaling_shrinks_at_high_vol():
    from polymarket_trader.domain.kelly import apply_vol_scaling

    # σ 翻倍 → factor=0.5² = 0.25
    result = apply_vol_scaling(D("100"), realized_vol=D("0.20"), baseline_vol=D("0.10"))
    assert result == D("25")
    # σ 不超过 baseline → 不缩
    assert apply_vol_scaling(D("100"), realized_vol=D("0.05"), baseline_vol=D("0.10")) == D("100")


def test_apply_settlement_discount_long_horizon():
    from polymarket_trader.domain.kelly import apply_settlement_discount

    # 半年持仓 + 5% 年率 → 折 ~2.5%
    half_year = 182 * 86400
    result = apply_settlement_discount(D("100"), settlement_seconds=half_year, annualized_rate=D("0.05"))
    # 0.05 × 0.5 ≈ 0.025 → stake' ≈ 97.5
    assert D("97") < result < D("98")
    # 短持仓 ~ 不缩
    assert apply_settlement_discount(D("100"), settlement_seconds=300, annualized_rate=D("0.05")) > D("99.99")


def test_apply_dispute_premium_subtracts_from_edge():
    from polymarket_trader.domain.kelly import apply_dispute_premium

    # 200 bps premium 从 0.10 edge 扣到 0.08
    assert apply_dispute_premium(D("0.10"), premium_bps=200) == D("0.08")
    # 0 premium → 不变
    assert apply_dispute_premium(D("0.10"), premium_bps=0) == D("0.10")


def test_kelly_exit_signal_holds_when_no_reverse_edge():
    from polymarket_trader.domain.kelly import kelly_exit_signal

    sig = kelly_exit_signal(current_price_c=D("0.30"), fair_value_p=D("0.50"))
    assert sig.sell_fraction == D("0")
    assert sig.reason == "hold"


def test_kelly_exit_signal_partial_exit_on_modest_reverse_edge():
    from polymarket_trader.domain.kelly import kelly_exit_signal

    # c=0.55, p=0.50 → reverse_edge=0.05, denom=0.50 → sell 10%
    sig = kelly_exit_signal(current_price_c=D("0.55"), fair_value_p=D("0.50"))
    assert sig.reason == "partial_exit"
    assert sig.sell_fraction == D("0.10")


def test_kelly_exit_signal_full_exit_when_reverse_edge_caps_at_one():
    """``sell_fraction`` 公式 (c-p)/(1-p) 上限 = 1，发生在 c→1 极端。"""

    from polymarket_trader.domain.kelly import kelly_exit_signal

    # p=0 / c=1 → reverse_edge=1, denom=1 → exactly 1.0
    sig = kelly_exit_signal(current_price_c=D("1"), fair_value_p=D("0"))
    assert sig.reason == "full_exit"
    assert sig.sell_fraction == D("1")


def test_kelly_exit_signal_high_reverse_edge_drives_large_partial_exit():
    """非端点情况下 sell_fraction 单调随 reverse_edge 上升但 < 1。"""

    from polymarket_trader.domain.kelly import kelly_exit_signal

    sig = kelly_exit_signal(current_price_c=D("0.95"), fair_value_p=D("0.10"))
    assert sig.reason == "partial_exit"
    # 0.85 / 0.90 ≈ 0.944
    assert sig.sell_fraction > D("0.9")
    assert sig.sell_fraction < D("1")


def test_round_up_kelly_stake_within_risk_cap():
    """Kelly engine round-up 路径：stake 必 ≤ RiskManager 的 effective cap (含 ratio)。
    用 5 USDC bankroll + ratio=10 的极端场景，验证两侧不会出现"engine 接受 / risk 拒"。"""

    bankroll = D("5")
    fraction = D("0.10")
    ratio = D("10")
    result = kelly_stake(
        bankroll_usdc=bankroll,
        price_c=D("0.10"),
        fair_value_p=D("0.50"),
        side="BUY_YES",
        kelly_fraction=D("0.25"),
        prob_confidence=D("1"),
        max_position_fraction=fraction,
        min_edge=D("0"),
        min_stake_usdc=D("1"),
        market_min_order_size_shares=D("20"),  # 强制 round-up
        fee_rate_bps=0,
        fees_enabled=True,
        liquidity_usdc=None,
        allow_round_up_to_market_min=True,
        round_up_max_overbet_ratio=ratio,
    )
    risk_cap = _cap_fn(
        bankroll_usdc=bankroll,
        max_position_fraction=fraction,
        round_up_max_overbet_ratio=ratio,
    )
    assert result.reject_reason is None, f"unexpected reject: {result.reject_reason}"
    assert result.stake_usdc <= risk_cap, (
        f"engine stake {result.stake_usdc} > risk cap {risk_cap}; double-gate漂移"
    )
