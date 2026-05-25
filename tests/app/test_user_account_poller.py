"""UserAccountPoller 行为契约。

覆盖：
- ``run_once`` 调到 refresher.refresh_account 并递增 tick_count
- 异常被吞、记录 last_failure，不向上抛
- ``interval_s<=0`` 构造期 raise
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from polymarket_trader.app.user_account_poller import UserAccountPoller


class _StubRefresher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fail = fail

    async def refresh_account(self, *, trace_id: str, markets: tuple[Any, ...]) -> None:
        self.calls.append((trace_id, markets))
        if self._fail:
            raise RuntimeError("upstream down")


def test_constructor_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError):
        UserAccountPoller(refresher=_StubRefresher(), interval_s=0)
    with pytest.raises(ValueError):
        UserAccountPoller(refresher=_StubRefresher(), interval_s=-1.0)


def test_run_once_invokes_refresh_account_and_increments_tick() -> None:
    refresher = _StubRefresher()
    poller = UserAccountPoller(refresher=refresher, interval_s=10.0)
    asyncio.run(poller.run_once(trace_id="t1"))
    asyncio.run(poller.run_once(trace_id="t2"))
    assert poller.tick_count == 2
    assert poller.last_failure is None
    assert [trace for trace, _ in refresher.calls] == ["t1", "t2"]
    # markets 是空 tuple——poller 不关心市场范围，refresh_account 内部按账户态拉
    assert refresher.calls[0][1] == ()


def test_run_once_swallows_refresh_failure_and_records_reason() -> None:
    """单次失败不向上抛——主循环不能因为用户态拉一次失败就 stop。"""
    refresher = _StubRefresher(fail=True)
    poller = UserAccountPoller(refresher=refresher, interval_s=10.0)
    asyncio.run(poller.run_once(trace_id="boom"))
    assert poller.tick_count == 0  # 失败不计入
    assert poller.last_failure == "upstream down"
