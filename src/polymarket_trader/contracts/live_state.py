from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from polymarket_trader.domain.market import Market
from polymarket_trader.domain.sports_live import LiveEvent


@dataclass(frozen=True, slots=True)
class SeriesState:
    """系列赛热态快照——framework 级类型，infra/workers/策略均使用此定义。

    infra 层（series_state_client）生产，workers 层写入 metadata store，
    策略层的 evaluator 消费。不允许策略包定义独立副本。
    """

    team_a: str
    team_b: str
    wins_a: int
    wins_b: int
    best_of: int
    next_game_at: datetime | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class LiveStateMatch:
    """策略 ``match_live_state`` hook 的返回值。

    ``signal_allowed`` / ``signal_reason`` 让 framework 不必读策略私有 metadata
    字段就能判断市场是否在策略期望的活跃窗口内（用于 ws 订阅、入场闸门等通用判断）。
    ``phase`` 是策略归一后的活跃阶段标识（如 "live" / "ended" / "scheduled"），
    framework 据此判断是否启动 ws 订阅或扩展 discovery，不再读 ``payload`` 嵌套字典。
    ``primary_source`` / ``contributing_sources`` / ``confidence`` 把"该 market
    实际匹配到的源"作为一等可观测信息（缺口 1：per-market 源选择可观测性）。
    ``payload`` 是策略附带的展示用透传 dict，framework 不解析其字段语义，仅整体存储。
    """

    market: Market
    event: LiveEvent
    signal_allowed: bool
    signal_reason: str = ""
    phase: str = ""
    primary_source: str = ""
    contributing_sources: tuple[str, ...] = ()
    confidence: float = 0.0
    payload: Mapping[str, Any] = field(default_factory=dict)
