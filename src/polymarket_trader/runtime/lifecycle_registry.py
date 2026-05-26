"""市场生命周期事件 fan-out 中枢 —— `LifecycleRegistry`。

替代散落在 `main.py` 9 处的 `market_registry.register_prune_callback(...)`：
所有需要在 market 进入 / 退出 universe 时执行联动清理 / 订阅的组件，统一通过
本 registry 注册 listener。Registry 自身只注册 1 次回调到 `market_registry`，
然后 fan-out 给所有 listener，方便审计 / 排查 / 后续扩展（如加入 metric 计数、
顺序控制等）。

# 与 InProcessLifecycleBus 的关系

`InProcessLifecycleBus`（`lifecycle_bus.py`）是**事件发布订阅总线**——框架向
策略广播业务事件（ORDER_SUBMITTED / ORDER_FILLED / LIVE_STATE_UPDATED 等）。

`LifecycleRegistry`（本模块）是**资源清理协调器**——market 进出 universe 时
通知所有需要联动的 store / worker / subscriber 执行 evict / subscribe / cleanup。

两者职责正交，并存。

# listener 签名

- `prune` listener: `Callable[[str, tuple[str, ...]], None]` — (condition_id, token_ids)
- `added` listener: `Callable[[Market], None]` — 新 market 进入 universe（第 3 步
  在 `market_registry.upsert` 内加 added hook 后启用）

# 异常隔离

每个 listener 调用独立 try/except——任一 listener 抛错不影响其他 listener
执行，错误以日志形式记录（不抛回主路径，避免一个组件错配带垮整个 prune 路径）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polymarket_trader.domain.market import Market

_logger = logging.getLogger(__name__)


PruneListener = Callable[[str, tuple[str, ...]], None]
AddedListener = Callable[["Market"], None]


class LifecycleRegistry:
    """市场生命周期事件 fan-out 中枢。"""

    def __init__(self) -> None:
        self._prune_listeners: list[tuple[str, PruneListener]] = []
        self._added_listeners: list[tuple[str, AddedListener]] = []
        self._lock = Lock()

    def register_prune_listener(self, name: str, listener: PruneListener) -> None:
        """注册 market 退出 universe 时的清理回调。

        `name` 仅用于审计 / 排错（日志 / metric label），不参与去重——重复注册
        同名 listener 会执行多次（这是 caller 编程错误，registry 不掩盖）。
        """

        with self._lock:
            self._prune_listeners.append((name, listener))

    def register_added_listener(self, name: str, listener: AddedListener) -> None:
        """注册 market 新进 universe 时的联动回调（订阅直播源 / odds 等）。"""

        with self._lock:
            self._added_listeners.append((name, listener))

    def emit_pruned(self, condition_id: str, token_ids: tuple[str, ...]) -> None:
        """由 `market_registry` 在 market 退出时调用一次，fan-out 给所有 listener。

        # 异常隔离

        每个 listener 独立 try/except——一个失败不阻断其他 listener。
        """

        with self._lock:
            listeners = tuple(self._prune_listeners)
        for name, listener in listeners:
            try:
                listener(condition_id, token_ids)
            except Exception:  # noqa: BLE001 — 必须吞所有异常，避免一个 listener 拖死整链
                _logger.exception(
                    "lifecycle prune listener %s failed for condition_id=%s",
                    name,
                    condition_id,
                )

    def emit_added(self, market: "Market") -> None:
        """由 `market_registry.upsert` 在新 market 首次进入时调用，fan-out 给 listener。

        第 3 步（discovery + orderbook_ws 改名）会把 added hook 接入 market_registry；
        本方法在此之前只对 `LiveSourceRegistry.on_market_added` 等新组件提供同步入口。
        """

        with self._lock:
            listeners = tuple(self._added_listeners)
        for name, listener in listeners:
            try:
                listener(market)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "lifecycle added listener %s failed for condition_id=%s",
                    name,
                    market.condition_id,
                )

    def listener_summary(self) -> dict[str, tuple[str, ...]]:
        """供 operator / metric 查询当前注册的 listener 名单。"""

        with self._lock:
            return {
                "prune": tuple(name for name, _ in self._prune_listeners),
                "added": tuple(name for name, _ in self._added_listeners),
            }
