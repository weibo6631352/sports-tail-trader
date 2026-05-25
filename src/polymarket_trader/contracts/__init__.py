"""quant 策略与 framework 共享的数据类型契约。

承载跨模块传递的数据类型：DecisionContext / TradingDecision / QuantDecision /
RuntimePorts / lifecycle events / DiscoveryQuery / UniverseDecision / LiveStateMatch
等。这层不含任何业务规则——业务规则全部在 polymarket_trader.quant 内。
"""

from __future__ import annotations

from polymarket_trader.contracts.config_loader import load_extension_config, load_mapping_file
from polymarket_trader.contracts.context import (
    AccountSnapshotView,
    DecisionContext,
)
from polymarket_trader.contracts.discovery import DiscoveryQuery
from polymarket_trader.contracts.decisions import (
    DecisionKind,
    EntryCandidate,
    EntrySizing,
    MarketTokenView,
    QuantDecision,
    QuantTriggerKind,
    TradeAction,
    TradingDecision,
    UniverseDecision,
)
from polymarket_trader.contracts.errors import ConfigFileLoadError
from polymarket_trader.contracts.lifecycle import (
    LifecycleBus,
    LifecycleCallback,
    LifecycleEnvelope,
    LifecycleEvent,
    SubscriptionHandle,
)
from polymarket_trader.contracts.live_state import LiveStateMatch
from polymarket_trader.contracts.manual_confirmation import ManualConfirmation
from polymarket_trader.contracts.ports import (
    RuntimePorts,
    MetricsPort,
    ParameterPort,
    SeasonStateReadPort,
)
from polymarket_trader.contracts.summary import StrategySummary
from polymarket_trader.domain.events import AuditEvent, DomainEventType, Fill

__all__ = (
    "AccountSnapshotView",
    "AuditEvent",
    "DecisionKind",
    "DomainEventType",
    "DiscoveryQuery",
    "EntryCandidate",
    "EntrySizing",
    "TradeAction",
    "DecisionContext",
    "TradingDecision",
    "ConfigFileLoadError",
    "RuntimePorts",
    "Fill",
    "LifecycleBus",
    "LifecycleCallback",
    "LifecycleEnvelope",
    "LifecycleEvent",
    "LiveStateMatch",
    "ManualConfirmation",
    "MarketTokenView",
    "MetricsPort",
    "ParameterPort",
    "QuantDecision",
    "QuantTriggerKind",
    "SeasonStateReadPort",
    "StrategySummary",
    "SubscriptionHandle",
    "UniverseDecision",
    "load_extension_config",
    "load_mapping_file",
)
