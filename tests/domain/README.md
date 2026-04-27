# domain 测试目录说明

该目录存放纯业务规则单元测试。这里的测试应该快速、确定、无网络、无数据库，能在任何开发机器上直接运行。

## 覆盖范围

- `allocation.py`：框架分配 DTO 契约、已持仓和 open BUY exposure 计算。
- `events.py`：审计事件、outbox 事件和外部响应脱敏。
- `fees.py`：Polymarket 费用公式和 taker fee 预估。
- `risk.py`：价格、notional、tick size、min order、集中度和 open BUY 异常。
- `domain_import_boundaries.py`：Domain 不持有外部 market payload parser。
- `registry.py`：运行时 market 索引快照和 fee schedule 更新。

策略化的资金分配策略不属于 domain 测试范围；例如当前业务扩展的等权分配测试放在
`tests/strategies/current/`。

## 必须保持的边界

- 不连接数据库。
- 不调用 Polymarket SDK。
- 不依赖 FastAPI。
- 金额和价格断言使用 `Decimal`。
- 拒绝结果要断言 reason，避免只有布尔值。

## 输入与输出

- 输入：领域模型、常量、订单簿数据、价格与资金参数。
- 输出：决策结果、拒绝原因、领域事件和状态转移断言。
