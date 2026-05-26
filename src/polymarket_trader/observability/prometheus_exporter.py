"""Prometheus 文本格式 exporter（docs/新架构方案.md §11.4 `/metrics` endpoint）。

把 MetricsRegistry.snapshot() 转成 Prometheus exposition format text/plain。
外部 Prometheus / VictoriaMetrics / Grafana Agent 可直接 scrape。

# 格式（Prometheus 文本协议）

```
# HELP <metric_name> <description>
# TYPE <metric_name> <type>
<metric_name>{label1="value1"} <value> <timestamp_ms?>
```

- counter → "_total" 后缀（Prometheus 约定）
- gauge → 原名
- histogram → 输出 buckets / sum / count（_bucket / _sum / _count）

# 用法

```python
from polymarket_trader.observability.prometheus_exporter import render_prometheus

# api/routes/metrics.py:
@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(runtime = Depends(get_runtime)):
    return render_prometheus(runtime.metrics.snapshot())
```

# label 转义

按 Prometheus 规范：反斜杠/双引号/换行均转义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .metrics import MetricsSnapshot


def _escape_label_value(value: str) -> str:
    """Prometheus label value 转义。"""
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    parts = [f'{k}="{_escape_label_value(str(v))}"' for k, v in labels]
    return "{" + ",".join(parts) + "}"


def _format_value(value: float) -> str:
    """避免 1.0 输出为 "1.0"——Prometheus 期望 "1"。整数值用 int 表示。"""
    if value == int(value) and not (value != value):  # NaN check
        return str(int(value))
    return repr(value)


def render_prometheus(snapshot: "MetricsSnapshot") -> str:
    """将 MetricsSnapshot 渲染为 Prometheus 文本格式。"""

    lines: list[str] = []

    # === Counters ===
    counter_groups: dict[str, list] = {}
    for counter in snapshot.counters:
        counter_groups.setdefault(counter.name, []).append(counter)
    for name, items in sorted(counter_groups.items()):
        metric_name = name if name.endswith("_total") else f"{name}_total"
        lines.append(f"# TYPE {metric_name} counter")
        for c in items:
            lines.append(f"{metric_name}{_format_labels(c.labels)} {_format_value(c.value)}")

    # === Gauges ===
    gauge_groups: dict[str, list] = {}
    for gauge in snapshot.gauges:
        gauge_groups.setdefault(gauge.name, []).append(gauge)
    for name, items in sorted(gauge_groups.items()):
        lines.append(f"# TYPE {name} gauge")
        for g in items:
            lines.append(f"{name}{_format_labels(g.labels)} {_format_value(g.value)}")

    # === Histograms ===
    hist_groups: dict[str, list] = {}
    for hist in snapshot.histograms:
        hist_groups.setdefault(hist.name, []).append(hist)
    for name, items in sorted(hist_groups.items()):
        lines.append(f"# TYPE {name} histogram")
        for h in items:
            labels_base = _format_labels(h.labels)
            # buckets (HistogramBucketSnapshot.upper_bound_ms: None=+Inf)
            cumulative = 0
            for bucket in h.buckets:
                cumulative += bucket.count
                le_text = "+Inf" if bucket.upper_bound_ms is None else str(bucket.upper_bound_ms)
                if labels_base:
                    bucket_labels = labels_base[:-1] + f',le="{le_text}"' + "}"
                else:
                    bucket_labels = f'{{le="{le_text}"}}'
                lines.append(
                    f"{name}_bucket{bucket_labels} {_format_value(cumulative)}"
                )
            lines.append(f"{name}_sum{labels_base} {_format_value(h.sum_ms)}")
            lines.append(f"{name}_count{labels_base} {_format_value(h.count)}")

    lines.append("")  # 末尾空行（Prometheus 推荐）
    return "\n".join(lines)


__all__ = ["render_prometheus"]
