"""当前体育扫尾策略侧的运行时限额常量。

策略侧的运行时上限（非框架运行参数）按 CLAUDE.md §10 留在策略包内，
不进 framework Settings；调用方（API 路由等）直接 import 这里的常量。
"""

from __future__ import annotations

# SSE 订阅同时在线的软上限。超过会返回 HTTP 429 Retry-After=5，由订阅方退避重连。
# 取值取舍：32 个并发观察者足够覆盖前端管理台 + 偶发调试工具，
# 同时不会让每条事件 fan-out 的常数开销显著拉高 publish() 延迟。
SSE_SUBSCRIBER_CAP: int = 32
