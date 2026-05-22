"""livescore demand-driven 轮询的 active_sports_provider 回归测试。

验证：① 运动码 → feed key 映射覆盖所有策略侧规范码；② 运行时 provider 只把
有 live/即将开赛 Polymarket 市场的运动 feed 纳入，far-future / 仅结束的市场被排除。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.infra.sports.goalserve_livescore_client import (
    SPORT_CODE_TO_FEED_KEYS,
    _SPORT_FEEDS,
)
from polymarket_trader.main import _build_livescore_active_sports_provider
from polymarket_trader.runtime.registry import MarketRegistry


def _market(
    condition_id: str,
    *,
    tags: tuple[str, ...] = (),
    game_start_time: datetime | None = None,
    end_date: datetime | None = None,
) -> Market:
    return Market(
        condition_id=condition_id,
        market_slug=condition_id,
        outcomes=(
            MarketOutcome(token_id=f"{condition_id}-y", outcome="Yes"),
            MarketOutcome(token_id=f"{condition_id}-n", outcome="No"),
        ),
        tags=tags,
        game_start_time=game_start_time,
        end_date=end_date,
    )


def test_feed_key_map_values_are_known_feed_keys() -> None:
    """映射中所有 feed key 必须是真实的 _SPORT_FEEDS 入口。"""
    for code, keys in SPORT_CODE_TO_FEED_KEYS.items():
        for key in keys:
            assert key in _SPORT_FEEDS, f"{code} 映射到未知 feed key {key}"


def test_feed_key_map_all_sports_have_livescore_feed() -> None:
    """volleyball / american-football / table-tennis 均已补 livescore getfeed
    兜底源，映射到非空 feed key 集合——demand-driven 轮询可正常触发抓取。
    """
    assert SPORT_CODE_TO_FEED_KEYS["table-tennis"] == frozenset({"table-tennis"})
    assert SPORT_CODE_TO_FEED_KEYS["volleyball"] == frozenset({"volleyball"})
    assert SPORT_CODE_TO_FEED_KEYS["american-football"] == frozenset({"amfootball"})


def test_provider_includes_live_market_sport() -> None:
    """正在直播的市场，其运动 feed key 全部被纳入。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "nba-live",
            tags=("NBA", "Basketball"),
            game_start_time=now - timedelta(minutes=30),
            end_date=now + timedelta(hours=2),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    active = provider()
    assert active == frozenset({"basketball", "nba", "wnba"})


def test_provider_includes_near_start_market_sport() -> None:
    """60 分钟内即将开赛的市场，其运动 feed key 被纳入。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "mlb-soon",
            tags=("MLB", "Baseball"),
            game_start_time=now + timedelta(minutes=45),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert provider() == frozenset({"baseball", "mlb"})


def test_provider_excludes_far_future_market() -> None:
    """开赛时间在 60 分钟以外的市场不纳入。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "nhl-far",
            tags=("NHL", "Hockey"),
            game_start_time=now + timedelta(hours=6),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert provider() == frozenset()


def test_provider_excludes_ended_market() -> None:
    """已结束（end_date 过去）的市场不纳入。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "esports-ended",
            tags=("Esports", "CS2"),
            game_start_time=now - timedelta(hours=4),
            end_date=now - timedelta(hours=1),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert provider() == frozenset()


def test_provider_empty_registry_returns_empty() -> None:
    """无 tracked market 时返回空集，整轮跳过 livescore 抓取。"""
    provider = _build_livescore_active_sports_provider(MarketRegistry())
    assert provider() == frozenset()


def test_provider_includes_amfootball_livescore_feed() -> None:
    """american-football 已补 livescore getfeed 兜底源，live NFL 市场触发 amfootball 抓取。

    标签只用 "NFL"——_market_sport_codes 对 "american football" 文本会同时命中
    "football" 子串，导致额外的 soccer 误分类；这里隔离纯 american-football 场景。
    """
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "nfl-live",
            tags=("NFL",),
            game_start_time=now - timedelta(minutes=10),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert "amfootball" in provider()


def test_provider_includes_table_tennis_livescore_feed() -> None:
    """table-tennis 已补 tennis_scores/tt_live livescore 兜底源——
    live 乒乓市场触发 table-tennis 抓取。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "tt-live",
            tags=("Table Tennis",),
            game_start_time=now - timedelta(minutes=10),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert "table-tennis" in provider()


def test_provider_includes_market_with_no_start_time_via_end_date() -> None:
    """game_start_time 缺失（如 esports 市场）时用 end_date 兜底：

    end_date 在未来 6h 内 → 视为正在进行/临近 → 纳入轮询。否则整类运动
    （esports）永不进 active 集合、直播源不被轮询、状态恒 stale。
    """
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "cs2-fokus-rbls-2026-05-22",
            tags=("Esports", "CS2"),
            game_start_time=None,
            end_date=now + timedelta(hours=3),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert provider() == frozenset({"esports"})


def test_provider_excludes_no_start_market_with_far_future_end() -> None:
    """game_start_time 缺失且 end_date 远在 6h 之外 → 不纳入（轮询范围有界）。"""
    registry = MarketRegistry()
    now = datetime.now(timezone.utc)
    registry.upsert(
        _market(
            "cs2-far-future",
            tags=("Esports", "CS2"),
            game_start_time=None,
            end_date=now + timedelta(hours=20),
        )
    )
    provider = _build_livescore_active_sports_provider(registry)
    assert provider() == frozenset()
