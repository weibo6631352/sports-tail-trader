"""UserAccountPoller — 用户态独立后台轮询。

设计动机
========

之前 ``ReconcileAuthorityRefresher.refresh()`` 主循环内联调 ``refresh_account()``，
意味着每 20s reconcile tick 都要同步等：

  - ``data_client.list_positions()``       — Polymarket data API
  - ``clob_client.list_open_orders()``     — clob REST
  - ``clob_client.list_fills()``           — clob REST
  - ``clob_client.get_balance_allowance()`` — clob REST

虽然 4 个 fetch 走 ``asyncio.gather`` 并发，但 reconcile 主链路要等最慢的那个。
如果某个 API 抖动 500ms，整个 reconcile 周期也卡 500ms——而 reconcile 同时承担
gamma 元数据刷新 + outbox flush + state 提交等责任，不应被用户态网络抖动牵连。

把这 4 个 fetch 抽到独立 asyncio task：
  - reconcile 主循环纯做 market 元数据 / orderbook / fee_rate 刷新（已经走
    gamma_snapshot_store cache，~10ms 完成）
  - poller 自管 cadence（默认同 reconcile 的 20s），独立失败、独立重试
  - 两路通过 ``AccountStateStore`` 共享内存，没有 race（参照 paper_mode 同模式）

paper 模式
=========

paper 模式下 paper_balance_syncer 已经在每秒投影 paper_ledger 到 store，
是用户态唯一权威。这种情况下：
  - ``ReconcileAuthorityRefresher.refresh_account()`` 自身有 ``paper_mode`` 早退（已实现）
  - 所以即便 poller 在 paper 模式下调到 refresh_account 也 no-op 不写
  - main.py 在 paper 模式下可以选择不启 poller（更省），或启也无害（早退）
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_trader.workers.reconcile.authority_refresher import (
        ReconcileAuthorityRefresher,
    )

logger = logging.getLogger(__name__)


class UserAccountPoller:
    """周期调 ``refresher.refresh_account()``，让用户态拉取与 reconcile 主链路解耦。

    内部不直接持有 client——所有 fetch + 合并 + 落 store 的逻辑都在
    ``ReconcileAuthorityRefresher.refresh_account``。Poller 只负责调度。
    """

    def __init__(
        self,
        *,
        refresher: "ReconcileAuthorityRefresher",
        interval_s: float = 20.0,
        trace_id_prefix: str = "user-account-poll",
    ) -> None:
        if interval_s <= 0:
            raise ValueError("UserAccountPoller interval_s must be positive")
        self._refresher = refresher
        self._interval_s = interval_s
        self._trace_id_prefix = trace_id_prefix
        self._task: asyncio.Task[None] | None = None
        self._tick_count = 0
        self._last_failure: str | None = None

    @property
    def tick_count(self) -> int:
        return self._tick_count

    @property
    def last_failure(self) -> str | None:
        return self._last_failure

    def start(self) -> asyncio.Task[None]:
        """启动后台轮询任务。重复调用返回同一个 task（幂等）。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="user_account_poller")
        return self._task

    async def stop(self) -> None:
        """停止后台任务并等待退出。supervisor 关闭时调。"""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — shutdown 阶段允许吞
            pass

    async def run_once(self, *, trace_id: str | None = None) -> None:
        """单次拉取——测试用，也供调度器单次触发。"""
        tid = trace_id or f"{self._trace_id_prefix}-{self._tick_count}"
        try:
            await self._refresher.refresh_account(trace_id=tid, markets=())
            self._tick_count += 1
            self._last_failure = None
        except Exception as exc:  # noqa: BLE001
            self._last_failure = str(exc)
            logger.warning(
                "user_account_poller.tick_failed",
                extra={"trace_id": tid, "reason": str(exc)},
                exc_info=True,
            )

    async def _run(self) -> None:
        while True:
            await self.run_once()
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                raise
