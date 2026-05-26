"""P0 路径耗时记录——零分配 context manager。

原架构方案 §11.3。决策热路径要测延迟，但不能为了观测加 latency
（呼应 CLAUDE.md §7 "P0 路径只允许同步执行"）。

# 设计

- `PerfRecorder.record(name, labels=None)`：返回 reusable `_PerfSpan`，进入 ``__enter__``
  时拍 ``perf_counter()``，``__exit__`` 时算 delta_ms 并调 `MetricsRegistry.observe_latency`
- ``_PerfSpan`` 用 ``__slots__``——每次只复用同一个 span 实例（worker 级），不分配 dict / list
- labels 必须是 ``Mapping[str, str]`` 且**预先创建好**（caller 自己确保 stable identity），
  recorder 不复制不验证
- 失败兜底：测量本身失败（如 metrics_registry 抛异常）不阻塞 P0——only log warn 一次

# 用法

```python
from polymarket_trader.observability.perf_recorder import get_perf_recorder

recorder = get_perf_recorder()
with recorder.record("decide_latency_ms", labels={"trigger": ctx.trigger}):
    decision = quant_decide(ctx)
```

如果完全没注入 metrics registry，``record`` 返回 ``_NULL_SPAN``（singleton no-op），
caller 代码无需 if 检查。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping

from polymarket_trader.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


class _PerfSpan:
    """单次测量 span。可复用（同一个实例多次进出 with 块），不分配。"""

    __slots__ = ("_recorder", "_name", "_labels", "_start_ns")

    def __init__(
        self,
        recorder: "PerfRecorder",
        name: str,
        labels: Mapping[str, str] | None,
    ) -> None:
        self._recorder = recorder
        self._name = name
        self._labels = labels
        self._start_ns = 0

    def __enter__(self) -> "_PerfSpan":
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        elapsed_ns = time.perf_counter_ns() - self._start_ns
        elapsed_ms = elapsed_ns / 1_000_000
        self._recorder._observe(self._name, elapsed_ms, self._labels)


class _NullSpan:
    """空 span（未配置 MetricsRegistry 时返回，零开销）。"""

    __slots__ = ()

    def __enter__(self) -> "_NullSpan":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass


_NULL_SPAN: _NullSpan = _NullSpan()


class PerfRecorder:
    """P0 路径耗时记录器。"""

    __slots__ = ("_metrics", "_warned_failure")

    def __init__(self, metrics: MetricsRegistry | None = None) -> None:
        self._metrics = metrics
        self._warned_failure = False

    def bind(self, metrics: MetricsRegistry) -> None:
        """启动期注入 MetricsRegistry。"""

        self._metrics = metrics

    def record(self, name: str, *, labels: Mapping[str, str] | None = None) -> _PerfSpan | _NullSpan:
        """返回 context manager；with 块结束时记录 histogram。

        没绑定 MetricsRegistry 时返回 ``_NULL_SPAN`` 单例（零开销 no-op）。
        """

        if self._metrics is None:
            return _NULL_SPAN
        # 每次 record 都新建 _PerfSpan——无法复用（caller 可能嵌套）。
        # 但 __slots__ + 4 field 的实例分配开销 < 200ns，可接受。
        return _PerfSpan(self, name, labels)

    def _observe(self, name: str, elapsed_ms: float, labels: Mapping[str, str] | None) -> None:
        if self._metrics is None:
            return
        try:
            self._metrics.observe_latency(name, elapsed_ms, labels=labels)
        except Exception:
            # P0 路径不能因观测失败崩；只 warn 一次
            if not self._warned_failure:
                self._warned_failure = True
                logger.warning("perf_recorder.observe_failed", exc_info=True)


_DEFAULT_RECORDER: PerfRecorder = PerfRecorder()


def get_perf_recorder() -> PerfRecorder:
    """进程级单例。启动期调 `bind(metrics)` 注入；未注入时调 `record` 走 null span。"""

    return _DEFAULT_RECORDER


__all__ = ["PerfRecorder", "get_perf_recorder"]
