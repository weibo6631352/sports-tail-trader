from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from polymarket_trader.domain.sports_live import (
    LiveEvent,
    LiveEventKind,
    Participant,
    SportsLiveGameStatus,
)
from strategies.current.config import CurrentStrategyConfig
from strategies.current.discovery import (
    build_configured_discovery_queries,
    build_live_event_discovery_queries,
)


def _live_game() -> LiveEvent:
    return LiveEvent(
        source="test",
        source_event_id="evt-1",
        kind=LiveEventKind.TEAM_MATCH,
        league="NBA",
        sport="basketball",
        participants=(
            Participant(role="home", name="Boston Celtics"),
            Participant(role="away", name="New York Knicks"),
        ),
        status=SportsLiveGameStatus.LIVE,
        observed_at=datetime.now(timezone.utc),
    )


def test_configured_discovery_queries_use_official_live_and_start_time_method() -> None:
    """build_configured_discovery_queries 复用官方 sports/live 的发现方法：

    每个 tag 生成三个定向查询——live=true / start_time 进行中窗口 /
    start_time 即将开赛窗口——不再做 title_search × tag_slug 全量翻页。
    """

    config = CurrentStrategyConfig()
    queries = build_configured_discovery_queries(config)

    by_name = {q.name.split(":")[0]: q for q in queries}
    assert set(by_name) == {"sports_live", "sports_inplay_soon", "sports_upcoming"}

    live = by_name["sports_live"]
    assert live.params.get("live") == "true"
    assert "start_time_min" not in live.params

    inplay = by_name["sports_inplay_soon"]
    assert "start_time_min" in inplay.params and "start_time_max" in inplay.params
    # 进行中窗口按 start_time 取——与 live 标志无关，是 live=true 的完整兜底。
    assert inplay.params["start_time_min"] < inplay.params["start_time_max"]

    upcoming = by_name["sports_upcoming"]
    assert "start_time_min" in upcoming.params and "start_time_max" in upcoming.params
    # 即将开赛窗口接在进行中窗口之后。
    assert upcoming.params["start_time_min"] >= inplay.params["start_time_max"]


def test_configured_discovery_queries_cover_every_tag_slug() -> None:
    config = dataclasses.replace(
        CurrentStrategyConfig(), discovery_tag_slugs=("sports", "games")
    )
    queries = build_configured_discovery_queries(config)
    tags = {q.params.get("tag_slug") for q in queries}
    assert tags == {"sports", "games"}
    # 每个 tag 三个查询。
    assert len(queries) == 6


def test_live_event_discovery_queries_disabled() -> None:
    """直播源驱动的反查已停用——官方 live=true + start_time 查询已完整覆盖，

    且不依赖 Goalserve↔Polymarket 名字匹配，不会漏市场。
    """

    queries = build_live_event_discovery_queries(CurrentStrategyConfig(), (_live_game(),))
    assert queries == ()
