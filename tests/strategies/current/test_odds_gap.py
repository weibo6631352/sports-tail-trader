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
        buyable_liquidity_usdc=Decimal("1000"),
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
    # ask=0.50:devigged edge_gross ≈ 0.0536(对比 raw 错算 0.12),小但正,扣 30bps fee 后
    # edge_net ≈ 0.0386,Kelly 会算出小 fraction。这正是 de-vig + 信任 Kelly 的意义:
    # 不去抽水会凭空多算 edge,真 devigged edge 由 Kelly 用 net edge 自动定 fraction。
    gs = {"home_implied_prob": 0.62, "away_implied_prob": 0.50, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.50"), goalserve_moneyline=gs)
    candidate = _candidate(_game(), market)
    ev = evaluate_odds_gap_opportunity(candidate, TailPolicy())
    assert ev.accepted
    md = ev.metadata
    assert md.get("odds_gap_market_type") == "moneyline"
    # devigged true_p ≈ 0.5536,edge_gross ≈ 0.0536(不是 raw 0.12)
    assert Decimal(str(md["odds_gap_true_p"])) < Decimal("0.56")
    assert Decimal(str(md["odds_gap_edge"])) < Decimal("0.06")
    # edge_net = edge_gross - fee_per_share(=ask × 30bps)
    assert Decimal(str(md["odds_gap_edge_net"])) < Decimal(str(md["odds_gap_edge"]))


# ---------------------------------------------------------------------------
# reject 场景（直接调赔率差价评估器，隔离 de-vig / edge / suspended 逻辑）
# ---------------------------------------------------------------------------


def test_odds_gap_small_positive_edge_still_accepted_kelly_decides() -> None:
    # devigged home_true_p ≈ 0.5238;ask=0.50 → edge_gross ≈ 0.0238。
    # 旧逻辑:< 6% 阈值就拒。新逻辑(无门槛):accept,让 Kelly 用 net edge 自决。
    # edge_net = 0.0238 - 0.50 × 0.003 = 0.0223 > 0 → Kelly 给小 fraction。
    gs = {"home_implied_prob": 0.55, "away_implied_prob": 0.50, "suspended": False}
    market = _moneyline_market(SportsMarketSide.HOME, Decimal("0.50"), goalserve_moneyline=gs)
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert ev.accepted
    assert Decimal(str(ev.metadata["odds_gap_edge_net"])) > Decimal("0")


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
        liquidity_usdc=Decimal("1000"),
    )
    # true_p≈0.6364 > ask 0.55 → 正 edge → Kelly 给出非零仓位。
    assert stake.stake_usdc > Decimal("0")
    assert stake.edge > Decimal("0")
    # 用 implied≈1.0 定注会严重 over-bet；devigged 定注的 f_star 应明显小于 1。
    assert stake.f_star < Decimal("1")


# ===========================================================================
# Totals（Over/Under）赔率差价：线 + 范围对账
# ===========================================================================


def _totals_market(
    side: SportsMarketSide,
    best_ask: Decimal,
    *,
    line: Decimal | None,
    market_slug: str = "nba-lal-bos-2026-05-23-total-220pt5",
    goalserve_totals: dict | None = None,
) -> SportsMarketSnapshot:
    metadata: dict = {}
    if goalserve_totals is not None:
        metadata["goalserve_totals"] = goalserve_totals
    return SportsMarketSnapshot(
        market_type=SportsMarketType.TOTALS,
        side=side,
        token_id="tok",
        line=line,
        best_ask=best_ask,
        best_bid=best_ask - Decimal("0.02"),
        buyable_liquidity_usdc=Decimal("1000"),
        market_slug=market_slug,
        metadata=metadata,
    )


def _spread_market(
    side: SportsMarketSide,
    best_ask: Decimal,
    *,
    line: Decimal | None,
    market_slug: str = "nba-lal-bos-2026-05-23-spread",
    goalserve_spread: dict | None = None,
) -> SportsMarketSnapshot:
    metadata: dict = {}
    if goalserve_spread is not None:
        metadata["goalserve_spread"] = goalserve_spread
    return SportsMarketSnapshot(
        market_type=SportsMarketType.SPREADS,
        side=side,
        token_id="tok",
        line=line,
        best_ask=best_ask,
        best_bid=best_ask - Decimal("0.02"),
        buyable_liquidity_usdc=Decimal("1000"),
        market_slug=market_slug,
        metadata=metadata,
    )


def test_totals_odds_gap_accepted_when_line_matches_and_edge_clears() -> None:
    # Goalserve raw over=0.70 under=0.40 → overround 1.10, devigged over ≈ 0.6364。
    # line 220.5 = Polymarket line 220.5；ask=0.55 → edge ≈ 0.0864 ≥ 0.06 → accept。
    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.opportunity_type == SportsTailOpportunityType.ODDS_GAP
    assert ev.metadata["odds_gap_side"] == "over"
    assert ev.metadata["odds_gap_market_type"] == "totals"
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
    assert Decimal(ev.metadata["odds_gap_edge"]) == true_p - Decimal("0.55")


def test_totals_odds_gap_rejected_when_line_mismatch_no_fabricated_edge() -> None:
    # Goalserve totals 线 9.5 ≠ Polymarket 市场线 8.5：over 9.5 与 over 8.5 结算条件
    # 不同，概率不可比。即便 edge 看似充足也不得入场 → odds_gap_line_mismatch。
    gs = {
        "market_name": "Total",
        "total_line": "9.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER,
        Decimal("0.55"),
        line=Decimal("8.5"),
        market_slug="mlb-nyy-bos-2026-05-23-total-8pt5",
        goalserve_totals=gs,
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ODDS_GAP_LINE_MISMATCH.value


def test_totals_odds_gap_rejected_when_scope_mismatch() -> None:
    # Polymarket 是篮球上半场 totals（1h total），Goalserve totals 是整场 "Total"。
    # 范围不一致 → 概率不可比 → odds_gap_line_mismatch（不凭空造 edge）。
    gs = {
        "market_name": "Total",
        "total_line": "110.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER,
        Decimal("0.55"),
        line=Decimal("110.5"),
        market_slug="nba-lal-bos-2026-05-23-1h-total-110pt5",
        goalserve_totals=gs,
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ODDS_GAP_LINE_MISMATCH.value


def test_totals_odds_gap_rejected_when_goalserve_market_name_is_subperiod() -> None:
    # Polymarket 整场 totals，但 Goalserve market_name 携带子周期标记 "(map 2)"。
    # 二次范围确认必须捕获——电竞分图 totals 不能与整场比较。
    gs = {
        "market_name": "Total (map 2)",
        "total_line": "220.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ODDS_GAP_LINE_MISMATCH.value


def test_totals_odds_gap_under_side_uses_under_true_p() -> None:
    # raw under=0.72 over=0.45 → overround 1.17, devigged under ≈ 0.6154。
    # ask=0.52 → edge ≈ 0.0954 ≥ 0.06 → accept；验证 under 侧取 under_true_p。
    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.45,
        "under_implied_prob": 0.72,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.UNDER, Decimal("0.52"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.metadata["odds_gap_side"] == "under"
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
    # devigged under ≈ 0.72/1.17；over 侧若被误取会是 0.45/1.17 < ask，必拒。
    assert abs(true_p - (Decimal("0.72") / Decimal("1.17"))) < Decimal("1e-9")


def test_totals_odds_gap_small_positive_edge_still_accepted() -> None:
    # devigged over ≈ 0.5238;ask=0.50 → edge_gross ≈ 0.0238。新逻辑无门槛 accept,
    # Kelly 用 net edge 自决:edge_net > 0 时小 fraction,Polymarket 30bps fee 已扣。
    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.55,
        "under_implied_prob": 0.50,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.50"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert ev.accepted
    assert Decimal(str(ev.metadata["odds_gap_edge_net"])) > Decimal("0")


def test_totals_odds_gap_rejected_when_market_suspended() -> None:
    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": True,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_totals_odds_gap_rejected_when_our_side_suspended() -> None:
    # over 侧被单独暂停 → 该侧赔率不可信 → no_odds_gap。
    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
        "over_suspended": True,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_totals_odds_gap_rejected_when_no_goalserve_totals() -> None:
    market = _totals_market(SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"))
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


# ===========================================================================
# Spreads（让分盘）赔率差价：让分线 + 范围对账
# ===========================================================================


def test_spread_odds_gap_accepted_when_line_matches_and_edge_clears() -> None:
    # Goalserve raw home=0.70 away=0.40 → overround 1.10, devigged home ≈ 0.6364。
    # home_handicap -3.5 = Polymarket line -3.5；ask=0.55 → edge ≈ 0.0864 → accept。
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": False,
    }
    market = _spread_market(
        SportsMarketSide.HOME, Decimal("0.55"), line=Decimal("-3.5"), goalserve_spread=gs
    )
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.opportunity_type == SportsTailOpportunityType.ODDS_GAP
    assert ev.metadata["odds_gap_side"] == "home"
    assert ev.metadata["odds_gap_market_type"] == "spreads"


def test_spread_odds_gap_rejected_when_handicap_mismatch() -> None:
    # Goalserve home_handicap -2.5 ≠ Polymarket line -3.5 → 让分条件不同，不可比。
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-2.5",
        "away_handicap": "2.5",
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": False,
    }
    market = _spread_market(
        SportsMarketSide.HOME, Decimal("0.55"), line=Decimal("-3.5"), goalserve_spread=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ODDS_GAP_LINE_MISMATCH.value


def test_spread_odds_gap_rejected_when_scope_mismatch() -> None:
    # Polymarket 篮球上半场让分盘（1h spread），Goalserve 让分盘是整场 → 范围不一致。
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": False,
    }
    market = _spread_market(
        SportsMarketSide.HOME,
        Decimal("0.55"),
        line=Decimal("-3.5"),
        market_slug="nba-lal-bos-2026-05-23-1h-spread",
        goalserve_spread=gs,
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.ODDS_GAP_LINE_MISMATCH.value


def test_spread_odds_gap_away_side_uses_away_handicap_and_true_p() -> None:
    # away 侧：对账 Polymarket line 与 away_handicap，取 devigged away_true_p。
    # raw away=0.72 home=0.45 → overround 1.17, devigged away ≈ 0.6154。
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.45,
        "away_implied_prob": 0.72,
        "suspended": False,
    }
    market = _spread_market(
        SportsMarketSide.AWAY, Decimal("0.52"), line=Decimal("3.5"), goalserve_spread=gs
    )
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    assert ev.metadata["odds_gap_side"] == "away"
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
    assert abs(true_p - (Decimal("0.72") / Decimal("1.17"))) < Decimal("1e-9")


def test_spread_odds_gap_small_positive_edge_still_accepted() -> None:
    # 同 ML/Totals 用例:无门槛 accept,Kelly 用 net edge 自决。
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.55,
        "away_implied_prob": 0.50,
        "suspended": False,
    }
    market = _spread_market(
        SportsMarketSide.HOME, Decimal("0.50"), line=Decimal("-3.5"), goalserve_spread=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert ev.accepted
    assert Decimal(str(ev.metadata["odds_gap_edge_net"])) > Decimal("0")


def test_spread_odds_gap_rejected_when_suspended() -> None:
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": True,
    }
    market = _spread_market(
        SportsMarketSide.HOME, Decimal("0.55"), line=Decimal("-3.5"), goalserve_spread=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_spread_odds_gap_rejected_when_our_side_suspended() -> None:
    gs = {
        "market_name": "Handicap",
        "home_handicap": "-3.5",
        "away_handicap": "3.5",
        "home_implied_prob": 0.70,
        "away_implied_prob": 0.40,
        "suspended": False,
        "home_suspended": True,
    }
    market = _spread_market(
        SportsMarketSide.HOME, Decimal("0.55"), line=Decimal("-3.5"), goalserve_spread=gs
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


def test_spread_odds_gap_rejected_when_no_goalserve_spread() -> None:
    market = _spread_market(SportsMarketSide.HOME, Decimal("0.55"), line=Decimal("-3.5"))
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert not ev.accepted
    assert ev.reason == TailRejectReason.NO_ODDS_GAP.value


# ===========================================================================
# 回归：moneyline 行为不变 + Kelly 对 totals 赔率差价同样用去抽水 true_p
# ===========================================================================


def test_moneyline_odds_gap_unchanged_does_not_use_totals_metadata() -> None:
    # moneyline 市场即便带了 goalserve_totals 也只看 goalserve_moneyline——
    # 验证分派后 moneyline 路径与扩展前完全一致。
    gs_ml = {"home_implied_prob": 0.70, "away_implied_prob": 0.40, "suspended": False}
    # 注入一个会构成 over edge 的 totals dict——不应影响 moneyline 判断。
    market = SportsMarketSnapshot(
        market_type=SportsMarketType.MONEYLINE,
        side=SportsMarketSide.HOME,
        token_id="tok",
        line=None,
        best_ask=Decimal("0.55"),
        best_bid=Decimal("0.53"),
        buyable_liquidity_usdc=Decimal("1000"),
        market_slug="nba-lal-bos-2026-05-23",
        metadata={
            "goalserve_moneyline": gs_ml,
            "goalserve_totals": {
                "market_name": "Total",
                "total_line": "220.5",
                "over_implied_prob": 0.90,
                "under_implied_prob": 0.20,
                "suspended": False,
            },
        },
    )
    ev = evaluate_odds_gap_opportunity(_candidate(_game(), market), TailPolicy())
    assert ev.accepted
    assert ev.metadata["odds_gap_market_type"] == "moneyline"
    assert ev.metadata["odds_gap_side"] == "home"
    # true_p 必须来自 ML devig（0.70/1.10），不是 totals。
    assert abs(Decimal(ev.metadata["odds_gap_true_p"]) - (Decimal("0.70") / Decimal("1.10"))) < Decimal("1e-9")


def test_kelly_sizes_totals_odds_gap_on_devigged_prob() -> None:
    from polymarket_trader.domain.kelly import kelly_stake

    gs = {
        "market_name": "Total",
        "total_line": "220.5",
        "over_implied_prob": 0.70,
        "under_implied_prob": 0.40,
        "suspended": False,
    }
    market = _totals_market(
        SportsMarketSide.OVER, Decimal("0.55"), line=Decimal("220.5"), goalserve_totals=gs
    )
    ev = evaluate_tail_opportunity(_game(), market, policy=TailPolicy())
    assert ev.accepted, ev.reason
    true_p = Decimal(ev.metadata["odds_gap_true_p"])
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
        liquidity_usdc=Decimal("1000"),
    )
    assert stake.stake_usdc > Decimal("0")
    assert stake.edge > Decimal("0")
    assert stake.f_star < Decimal("1")
