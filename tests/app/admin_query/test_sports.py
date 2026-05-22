"""AdminSportsQueryMixin 直接 unit 测试。

覆盖：
- ``list_sports_live_events_history`` 直通到 list_audit_events (with event_title)
- ``list_sports_live_states`` 无 store 时返回空 page
- ``list_sports_live_states`` 过滤掉 live_state_payload 为空的 record
- ``list_sports_live_source_gaps`` registry=None 时返回空 envelope + 计数 0
- ``list_sports_live_source_gaps`` 没有 metadata store 时所有 market 都视为 missing
- ``list_sports_live_source_gaps`` 按 prefix 过滤
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from polymarket_trader.app.admin_query.sports import AdminSportsQueryMixin
from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.db import RepositoryPage


def _market(idx: int, *, slug_prefix: str = "nba", hours_from_now: float = 1.0) -> Market:
    return Market(
        condition_id=f"cond-{idx}",
        market_slug=f"{slug_prefix}-game-{idx}",
        outcomes=(MarketOutcome(token_id=f"tok-{idx}", outcome="YES"),),
        trading_status=TradingStatus.ELIGIBLE,
        game_start_time=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
        + timedelta(hours=hours_from_now),
    )


@dataclass
class _StubRecord:
    condition_id: str
    market_slug: str
    event_slug: str | None
    live_state_payload: dict[str, Any] = field(default_factory=dict)
    live_state_phase: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {"condition_id": self.condition_id, "has_state": bool(self.live_state_payload)}


@dataclass
class _StubStore:
    items: list[_StubRecord] = field(default_factory=list)

    def records(self) -> list[_StubRecord]:
        return list(self.items)

    def find(self, *, condition_id: str, market_slug: str | None, event_slug: str | None) -> _StubRecord | None:
        for r in self.items:
            if r.condition_id == condition_id:
                return r
        return None


@dataclass
class _StubRegistry:
    markets: tuple[Market, ...]

    def snapshot(self) -> "_StubRegistry":
        return self


@dataclass
class _StubAuditPayload:
    items: tuple[Any, ...]
    total: int


class _Host(AdminSportsQueryMixin):
    """注入 store / runtime 等 helper stub。

    `runtime` 缺 extension/market_service ⇒ _runtime_extension_hooks 返回 None ⇒
    _live_source_gap_scope_markets 返回全部 markets。"""

    def __init__(
        self,
        *,
        store: _StubStore | None = None,
        registry_markets: tuple[Market, ...] | None = None,
        registry_none: bool = False,
    ) -> None:
        self._store = store
        if registry_none:
            self.runtime = type("RT", (), {"registry": None, "settings": None})()  # registry=None → early return
        else:
            self.runtime = type(
                "RT",
                (),
                {
                    "registry": _StubRegistry(markets=registry_markets or ()),
                    "settings": None,
                },
            )()
        self._captured_audit_call: dict[str, Any] | None = None

    def _entry_metadata_store(self) -> _StubStore | None:
        return self._store

    def _slice_sequence(self, items: Any, *, limit: int, offset: int) -> RepositoryPage[Any]:
        items_list = tuple(items)
        sliced = items_list[offset : offset + limit]
        return RepositoryPage(items=sliced, total=len(items_list), limit=limit, offset=offset)

    async def list_audit_events(self, **kwargs: Any) -> dict[str, Any]:
        # 拦截以验证 list_sports_live_events_history 透传参数
        self._captured_audit_call = kwargs
        return {"items": [], "total": 0, "limit": kwargs.get("limit"), "offset": kwargs.get("offset")}


def test_list_sports_live_events_history_forwards_to_list_audit_events() -> None:
    host = _Host()
    payload = asyncio.run(
        host.list_sports_live_events_history(limit=50, offset=5, condition_id="cond-X")
    )
    assert payload["limit"] == 50
    assert host._captured_audit_call is not None
    assert host._captured_audit_call["event_title"] == "sports_live_state_recorded"
    assert host._captured_audit_call["condition_id"] == "cond-X"


def test_list_sports_live_states_empty_when_no_store() -> None:
    host = _Host(store=None)
    payload = asyncio.run(host.list_sports_live_states(limit=20, offset=0))
    assert payload == {"items": [], "total": 0, "limit": 20, "offset": 0}


def test_list_sports_live_states_filters_records_without_live_state_payload() -> None:
    store = _StubStore(
        items=[
            _StubRecord("c1", "nba-1", None, live_state_payload={"score": "1-0"}),
            _StubRecord("c2", "nba-2", None, live_state_payload={}),  # 必须被剔除
        ]
    )
    host = _Host(store=store)
    payload = asyncio.run(host.list_sports_live_states(limit=20, offset=0))
    assert payload["total"] == 1
    assert payload["items"][0]["condition_id"] == "c1"


def test_list_sports_live_source_gaps_returns_zero_when_registry_missing() -> None:
    host = _Host(registry_none=True)
    payload = asyncio.run(host.list_sports_live_source_gaps(limit=10, offset=0))
    assert payload["tracked_markets"] == 0
    assert payload["live_state_markets"] == 0
    assert payload["missing_live_state_markets"] == 0
    assert payload["by_prefix"] == []
    assert payload["items"] == []


def test_list_sports_live_source_gaps_treats_all_markets_as_missing_without_store() -> None:
    # store=None ⇒ live_state_markets=0；所有 in-window market 都 missing。
    # 用 hours_from_now=1 保证 urgency=starts_within_24h（< 24h）→ 默认 include
    markets = (_market(1), _market(2))
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    host = _Host(store=None, registry_markets=markets)
    payload = asyncio.run(host.list_sports_live_source_gaps(limit=10, offset=0, now=now))
    assert payload["missing_live_state_markets"] == 2
    assert payload["live_state_markets"] == 0
    # by_prefix 应聚合到 "nba"
    by_prefix = {row["prefix"]: row["count"] for row in payload["by_prefix"]}
    assert by_prefix == {"nba": 2}


def test_list_sports_live_source_gaps_filters_by_prefix_argument() -> None:
    markets = (_market(1, slug_prefix="nba"), _market(2, slug_prefix="kbo"))
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    host = _Host(registry_markets=markets)
    payload = asyncio.run(
        host.list_sports_live_source_gaps(limit=10, offset=0, prefix="kbo", now=now)
    )
    assert payload["missing_live_state_markets"] == 1
    assert payload["prefix"] == "kbo"


def test_list_sports_live_source_gaps_defers_future_schedule_markets() -> None:
    # 起赛时间 > 24h 后 → urgency=future_schedule，未开启 include 时被 defer
    markets = (_market(1, hours_from_now=48.0),)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    host = _Host(registry_markets=markets)
    payload = asyncio.run(host.list_sports_live_source_gaps(limit=10, offset=0, now=now))
    assert payload["missing_live_state_markets"] == 0
    assert payload["deferred_future_schedule_markets"] == 1


def test_list_sports_live_source_gaps_include_future_schedule_overrides_defer() -> None:
    markets = (_market(1, hours_from_now=48.0),)
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)
    host = _Host(registry_markets=markets)
    payload = asyncio.run(
        host.list_sports_live_source_gaps(
            limit=10, offset=0, include_future_schedule=True, now=now
        )
    )
    assert payload["missing_live_state_markets"] == 1
    assert payload["deferred_future_schedule_markets"] == 0
