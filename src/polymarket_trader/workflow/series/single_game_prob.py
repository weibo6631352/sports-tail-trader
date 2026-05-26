"""派生单场胜率 p_a：series winner model 的输入。

优先级（高到低）：
1. ``metadata["game_odds"]`` 存在且 fresh（年龄 <= ttl）→ 直接用 GameOdds.p_a。
2. ``SeasonSnapshot`` 战绩 Pythagorean expectation 兜底——win_pct(a) / (win_pct(a) +
   win_pct(b))。需要双方都能在 standings 里 normalize 命中。
3. 缺失 → 返回 None；evaluator 报 ``MISSING_SERIES_ODDS``，不下任何盘。

不返回硬编码 0.5：series winner 的资金风险足够大，缺锚点时必须显式拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from polymarket_trader.domain.sports_season import SeasonSnapshot
from polymarket_trader.workflow._shared.team_normalize import normalize_team_name
from polymarket_trader.workflow.series.match import GameOdds, game_odds_from_metadata
from polymarket_trader.workflow.series.types import SeriesState


@dataclass(frozen=True, slots=True)
class SingleGameProb:
    """派生结果。``source`` 落审计——"game_odds" / "pythagorean" / 等。"""

    p_a: Decimal
    source: str


def derive_single_game_prob(
    *,
    state: SeriesState,
    metadata: Mapping[str, Any],
    season_snapshot: SeasonSnapshot | None,
    now: datetime,
    max_game_odds_age_seconds: int,
) -> SingleGameProb | None:
    """按优先级返回 team_a 单场胜率；缺失返回 None。"""

    # 1) game_odds metadata —— 同时校验球队对齐：order 必须是 (team_a, team_b)
    #    与 series_state 一致；不一致就主动取反。fresh ttl 校验也在这里。
    game = game_odds_from_metadata(metadata)
    if game is not None:
        age = _age_seconds(game.observed_at, now)
        if age <= max_game_odds_age_seconds:
            aligned = _align_p(game, state)
            if aligned is not None:
                return SingleGameProb(p_a=aligned, source=f"game_odds:{game.source}")

    # 2) pythagorean expectation：从 SeasonSnapshot 战绩反推。
    if season_snapshot is not None:
        pyth = _pythagorean_from_standings(state, season_snapshot)
        if pyth is not None:
            return SingleGameProb(p_a=pyth, source="pythagorean")
    return None


def _align_p(game: GameOdds, state: SeriesState) -> Decimal | None:
    """game_odds 的 (team_a/p_a) 不一定和 series_state.team_a 对齐——做归一化匹配。"""

    a_norm = normalize_team_name(state.team_a)
    b_norm = normalize_team_name(state.team_b)
    g_a = normalize_team_name(game.team_a)
    g_b = normalize_team_name(game.team_b)
    if not a_norm or not b_norm:
        return None
    if g_a == a_norm and g_b == b_norm:
        return game.p_a
    if g_a == b_norm and g_b == a_norm:
        return Decimal(1) - game.p_a
    # last-token 兜底（"Boston Celtics" vs "celtics"）。
    last_a = a_norm.split()[-1]
    last_b = b_norm.split()[-1]
    last_g_a = g_a.split()[-1] if g_a else ""
    last_g_b = g_b.split()[-1] if g_b else ""
    if last_g_a == last_a and last_g_b == last_b:
        return game.p_a
    if last_g_a == last_b and last_g_b == last_a:
        return Decimal(1) - game.p_a
    return None


def _pythagorean_from_standings(state: SeriesState, snapshot: SeasonSnapshot) -> Decimal | None:
    """team_a / team_b 的 win_pct 做相对归一：p_a = wp_a / (wp_a + wp_b)。

    严格意义上 Pythagorean expectation 用 RS^k / (RS^k + RA^k)（k=2 for MLB,
    14 for NFL 等），需要 runs scored / allowed。ESPN standings 不一定带这些
    字段，但 win_pct 在所有联赛都有；用 win_pct 的相对比作为 fallback——
    比硬编码 0.5 强，且明确标 source=pythagorean 供审计。
    """

    wp_a = _win_pct_for(state.team_a, snapshot)
    wp_b = _win_pct_for(state.team_b, snapshot)
    if wp_a is None or wp_b is None:
        return None
    if wp_a <= 0 and wp_b <= 0:
        return None
    total = wp_a + wp_b
    if total <= 0:
        return None
    p = wp_a / total
    if p <= 0:
        return Decimal("0.01")
    if p >= 1:
        return Decimal("0.99")
    return p


def _win_pct_for(team: str, snapshot: SeasonSnapshot) -> Decimal | None:
    team_norm = normalize_team_name(team)
    if not team_norm:
        return None
    last = team_norm.split()[-1]
    for standings in snapshot.standings:
        for row in standings.rows:
            row_norm = normalize_team_name(row.team)
            if not row_norm:
                continue
            if row_norm == team_norm or row_norm.split()[-1] == last:
                if row.win_pct is not None:
                    return row.win_pct
                games = row.wins + row.losses + row.ties
                if games > 0:
                    return Decimal(row.wins) / Decimal(games)
    return None


def _age_seconds(observed_at: datetime, now: datetime) -> int:
    a = observed_at if observed_at.tzinfo else observed_at.replace(tzinfo=timezone.utc)
    b = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return max(0, int((b - a).total_seconds()))


__all__ = ["SingleGameProb", "derive_single_game_prob"]
