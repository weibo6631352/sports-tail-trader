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


def test_esports_market_filters_out_cross_sport_events() -> None:
    """esports 市场必须有 esports 运动码，否则跨运动错配。

    历史 bug：_market_sport_codes 无 esports 条目 → CS2 市场在运动预过滤里
    无 sport 约束 → 足球预备队 "Kongsvinger 2" 的低分别名 "2" 命中 esports
    slug 里的 "cs2"/"2026" → CS2 市场被错配到挪威三级联赛足球赛。
    """

    cs2_market = SimpleNamespace(
        market_question="",
        market_name=None,
        market_slug="cs2-fdb-paina-2026-05-22",
        event_title="Counter-Strike: Fake do Biru vs paiN Academy (BO3)",
        event_slug="cs2-fdb-paina-2026-05-22",
        category="Sports",
        tags=(),
        outcomes=(),
        game_start_time=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
    )
    assert _market_sport_codes(cs2_market) == {"esports"}

    soccer_event = LiveEvent(
        source="goalserve_livescore",
        source_event_id="s1",
        kind=LiveEventKind.TEAM_MATCH,
        league="Norway: Division 3 - Group 5",
        sport="soccer",
        participants=(
            Participant(role="home", name="Kongsvinger 2", score=2),
            Participant(role="away", name="Tromso 2", score=1),
        ),
        status=SportsLiveGameStatus.ENDED,
        observed_at=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
    )
    esports_event = LiveEvent(
        source="goalserve_livescore",
        source_event_id="e1",
        kind=LiveEventKind.TEAM_MATCH,
        league="CCT South America",
        sport="esports",
        participants=(
            Participant(role="home", name="Fake do Biru", score=0),
            Participant(role="away", name="paiN Academy", score=0),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=datetime(2026, 5, 22, 18, 0, tzinfo=timezone.utc),
    )

    candidates = candidate_live_events_for_market(cs2_market, (soccer_event, esports_event))
    assert soccer_event not in candidates  # 跨运动足球赛被过滤
    assert esports_event in candidates
