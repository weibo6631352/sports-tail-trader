from __future__ import annotations

import asyncio
from dataclasses import replace
from itertools import count

from polymarket_trader.domain.events import (
    OUTBOX_RAW_RESPONSE_SUMMARY_LIMIT,
    OutboxEvent,
    sanitize_raw_response,
)

DEFAULT_ENQUEUE_TIMEOUT = 0.01
DEFAULT_RAW_RESPONSE_SUMMARY_LIMIT = OUTBOX_RAW_RESPONSE_SUMMARY_LIMIT
RETAINED_OVERFLOW_REASON = "retained_low_priority_overflow"


class LocalOutbox:
    def __init__(
        self,
        max_size: int,
        *,
        enqueue_timeout: float = DEFAULT_ENQUEUE_TIMEOUT,
        retained_max_size: int | None = None,
    ) -> None:
        self._max_size = max_size
        self._retained_max_size = retained_max_size if retained_max_size is not None else max_size
        self._enqueue_timeout = enqueue_timeout
        self._sequence = count()
        self._ready: asyncio.PriorityQueue[tuple[int, int, str]] = asyncio.PriorityQueue(maxsize=max_size)
        self._events_by_id: dict[str, OutboxEvent] = {}
        self._queued_event_ids: set[str] = set()
        self._merge_index: dict[str, str] = {}
        self._retained: dict[str, OutboxEvent] = {}
        self._dead_letters: list[OutboxEvent] = []

    async def enqueue(self, event: OutboxEvent, *, timeout: float | None = None) -> bool:
        event = self._prepare_event(event)
        merge_key = event.merge_key

        if merge_key is not None:
            existing_id = self._merge_index.get(merge_key)
            if existing_id is not None:
                current = self._events_by_id.get(existing_id) or self._retained.get(existing_id)
                if current is not None:
                    merged = replace(
                        event,
                        event_id=current.event_id,
                        created_at=current.created_at,
                        retry_count=current.retry_count,
                        priority=min(current.priority, event.priority),
                    )
                    if current.event_id in self._queued_event_ids:
                        self._store_pending(merged)
                    else:
                        self._store_retained(merged)
                    return True

        if self._has_capacity():
            if await self._enqueue_ready(event, timeout=timeout):
                self._store_pending(event)
                return True

        # 先写 outbox 再异步落库；P0 线程只允许短超时等待，数据库或日志慢时不阻塞提交线程。
        # 关键交易事件不能丢，低优先级快照可以合并或延后，先进入保留区等待后续消费。
        self._store_retained(event)
        return True

    async def put(self, event: OutboxEvent) -> None:
        await self.enqueue(event)

    async def get(self) -> OutboxEvent:
        while True:
            await self._promote_retained_best_effort()
            _, _, event_id = await self._ready.get()
            event = self._events_by_id.get(event_id)
            if event is None:
                self._queued_event_ids.discard(event_id)
                continue
            self._queued_event_ids.discard(event_id)
            return event

    async def ack(self, event: str | OutboxEvent) -> None:
        event_id = self._event_id(event)
        pending = self._events_by_id.pop(event_id, None)
        self._queued_event_ids.discard(event_id)
        self._retained.pop(event_id, None)
        if pending is not None:
            merge_key = pending.merge_key
            if merge_key is not None and self._merge_index.get(merge_key) == event_id:
                self._merge_index.pop(merge_key, None)
        await self._promote_retained_best_effort()

    async def retry(self, event: str | OutboxEvent, *, last_error: str | None = None) -> OutboxEvent:
        event_id = self._event_id(event)
        current = self._events_by_id.get(event_id) or self._retained.get(event_id)
        if current is None:
            raise KeyError(f"unknown outbox event: {event_id}")
        retried = current.with_retry(last_error=last_error)
        if event_id in self._queued_event_ids:
            self._store_pending(retried)
        else:
            self._store_retained(retried)
        if event_id not in self._queued_event_ids:
            if await self._enqueue_ready(retried, timeout=self._enqueue_timeout):
                self._retained.pop(event_id, None)
            else:
                self._store_retained(retried)
        return retried

    async def dead_letter(self, event: str | OutboxEvent, *, last_error: str | None = None) -> OutboxEvent:
        event_id = self._event_id(event)
        current = self._events_by_id.pop(event_id, None) or self._retained.pop(event_id, None)
        if current is None:
            raise KeyError(f"unknown outbox event: {event_id}")
        dead_lettered = current.with_dead_letter(last_error=last_error)
        self._queued_event_ids.discard(event_id)
        merge_key = dead_lettered.merge_key
        if merge_key is not None and self._merge_index.get(merge_key) == event_id:
            self._merge_index.pop(merge_key, None)
        self._dead_letters.append(dead_lettered)
        return dead_lettered

    def get_dead_letters(self) -> tuple[OutboxEvent, ...]:
        return tuple(self._dead_letters)

    def pending_events(self) -> tuple[OutboxEvent, ...]:
        return tuple(
            sorted(
                self._events_by_id.values(),
                key=lambda event: (event.priority, event.created_at, event.event_id),
            )
        )

    def put_nowait(self, event: OutboxEvent) -> bool:
        """同步快路径的轻量入口；只是把事件放进 outbox，不做持久化。"""

        event = self._prepare_event(event)
        if event.merge_key is not None and event.merge_key in self._merge_index:
            existing_id = self._merge_index[event.merge_key]
            current = self._events_by_id.get(existing_id) or self._retained.get(existing_id)
            if current is not None:
                merged = replace(
                    event,
                    event_id=current.event_id,
                    created_at=current.created_at,
                    retry_count=current.retry_count,
                    priority=min(current.priority, event.priority),
                )
                if current.event_id in self._queued_event_ids:
                    self._store_pending(merged)
                else:
                    self._store_retained(merged)
                return True
        if self._has_capacity():
            try:
                self._ready.put_nowait((int(event.priority), next(self._sequence), event.event_id))
            except asyncio.QueueFull:
                pass
            else:
                self._store_pending(event)
                return True
        self._store_retained(event)
        return True

    def _prepare_event(self, event: OutboxEvent) -> OutboxEvent:
        if event.raw_response_summary is not None and len(event.raw_response_summary) > DEFAULT_RAW_RESPONSE_SUMMARY_LIMIT:
            return replace(
                event,
                raw_response_summary=sanitize_raw_response(
                    event.raw_response_summary,
                    max_length=DEFAULT_RAW_RESPONSE_SUMMARY_LIMIT,
                ),
            )
        return event

    def _event_id(self, event: str | OutboxEvent) -> str:
        return event.event_id if isinstance(event, OutboxEvent) else event

    def _has_capacity(self) -> bool:
        return self._ready.qsize() < self._max_size if self._max_size > 0 else True

    def _store_pending(self, event: OutboxEvent) -> None:
        self._events_by_id[event.event_id] = event
        self._retained.pop(event.event_id, None)
        merge_key = event.merge_key
        if merge_key is not None:
            self._merge_index[merge_key] = event.event_id

    def _store_retained(self, event: OutboxEvent) -> None:
        self._events_by_id[event.event_id] = event
        self._retained[event.event_id] = event
        merge_key = event.merge_key
        if merge_key is not None:
            self._merge_index[merge_key] = event.event_id
        self._enforce_retained_capacity()

    def _enforce_retained_capacity(self) -> None:
        if self._retained_max_size is None or self._retained_max_size <= 0:
            return
        while len(self._retained) > self._retained_max_size:
            evictable = [
                item
                for item in self._retained.items()
                if not item[1].is_critical and item[0] not in self._queued_event_ids
            ]
            if not evictable:
                return
            event_id, event = max(
                evictable,
                key=lambda item: (item[1].priority, item[1].created_at, item[0]),
            )
            self._retained.pop(event_id, None)
            self._events_by_id.pop(event_id, None)
            merge_key = event.merge_key
            if merge_key is not None and self._merge_index.get(merge_key) == event_id:
                self._merge_index.pop(merge_key, None)
            self._dead_letters.append(
                replace(event, last_error=event.last_error or RETAINED_OVERFLOW_REASON)
            )

    async def _enqueue_ready(self, event: OutboxEvent, *, timeout: float | None = None) -> bool:
        timeout = self._enqueue_timeout if timeout is None else timeout
        try:
            await asyncio.wait_for(
                self._ready.put((int(event.priority), next(self._sequence), event.event_id)),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            return False
        self._queued_event_ids.add(event.event_id)
        return True

    async def _promote_retained_best_effort(self) -> None:
        if not self._retained:
            return

        for event_id, event in sorted(
            self._retained.items(),
            key=lambda item: (item[1].priority, item[1].created_at, item[0]),
        ):
            if event_id in self._queued_event_ids:
                continue
            if await self._enqueue_ready(event, timeout=self._enqueue_timeout):
                self._retained.pop(event_id, None)
