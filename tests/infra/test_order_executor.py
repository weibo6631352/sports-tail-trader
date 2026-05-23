"""PolymarketOrderExecutor 骨架契约测试。

OrderExecutor 是 CLAUDE.md §3 唯一允许下单/签名/取消/替换 Polymarket 订单的模块。
此前只有 test_order_persistence_keys.py 间接覆盖 OrderModel 映射，OrderExecutor 公共
接口本身（submit / cancel / replace / 幂等 / lifecycle outbox）无直接行为测试。

本文件覆盖：
1. 主路径：submit BUY/SELL、cancel、replace 各自返回符合预期 OrderResultStatus
2. 幂等：同 idempotency_key 二次 submit 复用缓存，不再调 client
3. lifecycle：成功 / 失败均产生 outbox event
4. client 抛错 → ERROR 结果（不再 raise 出 executor 边界）
5. 类型保护：submit 不接 Cancel/Replace intent
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from polymarket_trader.domain.events import OutboxEvent
from polymarket_trader.domain.order import (
    BuyOrderIntent,
    CancelOrderIntent,
    OrderResultStatus,
    OrderType,
    ReplaceOrderIntent,
    SellOrderIntent,
)
from polymarket_trader.infra.polymarket.order_executor import (
    InMemoryPolymarketOrderClient,
    PolymarketOrderExecutor,
)
from polymarket_trader.infra.polymarket.order_execution_types import (
    OrderExecutionRequest,
    OrderExecutionResponse,
)


class _CollectingOutboxSink:
    """收集 put_nowait 调用的 outbox stub；put_nowait 必须返回 bool。"""

    def __init__(self) -> None:
        self.events: list[OutboxEvent] = []

    def put_nowait(self, event: OutboxEvent) -> bool:
        self.events.append(event)
        return True


def _build_buy_intent(
    *,
    trace_id: str = "trace-1",
    idempotency_key: str | None = None,
    order_type: OrderType = OrderType.GTC,
) -> BuyOrderIntent:
    return BuyOrderIntent(
        strategy_id="sports_tail",
        trace_id=trace_id,
        condition_id="cond-1",
        token_id="tok-1",
        price=Decimal("0.72"),
        amount_usdc=Decimal("5"),
        market_slug="slug-1",
        order_type=order_type,
        idempotency_key=idempotency_key,
    )


def _build_sell_intent(
    *,
    trace_id: str = "trace-sell",
    idempotency_key: str | None = None,
) -> SellOrderIntent:
    return SellOrderIntent(
        strategy_id="sports_tail",
        trace_id=trace_id,
        condition_id="cond-1",
        token_id="tok-1",
        price=Decimal("0.85"),
        size_shares=Decimal("6"),
        market_slug="slug-1",
        order_type=OrderType.GTC,
        idempotency_key=idempotency_key,
    )


def _run(coro):
    return asyncio.run(coro)


def _settle_pending_tasks() -> None:
    """让 fire-and-forget lifecycle task 跑完——asyncio.create_task 后必须 yield 一次。"""
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))


# ============================================================
# 主路径：submit / cancel / replace 三类 intent 的成功返回
# ============================================================

def test_submit_buy_returns_live_result() -> None:
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        async def run() -> None:
            result = await executor.submit(_build_buy_intent())
            await asyncio.sleep(0)  # 让 lifecycle fire-and-forget task 跑完
            assert result.status == OrderResultStatus.LIVE
            assert result.side.value == "BUY"
            assert result.order_id and result.order_id.startswith("fake-")
            assert result.trace_id == "trace-1"

        _run(run())
    finally:
        executor.close()


def test_submit_buy_fak_no_fill() -> None:
    """InMemoryPolymarketOrderClient 对 FAK BUY 默认返回 NO_FILL——验证状态映射。"""
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        async def run() -> None:
            result = await executor.submit(_build_buy_intent(order_type=OrderType.FAK))
            await asyncio.sleep(0)
            assert result.status == OrderResultStatus.NO_FILL
            assert result.reason == "simulated_fak_no_fill"

        _run(run())
    finally:
        executor.close()


def test_submit_sell_returns_live_result() -> None:
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        async def run() -> None:
            result = await executor.submit(_build_sell_intent())
            await asyncio.sleep(0)
            assert result.status == OrderResultStatus.LIVE
            assert result.side.value == "SELL"

        _run(run())
    finally:
        executor.close()


def test_cancel_returns_cancelled_result() -> None:
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        intent = CancelOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-cancel",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="exchange-order-id-1",
            reason="user_requested",
        )

        async def run() -> None:
            result = await executor.cancel(intent)
            await asyncio.sleep(0)
            assert result.status == OrderResultStatus.CANCELLED
            assert result.order_id == "exchange-order-id-1"

        _run(run())
    finally:
        executor.close()


def test_replace_returns_live_result() -> None:
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        intent = ReplaceOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-replace",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="exchange-order-id-2",
            new_price=Decimal("0.80"),
            size_shares=Decimal("3"),
            reason="adjust_price",
        )

        async def run() -> None:
            result = await executor.replace(intent)
            await asyncio.sleep(0)
            assert result.status == OrderResultStatus.LIVE
            assert result.reason == "replaced"

        _run(run())
    finally:
        executor.close()


# ============================================================
# 幂等：同 idempotency_key 重入应复用结果，不再调 client
# ============================================================

def test_same_idempotency_key_reuses_cached_result() -> None:
    """关键不变量：CLAUDE.md §7 要求 outbox + 幂等键防双发。
    同 (trace_id, condition_id, token_id, side, ...) 派生同 idempotency_key
    → 第二次 submit 必须复用第一次的 OrderResult，不能再调 client。"""
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        # 显式提供同一 idempotency_key 让两次 submit 派生同 key
        intent_a = _build_buy_intent(idempotency_key="dup-key-1")
        intent_b = _build_buy_intent(idempotency_key="dup-key-1")

        async def run() -> tuple:
            r1 = await executor.submit(intent_a)
            r2 = await executor.submit(intent_b)
            await asyncio.sleep(0)
            return r1, r2

        r1, r2 = _run(run())
        # 两次状态一致——第二次走幂等缓存
        assert r1.status == r2.status == OrderResultStatus.LIVE
        # 客户端只接收一次真实 submit；submit 一次共产生 1 个 client request
        # （sign 路径在 InMemory 不走，submit_order 是唯一入口）
        submit_calls = [r for kind, r in client.requests if kind == "submit"]
        assert len(submit_calls) == 1, f"expected 1 submit but got {len(submit_calls)}"
    finally:
        executor.close()


# ============================================================
# lifecycle：成功路径产生 outbox event
# ============================================================

def test_submit_publishes_lifecycle_to_outbox() -> None:
    """submit 后必须向 OutboxSink 投递 order_state_updated（或同义）事件。
    CLAUDE.md §7 要求 P0 路径只 put_nowait，不 await 持久化。"""
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        async def run() -> None:
            await executor.submit(_build_buy_intent())
            # lifecycle 用 asyncio.create_task fire-and-forget；显式等所有 pending task
            # 跑完，避免依赖固定 sleep 时长。
            pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        _run(run())
        assert len(sink.events) >= 1, "outbox 应至少收到一条 lifecycle event"
        evt = sink.events[0]
        assert evt.trace_id == "trace-1"
        assert evt.event_type  # 非空字符串
    finally:
        executor.close()


# ============================================================
# 错误兜底：client 抛错时返回 ERROR 结果，不让异常逃出 executor
# ============================================================

def test_client_exception_returns_failed_result() -> None:
    """client.submit_order 抛错 → executor 返回 FAILED 状态的 OrderResult，
    不把异常传出去——否则上游 TradingService 会崩。"""

    async def failing_submit(_request: OrderExecutionRequest) -> OrderExecutionResponse:
        raise RuntimeError("simulated_network_failure")

    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient(submit_handler=failing_submit)
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        async def run() -> None:
            result = await executor.submit(_build_buy_intent())
            await asyncio.sleep(0)
            # client 异常应映射到失败态，不让 RuntimeError 逃出 executor
            assert result.status in {
                OrderResultStatus.FAILED,
                OrderResultStatus.REJECTED,
                OrderResultStatus.UNKNOWN_TIMEOUT,
            }

        _run(run())
    finally:
        executor.close()


# ============================================================
# 类型保护：submit 不接 Cancel/Replace intent
# ============================================================

def test_submit_rejects_cancel_intent_with_type_error() -> None:
    """submit 接口契约：只接 BuyOrderIntent / SellOrderIntent。
    误把 CancelOrderIntent 传给 submit 必须立刻抛 TypeError，避免后续误下单。"""
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        cancel_intent = CancelOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-typo",
            condition_id="cond-1",
            token_id="tok-1",
            order_id="exchange-order-id-3",
        )

        async def run() -> None:
            with pytest.raises(TypeError):
                await executor.submit(cancel_intent)  # type: ignore[arg-type]

        _run(run())
    finally:
        executor.close()


def test_concurrent_submit_same_idempotency_key_deduplicates() -> None:
    """并发同 idempotency_key → 只有一次真实 client.submit；两个协程都拿到同一结果。

    验证幂等锁在并发路径（asyncio.gather）下的正确性：
    CLAUDE.md §7 「幂等键防双发」不仅对顺序调用成立，也对并发调用成立。
    """
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink)
    try:
        intent_a = _build_buy_intent(idempotency_key="concurrent-key-1")
        intent_b = _build_buy_intent(idempotency_key="concurrent-key-1")

        async def run() -> tuple:
            r1, r2 = await asyncio.gather(
                executor.submit(intent_a),
                executor.submit(intent_b),
            )
            await asyncio.sleep(0)
            return r1, r2

        r1, r2 = _run(run())
        assert r1.status == r2.status
        submit_calls = [r for kind, r in client.requests if kind == "submit"]
        assert len(submit_calls) == 1, (
            f"concurrent同 key 应只触发 1 次 client.submit，实际 {len(submit_calls)}"
        )
    finally:
        executor.close()


# ============================================================
# P0 latency gauges：supervisor load-shedding 依赖三个 gauge 持续更新；
# 任何一个缺失/全 0 → 削载信号死路一条。
# ============================================================


def _gauge_value(metrics, name: str) -> float | None:
    snap = metrics.snapshot()
    for gauge in snap.gauges:
        if gauge.name == name:
            return gauge.value
    return None


def test_metrics_none_does_not_break_executor() -> None:
    """metrics=None（测试场景默认）必须正常下单——发布到 gauge 是可选副作用。"""
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink, metrics=None)
    try:
        async def run() -> None:
            result = await executor.submit(_build_buy_intent())
            await asyncio.sleep(0)
            assert result.status == OrderResultStatus.LIVE

        _run(run())
    finally:
        executor.close()


def test_trading_lock_wait_gauge_published() -> None:
    """_acquire_lock 必须发布 trading_lock_wait_ms gauge（即使 wait ≈ 0 也要更新到非 None）。"""
    from polymarket_trader.observability.metrics import MetricsRegistry

    metrics = MetricsRegistry()
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink, metrics=metrics)
    try:
        async def run() -> None:
            await executor.submit(_build_buy_intent())
            await asyncio.sleep(0)

        _run(run())
        value = _gauge_value(metrics, "trading_lock_wait_ms")
        assert value is not None, "trading_lock_wait_ms 应被发布"
        assert value >= 0.0
    finally:
        executor.close()


def test_executor_queue_wait_gauge_published() -> None:
    """submit_started_at - queued_at 必须发布到 executor_queue_wait_ms。"""
    from polymarket_trader.observability.metrics import MetricsRegistry

    metrics = MetricsRegistry()
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink, metrics=metrics)
    try:
        async def run() -> None:
            await executor.submit(_build_buy_intent())
            await asyncio.sleep(0)

        _run(run())
        value = _gauge_value(metrics, "executor_queue_wait_ms")
        assert value is not None, "executor_queue_wait_ms 应被发布"
        assert value >= 0.0
    finally:
        executor.close()


def test_entry_signal_to_submit_gauge_reflects_signal_at() -> None:
    """intent.metadata['signal_at'] 存在时，executor 必须发布 entry_signal_to_submit_ms。

    显式构造一个 100ms 前的 signal_at，验证 gauge 至少 ≥ 100ms。"""
    from datetime import timedelta
    from polymarket_trader.observability.metrics import MetricsRegistry
    from polymarket_trader.serialization import utc_now

    metrics = MetricsRegistry()
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink, metrics=metrics)
    try:
        signal_at = utc_now() - timedelta(milliseconds=100)
        intent = BuyOrderIntent(
            strategy_id="sports_tail",
            trace_id="trace-signal",
            condition_id="cond-1",
            token_id="tok-1",
            price=Decimal("0.72"),
            amount_usdc=Decimal("5"),
            market_slug="slug-1",
            order_type=OrderType.GTC,
            metadata={"signal_at": signal_at},
        )

        async def run() -> None:
            await executor.submit(intent)
            await asyncio.sleep(0)

        _run(run())
        value = _gauge_value(metrics, "entry_signal_to_submit_ms")
        assert value is not None, "entry_signal_to_submit_ms 应被发布"
        assert value >= 100.0, f"signal_at 在 100ms 前，gauge 应 ≥ 100ms，实际 {value}"
    finally:
        executor.close()


def test_entry_signal_to_submit_skipped_when_no_signal_at() -> None:
    """没有 signal_at 时不应发布 entry_signal_to_submit_ms（保留旧值不污染信号）。"""
    from polymarket_trader.observability.metrics import MetricsRegistry

    metrics = MetricsRegistry()
    sink = _CollectingOutboxSink()
    client = InMemoryPolymarketOrderClient()
    executor = PolymarketOrderExecutor(client=client, outbox=sink, metrics=metrics)
    try:
        async def run() -> None:
            await executor.submit(_build_buy_intent())  # metadata 为空
            await asyncio.sleep(0)

        _run(run())
        # gauge 不应被更新（snapshot 中不含该 metric）
        value = _gauge_value(metrics, "entry_signal_to_submit_ms")
        assert value is None, f"无 signal_at 时不应发布，得到 {value}"
    finally:
        executor.close()
