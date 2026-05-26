"""沙箱（paper trading）核心：订单簿撮合、fee 计算与虚拟账本。

所有沙箱组件按职责分文件，对外只暴露顶层入口；与生产路径完全隔离，
不引入开关与配置项（沙箱通过独立函数构造，参见 ``app/virtual_paper_trading.py``）。
"""

from __future__ import annotations

from .client import PaperSubmitOnlyOrderClient
from .event_stream import (
    EventStreamSource,
    RecordedEventStream,
    ShadowEvent,
    SyntheticEventStream,
    dump_shadow_events_to_jsonl,
    load_shadow_events_from_jsonl,
)
from .fill_engine import SimulationOutcome, simulate_fill
from .orderbook_matcher import (
    ConsumedLevel,
    MatchResult,
    match_taker_buy,
    match_taker_sell,
)
from .state import PaperVirtualLedger
from .virtual_clock import EventTimestampClock, FrozenClock, RealClock, VirtualClock

__all__ = [
    "ConsumedLevel",
    "EventStreamSource",
    "EventTimestampClock",
    "FrozenClock",
    "MatchResult",
    "PaperSubmitOnlyOrderClient",
    "PaperVirtualLedger",
    "RealClock",
    "RecordedEventStream",
    "ShadowEvent",
    "SimulationOutcome",
    "SyntheticEventStream",
    "VirtualClock",
    "dump_shadow_events_to_jsonl",
    "load_shadow_events_from_jsonl",
    "match_taker_buy",
    "match_taker_sell",
    "simulate_fill",
]
