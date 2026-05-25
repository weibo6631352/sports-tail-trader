"""MARKET_FILTERED_OUT 高频事件 dedup：同 cid 同 reason 静默；reason 变更或
转回 DISCOVERED/UPDATED 时清缓存以保证后续可重新发出。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polymarket_trader.app.market_service import MarketService
from polymarket_trader.domain.account import AccountSnapshot
from polymarket_trader.domain.events import DomainEventType
from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api import ExtensionContext
from polymarket_trader.extension_api.decisions import (
    EntrySizing,
    ExtensionDecision,
    QuantDecision,
    UniverseDecision,
)
from polymarket_trader.runtime.registry import MarketRegistry


class _ConfigurableHooks:
    """允许用 callable 控制 select_market 输出的测试 stub。"""

    def __init__(self) -> None:
        self.select_fn = lambda market: UniverseDecision.exclude(reason="reason_a")

    def discovery_queries(self):
        return ()

    def select_market(self, market: Market) -> UniverseDecision:
        return self.select_fn(market)

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        raise NotImplementedError

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="test")

    def quant_decide(self, context: ExtensionContext) -> QuantDecision:
        return QuantDecision(reason="test")

    def should_keep_tracking(self, market: Market, account_snapshot: AccountSnapshot | None) -> bool:
        return False

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market


def _raw_market(condition_id: str = "condition-1") -> dict[str, object]:
    return {
        "conditionId": condition_id,
        "slug": f"market-{condition_id}",
        "question": "Sports market",
        "clobTokenIds": [f"{condition_id}-yes", f"{condition_id}-no"],
        "outcomes": ["YES", "NO"],
        "tickSize": "0.01",
        "orderMinSize": "5",
        "category": "Sports",
        "tags": ["Sports"],
        "endDate": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
    }


def _make_service(hooks: _ConfigurableHooks) -> MarketService:
    return MarketService(
        extension_hooks=hooks,
        registry=MarketRegistry(),
        market_tracker=None,
        account_snapshot_provider=lambda: AccountSnapshot(),
    )


def test_same_reason_suppresses_second_emit() -> None:
    hooks = _ConfigurableHooks()
    service = _make_service(hooks)

    out1 = service.ingest_raw_market(_raw_market(), source="test", trace_id="t1")
    out2 = service.ingest_raw_market(_raw_market(), source="test", trace_id="t2")

    assert out1.discovery_kind == DomainEventType.MARKET_FILTERED_OUT.value
    assert out1.suppress_event is False
    assert out2.suppress_event is True
    assert service.suppressed_filter_emits == 1


def test_reason_change_reemits() -> None:
    hooks = _ConfigurableHooks()
    service = _make_service(hooks)

    service.ingest_raw_market(_raw_market(), source="test", trace_id="t1")
    hooks.select_fn = lambda market: UniverseDecision.exclude(reason="reason_b")
    out2 = service.ingest_raw_market(_raw_market(), source="test", trace_id="t2")

    assert out2.suppress_event is False
    assert service.suppressed_filter_emits == 0


def test_transition_to_discovered_clears_cache() -> None:
    hooks = _ConfigurableHooks()
    service = _make_service(hooks)

    service.ingest_raw_market(_raw_market(), source="test", trace_id="t1")
    # 切到 included → DISCOVERED；缓存应被清。
    hooks.select_fn = lambda market: UniverseDecision.include(reason="included")
    out_included = service.ingest_raw_market(_raw_market(), source="test", trace_id="t2")
    assert out_included.discovery_kind in {
        DomainEventType.MARKET_DISCOVERED.value,
        DomainEventType.MARKET_UPDATED.value,
    }
    assert "condition-1" not in service._last_filter_reason

    # 再切回 excluded 同 reason → 应重新发出（cache 已清）
    hooks.select_fn = lambda market: UniverseDecision.exclude(reason="reason_a")
    out_refiltered = service.ingest_raw_market(_raw_market(), source="test", trace_id="t3")
    assert out_refiltered.discovery_kind == DomainEventType.MARKET_FILTERED_OUT.value
    assert out_refiltered.suppress_event is False
