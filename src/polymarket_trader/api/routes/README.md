# routes 目录说明

该目录存放 Admin API 的路由模块。路由模块负责 HTTP 边界，不负责业务决策。

## 文件职责

- `health.py`：暴露 `/health` 和 `/ready`。
- `runtime.py`：暴露 `/runtime`、`/workers`、`/metrics`。
- `audit_events.py`：暴露 `/audit-events`。
- `allocations.py`：暴露 `/allocations`。
- `markets.py`：market 列表、详情、orderbook、midpoint、价格历史查询。
- `orders.py`：订单查询和通用 order replace 入口。
- `fills.py`：成交查询。
- `positions.py`：持仓查询。
- `portfolio.py`：组合预算、exposure 和分配状态查询。
- `outbox.py`：暴露 `/outbox/pending`。
- `operations.py`：受控 reconcile 操作。

## 路由层只能做

- 解析 path / query / body。
- 调用 FastAPI dependency 获取应用服务。
- 做轻量请求校验。
- 返回统一 response schema。

## 路由层不能做

- 策略判断。
- 风控判断。
- 订单 payload 拼接。
- 签名、下单、撤单。
- 持有交易状态锁。
- 直接查询 Polymarket API。

## 输入与输出

- 输入：已经过 FastAPI 校验的 path、query、body 参数，以及 dependency 注入的应用服务。
- 输出：路由响应模型、分页结果或受控操作返回值。
