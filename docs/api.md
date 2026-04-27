# 对外 API 清单

这里只写已经存在的 HTTP API。

- 核对日期：2026-04-22
- 适用仓库：`polymarket-trader`
- 服务入口：`src/polymarket_trader/api/app.py`
- 默认无应用层鉴权，只放在本机或受控内网。

## 1. 已注册路由

由 `create_app()` 注册的路由如下。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/openapi.json` | OpenAPI 描述 |
| `GET` | `/docs` | Swagger UI |
| `GET` | `/docs/oauth2-redirect` | Swagger UI OAuth redirect helper |
| `GET` | `/redoc` | ReDoc |
| `GET` | `/health` | 存活检查 |
| `GET` | `/ready` | readiness 与阻塞原因 |
| `GET` | `/runtime` | 运行态总览 |
| `GET` | `/workers` | worker 健康与调度状态 |
| `GET` | `/metrics` | 当前指标快照 |
| `GET` | `/audit-events` | 审计事件分页查询 |
| `GET` | `/allocations` | 资金分配分页查询 |
| `GET` | `/markets` | 市场分页查询 |
| `GET` | `/markets/detail` | 单 market 详情 |
| `GET` | `/markets/orderbook` | 市场盘口快照 |
| `GET` | `/markets/midpoint` | 市场中间价 |
| `GET` | `/markets/prices-history` | 市场价格历史 |
| `GET` | `/orders` | 订单分页查询 |
| `POST` | `/orders/replace` | 人工替换单个 open order |
| `GET` | `/fills` | fills 分页查询 |
| `GET` | `/positions` | 持仓分页查询 |
| `GET` | `/portfolio` | 组合与账户摘要 |
| `GET` | `/outbox/pending` | outbox 待处理事件 |
| `POST` | `/operations/reconcile` | 手动触发 reconcile |

不对外暴露的路由：

- 直接下 BUY 单
- 直接撤任意单
- 直接暂停/恢复 market

## 2. 通用返回

### 2.1 分页接口

`/markets`、`/orders`、`/fills`、`/positions` 统一返回：

```json
{
  "items": [],
  "total": 0,
  "limit": 100,
  "offset": 0
}
```

`/markets/prices-history` 返回：

```json
{
  "token_id": "no-token-sample",
  "interval": "1h",
  "fidelity": 60,
  "history": [
    {
      "timestamp": "2026-04-13T10:00:00+00:00",
      "price": "0.54"
    }
  ]
}
```

`/markets/midpoint` 返回：

```json
{
  "token_id": "no-token-sample",
  "condition_id": "condition-sample",
  "market_slug": "sample-market-a",
  "source": "hot",
  "midpoint": "0.57",
  "best_bid": "0.55",
  "best_ask": "0.59",
  "last_trade_price": "0.54",
  "spread": "0.04",
  "received_at": "2026-04-13T10:00:00+00:00"
}
```

### 2.2 序列化规则

- `Decimal` 字段统一序列化成字符串。
- 时间统一输出 ISO 8601 UTC 字符串。
- 枚举统一输出小写或固定字符串值。
- `market_slug` 是 market 级标识；Polymarket 官网事件链接使用 `event_slug`。
- `audit-events` / `outbox` 的 `event_slug` 来自事件自身的一等字段，不在序列化层回填。
- `orders` / `fills` / `positions` / `allocations` 的 `event_slug` 是管理视图字段，用 registry 按 `condition_id`、`token_id` 或 `market_slug` 解析，用于前端打开官网事件页。

### 2.3 热态优先级

- `/markets` 运行中优先读内存 `MarketRegistry` 热态快照。
- `/orders` 的 `open_only=true` 读运行态 `AccountStateStore`。
- `/positions` 读运行态 `AccountStateStore`。
- `/fills` 在有 DB session factory 时优先查仓储；否则回退运行态 fills。

## 3. 只读接口

### 3.1 `GET /health`

用途：

- 只看进程是否活着。

典型返回：

```json
{
  "status": "ok",
  "timestamp": "2026-04-13T10:00:00+00:00"
}
```

### 3.2 `GET /ready`

用途：

- 返回当前是否允许自动交易。
- 返回阻塞原因、warning、运行态摘要。

关键字段：

- `ready_to_trade`
- `phase`
- `blocking_issues`
- `blocking_reasons`
- `warnings`
- `runtime.user_ws_connected`
- `runtime.allow_new_entries`
- `runtime.last_reconcile_at`
- `runtime.blocking_reasons`

说明：

- 该接口是运维判断“现在能不能自动下单”的首选入口。
- `blocking_reasons` 来自 Supervisor，是运行态阻塞判断的事实来源。
- `blocking_issues` 只保留配置校验的结构化问题；没有配置问题时，会把运行态 reason 原样透出为通用 issue。

### 3.3 `GET /runtime`

用途：

- 输出完整运行态总览，方便本地排障。

主要顶层字段：

- `phase`
- `ready_to_trade`
- `readiness`
- `settings`
- `runtime`
- `bootstrap_summary`
- `market_discovery`
- `registry`
- `account`
- `event_bus`
- `persistence`
- `markets`
- `portfolio`

说明：

- `settings` 已脱敏，测试里已覆盖 `wallet_private_key -> "***"`。
- `market_discovery.query_cursors` / `completed_query_names` 用于观察远端 discovery 多 query 分页状态。
- `markets[].market.fees` 输出：
  - `enabled`
  - `maker_base_fee_bps`
  - `taker_base_fee_bps`
  - `fee_rate_bps`
  - `fee_rate_updated_at`

### 3.4 `GET /markets`

用途：

- 分页查询当前跟踪市场。
- 支持费率筛选和排序。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `trading_status` | `str` | `null` | 按交易状态过滤 |
| `fees_enabled` | `bool` | `null` | 按是否启用费率过滤 |
| `fee_rate_bps_min` | `int` | `null` | `>=0` |
| `fee_rate_bps_max` | `int` | `null` | `>=0` |
| `maker_base_fee_bps_min` | `int` | `null` | `>=0` |
| `maker_base_fee_bps_max` | `int` | `null` | `>=0` |
| `taker_base_fee_bps_min` | `int` | `null` | `>=0` |
| `taker_base_fee_bps_max` | `int` | `null` | `>=0` |
| `sort_by` | `str` | `null` | `market_slug` / `fee_rate_bps` / `fee_rate_updated_at` / `maker_base_fee_bps` / `taker_base_fee_bps` |
| `sort_direction` | `str` | `desc` | `asc` / `desc` |

单项结构重点：

- `market`
- `tracked`
- `market.market_slug`
- `market.event_slug`
- `market.token_ids[]`
- `market.outcomes[]`
- `token_views[]`

说明：

- handler 不现场请求外部费率接口。
- 费率查询只使用本地缓存字段。
- `token_views[]` 是唯一主视图；每个 token view 都包含 `token_id`、`outcome`、`orderbook`、`position`、`open_orders`、`best_ask`、`best_bid`、`spread`、`fee_preview`。
- `fee_preview` 是逐 token 的热态派生视图，默认按 `100 shares` 结合 `best_ask` / `best_bid` 预估 taker 手续费。
- `fee_preview` 使用 `src/polymarket_trader/domain/fees.py` 的 `build_taker_fee_preview(...)`，公式为 `fee = size_shares * feeRate * price * (1 - price)`，其中 `feeRate = fee_rate_bps / 1000`。
- `fee_preview` 不落库，不作为 `Market` 静态事实。

### 3.5 `GET /markets/detail`

用途：

- 按 `market_slug`、`condition_id` 或 `token_id` 精确查询单个 market。

查询参数：

- `market_slug`
- `condition_id`
- `token_id`

约束：

- 三者至少给一个。
- 找不到时返回 `404 market not found`。

返回结构：

- 与 `/markets.items[]` 单项结构一致。
- `token_views[]` 是唯一正式的逐 token 视图；响应不包含默认 `NO` 视图或 `yes_*` 字段。

### 3.6 `GET /markets/orderbook`

用途：

- 查询单个 market 当前盘口快照，适合详情页实时价格、spread 和深度展示。

查询参数：

- `market_slug`
- `condition_id`
- `token_id`

约束：

- `token_id` 必填。
- `market_slug` 和 `condition_id` 只作为附加定位信息，不再隐式推断默认 outcome。

关键返回字段：

- `token_id`
- `condition_id`
- `market_slug`
- `source`
- `orderbook.best_bid`
- `orderbook.best_ask`
- `orderbook.best_bid_size`
- `orderbook.best_ask_size`
- `orderbook.last_trade_price`
- `orderbook.tick_size`
- `orderbook.spread`
- `orderbook.bids`
- `orderbook.asks`

说明：

- 优先返回本地 `market_ws_worker` 热态快照，`source=hot`。
- 热态缺失时回退 `ClobClient.get_orderbook()`，`source=rest`。
- 上游 Polymarket 暂时不可用时返回 `502 market_orderbook_upstream_unavailable`。
- 若运行时没有可用 `clob_client`，返回 `503 clob_client_unavailable`。

### 3.7 `GET /markets/midpoint`

用途：

- 查询单个 market 当前中间价，适合前端高频轻量轮询和卡片价格展示。

查询参数：

- `market_slug`
- `condition_id`
- `token_id`

约束：

- `token_id` 必填。
- `market_slug` 和 `condition_id` 只作为附加定位信息，不再隐式推断默认 outcome。

关键返回字段：

- `token_id`
- `condition_id`
- `market_slug`
- `source`
- `midpoint`
- `best_bid`
- `best_ask`
- `last_trade_price`
- `spread`
- `received_at`

说明：

- 当本地热态 snapshot 同时存在 `best_bid` 和 `best_ask` 时，直接本地计算 midpoint，`source=hot`。
- 热态缺失时回退 `ClobClient.get_midpoint()`，`source=rest`。
- 上游 Polymarket 暂时不可用时返回 `502 market_midpoint_upstream_unavailable`。
- 若运行时没有可用 `clob_client`，返回 `503 clob_client_unavailable`。

### 3.8 `GET /markets/prices-history`

用途：

- 查询单个 token 的历史价格序列，适合前端做走势回放、局部区间放大和基础图表展示。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `token_id` | `str` | 必填 | 官方 `market` 参数，对应 asset id |
| `start_ts` | `float` | `null` | 可选，Unix 时间戳下界 |
| `end_ts` | `float` | `null` | 可选，Unix 时间戳上界 |
| `interval` | `str` | `null` | `max` / `all` / `1m` / `1w` / `1d` / `6h` / `1h` |
| `fidelity` | `int` | `null` | 分钟精度，`>=1` |

约束：

- `start_ts`、`end_ts` 同时存在时必须满足 `start_ts <= end_ts`。

关键返回字段：

- `token_id`
- `interval`
- `fidelity`
- `history[].timestamp`
- `history[].price`

说明：

- 该接口直连 `ClobClient.get_prices_history()`。
- 返回时间统一转成 ISO 8601 UTC 字符串；上游原始字段是 `t`。
- 上游 Polymarket 暂时不可用时返回 `502 market_prices_history_upstream_unavailable`。
- 若运行时没有可用 `clob_client`，返回 `503 clob_client_unavailable`。

### 3.9 `GET /orders`

用途：

- 分页查询订单。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `open_only` | `bool` | `true` | `true` 时只看当前 open orders |
| `condition_id` | `str` | `null` | 可选过滤 |
| `token_id` | `str` | `null` | 可选过滤 |
| `trace_id` | `str` | `null` | 可选过滤 |
| `order_id` | `str` | `null` | 可选过滤 |
| `trade_id` | `str` | `null` | 可选过滤 |

单项结构重点：

- `trace_id`
- `condition_id`
- `token_id`
- `market_slug`
- `event_slug`
- `side`
- `order_type`
- `price`
- `amount_usdc`
- `size_shares`
- `filled_shares`
- `remaining_shares`
- `notional_usdc`
- `order_id`
- `trade_id`
- `status`
- `idempotency_key`
- `reason`
- `post_only`

说明：

- `open_only=true` 时只返回运行态 open orders。
- `open_only=false` 且存在 DB session factory 时，走仓储快照查询。
- `open_only=false` 但运行时没有 DB session factory 时，仍然回退运行态 open orders。

### 3.10 `GET /fills`

用途：

- 分页查询 fills。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `trace_id` | `str` | `null` | 可选过滤 |
| `order_id` | `str` | `null` | 可选过滤 |
| `trade_id` | `str` | `null` | 可选过滤 |

单项结构重点：

- `trace_id`
- `event_type`
- `event_id`
- `market_slug`
- `event_slug`
- `condition_id`
- `token_id`
- `order_id`
- `trade_id`
- `side`
- `price`
- `size`
- `notional_usdc`
- `status`
- `confirmed_at`

说明：

- 有 DB session factory 时读仓储快照。
- 没有 DB session factory 时回退运行态 fills。

### 3.11 `GET /positions`

用途：

- 分页查询当前持仓。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `condition_id` | `str` | `null` | 可选过滤 |
| `token_id` | `str` | `null` | 可选过滤 |

单项结构重点：

- `condition_id`
- `token_id`
- `market_slug`
- `event_slug`
- `shares`
- `cost_usdc`
- `open_buy_shares`
- `open_sell_shares`
- `pending_buy_shares`
- `confirmed_shares`
- `last_order_id`
- `last_trade_id`
- `confirmation_status`
- `updated_at`

### 3.12 `GET /portfolio`

用途：

- 给出账户和组合摘要。

返回重点：

- `balance_usdc`
- `allowance_usdc`
- `available_usdc`
- `position_count`
- `open_order_count`
- `fill_count`
- `pause_count`
- `last_reconcile_at`
- `user_ws_connected`
- `allow_new_entries`
- `markets_tracked`
- `recent_allocations`

说明：

- `available_usdc` 直接等于 `balance_usdc`。
- 若仓储可用，会补 `recent_allocations`；否则返回空数组。

### 3.13 `GET /audit-events`

用途：

- 分页查询审计事件。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `trace_id` | `str` | `null` | 可选过滤 |
| `event_title` | `str` | `null` | 按事件标题精确过滤 |

单项结构重点：

- `trace_id`
- `event_id`
- `event_title`
- `market_slug`
- `event_slug`
- `condition_id`
- `token_id`
- `outcome`
- `side`
- `order_type`
- `price`
- `size`
- `notional_usdc`
- `order_id`
- `trade_id`
- `tx_hash`
- `status`
- `reason`
- `payload`
- `created_at`

说明：

- `payload` 为脱敏后的审计载荷，可包含入场计划 metadata、候选原因、执行权限、风控结果和事件输入等复盘信息。

### 3.14 `GET /allocations`

用途：

- 分页查询已落库的资金分配快照。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `trace_id` | `str` | `null` | 可选过滤 |
| `condition_id` | `str` | `null` | 可选过滤 |
| `token_id` | `str` | `null` | 可选过滤 |
| `market_slug` | `str` | `null` | 可选过滤 |

单项结构重点：

- `condition_id`
- `market_slug`
- `event_slug`
- `token_id`
- `target_budget_usdc`
- `buy_budget_usdc`
- `current_exposure_usdc`
- `released_budget_usdc`
- `reason`
- `release_reason`
- `idempotency_key`

说明：

- 有 DB session factory 时返回已落库分配快照。
- 没有 DB session factory 时返回空分页结果。

### 3.15 `GET /workers`

用途：

- 输出 worker 健康状态、调度器快照和队列深度。

返回重点：

- `phase`
- `automatic_trading_enabled`
- `queue_depths`
- `scheduler`
- `workers`

### 3.16 `GET /metrics`

用途：

- 输出 runtime 指标快照。

返回重点：

- `phase`
- `automatic_trading_enabled`
- `queue_depths`
- `metrics`

### 3.17 `GET /outbox/pending`

用途：

- 分页查询运行时中尚未 `ack` / `dead-letter` 的 outbox 事件。

查询参数：

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `limit` | `int` | `100` | `1..500` |
| `offset` | `int` | `0` | `>=0` |
| `trace_id` | `str` | `null` | 可选过滤 |

单项结构重点：

- `trace_id`
- `event_type`
- `idempotency_key`
- `event_id`
- `market_slug`
- `event_slug`
- `condition_id`
- `token_id`
- `reason`
- `created_at`
- `priority`
- `retry_count`
- `last_error`
- `raw_response_summary`
- `payload`

说明：

- 运行时存在本地 outbox 时，返回真实待处理队列快照。
- 运行时 outbox 不可用时返回空分页结果，不使用已持久化记录冒充 pending 队列。

## 4. 受控操作接口

### 4.1 `POST /operations/reconcile`

用途：

- 人工触发一次 reconcile。
- 可只针对部分 `condition_id` 执行。

请求体：

```json
{
  "trace_id": "trace-manual-reconcile",
  "condition_ids": ["condition-sample"]
}
```

返回重点：

- `status`
- `trace_id`
- `plan`
- `applied_actions`
- `failed_actions`

`plan.market_plans[].actions[].action_type` 可能出现：

- `cancel_order`
- `replace_order`
- `submit_order`
- `pause_trading`

失败口径：

- 若 `reconcile_worker` 不可用，返回 `{"status":"failed","reason":"reconcile_worker_unavailable"}`。

### 4.2 `POST /orders/replace`

用途：

- 对单个 open order 做人工 replace，不绑定 BUY / SELL，也不绑定 NO / YES。

请求体：

```json
{
  "order_id": "sell-1",
  "new_price": "0.78",
  "operator": "manual",
  "reason": "admin_reprice",
  "trace_id": "trace-reprice"
}
```

字段约束：

| 字段 | 说明 |
| --- | --- |
| `order_id` | 必填，目标 open order 的 `order_id` 或 `idempotency_key` |
| `market_slug` / `condition_id` / `token_id` | 可选，用于辅助唯一定位订单 |
| `new_price` | `0 < new_price < 1` |
| `size_shares` | 可选；不传则默认沿用当前 open order 的剩余份额 |
| `operator` | 默认 `manual` |
| `reason` | 默认 `admin_replace_order` |
| `trace_id` | 可选，不传则自动生成 |

成功返回重点：

- `status`
- `trace_id`
- `operator`
- `market`
- `order`
- `replace_review`
- `replace_order_submitted`

说明：

- 这是对单张 open order 的真正 replace，不会先把整个 market 的同类订单全部撤掉。
- hot state 会按原订单的 `side` 和 `token_id` 更新，不再默认按 SELL / NO 处理。

明确失败原因：

- `order_not_found`
- `market_not_found`
- `market_not_operable`
- `invalid_price`
- `invalid_tick_size`
- `price_not_aligned_to_tick_size`
- `order_size_unknown`
- `replace_order_failed`

## 5. 接口边界

Admin API 是人工查询和受控操作入口，不是交易策略入口。

允许：

- 健康检查
- readiness 查询
- runtime 查询
- market / order / fill / position / portfolio 查询
- 手动 reconcile
- 手动 replace 单个 open order

禁止：

- 直接创建 FAK BUY 的 HTTP endpoint
- 绕过 Risk Manager 的订单提交
- 在 handler 里直接调用 Polymarket SDK
- 在 handler 里直接改写 `MarketRegistry`
- 在 handler 里直接改写 `Orderbook Cache`
- 无分页的大查询

## 6. 使用说明

- 自动化探活用 `/health`
- 判断能否自动交易用 `/ready`
- 本地排障先看 `/runtime`
- 市场扫描和费率筛选用 `/markets`
- 市场实时盘口用 `/markets/orderbook`
- 市场轻量价格轮询用 `/markets/midpoint`
- 市场价格走势回放用 `/markets/prices-history`
- 人工修复只用 `/operations/reconcile` 和 `/orders/replace`
