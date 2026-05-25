"""Gamma 市场元数据快照 — 共享内存视图。

设计动机
========

Polymarket 的 ``/events?live=true`` 已经嵌入每个 event 下所有 markets 的完整
``GammaMarketDTO`` 字段（tick_size / fee / outcomes / closed / token_ids）。
``discovery_runner`` 每 0.5 秒拉一次 events，所以"当前 live 的市场"的元数据天然
是新鲜的——任何路径再去单调一次 ``/markets?slug=...`` 都是重复工作。

之前 ``reconcile authority_refresher`` 周期对每个 tracked market 串行调一次
``/markets`` 拉 fee/tick，~200 markets × 150ms = 30s 阻塞，全部可以省。

这个 store 把 ``GammaMarketDTO`` 按 ``condition_id`` 缓存进内存，由 discovery
单一 writer 写入，所有读取方（reconcile、settlement_scanner、enrich）共享。
``last_updated_at`` 让消费方可判 freshness——stale 数据应该 fallback 到
``GammaClient.get_market_by_condition_id`` 直查（settlement / 孤儿 condition 路径）。

并发模型
========

- 写入只在 discovery_runner 单一 asyncio task 内发生（每 0.5s 一轮）。
- 读取走 lock-free ``snapshot()`` —— CPython GIL 保证 dict 替换的原子性。
- copy-on-write：每次写都构造新 dict，原子 ref 替换；老 dict 不变。
- 跟 ``MarketRegistry`` 同一并发模式（lock-free read + commit_lock 串行化写）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_trader.infra.polymarket.schemas import GammaMarketDTO


@dataclass(frozen=True, slots=True)
class GammaSnapshotEntry:
    """单条市场快照——含 DTO 本体 + 写入时点。

    consumer 用 ``is_fresh(max_age_s)`` 判断要不要走 fallback 直查 gamma。
    """

    dto: "GammaMarketDTO"
    last_updated_at_mono: float  # time.monotonic() 时点

    def is_fresh(self, *, max_age_s: float) -> bool:
        return (time.monotonic() - self.last_updated_at_mono) <= max_age_s


class GammaMarketSnapshotStore:
    """condition_id → GammaSnapshotEntry 的 lock-free 读、CoW 写共享内存视图。"""

    def __init__(self) -> None:
        self._entries: dict[str, GammaSnapshotEntry] = {}
        self._commit_lock = Lock()

    def upsert(self, dto: "GammaMarketDTO") -> None:
        """从 events.markets 或 /markets 反查结果回写。``condition_id`` 缺失即忽略。"""
        condition_id = dto.condition_id
        if not condition_id:
            return
        entry = GammaSnapshotEntry(dto=dto, last_updated_at_mono=time.monotonic())
        with self._commit_lock:
            entries = dict(self._entries)
            entries[condition_id] = entry
            self._entries = entries

    def upsert_many(self, dtos: "tuple[GammaMarketDTO, ...] | list[GammaMarketDTO]") -> int:
        """批量写入（discovery 一轮多个 event 嵌套多个 market）。返回成功写入条数。"""
        if not dtos:
            return 0
        now = time.monotonic()
        new_entries: dict[str, GammaSnapshotEntry] = {}
        for dto in dtos:
            cid = dto.condition_id
            if not cid:
                continue
            new_entries[cid] = GammaSnapshotEntry(dto=dto, last_updated_at_mono=now)
        if not new_entries:
            return 0
        with self._commit_lock:
            entries = dict(self._entries)
            entries.update(new_entries)
            self._entries = entries
        return len(new_entries)

    def get(self, condition_id: str) -> GammaSnapshotEntry | None:
        """lock-free 读取——直接返回当前 dict 引用上的查找。"""
        return self._entries.get(condition_id)

    def get_dto_if_fresh(
        self, condition_id: str, *, max_age_s: float
    ) -> "GammaMarketDTO | None":
        """常用便捷方法：新鲜则返回 DTO，否则返回 None（让调用方走 fallback）。"""
        entry = self._entries.get(condition_id)
        if entry is None:
            return None
        if not entry.is_fresh(max_age_s=max_age_s):
            return None
        return entry.dto

    def prune(self, condition_id: str) -> None:
        """market lifecycle 结束时清理（registry prune callback 触发）。

        无对应条目时是 no-op；幂等。
        """
        with self._commit_lock:
            if condition_id not in self._entries:
                return
            entries = dict(self._entries)
            entries.pop(condition_id, None)
            self._entries = entries

    def size(self) -> int:
        """诊断用：当前 store 里有多少条目。lock-free。"""
        return len(self._entries)

    def condition_ids(self) -> tuple[str, ...]:
        """诊断用：当前所有 cached condition_id。lock-free。"""
        return tuple(self._entries.keys())
