"""量化决策器与框架共享的数据类型集合。

历史上是"框架 + 策略插件"二次开发抽象层，现仅承载跨模块传递的数据类型
（Context / Decision / Ports / lifecycle 事件等）。Protocol 类（ExtensionHooks /
BusinessExtension / 各种 *Hooks）已删除——quant 策略直接装配，无插件层。
"""

from __future__ import annotations

from polymarket_trader.extension_api.config_loader import load_extension_config, load_mapping_file
from polymarket_trader.extension_api.context import (
    AccountSnapshotView,
    ExtensionContext,
)
from polymarket_trader.extension_api.discovery import DiscoveryQuery
from polymarket_trader.extension_api.decisions import (
    DecisionKind,
    EntryCandidate,
    EntrySizing,
    MarketTokenView,
    QuantDecision,
    QuantTriggerKind,
    ExtensionAction,
    ExtensionDecision,
    UniverseDecision,
)
from polymarket_trader.extension_api.errors import ExtensionLoadError
from polymarket_trader.extension_api.lifecycle import (
    LifecycleBus,
    LifecycleCallback,
    LifecycleEnvelope,
    LifecycleEvent,
    SubscriptionHandle,
)
from polymarket_trader.extension_api.live_state import LiveStateMatch
from polymarket_trader.extension_api.manual_confirmation import ManualConfirmation
from polymarket_trader.extension_api.ports import (
    ExtensionPorts,
    MetricsPort,
    ParameterPort,
    SeasonStateReadPort,
)
from polymarket_trader.extension_api.summary import StrategySummary
from polymarket_trader.domain.events import AuditEvent, DomainEventType, Fill

__all__ = (
    "AccountSnapshotView",
    "AuditEvent",
    "DecisionKind",
    "DomainEventType",
    "DiscoveryQuery",
    "EntryCandidate",
    "EntrySizing",
    "ExtensionAction",
    "ExtensionContext",
    "ExtensionDecision",
    "ExtensionLoadError",
    "ExtensionPorts",
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
