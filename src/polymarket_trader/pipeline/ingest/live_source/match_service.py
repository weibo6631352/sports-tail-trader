"""LiveStateMatchService —— 编排 matcher + calibrator，写 metadata store + 发 signal。

订阅 `LiveStateStore` 的 refresh listener，bucket 更新时：

1. 列出该 source 的所有 subscriber market（从 `LiveSourceRegistry`）
2. 对每个 market 跑 `matcher.match()` → 拿到候选 LiveStateMatch
3. matcher 返回 → `calibrator.calibrate()` 三角验证
4. 校准通过 → 写 `MarketMetadataStore` + 若 `signal_allowed` 则 publish
   `ENTRY_SIGNAL_TRIGGERED` 给决策层
5. 校准失败 → **不写 metadata store**，落 `SPORTS_LIVE_MATCH_GAP_RECORDED` audit

# 同步执行

`refresh_listener` 在 `LiveStateStore.update()` 内同步 fan-out（feeder 主循环上）。
service 内的 matcher / calibrator 是 CPU-bound 快速操作（每 market 几 ms），可
接受。如果未来 subscriber 数变大（>1000）成为瓶颈，可改成 `asyncio.Queue` 解耦。

# 与 原架构方案 §3 一致

ENTRY_SIGNAL_TRIGGERED 是 P1，对应"层 2 决策响应"的触发源之一（与
ORDERBOOK_SNAPSHOT_UPDATED 并列）。MarketTickWorker 订阅它后跑 quant_decide。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from polymarket_trader.domain.events import DomainEvent, DomainEventType, OutboxPriority
from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveStateMatch

from .calibrator import CalibrationResult, LiveSourceCalibrator
from .matcher import LiveSourceMatcher
from .registry import LiveSourceRegistry
from .source import LiveSourceKey
from .store import LiveStateStore

if TYPE_CHECKING:
    from polymarket_trader.runtime.event_bus import EventBus
    from polymarket_trader.runtime.market_metadata import MarketMetadataStore
    from polymarket_trader.runtime.registry import MarketRegistry

logger = logging.getLogger(__name__)


class LiveStateMatchService:
    def __init__(
        self,
        *,
        store: LiveStateStore,
        registry: LiveSourceRegistry,
        market_registry: "MarketRegistry",
        market_metadata_store: "MarketMetadataStore",
        matcher: LiveSourceMatcher,
        calibrator: LiveSourceCalibrator,
        event_bus: "EventBus",
    ) -> None:
        self._store = store
        self._registry = registry
        self._market_registry = market_registry
        self._metadata = market_metadata_store
        self._matcher = matcher
        self._calibrator = calibrator
        self._event_bus = event_bus

    def attach(self) -> None:
        """注册到 `LiveStateStore` 的 refresh listener，启动后即开始监听。"""

        self._store.register_refresh_listener(self._on_source_refreshed)

    def _on_source_refreshed(self, source: LiveSourceKey) -> None:
        events = self._store.events_for(source)
        if not events:
            # 空桶不触发 match（feeder 的 SUCCESS_EMPTY / FAILED 状态由 health 监控反映）
            return
        for market_key in self._registry.subscribers_for(source):
            market = self._market_registry.get_by_condition_id(market_key)
            if market is None:
                continue
            try:
                self._process_market(market, source)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "match_service _process_market failed market=%s source=%s",
                    market_key,
                    source.as_label(),
                )

    def _process_market(self, market: Market, source: LiveSourceKey) -> None:
        events = self._store.events_for(source)
        match = self._matcher.match(market, events)
        if match is None:
            return
        calibration = self._calibrator.calibrate(
            market=market,
            matched_event=match.event,
            source=source,
        )
        if not calibration.accepted:
            self._record_calibration_failure(market, source, match, calibration)
            return
        self._apply_match(market, source, match, calibration)

    def _apply_match(
        self,
        market: Market,
        source: LiveSourceKey,
        match: LiveStateMatch,
        calibration: CalibrationResult,
    ) -> None:
        self._metadata.upsert(
            metadata=match.payload,
            condition_id=market.condition_id,
            market_slug=market.market_slug,
            event_slug=market.event_slug,
            source=source.as_label(),
            live_state_signal_allowed=match.signal_allowed,
            live_state_signal_reason=match.signal_reason,
            live_state_phase=match.phase,
            live_state_payload=match.payload,
        )
        if not match.signal_allowed:
            return
        for outcome in market.outcomes:
            self._event_bus.publish_nowait(
                OutboxPriority.P1,
                DomainEvent(
                    trace_id=f"live-match:{source.as_label()}:{market.condition_id}",
                    event_type=DomainEventType.ENTRY_SIGNAL_TRIGGERED.value,
                    event_id=uuid4().hex,
                    condition_id=market.condition_id,
                    token_id=outcome.token_id,
                    market_slug=market.market_slug,
                    event_slug=market.event_slug,
                    reason="live_state_match",
                    payload={
                        "source": source.as_label(),
                        "source_event_id": match.event.source_event_id,
                        "confidence": calibration.confidence,
                        "phase": match.phase,
                        "primary_source": match.primary_source,
                        "contributing_sources": list(match.contributing_sources),
                    },
                ),
            )

    def _record_calibration_failure(
        self,
        market: Market,
        source: LiveSourceKey,
        match: LiveStateMatch,
        calibration: CalibrationResult,
    ) -> None:
        # 错配防御：不写 metadata + 落 audit 给运维查证（CLAUDE.md §18）
        self._event_bus.publish_nowait(
            OutboxPriority.P3,
            DomainEvent(
                trace_id=f"calibration-failed:{source.as_label()}:{market.condition_id}",
                event_type=DomainEventType.SPORTS_LIVE_MATCH_GAP_RECORDED.value,
                event_id=uuid4().hex,
                condition_id=market.condition_id,
                market_slug=market.market_slug,
                event_slug=market.event_slug,
                reason=calibration.rejection_reason or "calibration_failed",
                payload={
                    "source": source.as_label(),
                    "matched_event_id": match.event.source_event_id,
                    "diagnostics": dict(calibration.diagnostics),
                },
            ),
        )
