"""尾盘时间窗口 bypass 判断：决定是否允许跳过远期 endDate 粗筛。

这里是策略决策逻辑：什么局面下认为比赛已进入尾盘、结果是否数学锁定。
与体育匹配工具（live_state.py）分离，避免策略阈值泄入匹配层。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import BaseballGameState, LiveEvent, TennisGameState

if TYPE_CHECKING:
    from strategies.current.config import CurrentStrategyConfig
    from strategies.current.outcomes import SportsMarketDescriptor


def market_tail_window_bypass_reason(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
    config: CurrentStrategyConfig,
) -> str | None:
    """返回可绕过远期 endDate 粗筛的直播尾盘原因；None 表示无法绕过。"""
    if _market_can_lock_before_tail_window(market, event, descriptor):
        return "live_outcome_lock_candidate"
    if _market_has_live_tail_state(market, event, descriptor, config):
        return "live_tail_state_candidate"
    return None


def _market_can_lock_before_tail_window(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
) -> bool:
    """市场是否存在不依赖封盘时间的数学锁定机会。"""
    status = event.status.value.lower()
    if status != "live":
        return False
    market_type = descriptor.market_type
    if market_type == "totals":
        return _totals_market_is_already_over(market, event, descriptor)
    if market_type != "moneyline" or not _is_tennis_set_winner_market(market):
        return False
    state = event.tennis_state
    if state is None:
        return False
    set_number = _tennis_set_winner_number(market)
    current_set = state.current_set
    return set_number is not None and current_set is not None and current_set > set_number and bool(state.set_scores)


def _market_has_live_tail_state(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
    config: CurrentStrategyConfig,
) -> bool:
    """直播局面是否已达到策略尾盘条件（结果尚未完全数学锁定）。"""
    status = event.status.value.lower()
    if status != "live":
        return False
    market_type = descriptor.market_type
    state = event.tennis_state
    if state is not None:
        if market_type == "moneyline":
            if _is_tennis_set_winner_market(market):
                return _tennis_set_winner_tail_state_reached(market, state)
            return _tennis_moneyline_tail_state_reached(state)
        return False
    baseball_state = event.baseball_state
    if baseball_state is not None:
        return _baseball_tail_state_reached(baseball_state)
    if market_type == "moneyline":
        seconds_remaining = _int_value(event.seconds_remaining)
        if seconds_remaining is None:
            return False
        home = event.home
        away = event.away
        if home is None or away is None:
            return False
        return (
            seconds_remaining <= config.tail_max_moneyline_seconds_remaining
            and abs((home.score or 0) - (away.score or 0)) >= config.tail_min_moneyline_lead
        )
    return False


def _baseball_tail_state_reached(state: BaseballGameState) -> bool:
    current_inning = _int_value(state.current_inning)
    outs = _int_value(state.outs)
    occupied_bases = tuple(state.occupied_bases or ())
    if current_inning is None or outs is None:
        return False
    # 第 9 局及以上：2 出局垒上无人（分差由评估器二次校验）
    if current_inning >= 9 and outs >= 2 and not occupied_bases:
        return True
    # 第 8 局：1+ 出局即可触发信号，分差阈值由 evaluator._mlb_eighth_moneyline_lead_reached 负责
    if current_inning == 8 and outs >= 1:
        return True
    return False


def _tennis_moneyline_tail_state_reached(state: TennisGameState) -> bool:
    home_games = state.home_current_set_games
    away_games = state.away_current_set_games
    if home_games is None or away_games is None:
        return False
    home_sets = state.home_sets_won
    away_sets = state.away_sets_won
    if home_sets is None or away_sets is None:
        return False
    if home_games >= 5 and home_games - away_games >= 2 and home_sets - away_sets >= 1:
        return True
    return away_games >= 5 and away_games - home_games >= 2 and away_sets - home_sets >= 1


def _tennis_set_winner_tail_state_reached(market: Market, state: TennisGameState) -> bool:
    set_number = _tennis_set_winner_number(market)
    current_set = state.current_set
    if set_number is None or current_set != set_number:
        return False
    home_games = state.home_current_set_games
    away_games = state.away_current_set_games
    if home_games is None or away_games is None:
        return False
    return max(home_games, away_games) >= 5 and abs(home_games - away_games) >= 2


def _totals_market_is_already_over(
    market: Market,
    event: LiveEvent,
    descriptor: SportsMarketDescriptor,
) -> bool:
    line = descriptor.line
    if line is None:
        return False
    state = event.tennis_state
    if state is not None:
        if _is_set_total_market_slug(market.market_slug):
            current_set = state.current_set
            return current_set is not None and current_set > line
        return state.total_games > line or _tennis_match_total_min_final_games_is_over(state, line)
    home = event.home
    away = event.away
    if home is None or away is None:
        return False
    return ((home.score or 0) + (away.score or 0)) > line


def _is_tennis_set_winner_market(market: Market) -> bool:
    text = _normalized_slug(market)
    return "set winner" in text or "first set winner" in text


def _tennis_set_winner_number(market: Market) -> int | None:
    text = _normalized_slug(market)
    if "first set winner" in text or "1st set winner" in text or "set 1 winner" in text:
        return 1
    if "second set winner" in text or "2nd set winner" in text or "set 2 winner" in text:
        return 2
    if "third set winner" in text or "3rd set winner" in text or "set 3 winner" in text:
        return 3
    return None


def _is_set_total_market_slug(slug: str) -> bool:
    text = slug.strip().lower().replace("_", " ").replace("-", " ")
    return "set total" in text or "set totals" in text or "total sets" in text


def _tennis_match_total_min_final_games_is_over(state: TennisGameState, line: object) -> bool:
    """当前网球局面下，整场最低可能最终总局数是否已越过 totals 线。"""
    current_home = state.home_current_set_games
    current_away = state.away_current_set_games
    if current_home is None or current_away is None:
        return False
    minimum_current_set_games = _tennis_minimum_final_set_games(current_home, current_away)
    if minimum_current_set_games is None:
        return False
    current_games = current_home + current_away
    already_counted_total = state.home_total_games + state.away_total_games
    minimum_final_games = already_counted_total - current_games + minimum_current_set_games
    return minimum_final_games > line


def _tennis_minimum_final_set_games(home_games: int, away_games: int) -> int | None:
    if home_games < 0 or away_games < 0:
        return None
    if _tennis_set_score_is_final(home_games, away_games):
        return home_games + away_games
    for extra_games in range(0, 8):
        for home_extra in range(extra_games + 1):
            away_extra = extra_games - home_extra
            final_home = home_games + home_extra
            final_away = away_games + away_extra
            if _tennis_set_score_is_final(final_home, final_away):
                return final_home + final_away
    return None


def _tennis_set_score_is_final(home_games: int, away_games: int) -> bool:
    winner_games = max(home_games, away_games)
    loser_games = min(home_games, away_games)
    return (winner_games >= 6 and winner_games - loser_games >= 2) or winner_games == 7


def _normalized_slug(market: Market) -> str:
    return market.market_slug.strip().lower().replace("_", " ").replace("-", " ")


def _int_value(value: object) -> int | None:
    try:
        return None if value is None or value == "" else int(str(value))
    except (TypeError, ValueError):
        return None
