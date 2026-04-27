# Runtime Performance Boundary Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正最新 review 的三处边界问题：交易 worker 不再静默丢盘口事件，市场发现恢复后台 SLA，`/runtime` 不再用 `markets` 字段表达样本数据。

**Architecture:** 性能控制放到队列/展示边界，不放到交易决策 worker 里削弱业务判断。交易盘口事件采用“同一 token 尚未消费事件只保留最新”的队列合并，保证 worker 处理的是最新事实；市场发现恢复扫描节奏；Admin runtime 显式命名样本字段，完整市场列表继续只由 `/markets` 提供。

**Tech Stack:** Python 3.13, asyncio, FastAPI, pytest, TypeScript, React, Vite.

---

## File Map

- Modify `src/polymarket_trader/runtime/event_bus.py`: 为 trading lane 增加基于 `merge_key` 的 pending-event 合并，只替换尚未消费的旧事件，不在 worker 内按时间丢事件。
- Modify `src/polymarket_trader/workers/trading_decision_worker.py`: 删除 `ORDERBOOK_DECISION_*` 节流常量、时间状态和 `_should_throttle_orderbook_decision`；保留账户/市场入场闸门。
- Modify `tests/workers/test_trading_decision_gate.py`: 删除“orderbook 事件会被 rate limit”的断言，新增连续 orderbook 事件都会进入决策 worker 的测试。
- Create `tests/runtime/test_event_bus_trading_coalescing.py`: 覆盖 trading lane merge-key 合并行为。
- Modify `src/polymarket_trader/runtime/discovery_runner.py`: 恢复全量发现默认扫描预算和 tick 周期。
- Create `tests/runtime/test_discovery_runner_budget.py`: 固化全量发现默认预算，防止再次把后台发现能力降成前台性能补丁。
- Modify `src/polymarket_trader/app/admin_runtime_view.py`: 将 runtime 样本字段从含混的 `markets` 改成 `market_sample` / `registry.market_sample`。
- Modify `frontend/src/core/api/types.ts`: 同步 runtime payload 类型。
- Modify docs: `docs/api.md`, `docs/设计文档.md`, `docs/开发进度.md`，说明 `/runtime` 是轻量样本，完整列表用 `/markets`。

---

### Task 1: Move Orderbook Load Control To EventBus Coalescing

**Files:**
- Modify: `src/polymarket_trader/runtime/event_bus.py`
- Modify: `src/polymarket_trader/workers/trading_decision_worker.py`
- Modify: `tests/workers/test_trading_decision_gate.py`
- Create: `tests/runtime/test_event_bus_trading_coalescing.py`

- [ ] **Step 1: Write failing EventBus coalescing tests**

Create `tests/runtime/test_event_bus_trading_coalescing.py`:

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polymarket_trader.domain.events import OutboxPriority
from polymarket_trader.runtime.event_bus import EventBus


def _event(event_id: str, *, merge_key: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        trace_id=f"trace-{event_id}",
        event_id=event_id,
        event_type="orderbook_snapshot_updated",
        merge_key=merge_key,
    )


def test_trading_lane_keeps_latest_unconsumed_event_per_merge_key() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(OutboxPriority.P1, _event("old", merge_key="orderbook|token-1"))
        await bus.publish(OutboxPriority.P1, _event("new", merge_key="orderbook|token-1"))

        event = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)

        assert event.event_id == "new"
        assert bus.trading_queue_depth() == 0

    asyncio.run(run())


def test_trading_lane_does_not_coalesce_events_without_merge_key() -> None:
    async def run() -> None:
        bus = EventBus(trading_capacity=10)
        await bus.publish(OutboxPriority.P1, _event("first"))
        await bus.publish(OutboxPriority.P1, _event("second"))

        first = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)
        second = await asyncio.wait_for(bus.next_trading_event(), timeout=0.1)

        assert first.event_id == "first"
        assert second.event_id == "second"

    asyncio.run(run())
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
pytest -q tests/runtime/test_event_bus_trading_coalescing.py
```

Expected now: first test fails because `next_trading_event()` returns `old`.

- [ ] **Step 3: Implement trading-lane pending merge in EventBus**

In `src/polymarket_trader/runtime/event_bus.py`:

- Add fields in `EventBus.__init__`:

```python
self._trading_pending_events: dict[str, Any] = {}
self._trading_queued_keys: set[str] = set()
```

- Add helper:

```python
def _trading_event_key(self, event: Any) -> str:
    return _event_identity(event)
```

- Replace the `lane == QueueLane.TRADING` branch in `publish()` with:

```python
if lane == QueueLane.TRADING:
    key = self._trading_event_key(event)
    if key in self._trading_pending_events:
        self._trading_pending_events[key] = event
        self._mirror_to_outbox(outbox_priority, event)
        self._wake.set()
        return
    await self._trading_queue.put((next(self._sequence), key))
    self._trading_pending_events[key] = event
    self._trading_queued_keys.add(key)
    self._mirror_to_outbox(outbox_priority, event)
    self._wake.set()
    return
```

- Replace `next_trading_event()` with a loop that resolves keys:

```python
async def next_trading_event(self) -> Any:
    while True:
        _, key = await self._trading_queue.get()
        self._trading_queued_keys.discard(key)
        event = self._trading_pending_events.pop(key, None)
        if event is None:
            continue
        await self._flush_retained_best_effort()
        return event
```

- Update `trading_queue_depth()` to return live pending count:

```python
def trading_queue_depth(self) -> int:
    return len(self._trading_pending_events)
```

- [ ] **Step 4: Remove decision-worker time throttling**

In `src/polymarket_trader/workers/trading_decision_worker.py`:

- Remove `from time import monotonic`.
- Remove `ORDERBOOK_DECISION_GLOBAL_MIN_INTERVAL_S` and `ORDERBOOK_DECISION_TOKEN_MIN_INTERVAL_S`.
- Remove `_last_orderbook_decision_at` and `_last_orderbook_decision_by_key`.
- Remove this block from `_handle_orderbook_snapshot_updated()`:

```python
if self._should_throttle_orderbook_decision(event):
    return None
```

- Delete `_should_throttle_orderbook_decision()`.

- [ ] **Step 5: Update worker tests**

In `tests/workers/test_trading_decision_gate.py`, replace `test_orderbook_events_are_rate_limited_before_sizing` with:

```python
def test_orderbook_events_are_not_dropped_by_decision_worker() -> None:
    async def run() -> None:
        account_state = _open_entry_gate()
        decision_service = _CountingDecisionService()
        worker = TradingDecisionWorker(
            trading_decision_service=decision_service,
            account_state_store=account_state,
        )

        await worker.process_event(_orderbook_event(event_id="event-orderbook-1"))
        await worker.process_event(_orderbook_event(event_id="event-orderbook-2"))

        assert decision_service.calls == 2

    asyncio.run(run())
```

- [ ] **Step 6: Verify Task 1**

Run:

```bash
pytest -q tests/runtime/test_event_bus_trading_coalescing.py tests/workers/test_trading_decision_gate.py
```

Expected: all selected tests pass.

---

### Task 2: Restore Market Discovery SLA

**Files:**
- Modify: `src/polymarket_trader/runtime/discovery_runner.py`
- Create: `tests/runtime/test_discovery_runner_budget.py`
- Modify: `docs/市场发现链路.md`

- [ ] **Step 1: Add a regression test for discovery defaults**

Create `tests/runtime/test_discovery_runner_budget.py`:

```python
from __future__ import annotations

from polymarket_trader.runtime import discovery_runner


def test_full_market_discovery_defaults_keep_background_sla() -> None:
    assert discovery_runner._MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK == 2
    assert discovery_runner._MARKET_DISCOVERY_MAX_RUNTIME_MS == 200.0
    assert discovery_runner.MARKET_DISCOVERY_TICK_SECONDS == 0.5
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest -q tests/runtime/test_discovery_runner_budget.py
```

Expected now: fails because current defaults are `1`, `100.0`, `5.0`.

- [ ] **Step 3: Restore discovery defaults**

In `src/polymarket_trader/runtime/discovery_runner.py`, set:

```python
_MARKET_DISCOVERY_REQUEST_BUDGET_PER_TICK = 2
_MARKET_DISCOVERY_MAX_RUNTIME_MS = 200.0
MARKET_DISCOVERY_TICK_SECONDS = 0.5
```

Do not add frontend-driven switches or disable background scanning.

- [ ] **Step 4: Update discovery docs**

In `docs/市场发现链路.md`, add one short rule:

```markdown
- 全量发现是后台业务能力，不因前台页面展示降频；若需要降载，应通过队列合并、协作式让出事件循环或独立 backpressure 处理，不能静态降低发现 SLA。
```

- [ ] **Step 5: Verify Task 2**

Run:

```bash
pytest -q tests/runtime/test_discovery_runner_budget.py
```

Expected: pass.

---

### Task 3: Rename Runtime Market Sample Fields

**Files:**
- Modify: `src/polymarket_trader/app/admin_runtime_view.py`
- Modify: `frontend/src/core/api/types.ts`
- Modify: `docs/api.md`
- Modify: `docs/设计文档.md`
- Modify: `docs/开发进度.md`
- Create: `tests/app/test_admin_runtime_view.py`

- [ ] **Step 1: Add runtime payload regression test**

Create `tests/app/test_admin_runtime_view.py`:

```python
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from polymarket_trader.app.admin_runtime_view import AdminRuntimeView
from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.runtime.registry import MarketRegistry


def _market(index: int) -> Market:
    return Market(
        condition_id=f"condition-{index}",
        market_slug=f"market-{index}",
        outcomes=(MarketOutcome(token_id=f"token-{index}", outcome="YES"),),
    )


def test_runtime_snapshot_uses_sample_field_names_for_markets() -> None:
    async def run() -> dict[str, object]:
        registry = MarketRegistry()
        for index in range(25):
            registry.upsert(_market(index))
        runtime = SimpleNamespace(
            registry=registry,
            supervisor=None,
            settings=SimpleNamespace(),
            account_state_store=None,
            event_bus=None,
            persistence_worker=None,
            market_discovery_scan=None,
            sports_live_state_worker=None,
        )
        return await AdminRuntimeView(runtime=runtime).runtime_snapshot()

    payload = asyncio.run(run())

    assert "markets" not in payload
    assert "market_sample" in payload
    registry_payload = payload["registry"]
    assert isinstance(registry_payload, dict)
    assert "markets" not in registry_payload
    assert len(registry_payload["market_sample"]) == 20
    assert registry_payload["market_count"] == 25
    assert registry_payload["markets_truncated"] is True
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```bash
pytest -q tests/app/test_admin_runtime_view.py
```

Expected now: fails because payload still has top-level `markets` and `registry.markets`.

- [ ] **Step 3: Rename backend payload fields**

In `src/polymarket_trader/app/admin_runtime_view.py`, change the runtime payload:

```python
"registry": {
    "market_count": len(registry.markets),
    "market_sample": [jsonable(market) for market in market_sample],
    "market_sample_limit": _RUNTIME_MARKET_SAMPLE_LIMIT,
    "markets_truncated": markets_truncated,
},
...
"market_sample": market_sample,
```

Remove the top-level `"markets": market_sample` entry and remove `registry["markets"]`.

- [ ] **Step 4: Update frontend types**

In `frontend/src/core/api/types.ts`, update `RuntimePayload`:

```ts
registry: {
  market_count: number
  market_sample: JsonValue[]
  market_sample_limit?: number
  markets_truncated?: boolean
}
market_sample: MarketView[]
```

Remove `markets: MarketView[]`.

- [ ] **Step 5: Update API docs**

In `docs/api.md`, document:

```markdown
- `/runtime` 只返回 `market_sample` / `registry.market_sample` 作为轻量运行时样本；`registry.market_count` 表示全量计数。
- 完整市场分页只能使用 `/markets`，不能从 `/runtime` 推断全量市场列表。
```

In `docs/设计文档.md`, document the same boundary under Admin/runtime query responsibilities.

In `docs/开发进度.md`, add one progress note that runtime sample naming has been corrected.

- [ ] **Step 6: Verify Task 3**

Run:

```bash
pytest -q tests/app/test_admin_runtime_view.py
npm --prefix frontend run typecheck
```

Expected: both pass.

---

### Task 4: Full Verification And Commit

**Files:**
- All files touched above.

- [ ] **Step 1: Run backend verification**

Run:

```bash
pytest -q
ruff check .
```

Expected: `pytest` passes all tests; `ruff` reports `All checks passed!`.

- [ ] **Step 2: Run frontend verification**

Run:

```bash
npm --prefix frontend run typecheck
npm --prefix frontend run lint
VITE_API_BASE_URL=/api npm --prefix frontend run build
```

Expected: all commands exit 0.

- [ ] **Step 3: Browser smoke**

Use the running app at `http://127.0.0.1:5173` and verify:

- `/markets` still reads full paginated list from `/api/markets`.
- `/runtime` response contains `market_sample`, not top-level `markets`.
- No console errors.
- `registry.market_count` remains full count.

- [ ] **Step 4: Commit and push**

Run:

```bash
git status --short
git add src tests frontend docs
git commit -m "fix: keep runtime performance boundaries explicit"
git push
```

Expected: working tree clean after push.

---

## Execution Notes

- These tasks can be parallelized:
  - Worker A owns Task 1 (`event_bus.py`, `trading_decision_worker.py`, related tests).
  - Worker B owns Task 2 (`discovery_runner.py`, discovery test/docs).
  - Worker C owns Task 3 (`admin_runtime_view.py`, frontend type/docs/test).
- Workers are not alone in the codebase. They must not revert other workers' edits and must keep write scopes disjoint.
- Integration owner runs Task 4 after all task branches are merged into the main workspace.

## Self-Review

- Spec coverage: addresses all three review findings.
- Placeholder scan: no deferred implementation placeholders.
- Type consistency: `market_sample` naming is consistent across backend, frontend, and docs.
