# api 目录说明

该目录存放 FastAPI Admin API。Admin API 是人工查询和受控操作入口，不是策略计算和订单执行入口。

## 职责

- 暴露健康检查和运行状态。
- 查询 target markets、eligible markets、orderbook 快照、持仓、open orders 和组合分配。
- 查询体育扫尾候选和直播状态快照。
- 触发受控人工操作，例如执行 reconcile、通用 order `replace` 和候选人工确认。
- 做 HTTP 参数校验和响应序列化；默认仅在受控环境暴露，不提供应用层鉴权。

## 允许依赖

- `polymarket_trader.app` 中的应用服务。
- `polymarket_trader.config` 中的只读配置。
- `polymarket_trader.observability` 中的 trace / metrics 基础能力。
- FastAPI / Pydantic 等接口层工具。

## 禁止依赖与禁止行为

- 不直接调用 `infra.polymarket` 或 Polymarket SDK。
- 不直接创建、签名、提交、取消订单。
- 不直接写 Market Registry、Orderbook Cache、Position State。
- 不在 HTTP handler 中执行慢数据库全表扫描或报表生成。
- 不绕过 Risk Manager 暴露 FAK BUY 入口。
- 候选人工确认必须重新构建入场计划并经过 `OrderGateway -> RiskManager -> OrderExecutor`。
- 不与 Order Executor 共用交易主链路线程池。

## 接口调用链

推荐：

```text
route -> app service -> domain / runtime snapshot / repository
```

订单相关人工操作：

```text
route -> OperatorService -> OrderGateway -> OrderExecutor
```

## 输入与输出

- 输入：HTTP path、query、body 参数，以及 FastAPI dependency 提供的应用服务和配置对象。
- 输出：统一响应 schema、HTTP 状态码，以及受控操作对应的可审计结果对象。

## 路由清单

- 对外 HTTP 路由、请求参数和响应结构以 FastAPI 自动生成的 `/openapi.json` 与 `/docs`（Swagger UI）为准。
- 本 README 只维护接口层职责、依赖边界和调用链，不重复展开完整路由表。
