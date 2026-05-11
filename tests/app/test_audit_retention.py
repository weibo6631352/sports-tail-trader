"""audit_events 保留期清理单元测试。"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from polymarket_trader.app.audit_retention import purge_audit_events_once
from polymarket_trader.serialization import utc_now


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeSession:
    def __init__(self, batch_returns: list[int]) -> None:
        self._batch_returns = list(batch_returns)
        self.executed: list[dict[str, Any]] = []
        self.commits = 0

    async def execute(self, _stmt: Any, params: dict[str, Any]) -> _FakeResult:
        self.executed.append(params)
        rowcount = self._batch_returns.pop(0) if self._batch_returns else 0
        return _FakeResult(rowcount)

    async def commit(self) -> None:
        self.commits += 1


class _FakeSessionFactory:
    """模拟 async_sessionmaker 的 ``__call__`` 返回 async context manager 行为。"""

    def __init__(self, batch_returns: list[int]) -> None:
        self.session = _FakeSession(batch_returns)

    def __call__(self) -> "_FakeSessionFactory":  # type: ignore[override]
        return self

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_purge_audit_events_deletes_rows_in_batches() -> None:
    """两批 10k + 一批 3k = 23k 行被删；最后一批不足 batch_size 时退出循环。"""

    factory = _FakeSessionFactory(batch_returns=[10_000, 10_000, 3_000])
    summary = await purge_audit_events_once(
        factory,  # type: ignore[arg-type]
        retention_days=14,
        batch_size=10_000,
    )
    assert summary["deleted_rows"] == 23_000
    assert summary["batches"] == 3
    assert summary["error"] is None
    assert summary["skipped"] is False
    # cutoff 应该是 now - retention_days
    assert summary["cutoff"] is not None
    # 每次 commit 都跟着一次 execute
    assert factory.session.commits == 3
    assert len(factory.session.executed) == 3
    # 每次都用相同 batch_size 参数
    assert all(call["batch_size"] == 10_000 for call in factory.session.executed)


@pytest.mark.asyncio
async def test_purge_audit_events_retention_zero_skips() -> None:
    """retention_days=0 直接跳过（运维显式禁用 retention 语义）。"""

    factory = _FakeSessionFactory(batch_returns=[])
    summary = await purge_audit_events_once(
        factory,  # type: ignore[arg-type]
        retention_days=0,
        batch_size=10_000,
    )
    assert summary["skipped"] is True
    assert summary["deleted_rows"] == 0
    assert summary["batches"] == 0
    assert summary["error"] is None
    # 跳过路径不应当真去 DELETE
    assert factory.session.commits == 0


@pytest.mark.asyncio
async def test_purge_audit_events_exits_on_short_batch() -> None:
    """单批返回少于 batch_size → 视为删干净，退出循环。"""

    factory = _FakeSessionFactory(batch_returns=[42])
    summary = await purge_audit_events_once(
        factory,  # type: ignore[arg-type]
        retention_days=14,
        batch_size=10_000,
    )
    assert summary["deleted_rows"] == 42
    assert summary["batches"] == 1


@pytest.mark.asyncio
async def test_purge_audit_events_exception_captured_not_raised() -> None:
    """DB 抛错时 summary.error 含原因，但 retention job 不向外抛（§7 后台不阻塞 P0）。"""

    class _FailingSession(_FakeSession):
        async def execute(self, _stmt: Any, _params: dict[str, Any]) -> _FakeResult:
            raise RuntimeError("simulated db failure")

    factory = _FakeSessionFactory(batch_returns=[])
    factory.session = _FailingSession([])
    summary = await purge_audit_events_once(
        factory,  # type: ignore[arg-type]
        retention_days=14,
        batch_size=10_000,
    )
    assert summary["error"] == "simulated db failure"
    assert summary["deleted_rows"] == 0


@pytest.mark.asyncio
async def test_purge_audit_events_cutoff_uses_retention_days() -> None:
    """cutoff 时间应等于 (started_at - retention_days)，让 DELETE 精确删超期数据。"""

    factory = _FakeSessionFactory(batch_returns=[0])
    before = utc_now()
    summary = await purge_audit_events_once(
        factory,  # type: ignore[arg-type]
        retention_days=7,
        batch_size=100,
    )
    # cutoff 应在 (before - 7 days) 附近，宽容 ±10s 误差
    cutoff_iso = summary["cutoff"]
    assert cutoff_iso is not None
    from datetime import datetime
    cutoff = datetime.fromisoformat(cutoff_iso)
    expected = before - timedelta(days=7)
    assert abs((cutoff - expected).total_seconds()) < 10
