"""运动类型规范码回归测试。

历史 bug：Goalserve livescore parser 用 ``sport="hockey"`` 标记冰球，而
``_market_sport_codes`` 对 NHL 市场用规范码 ``"ice-hockey"``。
``_normalize_sport_code`` 漏掉 ``hockey -> ice-hockey`` 映射，导致所有 NHL
市场的冰球直播事件在运动类型预过滤中被丢弃，整类比赛拿不到 live state。

之前的发现测试用已经规范化的 ``sport="ice-hockey"`` 构造事件，掩盖了这个
parser 与匹配层之间的命名漂移。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from strategies.current.live_state import (
    _event_sport_code,
    _market_sport_codes,
    _normalize_sport_code,
    candidate_live_events_for_market,
)
from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
)


def test_normalize_hockey_to_ice_hockey() -> None:
    assert _normalize_sport_code("hockey") == "ice-hockey"
    assert _normalize_sport_code("icehockey") == "ice-hockey"


def _hockey_event() -> LiveEvent:
    # parser 实际输出：sport="hockey"（Goalserve 命名）。
    return LiveEvent(
        source="goalserve_livescore",
        source_event_id="637000",
        kind=LiveEventKind.TEAM_MATCH,
        league="Usa: Nhl - Play Offs",
        sport="hockey",
        participants=(
            Participant(role="home", name="Carolina Hurricanes", score=1),
            Participant(role="away", name="Montreal Canadiens", score=4),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=datetime(2026, 5, 22, 0, 56, tzinfo=timezone.utc),
    )


def test_hockey_event_sport_code_matches_nhl_market() -> None:
    event = _hockey_event()
    assert _event_sport_code(event) == "ice-hockey"

    nhl_market = SimpleNamespace(
        market_question="Canadiens vs. Hurricanes",
        market_name=None,
        market_slug="nhl-mon-car-2026-05-21",
        event_title="Canadiens vs. Hurricanes",
        event_slug="nhl-mon-car-2026-05-21",
        category=None,
        tags=("NHL", "nhl", "Hockey"),
        outcomes=(),
        game_start_time=datetime(2026, 5, 22, 0, 0, tzinfo=timezone.utc),
    )
    assert _market_sport_codes(nhl_market) == {"ice-hockey"}

    # 运动类型预过滤不能把冰球事件丢掉。
    candidates = candidate_live_events_for_market(nhl_market, (event,))
    assert event in candidates
