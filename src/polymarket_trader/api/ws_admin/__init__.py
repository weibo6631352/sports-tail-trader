"""【api/ws_admin】admin 实时数据 WebSocket 增量推送。

docs/新架构方案.md §12.3 ④。agent / 前端运营查询从 polling 改 push——订阅
一次后被动接收 (portfolio / candidates / live_states / health) 增量更新。

# 设计

```
client → WS /admin/stream
  send: {topics: ["portfolio", "candidates", "live_states", "health"]}
  recv: {topic, change_set, timestamp}   // 只推增量，不推全量
```

订阅时一次性发送当前 snapshot，之后只推 diff。增量来源是已有 `event_bus`，
通过 `admin_ws_publisher` 桥接。

# 模块

- `publisher` —— `AdminWsPublisher`：订阅 event_bus 关键事件 → 计算 diff →
  推到对应 WS topic。当前接口骨架，等 WS endpoint 实装时接入。

# 现状

未启用，等 Step 7c 后续把 api/routes/stream.py 改造接 publisher。当前
api/routes/stream.py 已有 SSE 推送（不是 WS），可作为 transitional path。

# 与 SSE 区别

- SSE（server → client）：单向，HTTP 1.1，浏览器原生
- WS（双向）：浏览器同样支持，但 agent 直接用 WS 客户端更自然（订阅 /
  取消订阅 / topic 过滤都更灵活）

§7.1 操盘和审计统一走后端 API——agent 走 WS 订阅是首选。
"""

from .publisher import AdminWsPublisher

__all__ = ["AdminWsPublisher"]
