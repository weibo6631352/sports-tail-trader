"""赔率差价（odds-gap）入场策略回归测试。

覆盖 CLAUDE.md §17 的第二条入场路径：当 Goalserve 盘中去抽水真实概率显著高于
Polymarket ask 时入场。重点验证：
- de-vig 数学：原始 implied 之和 >1 → 归一后两侧和为 1；
- edge = devigged_true_p − best_ask；
- edge ≥ 阈值且未暂停时 accept，opportunity_type=ODDS_GAP；
- edge 不足 / 盘口暂停 / 我方一侧暂停 / 无 Goalserve 赔率时 reject；
- 扫尾锁定优先于赔率差价（锁定市场仍返回 LIVE_TAIL）；
- Kelly 用去抽水 true_p（而非 implied≈1.0）给赔率差价候选定注。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from strategies.current.tail import (
    SportsMarketSide,
    SportsMarketSnapshot,
    SportsMarketType,
    SportsTailOpportunityType,
    TailRejectReason,
    evaluate_tail_opportunity,
    live_game_state_from_metadata,
)
from strategies.current.tail.core import _candidate
from strategies.current.tail.odds_gap import _devig_two_way, evaluate_odds_gap_opportunity
from strategies.current.tail.types import TailPolicy


def _game(home_score: int = 1, away_score: int = 1):
    """构造一个 live、非锁定的常规比赛（剩余时间足够长，避免误入扫尾锁定）。"""
    return live_game_state_from_metadata(
        {
            "league": "NBA",
            "sport": "basketball",
            "home_name": "Lakers",
            "away_name": "Celtics",
            "home_score": home_score,
            "away_score": away_score,
            "period": "Q1",
            "status": "live",
            # 剩余时间远超 max_moneyline_seconds_remaining(180)，扫尾锁定必不命中。
            "seconds_remaining": 1800,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )


def _moneyline_market(
    side: SportsMarketSide,
    best_ask: Decimal,
    *,
    goalserve_moneyline: dict | None = None,
) -> SportsMarketSnapshot:
    metadata: dict = {}
    if goalserve_moneyline is not None:
        metadata["goalserve_moneyline"] = goalserve_moneyline
    return SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=side,
        token_id="tok",
        line=None,
        best_ask=best_ask,
        best_bid=best_ask - Decimal("0.02"),
        buyable_liquidity_usdc=Decimal("50"),
        market_slug="nba-lal-bos-2026-05-23",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# de-vig 数学
# ---------------------------------------------------------------------------


def test_devig_normalizes_overround_to_one() -> None:
    # 原始 implied：0.55 + 0.50 = 1.05（5% 抽水）→ 去抽水后两侧和必须为 1。
    result = _devig_two_way(Decimal("0.55"), Decimal("0.50"))
    assert result is not None
    assert result.overround == Decimal("1.05")
    assert result.home_true_p + result.away_true_p == Decimal("1")
    # home 真实概率 = 0.55 / 1.05 ≈ 0.5238
    assert abs(result.home_true_p - Decimal("0.52381")) < Decimal("0.0001")


def test_devig_raw_implied_sums_above_one() -> None:
    # 验证前提：1/eu 的原始 implied 跨双方相加确实 >1（博彩抽水）。
    home_implied = Decimal("1") / Decimal("1.80")  # ≈0.5556
    away_implied = Decimal("1") / Decimal("2.10")  # ≈0.4762
    assert home_implied + away_implied > Decimal("1")
    result = _devig_two_way(home_implied, away_implied)
    assert result is not None
    # 去抽水后严格归一到 1。
    assert abs((result.home_true_p + result.away_true_p) - Decimal("1")) < Decimal("1e-9")


def test_devig_rejects_non_positive_implied() -> None:
    assert _devig_two_way(Decimal("0"), Decimal("0.5")) is None
    assert _devig_two_way(Decimal("-0.1"), Decimal("0.5")) is None


# ---------------------------------------------------------------------------
# edge 计算 + accept
# ---------------------------------------------------------------------------


def test_odds_gap_accepted_when_edge_clears_threshold() -> None:
    # Goalserve raw implied home=0.70 away=0.40 → overround 1.10
    # devigged home_true_p = 0.70/1.10 ≈ 0.6364；ask=0.55 → edge ≈ 0.0864 ≥ 0.06。
    gs = {"home_implied_prob": 0.70, "away_implied_prob": 0.40, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.55"), goalserve_moneyline=gs)
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.opportunity_type == SportsTailOpportunityType.ODDS_GAP
    assert ev.reason == "odds_gap_entry"
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
    edge = Decimal(ev.metadata["odds_gap_edge"])
    # 验证 edge = true_p − best_ask 精确成立。
    assert edge == true_p - Decimal("0.55")
    assert edge >= TailPolicy().odds_gap_min_edge


def test_odds_gap_edge_uses_devigged_not_raw_implied() -> None:
    # raw implied home=0.62, away=0.50 → overround 1.12, devigged home ≈ 0.5536。
    # ask=0.50：raw edge 会是 0.12（虚高），devigged edge 只有 ≈0.0536 < 0.06 → 拒绝。
    # 这正是 de-vig 的意义：不去抽水会在每个市场凭空多算 edge。
    gs = {"home_implied_prob": 0.62, "away_implied_prob": 0.50, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.50"), goalserve_moneyline=gs)
    candidate = _candidate(_game(), market)
    ev = evaluate_odds_gap_opportunity(candidate, TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value
    # 反证：若用原始未去抽水 implied（0.62），edge = 0.62 − 0.50 = 0.12 会误判通过。
    assert Decimal("0.62") - Decimal("0.50") >= TailPolicy().odds_gap_min_edge


# ---------------------------------------------------------------------------
# reject 场景（直接调赔率差价评估器，隔离 de-vig / edge / suspended 逻辑）
# ---------------------------------------------------------------------------


def test_odds_gap_rejected_when_edge_below_threshold() -> None:
    # devigged home_true_p ≈ 0.5238；ask=0.50 → edge ≈ 0.0238 < 0.06。
    gs = {"home_implied_prob": 0.55, "away_implied_prob": 0.50, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.50"), goalserve_moneyline=gs)
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_odds_gap_rejected_when_market_suspended() -> None:
    # 即使 edge 充足，整体盘口暂停 → 赔率不可信 → 拒绝。
    gs = {"home_implied_prob": 0.70, "away_implied_prob": 0.40, "suspended": True}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.55"), goalserve_moneyline=gs)
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_odds_gap_rejected_when_our_side_suspended() -> None:
    # 我方一侧（home）被单独暂停 → 该侧赔率不反映真实概率 → 拒绝。
    gs = {
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": False,
        "home_suspended": True,
    }
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.55"), goalserve_moneyline=gs)
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_odds_gap_rejected_when_no_goalserve_odds() -> None:
    # 没有 Goalserve 赔率 → 无差价信号 → 拒绝。
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.55"))
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_odds_gap_away_side_uses_away_true_p() -> None:
    # raw implied away=0.72 home=0.45 → overround 1.17, devigged away ≈ 0.6154。
    # ask=0.52 → edge ≈ 0.0954 ≥ 0.06 → accept；验证 away 侧取 away_true_p。
    gs = {"home_implied_prob": 0.45, "away_implied_prob": 0.72, "suspended": False}
    market = _moneyline_market(SportsMarketSide.AWAY, Decimal("0.52"), goalserve_moneyline=gs)
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.metadata["odds_gap_side"] == "away"


# ---------------------------------------------------------------------------
# 扫尾锁定优先级：锁定市场不被赔率差价覆盖
# ---------------------------------------------------------------------------


def test_tail_lock_takes_priority_over_odds_gap() -> None:
    # 比赛末段大幅领先 → 扫尾锁定命中；即便带 Goalserve 赔率，仍返回 LIVE_TAIL，
    # 不被 odds-gap 覆盖。
    game = live_game_state_from_metadata(
        {
            "league": "NBA",
            "sport": "basketball",
            "home_name": "Lakers",
            "away_name": "Celtics",
            "home_score": 110,
            "away_score": 90,
            "period": "Q4",
            "status": "live",
            "seconds_remaining": 30,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    assert game is not None
    gs = {"home_implied_prob": 0.70, "away_implied_prob": 0.40, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.95"), goalserve_moneyline=gs)
    ev = evaluate_tail_opportunity(game, market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.opportunity_type == SportsTailOpportunityType.LIVE_TAIL
    assert ev.reason == "moneyline_late_lead"


# ---------------------------------------------------------------------------
# Kelly：赔率差价候选用去抽水 true_p 定注
# ---------------------------------------------------------------------------


def test_kelly_sizes_odds_gap_on_devigged_prob() -> None:
    from polymarket_trader.domain.kelly import kelly_stake

    gs = {"home_implied_prob": 0.70, "away_implied_prob": 0.40, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.55"), goalserve_moneyline=gs)
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
    # Kelly 必须用去抽水 true_p（≈0.6364），不是扫尾锁定的 implied≈1.0。
    stake = kelly_stake(
        bankroll_usdc=Decimal("100"),
        price_c=Decimal("0.55"),
        fair_value_p=true_p,
        side="BUY_YES",
        kelly_fraction=Decimal("0.25"),
        prob_confidence=Decimal("1"),
        max_position_fraction=Decimal("1.0"),
        min_edge=Decimal("0.02"),
        min_stake_usdc=Decimal("1"),
        market_min_order_size_shares=Decimal("5"),
        fee_rate_bps=0,
        fees_enabled=False,
        liquidity_usdc=Decimal("50"),
    )
    # true_p≈0.6364 > ask 0.55 → 正 edge → Kelly 给出非零仓位。
    assert stake.stake_usdc > Decimal("0")
    assert stake.edge > Decimal("0")
    # 用 implied≈1.0 定注会严重 over-bet；devigged 定注的 f_star 应明显小于 1。
    assert stake.f_star < Decimal("1")
