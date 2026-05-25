from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from polymarket_trader.quant.strategy import CurrentStrategy

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from polymarket_trader.app.market_payload_parser import MarketParseResult, MarketPayloadParser
from polymarket_trader.app.market_tracking_policy import (
    market_outside_trade_window,
    market_unsubscribe_prune_reason,
)
from polymarket_trader.domain.events import DomainEvent, DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.observability.trace import ensure_trace_id
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.runtime.registry import MarketRegistry
from polymarket_trader.domain.decisions import UniverseDecision

AccountSnapshotProvider = Callable[[], AccountSnapshot]

# market_filtered_out 高频事件 dedup 上限：condition_id → 上次发出的 reason。
# 同一市场反复以同一 reason 被拒（每次 discovery pass 都会扫到）会把 audit 表撑爆，
# 用 LRU 缓存按 reason 去重；reason 变化、或市场转回 DISCOVERED/UPDATED 时清缓存。
_FILTER_DEDUPE_CAPACITY = 50_000


class MarketTracker(Protocol):
    """订阅侧 worker 的 track/untrack 接口，由 `MarketWsWorker` 实现。"""

    def track_market(self, market: Market) -> None: ...

    def untrack_market(self, token_ids: tuple[str, ...]) -> None: ...

    def build_subscription_request(self, token_ids: tuple[str, ...]) -> dict[str, Any]: ...


class MarketService:
    """Coordinates market discovery, strategy universe filtering, and registry updates."""

    def __init__(
        self,
        *,
        strategy: "CurrentStrategy",
        parser: MarketPayloadParser | None = None,
        registry: MarketRegistry | None = None,
        market_tracker: MarketTracker | None = None,
        account_snapshot_provider: AccountSnapshotProvider | None = None,
        filter_emit_min_interval_s: float = 0.0,
    ) -> None:
        self._parser = parser or MarketPayloadParser()
        self._strategy = strategy
        self._registry = registry
        self._market_tracker = market_tracker
        self._account_snapshot_provider = account_snapshot_provider
        # MARKET_FILTERED_OUT dedup：仅在 (condition_id, reason) 与上次发出的不同时
        # 才让 worker 落 audit。reason 改变或市场转为 DISCOVERED/UPDATED 时移除条目，
        # 这样下次再被同一原因过滤会重新发出一次。
        self._last_filter_reason: OrderedDict[str, str] = OrderedDict()
        self._suppressed_filter_emits: int = 0
        # filter 拒掉的 cid 不进 registry 没 lifecycle 信号 → 用 TTL 主管理 + cap 兜底:
        # - TTL 2h:超过 2 小时没被扫到的 cid 自动清(主管理)
        # - cap 50000:safety net 防 2h 内 cid 暴涨(极端情况)
        self._last_filter_emit_at: OrderedDict[str, float] = OrderedDict()
        self._filter_emit_min_interval_s: float = filter_emit_min_interval_s
        # TTL:超 2 小时(7200s)未碰的 cid 视为陈旧,主动删除让下次重新 emit
        self._filter_ttl_seconds: float = 7_200.0

    @property
    def strategy(self) -> "CurrentStrategy":
        """返回市场发现链路正在使用的扩展筛选 hooks。"""

        return self._strategy

    def ingest_raw_market(
        self,
        raw_market: Mapping[str, Any],
        *,
        source: str,
        trace_id: str | None = None,
        discovered_at: datetime | None = None,
    ) -> "MarketDiscoveryOutcome":
        trace_id = trace_id or ensure_trace_id()
        discovered_at = discovered_at or datetime.now(timezone.utc)
        parse_result = self._parser.parse(raw_market)
        existing_market = self._lookup_existing_market(parse_result)
        account_snapshot = self._current_account_snapshot()

        market: Market | None = None
        tracked_market: Market | None = None
        tracking_retained = False
        subscription_request: dict[str, Any] | None = None
        universe_decision: UniverseDecision | None = None
        if parse_result.accepted:
            candidate_market = parse_result.to_market()
            if existing_market is not None:
                candidate_market = candidate_market.with_fee_schedule(
                    fees_enabled=(
                        candidate_market.fees_enabled
                        if candidate_market.fees_enabled is not None
                        else existing_market.fees_enabled
                    ),
                    maker_base_fee_bps=(
                        candidate_market.maker_base_fee_bps
                        if candidate_market.maker_base_fee_bps is not None
                        else existing_market.maker_base_fee_bps
                    ),
                    taker_base_fee_bps=(
                        candidate_market.taker_base_fee_bps
                        if candidate_market.taker_base_fee_bps is not None
                        else existing_market.taker_base_fee_bps
                    ),
                )
                if (
                    existing_market.fee_rate_bps is not None
                    and candidate_market.fee_rate_bps is None
                    and candidate_market.taker_base_fee_bps is None
                ):
                    candidate_market = candidate_market.with_fee_rate(
                        existing_market.fee_rate_bps,
                        fee_rate_updated_at=existing_market.fee_rate_updated_at,
                    )

            prune_reason = market_unsubscribe_prune_reason(
                account_snapshot,
                candidate_market,
                now=discovered_at,
            )
            if prune_reason is not None:
                # 发现链路也执行同一套终态清理，避免 reconcile 刚退订又被扫描重新订阅。
                universe_decision = UniverseDecision.exclude(reason=prune_reason)
                if existing_market is not None:
                    self._remove_market_tracking(existing_market)
            else:
                universe_decision = self._strategy.select_market(candidate_market)
                # 时间窗口门禁：远期未开赛 / 早已结束的单场赛事不纳入 WS 跟踪——
                # 否则 discovery 会 track 上万个远期市场、market WS 订阅追不上。
                # 有账户敞口的市场走下方 retain-filtered 分支，仍保留跟踪。
                if universe_decision.selected and market_outside_trade_window(
                    candidate_market, now=discovered_at
                ):
                    universe_decision = UniverseDecision.exclude(
                        reason="game_outside_trade_window"
                    )
                if universe_decision.selected:
                    # Universe + 时间窗口通过——直接把 market 加入 registry。WS 订阅
                    # 由 ws_loops.should_subscribe_ws 实时基于 entry_metadata 决定，
                    # 不再写 market 上的中间 flag（避免 stale 维护）。
                    market = candidate_market
                    tracked_market = market
                    if self._registry is not None:
                        self._registry.upsert(market)
                    if self._market_tracker is not None:
                        self._market_tracker.track_market(market)
                        subscription_request = self._market_tracker.build_subscription_request(
                            market.token_ids
                        )
                elif existing_market is not None:
                    if self._should_retain_filtered_market(existing_market, account_snapshot):
                        tracked_market = self._build_retained_filtered_market(
                            candidate_market,
                            existing_market=existing_market,
                            reason=universe_decision.reason,
                        )
                        tracking_retained = True
                        if self._registry is not None:
                            self._registry.upsert(tracked_market)
                        if self._market_tracker is not None:
                            self._market_tracker.track_market(tracked_market)
                    else:
                        self._remove_market_tracking(existing_market)

        discovery_kind = (
            DomainEventType.MARKET_UPDATED.value
            if market is not None and existing_market is not None
            else (
                DomainEventType.MARKET_DISCOVERED.value
                if market is not None
                else DomainEventType.MARKET_FILTERED_OUT.value
            )
        )
        event = self._build_event(
            parse_result,
            trace_id=trace_id,
            source=source,
            discovered_at=discovered_at,
            discovery_kind=discovery_kind,
            raw_market=raw_market,
            market=market,
            tracked_market=tracked_market,
            tracking_retained=tracking_retained,
            universe_decision=universe_decision,
        )
        suppress_event = self._apply_filter_dedupe(
            parse_result=parse_result,
            discovery_kind=discovery_kind,
            reason=event.reason,
        )
        return MarketDiscoveryOutcome(
            trace_id=trace_id,
            source=source,
            parse_result=parse_result,
            event=event,
            market=market,
            discovery_kind=discovery_kind,
            subscription_request=subscription_request,
            raw_market=raw_market,
            existing_market=existing_market,
            tracked_market=tracked_market,
            tracking_retained=tracking_retained,
            tracking_removed=(
                parse_result.accepted
                and market is None
                and existing_market is not None
                and not tracking_retained
            ),
            universe_decision=universe_decision,
            suppress_event=suppress_event,
        )

    def _apply_filter_dedupe(
        self,
        *,
        parse_result: MarketParseResult,
        discovery_kind: str,
        reason: str,
    ) -> bool:
        """高频 MARKET_FILTERED_OUT 去重：同一 cid 同一 reason 静默；reason 变更或转回
        DISCOVERED/UPDATED 时清缓存以保证后续可重新发出。

        WHY：实盘里同一批被拒市场每个 discovery pass 都被扫到，逐条落审计会撑爆
        audit_events 表。dedup 只关心 (condition_id, reason)，不影响首次/转换发出。
        """

        cid = parse_result.condition_id
        if cid is None:
            return False
        if discovery_kind == DomainEventType.MARKET_FILTERED_OUT.value:
            previous = self._last_filter_reason.get(cid)
            if previous == reason:
                self._last_filter_reason.move_to_end(cid)
                self._suppressed_filter_emits += 1
                return True
            # 最小间隔兜底(prod 60s):即使 reason 切换,同 cid 配置秒内最多 1 条 audit.
            if self._filter_emit_min_interval_s > 0:
                import time as _time
                now_mono = _time.monotonic()
                last_emit = self._last_filter_emit_at.get(cid, 0.0)
                if (now_mono - last_emit) < self._filter_emit_min_interval_s:
                    self._suppressed_filter_emits += 1
                    return True
                self._last_filter_emit_at[cid] = now_mono
                self._last_filter_emit_at.move_to_end(cid)
                # TTL sweep:OrderedDict leftmost=最久未更新,2h 未碰的 cid 整体删除
                # (reason+emit_at 联动).discovery 再次扫到该 cid 时如果还被拒,
                # 当作"首次"重新 emit(有 audit 价值).TTL 优于 cap LRU:
                # - cap 只在容量满时被动清,长期低活跃场景 dict 可能停在 cap 边界
                # - TTL 主动清陈旧数据,vol 低时 dict 自然瘦身.
                self._sweep_stale_filter_entries(now_mono)
            self._last_filter_reason[cid] = reason
            self._last_filter_reason.move_to_end(cid)
            # cap 兜底 safety net:极端情况(2h 内 50k+ 新 cid)防爆
            while len(self._last_filter_reason) > _FILTER_DEDUPE_CAPACITY:
                self._last_filter_reason.popitem(last=False)
            return False
        # discovery_kind 是 MARKET_DISCOVERED / MARKET_UPDATED 时清缓存，让下次重新被
        # 过滤会再发出一次（"过滤状态恢复"也是有审计价值的事件）。
        self._last_filter_reason.pop(cid, None)
        self._last_filter_emit_at.pop(cid, None)
        return False

    def _sweep_stale_filter_entries(self, now_mono: float) -> None:
        """amortized O(1):从最久未更新端开始 pop 过期 entry.

        OrderedDict 在每次 emit 时 move_to_end,所以 leftmost 永远是最久未碰的.
        遇到第一个未过期立即停止,大多数调用是 O(1).
        """
        cutoff = now_mono - self._filter_ttl_seconds
        # 同步清 emit_at 和 reason(同 cid)
        while self._last_filter_emit_at:
            try:
                oldest_cid = next(iter(self._last_filter_emit_at))
            except StopIteration:
                break
            if self._last_filter_emit_at[oldest_cid] >= cutoff:
                break
            self._last_filter_emit_at.popitem(last=False)
            self._last_filter_reason.pop(oldest_cid, None)

    @property
    def suppressed_filter_emits(self) -> int:
        """已被 dedup 抑制的 MARKET_FILTERED_OUT 事件数，供 admin/observability 观察。"""

        return self._suppressed_filter_emits

    def _lookup_existing_market(
        self,
        parse_result: MarketParseResult,
    ) -> Market | None:
        if self._registry is None or not parse_result.accepted:
            return None
        if parse_result.condition_id is not None:
            market = self._registry.get_by_condition_id(parse_result.condition_id)
            if market is not None:
                return market
        if parse_result.market_slug is not None:
            return self._registry.get_by_slug(parse_result.market_slug)
        return None

    def _build_event(
        self,
        parse_result: MarketParseResult,
        *,
        trace_id: str,
        source: str,
        discovered_at: datetime,
        discovery_kind: str,
        raw_market: Mapping[str, Any],
        market: Market | None,
        tracked_market: Market | None,
        tracking_retained: bool,
        universe_decision: UniverseDecision | None,
    ) -> DomainEvent:
        event_type = (
            DomainEventType.MARKET_UPDATED
            if market is not None and discovery_kind == DomainEventType.MARKET_UPDATED.value
            else (
                DomainEventType.MARKET_DISCOVERED
                if market is not None
                else DomainEventType.MARKET_FILTERED_OUT
            )
        )
        event_slug = market.event_slug if market is not None else parse_result.event_slug
        payload = {
            "source": source,
            "discovery_kind": discovery_kind,
            "parse_status": parse_result.status.value,
            "parse_reason": parse_result.reject_reason.value if parse_result.reject_reason
            else None,
            "parse_detail": parse_result.reject_detail,
            "matched_fields": parse_result.matched_fields,
            "matched_keywords": parse_result.matched_keywords,
            "accepted": market is not None,
            "strategy_selected": universe_decision.selected if universe_decision is not None else None,
            "strategy_reason": universe_decision.reason if universe_decision is not None else None,
            "market": _serialize_market(market),
            "tracked_market": _serialize_market(tracked_market),
            "tracking_retained": tracking_retained,
            "raw_market": raw_market,
            "discovered_at": discovered_at.isoformat(),
        }
        return DomainEvent(
            trace_id=trace_id,
            event_type=event_type,
            event_id=uuid4().hex,
            market_slug=parse_result.market_slug,
            event_slug=event_slug,
            condition_id=parse_result.condition_id,
            reason=self._event_reason(parse_result, universe_decision),
            created_at=discovered_at,
            payload=payload,
        )

    @staticmethod
    def _event_reason(
        parse_result: MarketParseResult,
        universe_decision: UniverseDecision | None,
    ) -> str:
        if universe_decision is not None and not universe_decision.selected:
            return universe_decision.reason
        if parse_result.reject_reason is not None:
            return parse_result.reject_reason.value
        return ""

    def _current_account_snapshot(self) -> AccountSnapshot | None:
        if self._account_snapshot_provider is None:
            return None
        return self._account_snapshot_provider()

    def _should_retain_filtered_market(
        self,
        market: Market,
        account_snapshot: AccountSnapshot | None,
    ) -> bool:
        return self._strategy.should_keep_tracking(market, account_snapshot)

    def _build_retained_filtered_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return self._strategy.build_filtered_tracking_market(
            candidate_market,
            existing_market=existing_market,
            reason=reason,
        )

    def _remove_market_tracking(self, market: Market) -> None:
        if self._registry is not None:
            self._registry.remove_market(market.condition_id)
        if self._market_tracker is not None:
            self._market_tracker.untrack_market(market.token_ids)


@dataclass(frozen=True, slots=True)
class MarketDiscoveryOutcome:
    trace_id: str
    source: str
    parse_result: MarketParseResult
    event: DomainEvent
    market: Market | None
    discovery_kind: str
    subscription_request: dict[str, Any] | None
    raw_market: Mapping[str, Any]
    existing_market: Market | None = None
    tracked_market: Market | None = None
    tracking_retained: bool = False
    tracking_removed: bool = False
    universe_decision: UniverseDecision | None = None
    # 同一 cid 同一 reason 的 MARKET_FILTERED_OUT 在高频 discovery 里被 dedup 抑制；
    # worker 看到 True 时跳过 publish。accept/tracking_* 路径不受影响。
    suppress_event: bool = False

    @property
    def accepted(self) -> bool:
        return self.market is not None

    @property
    def should_publish_event(self) -> bool:
        if self.suppress_event:
            return False
        return self.accepted or self.tracking_retained or self.tracking_removed


def _serialize_market(market: Market | None) -> dict[str, Any] | None:
    if market is None:
        return None
    return {
        "condition_id": market.condition_id,
        "market_slug": market.market_slug,
        "token_ids": list(market.token_ids),
        "outcomes": [
            {
                "token_id": outcome.token_id,
                "outcome": outcome.outcome,
            }
            for outcome in market.outcomes
        ],
        "event_id": market.event_id,
        "event_title": market.event_title,
        "event_slug": market.event_slug,
        "end_date": None if market.end_date is None else market.end_date.isoformat(),
        "game_start_time": (
            None if market.game_start_time is None else market.game_start_time.isoformat()
        ),
        "tick_size": str(market.tick_size),
        "min_order_size": str(market.min_order_size),
        "neg_risk": market.neg_risk,
        "fees": {
            "enabled": market.fees_enabled,
            "maker_base_fee_bps": market.maker_base_fee_bps,
            "taker_base_fee_bps": market.taker_base_fee_bps,
            "fee_rate_bps": market.fee_rate_bps,
            "fee_rate_updated_at": (
                None
                if market.fee_rate_updated_at is None
                else market.fee_rate_updated_at.isoformat()
            ),
        },
        "category": market.category,
        "tags": market.tags,
        "matched_keywords": market.matched_keywords,
        "trading_status": market.trading_status.value,
        "reject_reason": market.reject_reason,
    }
