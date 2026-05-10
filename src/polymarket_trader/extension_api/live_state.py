from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import SportsLiveGame


@dataclass(frozen=True, slots=True)
class LiveStateMatch:
    """策略 ``match_live_state`` hook 的返回值。

    ``signal_allowed`` / ``signal_reason`` 让 framework 不必读策略私有 metadata
    字段就能判断市场是否在策略期望的活跃窗口内（用于 ws 订阅、入场闸门等通用判断）。
    ``phase`` 是策略归一后的活跃阶段标识（如 "live" / "ended" / "scheduled"），
    framework 据此判断是否启动 ws 订阅或扩展 discovery，不再读 ``payload`` 嵌套字典。
    ``payload`` 是策略附带的展示用透传 dict，framework 不解析其字段语义，仅整体存储。
    """

    market: Market
    game: SportsLiveGame
    signal_allowed: bool
    signal_reason: str = ""
    phase: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
