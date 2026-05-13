"""``AdminService.list_series_state_snapshots`` 与 ``outright_team_resolution``
端到端 service-layer 测试——用真实 AdminService + 真实 EntryMetadataStore 装配，
验证 metadata 读取、SeriesState 解析、resolve_market_team_debug trace 透传。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from polymarket_trader.app.admin_service import AdminService
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.entry_metadata import EntryMetadataStore
from polymarket_trader.runtime.registry import MarketRegistry


_NOW = datetime(2026, 5, 13, 18, 0, tzinfo=timezone.utc)


def _runtime(*, registry: MarketRegistry, store: EntryMetadataStore) -> SimpleNamespace:
    """AdminService 通过 ``runtime`` 上的属性访问 registry / metadata store；
    其它字段保持 None 即可（本测试只走 series/outright 两个查询）。"""

    return SimpleNamespace(
        registry=registry,
        entry_metadata_store=store,
        supervisor=None,
        settings=None,
        account_state_store=None,
        event_bus=None,
        market_ws_worker=None,
        clob_client=None,
        trading_service=None,
        trading_decision_service=None,
        db_session_factory=None,
        extension=None,
    )


def _service(registry: MarketRegistry, store: EntryMetadataStore) -> AdminService:
    return AdminService().bind_runtime(_runtime(registry=registry, store=store))


# ---------------------------------------------------------------------------
# list_series_state_snapshots
# ---------------------------------------------------------------------------


def test_list_series_state_returns_empty_when_no_records() -> None:
    service = _service(MarketRegistry(), EntryMetadataStore())
    payload = asyncio.run(service.list_series_state_snapshots())
    assert payload["items"] == []
    assert payload["total_records"] == 0


def test_list_series_state_returns_records_sorted_by_observed_at_desc() -> None:
    store = EntryMetadataStore()
    older = "2026-05-13T17:00:00+00:00"
    newer = "2026-05-13T18:00:00+00:00"
    store.upsert(
        condition_id="cond-old",
        market_slug="series-old",
        metadata={
            "series_state": {
                "team_a": "Old A",
                "team_b": "Old B",
                "wins_a": 1,
                "wins_b": 0,
                "best_of": 7,
                "observed_at": older,
            }
        },
        source="series_state:espn",
    )
    store.upsert(
        condition_id="cond-new",
        market_slug="series-new",
        metadata={
            "series_state": {
                "team_a": "New A",
                "team_b": "New B",
                "wins_a": 2,
                "wins_b": 1,
                "best_of": 7,
                "observed_at": newer,
            }
        },
        source="series_state:espn",
    )

    service = _service(MarketRegistry(), store)
    payload = asyncio.run(service.list_series_state_snapshots(now=_NOW))

    assert payload["total_records"] == 2
    items = payload["items"]
    # 新的排前面
    assert items[0]["condition_id"] == "cond-new"
    assert items[1]["condition_id"] == "cond-old"
    # age_seconds 算的是 _NOW - observed_at；newer 已经 = _NOW → age = 0
    assert items[0]["age_seconds"] == pytest.approx(0.0, abs=1.0)
    assert items[1]["age_seconds"] == pytest.approx(3600.0, abs=1.0)


def test_list_series_state_skips_records_without_series_state() -> None:
    """metadata 里没 series_state 的 record 不应出现在结果中（仅 live_state 等其他字段）。"""

    store = EntryMetadataStore()
    store.upsert(
        condition_id="cond-x",
        market_slug="m-x",
        metadata={"live_state_payload": {"score": "1-0"}},
        source="manual",
    )
    service = _service(MarketRegistry(), store)
    payload = asyncio.run(service.list_series_state_snapshots())
    assert payload["total_records"] == 0


def test_list_series_state_handles_missing_store() -> None:
    """runtime 上的 entry_metadata_store 为 None（早期启动）→ 空数组而非异常。"""

    runtime = SimpleNamespace(
        registry=MarketRegistry(),
        entry_metadata_store=None,
        db_session_factory=None,
        extension=None,
    )
    service = AdminService().bind_runtime(runtime)
    payload = asyncio.run(service.list_series_state_snapshots())
    assert payload["items"] == []
    assert payload["total_records"] == 0


# ---------------------------------------------------------------------------
# outright_team_resolution
# ---------------------------------------------------------------------------


def _outright_market(condition_id: str = "cond-celtics") -> Market:
    return Market(
        condition_id=condition_id,
        market_slug="will-the-boston-celtics-win-2026-nba",
        market_question="Will the Boston Celtics win the 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
        outcomes=(
            MarketOutcome(token_id="yes-tok", outcome="Yes"),
            MarketOutcome(token_id="no-tok", outcome="No"),
        ),
    )


def _season_odds_meta() -> dict:
    return {
        "season_odds_snapshot": {
            "market_key": "2026-nba-championship-winner",
            "fair_probabilities": {
                "Boston Celtics": "0.33",
                "Denver Nuggets": "0.30",
            },
            "observed_at": _NOW.isoformat(),
            "source": "theoddsapi",
        }
    }


def test_team_resolution_returns_none_for_unknown_market() -> None:
    service = _service(MarketRegistry(), EntryMetadataStore())
    payload = asyncio.run(
        service.outright_team_resolution(condition_id="cond-missing")
    )
    assert payload is None


def test_team_resolution_returns_trace_when_resolved() -> None:
    registry = MarketRegistry()
    market = _outright_market()
    registry.upsert(market)
    store = EntryMetadataStore()
    store.upsert(
        condition_id=market.condition_id,
        market_slug=market.market_slug,
        event_slug=market.event_slug,
        metadata=_season_odds_meta(),
        source="season_odds:test",
    )

    service = _service(registry, store)
    payload = asyncio.run(service.outright_team_resolution(condition_id=market.condition_id))
    assert payload is not None
    assert payload["snapshot_available"] is True
    assert payload["trace"]["resolved"] == "Boston Celtics"
    assert payload["trace"]["ambiguous"] is False
    assert "Boston Celtics" in payload["trace"]["candidate_teams"]


def test_team_resolution_marks_missing_snapshot() -> None:
    """市场存在但 metadata 里没 season_odds → snapshot_available=False。"""

    registry = MarketRegistry()
    market = _outright_market()
    registry.upsert(market)
    store = EntryMetadataStore()  # 故意不写 season_odds_snapshot

    service = _service(registry, store)
    payload = asyncio.run(service.outright_team_resolution(condition_id=market.condition_id))
    assert payload is not None
    assert payload["snapshot_available"] is False
    assert payload["reason"] == "missing_season_odds"
    assert payload["trace"] is None


def test_team_resolution_returns_ambiguous_trace_when_multiple_match() -> None:
    registry = MarketRegistry()
    market = Market(
        condition_id="cond-x",
        market_slug="celtics-vs-nuggets-championship",
        market_question="Boston Celtics vs. Denver Nuggets 2026 NBA championship",
        event_slug="2026-nba-championship-winner",
        outcomes=(MarketOutcome(token_id="y", outcome="Yes"),),
    )
    registry.upsert(market)
    store = EntryMetadataStore()
    store.upsert(
        condition_id=market.condition_id,
        metadata={
            "season_odds_snapshot": {
                "market_key": "x",
                "fair_probabilities": {
                    "Boston Celtics": Decimal("0.5"),
                    "Denver Nuggets": Decimal("0.5"),
                },
                "observed_at": _NOW.isoformat(),
                "source": "theoddsapi",
            }
        },
    )
    service = _service(registry, store)
    payload = asyncio.run(service.outright_team_resolution(condition_id=market.condition_id))
    assert payload is not None
    assert payload["snapshot_available"] is True
    assert payload["trace"]["resolved"] is None
    assert payload["trace"]["ambiguous"] is True
    assert set(payload["trace"]["matches"]) == {"Boston Celtics", "Denver Nuggets"}
