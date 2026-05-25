# app 目录说明

该目录存放应用服务和用例编排。App 层是接口层、worker 和基础设施之间的协调层，不是纯规则层，也不是外部协议适配层。

## 职责

- 编排 market 发现、分类、注册、订阅和审计。
- 编排入场信号、资金分配、风控检查和订单执行。
- 编排 FAK fill / no fill / partial fill 后的资金释放和 GTC SELL。
- 编排 reconcile 差异检测与修复动作。
- 编排 Admin API 触发的人工操作。

## 文件职责

- `admin_service.py`：只读查询和受控人工操作编排。
- `market_ingest_service.py`：market discovery 结果解析、扩展 universe 精筛、registry 和订阅编排。
- `reconcile_service.py`：权威快照校准和修复动作编排。
- `decision_context_builder.py`：交易事件到扩展决策和订单意图的编排。
- `order_gateway.py`：风控通过后的订单意图执行编排。

## 允许依赖

- `polymarket_trader.domain`：业务规则、DTO、状态机。
- `polymarket_trader.infra`：外部 API、数据库、outbox 适配。
- `polymarket_trader.runtime`：事件总线、registry、调度。
- `polymarket_trader.observability`：审计、trace、metrics。

## 禁止行为

- 不在 App 层实现 Polymarket SDK 字段细节。
- 不把 FastAPI request / response 对象传入 Domain。
- 不在交易主链路中临时发起慢 REST 查询或数据库查询。
- 不绕过 Risk Manager 调用 Order Executor。
- 不把重 CPU 任务放在交易主事件循环。

## 输入与输出

App service 方法应优先接受内部 DTO 或基础类型，返回内部 DTO、结果对象或可序列化视图。外部 payload 转换应发生在 infra，HTTP 参数转换应发生在 api。

交易订单链路必须保持：

```text
event -> MarketTickWorker -> DecisionContextBuilder -> EntryPlanner -> RiskManager -> OrderGateway -> OrderExecutor -> outbox/audit
```
