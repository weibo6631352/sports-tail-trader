"""结算时间估算单元测试：棒球/网球无时钟推算 + _estimated_settlement_hold_minutes 路径。"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from polymarket_trader.domain.sports_live import BaseballGameState, TennisGameState
from strategies.current.trading.exit_overlay import (
    _baseball_seconds_remaining,
    _estimated_settlement_hold_minutes,
    _tennis_seconds_remaining,
)


# ---------------------------------------------------------------------------
# Baseball
# ---------------------------------------------------------------------------


def test_baseball_top_first_inning_estimates_full_game() -> None:
    state = BaseballGameState(current_inning=1, inning_half="top")
    seconds = _baseball_seconds_remaining(state)
    assert seconds is not None
    # top 1st: 2 + (9-1)*2 = 18 half-innings → 18 * 10 * 60 = 10800
    assert seconds == 18 * 10 * 60


def test_baseball_bottom_first_inning() -> None:
    state = BaseballGameState(current_inning=1, inning_half="bottom")
    seconds = _baseball_seconds_remaining(state)
    # bottom 1st: 1 + (9-1)*2 = 17 half-innings → 17 * 600
    assert seconds == 17 * 10 * 60


def test_baseball_top_ninth_inning() -> None:
    state = BaseballGameState(current_inning=9, inning_half="top")
    seconds = _baseball_seconds_remaining(state)
    # 第9局 top：remaining_half_innings = 2 (still need top + bottom)
    assert seconds == 2 * 10 * 60


def test_baseball_bottom_ninth_inning() -> None:
    state = BaseballGameState(current_inning=9, inning_half="bottom")
    seconds = _baseball_seconds_remaining(state)
    # bottom 9th = 1 half-inning remaining
    assert seconds == 1 * 10 * 60


def test_baseball_extra_innings_top() -> None:
    state = BaseballGameState(current_inning=10, inning_half="top")
    seconds = _baseball_seconds_remaining(state)
    # Extra inning top: 2 half-innings
    assert seconds == 2 * 10 * 60


def test_baseball_extra_innings_bottom() -> None:
    state = BaseballGameState(current_inning=11, inning_half="bottom")
    seconds = _baseball_seconds_remaining(state)
    assert seconds == 1 * 10 * 60


def test_baseball_no_inning_returns_none() -> None:
    state = BaseballGameState(current_inning=None)
    assert _baseball_seconds_remaining(state) is None


def test_baseball_no_inning_half_defaults_to_top() -> None:
    state = BaseballGameState(current_inning=9, inning_half=None)
    # No inning_half → defaults to "top"
    assert _baseball_seconds_remaining(state) == 2 * 10 * 60


# ---------------------------------------------------------------------------
# Tennis
# ---------------------------------------------------------------------------


def test_tennis_early_match_estimates_substantial_time() -> None:
    state = TennisGameState(
        home_sets_won=0,
        away_sets_won=0,
        current_set=1,
        home_current_set_games=1,
        away_current_set_games=0,
    )
    seconds = _tennis_seconds_remaining(state)
    assert seconds is not None
    # avg sets remaining = (2 + 2) / 2 = 2, current_games_remaining = max(0, 6-1) = 5
    # (2*45 + 5*5)*60 = (90+25)*60 = 6900
    assert seconds == 6900


def test_tennis_match_point_minimal_time() -> None:
    state = TennisGameState(
        home_sets_won=1,
        away_sets_won=1,
        current_set=3,
        home_current_set_games=5,
        away_current_set_games=4,
    )
    seconds = _tennis_seconds_remaining(state)
    assert seconds is not None
    # avg sets remaining = (1 + 1) / 2 = 1, current_games_remaining = max(0, 6-5) = 1
    # (1*45 + 1*5)*60 = 50*60 = 3000
    assert seconds == 3000


def test_tennis_nearly_done() -> None:
    state = TennisGameState(
        home_sets_won=2,
        away_sets_won=0,
        current_set=2,
        home_current_set_games=6,
        away_current_set_games=5,
    )
    seconds = _tennis_seconds_remaining(state)
    # avg sets remaining = (0 + 2) / 2 = 1, current_games_remaining = 0
    # (1*45 + 0)*60 = 2700
    assert seconds is not None
    assert seconds == 2700


def test_tennis_no_current_set_returns_none() -> None:
    state = TennisGameState(home_sets_won=0, away_sets_won=0, current_set=None)
    assert _tennis_seconds_remaining(state) is None


def test_tennis_minimum_floor() -> None:
    # Both players one set away from winning, same state → result >= 60
    state = TennisGameState(
        home_sets_won=1,
        away_sets_won=1,
        current_set=3,
        home_current_set_games=6,
        away_current_set_games=6,
    )
    seconds = _tennis_seconds_remaining(state)
    assert seconds is not None
    assert seconds >= 60


def test_tennis_unknown_game_scores_uses_zero_games_remaining() -> None:
    # current_set_games not set → current_games_remaining = 0, only set estimate
    state = TennisGameState(
        home_sets_won=0,
        away_sets_won=1,
        current_set=2,
        home_current_set_games=None,
        away_current_set_games=None,
    )
    seconds = _tennis_seconds_remaining(state)
    assert seconds is not None
    # avg sets remaining = (2 + 1) / 2 = 1.5, current_games_remaining = 0
    # (1.5*45 + 0)*60 = 67.5*60 = 4050
    assert seconds == int(1.5 * 45 * 60)


# ---------------------------------------------------------------------------
# _estimated_settlement_hold_minutes integration paths
# ---------------------------------------------------------------------------


def _make_config(
    buffer: int = 60,
    fallback: int = 180,
) -> MagicMock:
    cfg = MagicMock()
    cfg.tail_settlement_buffer_minutes = buffer
    cfg.tail_settlement_hold_minutes = fallback
    return cfg


def _make_context(metadata: dict | None = None, now: datetime | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.metadata = metadata or {}
    ctx.now = now
    return ctx


def test_hold_minutes_fallback_when_no_game_state() -> None:
    config = _make_config(fallback=180)
    context = _make_context(metadata={})
    minutes = _estimated_settlement_hold_minutes(config, context)
    assert minutes == 180


def test_hold_minutes_ended_game_returns_buffer_only() -> None:
    config = _make_config(buffer=60, fallback=180)
    context = _make_context(metadata={
        "live_game": {
            "league": "MLB",
            "home_name": "Atlanta Braves",
            "away_name": "Miami Marlins",
            "home_score": 5,
            "away_score": 3,
            "period": "Final",
            "status": "ended",
        }
    })
    minutes = _estimated_settlement_hold_minutes(config, context)
    assert minutes == 60


def test_hold_minutes_live_game_with_clock_uses_seconds_remaining() -> None:
    config = _make_config(buffer=60, fallback=180)
    context = _make_context(metadata={
        "live_game": {
            "league": "NBA",
            "home_name": "Celtics",
            "away_name": "Heat",
            "home_score": 105,
            "away_score": 98,
            "period": "Q4",
            "status": "live",
            "seconds_remaining": 300,
        }
    })
    minutes = _estimated_settlement_hold_minutes(config, context)
    assert minutes == 5 + 60  # ceil(300/60) + buffer


def test_hold_minutes_live_baseball_uses_inning_estimate() -> None:
    config = _make_config(buffer=60, fallback=180)
    context = _make_context(metadata={
        "live_game": {
            "league": "MLB",
            "home_name": "Atlanta Braves",
            "away_name": "Miami Marlins",
            "home_score": 4,
            "away_score": 3,
            "period": "Inning 9 Bottom",
            "status": "live",
            "baseball_state": {"current_inning": 9, "inning_half": "bottom"},
        }
    })
    minutes = _estimated_settlement_hold_minutes(config, context)
    # bottom 9th: 1 half-inning × 10 min = 10 min remaining + 60 buffer = 70
    assert minutes == 70


def test_hold_minutes_live_baseball_early_inning() -> None:
    config = _make_config(buffer=60, fallback=180)
    context = _make_context(metadata={
        "live_game": {
            "league": "MLB",
            "home_name": "Yankees",
            "away_name": "Red Sox",
            "home_score": 0,
            "away_score": 0,
            "period": "Inning 3 Top",
            "status": "live",
            "baseball_state": {"current_inning": 3, "inning_half": "top"},
        }
    })
    minutes = _estimated_settlement_hold_minutes(config, context)
    # top 3rd: 2 + (9-3)*2 = 14 half-innings × 10 min = 140 min + 60 buffer = 200
    assert minutes == 200
