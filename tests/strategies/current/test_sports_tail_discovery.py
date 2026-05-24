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


def test_configured_discovery_queries_only_live_flag() -> None:
    """build_configured_discovery_queries 只使用 polymarket 官方 live=true 标志.

    极简方案: 删除 start_time 兜底窗口, 完全依赖 live=true.
    旧 12h/8h 窗口拉了大量已结束 + 远期市场 (~1500 markets, 60%+ stale),
    收敛到只用 live=true 后约 458 markets (与 polymarket /sports/live 同口径).
    """

    config = CurrentStrategyConfig()
    queries = build_configured_discovery_queries(config)

    by_name = {q.name.split(":")[0]: q for q in queries}
    assert set(by_name) == {"sports_live"}

    live = by_name["sports_live"]
    assert live.params.get("live") == "true"
    assert "start_time_min" not in live.params
    assert "start_time_max" not in live.params


def test_configured_discovery_queries_cover_every_tag_slug() -> None:
    config = dataclasses.replace(
        CurrentStrategyConfig(), discovery_tag_slugs=("sports", "games")
    )
    queries = build_configured_discovery_queries(config)
    tags = {q.params.get("tag_slug") for q in queries}
    assert tags == {"sports", "games"}
    # 每个 tag 一个 live=true 查询.
    assert len(queries) == 2


def test_live_event_discovery_queries_disabled() -> None:
    """直播源驱动的反查已停用——官方 live=true + start_time 查询已完整覆盖，

    且不依赖 Goalserve↔Polymarket 名字匹配，不会漏市场。
    """

    queries = build_live_event_discovery_queries(CurrentStrategyConfig(), (_live_game(),))
    assert queries == ()
