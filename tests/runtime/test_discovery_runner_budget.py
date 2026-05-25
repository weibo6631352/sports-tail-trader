from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from polymarket_trader.domain.sports_live import (

    LiveEvent,
    SportsLiveGameStatus,
    Participant,
    LiveEventKind,
)
from polymarket_trader.extension_api import DiscoveryQuery
from polymarket_trader.runtime import discovery_runner


def test_full_market_discovery_defaults_keep_background_sla() -> None:
    assert discovery_runner._MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK == 2
    assert discovery_runner._MARKET_DISCOVERY_MAX_RUNTIME_MS == 200.0
    # 2.0s 是经过测算的稳态值：0.5s 实测浪费 80% 调用，新市场出现 2s 内捕获仍快于
    # 大部分散户；如果未来策略对入场延迟更敏感，单调本常量即可。
    assert discovery_runner.MARKET_DISCOVERY_TICK_SECONDS == 2.0


def test_discovery_queries_prioritize_live_game_queries_before_broad_queries() -> None:
    game = LiveEvent(
        
        participants=(Participant(role="home", name="Oilers", score=0, display_name="Edmonton Oilers"), Participant(role="away", name="Ducks", score=0, display_name="Anaheim Ducks"),),
        kind=LiveEventKind.TEAM_MATCH,
        sport="ice-hockey",source="nhl",
        source_event_id="2025030185",
        league="NHL",
        status=SportsLiveGameStatus.SCHEDULED,
        period="FUT",
    )
    hooks = SimpleNamespace(
        discovery_queries=lambda: (DiscoveryQuery.title_search("nhl"),),
    )
    live_state_hooks = SimpleNamespace(
        discovery_queries_for_live_events=lambda events: (
            DiscoveryQuery(
                name=f"live:{events[0].source_event_id}:oilers",
                params={"title_search": "oilers", "tag_slug": "sports"},
            ),
        ),
    )
    runtime = SimpleNamespace(
        extension=SimpleNamespace(hooks=hooks, live_state_hooks=live_state_hooks),
        sports_live_state_worker=SimpleNamespace(last_events=lambda: (game,)),
    )

    queries = discovery_runner._discovery_queries(runtime)

    assert queries[0].params == {"title_search": "oilers", "tag_slug": "sports"}
    assert queries[1].params == {"title_search": "nhl"}


def test_discovery_state_restarts_at_front_when_live_queries_are_added() -> None:
    state = discovery_runner.FullMarketDiscoveryState()
    broad_queries = (
        DiscoveryQuery.title_search("nba"),
        DiscoveryQuery.title_search("nhl"),
        DiscoveryQuery.title_search("mlb"),
    )

    assert state.next_query(broad_queries).name == "title_search:nba"
    assert state.next_query(broad_queries).name == "title_search:nhl"

    live_queries = (
        DiscoveryQuery(name="live_game:tennis:1:erhard nedic", params={"title_search": "erhard nedic"}),
        *broad_queries,
    )

    assert state.next_query(live_queries).name == "live_game:tennis:1:erhard nedic"


def test_record_page_does_not_reset_consecutive_failures_until_round_finishes() -> None:
    """F5：单页成功不清零失败计数；只有 finish_round 算"整轮稳定"才清零。

    旧实现每个 success page 都执行 ``consecutive_failures = 0``，结果
    "失败 → success → 再失败" 序列里指数回退计数器永远停在 1，永远只回退 5s，
    实际从未升到 10/20/40/60s。这违反 N7 指数回退设计意图。
    """

    state = discovery_runner.FullMarketDiscoveryState()

    # 模拟连续失败累积。
    state.record_failure("gamma timeout")
    state.record_failure("gamma timeout")
    assert state.consecutive_failures == 2
    # 第二次失败时按指数回退应该 ≥ 10s，而不是基础 5s。
    assert discovery_runner._retry_backoff_seconds(state.consecutive_failures) == 10

    # 中间夹一次单页 success 不应该清零失败计数。
    state.record_page(
        query_name="q1",
        total_queries=2,
        page_size=5,
        next_cursor="cursor-1",
    )
    assert state.consecutive_failures == 2, "单页 success 不能让回退器重置（F5）"

    # 再失败一次：回退应该继续升级到 20s。
    state.record_failure("gamma timeout")
    assert state.consecutive_failures == 3
    assert discovery_runner._retry_backoff_seconds(state.consecutive_failures) == 20

    # 只有整轮完成才认为系统稳定，清零计数器。
    state.completed_query_names = {"q1", "q2"}
    state.finish_round()
    assert state.consecutive_failures == 0


def test_live_event_expansion_uses_stale_live_metadata_event_slugs() -> None:
    now = datetime(2026, 4, 29, 9, 20, tzinfo=timezone.utc)
    state = discovery_runner.FullMarketDiscoveryState()
    # 301s 刚超过新冷却阈值 _LIVE_EVENT_EXPANSION_REFRESH_SECONDS=300（之前 30s）。
    # 跨洋链路下 30s 太短反复展开同一 event 浪费带宽，5 分钟内已有的 sub-market
    # 不会变；keyset discovery 兜住任何漏的新分盘。
    state.live_event_expanded_at["atp-live-1"] = now - timedelta(seconds=301)
    state.live_event_expanded_at["atp-live-2"] = now
    runtime = SimpleNamespace(
        market_discovery_scan=state,
        entry_metadata_store=SimpleNamespace(
            records=lambda: (
                SimpleNamespace(
                    event_slug="atp-live-1",
                    live_state_phase="live",
                    live_state_payload={"status": "live"},
                ),
                SimpleNamespace(
                    event_slug="atp-live-2",
                    live_state_phase="live",
                    live_state_payload={"status": "live"},
                ),
                SimpleNamespace(
                    event_slug="kbo-scheduled",
                    live_state_phase="scheduled",
                    live_state_payload={"status": "scheduled"},
                ),
            )
        ),
    )

    assert discovery_runner._live_event_slugs_for_expansion(runtime, now=now) == ("atp-live-1",)


def test_priority_condition_refresh_skips_recently_refreshed() -> None:
    """P3.1：最近 15s 内已刷新过的 condition_id 不再重复请求。"""

    now = datetime(2026, 4, 29, 9, 20, tzinfo=timezone.utc)
    state = discovery_runner.FullMarketDiscoveryState()
    # cond-1 刚刚 10s 前刷新过 → 未到 15s 间隔，不应列入 due。
    state.priority_condition_refreshed_at["cond-1"] = now - timedelta(seconds=10)
    # cond-2 20s 前刷新过 → 超过间隔，应列入 due。
    state.priority_condition_refreshed_at["cond-2"] = now - timedelta(seconds=20)
    account_snapshot = SimpleNamespace(
        positions=(
            SimpleNamespace(condition_id="cond-1", token_id="t1", settled_zero_value=False),
            SimpleNamespace(condition_id="cond-2", token_id="t2", settled_zero_value=False),
        ),
        open_orders=(),
    )
    runtime = SimpleNamespace(
        market_discovery_scan=state,
        account_state_store=SimpleNamespace(snapshot=lambda: account_snapshot),
    )

    due = discovery_runner._priority_condition_ids_due_for_refresh(runtime, now=now)

    assert "cond-1" not in due
    assert "cond-2" in due


def test_priority_condition_refresh_skips_settled_zero_positions() -> None:
    """P3.1：settled_zero_value=True 的仓位不列入 priority 刷新队列。"""

    now = datetime(2026, 4, 29, 9, 20, tzinfo=timezone.utc)
    state = discovery_runner.FullMarketDiscoveryState()
    account_snapshot = SimpleNamespace(
        positions=(
            SimpleNamespace(condition_id="cond-settled", token_id="ts", settled_zero_value=True),
            SimpleNamespace(condition_id="cond-active", token_id="ta", settled_zero_value=False),
        ),
        open_orders=(),
    )
    runtime = SimpleNamespace(
        market_discovery_scan=state,
        account_state_store=SimpleNamespace(snapshot=lambda: account_snapshot),
    )

    due = discovery_runner._priority_condition_ids_due_for_refresh(runtime, now=now)

    assert "cond-settled" not in due
    assert "cond-active" in due


def test_refresh_priority_condition_ids_fetches_market_and_ingests() -> None:
    """P3.1：对到期 priority 市场调 gamma.get_market_by_condition_id 并 ingest，
    更新 refreshed_at。走 condition_ids 过滤端点，避开 /markets/{id} 422 bug。"""

    ingested: list[str] = []
    fetched: list[str] = []

    class _FakeGammaClient:
        async def get_market_by_condition_id(
            self, condition_id: str, *, timeout_s: float
        ) -> SimpleNamespace:
            fetched.append(condition_id)
            return SimpleNamespace(raw={"condition_id": condition_id})

    state = discovery_runner.FullMarketDiscoveryState()
    # 只有 cond-exp 到期（无 refreshed_at 记录）
    account_snapshot = SimpleNamespace(
        positions=(
            SimpleNamespace(condition_id="cond-exp", token_id="t", settled_zero_value=False),
        ),
        open_orders=(),
    )

    async def fake_ingest(page, *, source, trace_id):
        ingested.append(source)

    runtime = SimpleNamespace(
        market_discovery_scan=state,
        account_state_store=SimpleNamespace(snapshot=lambda: account_snapshot),
        gamma_client=_FakeGammaClient(),
        market_discovery_worker=SimpleNamespace(ingest_source_page=fake_ingest),
    )

    asyncio.run(discovery_runner.refresh_priority_condition_ids(runtime))

    assert fetched == ["cond-exp"]
    assert ingested == ["gamma.priority_refresh"]
    assert "cond-exp" in state.priority_condition_refreshed_at


def test_refresh_priority_condition_ids_skips_when_gamma_returns_none() -> None:
    """P3.1：condition 已被 gamma 下架（list/condition_ids 反查为空）→
    不 ingest、不更新 refreshed_at。"""

    ingested: list[str] = []
    fetched: list[str] = []

    class _FakeGammaClient:
        async def get_market_by_condition_id(
            self, condition_id: str, *, timeout_s: float
        ) -> SimpleNamespace | None:
            fetched.append(condition_id)
            return None  # gamma 下架的 condition

    state = discovery_runner.FullMarketDiscoveryState()
    account_snapshot = SimpleNamespace(
        positions=(
            SimpleNamespace(condition_id="cond-dead", token_id="t", settled_zero_value=False),
        ),
        open_orders=(),
    )

    async def fake_ingest(page, *, source, trace_id):
        ingested.append(source)

    runtime = SimpleNamespace(
        market_discovery_scan=state,
        account_state_store=SimpleNamespace(snapshot=lambda: account_snapshot),
        gamma_client=_FakeGammaClient(),
        market_discovery_worker=SimpleNamespace(ingest_source_page=fake_ingest),
    )

    asyncio.run(discovery_runner.refresh_priority_condition_ids(runtime))

    assert fetched == ["cond-dead"]
    assert ingested == []
    assert "cond-dead" not in state.priority_condition_refreshed_at


def test_refresh_priority_condition_ids_caps_at_budget() -> None:
    """P3.1：单 tick 最多发 _PRIORITY_CONDITION_REFRESH_BUDGET_PER_TICK 次请求。"""

    fetched: list[str] = []

    class _FakeGammaClient:
        async def get_market_by_condition_id(
            self, condition_id: str, *, timeout_s: float
        ) -> SimpleNamespace:
            fetched.append(condition_id)
            return SimpleNamespace(raw={"condition_id": condition_id})

    state = discovery_runner.FullMarketDiscoveryState()
    # 3 个到期市场，但 budget=1 只允许拉 1 次
    account_snapshot = SimpleNamespace(
        positions=(
            SimpleNamespace(condition_id=f"cond-{i}", token_id=f"t{i}", settled_zero_value=False)
            for i in range(3)
        ),
        open_orders=(),
    )

    async def fake_ingest(page, *, source, trace_id):
        pass

    runtime = SimpleNamespace(
        market_discovery_scan=state,
        account_state_store=SimpleNamespace(snapshot=lambda: account_snapshot),
        gamma_client=_FakeGammaClient(),
        market_discovery_worker=SimpleNamespace(ingest_source_page=fake_ingest),
    )

    asyncio.run(discovery_runner.refresh_priority_condition_ids(runtime))

    assert len(fetched) <= discovery_runner._PRIORITY_CONDITION_REFRESH_BUDGET_PER_TICK


def test_run_market_discovery_scan_records_failure_when_gamma_raises() -> None:
    """Exception path (line 233): gamma error → state.consecutive_failures increments."""

    class _ErrorGammaClient:
        async def list_events_keyset_by_params(self, params: dict, *, timeout_s: float) -> tuple:
            raise ValueError("simulated_gamma_failure")

        async def list_events(self, **kwargs) -> tuple:
            return (), None

    state = discovery_runner.FullMarketDiscoveryState()
    recorded_failures: list[dict] = []
    runtime = SimpleNamespace(
        market_discovery_scan=state,
        market_discovery_worker=SimpleNamespace(
            last_failure=None,
            mark_scan_success=lambda: None,
            record_failure=lambda source, reason, retry_after_seconds: recorded_failures.append(
                {"source": source, "reason": reason}
            ),
            ingest_source_page=lambda page, source, trace_id: None,
        ),
        supervisor=SimpleNamespace(
            heartbeat_worker=lambda name, detail="": None,
            mark_worker_error=lambda name, detail="", last_error="": None,
        ),
        extension=SimpleNamespace(
            hooks=SimpleNamespace(discovery_queries=lambda: (DiscoveryQuery.title_search("nba"),)),
            live_state_hooks=SimpleNamespace(discovery_queries_for_live_events=lambda events: ()),
        ),
        sports_live_state_worker=SimpleNamespace(last_events=lambda: ()),
        gamma_client=_ErrorGammaClient(),
        metrics=SimpleNamespace(inc_counter=lambda name, value: None),
        entry_metadata_store=SimpleNamespace(records=lambda: ()),
    )

    asyncio.run(discovery_runner.run_market_discovery_scan(runtime))

    assert state.consecutive_failures == 1
    assert state.last_error == "simulated_gamma_failure"
    assert len(recorded_failures) == 1
    assert recorded_failures[0]["source"] == "gamma.events_keyset"
    assert "simulated_gamma_failure" in recorded_failures[0]["reason"]
