# infra 测试目录说明

该目录存放基础设施适配测试。这里验证外部协议转换、数据库仓储、outbox 和 WebSocket 行为。

## 覆盖范围

- Polymarket Gamma / CLOB / Data API 响应到内部 DTO 的转换。
- FAK BUY 和 GTC SELL payload 字段语义。
- BUY `amount` 表示 USDC.e 花费金额，SELL `amount` 表示 shares。
- WebSocket 断线重连和 REST 快照校准。
- 数据库失败不阻塞交易提交。
- outbox 幂等键、重试次数和最后错误。
- 仓储分页和索引字段。

## 必须保持的边界

- 默认使用 fake server、mock transport 或本地测试数据库。
- 不使用生产 API key、私钥或真实钱包。
- 不把 SDK 原始对象泄漏到 domain 断言之外。
- 对外部调用超时、重试和错误类型做断言。
- 真实 PostgreSQL 集成测试只在显式提供 `TRADER_TEST_POSTGRES_DSN` 时运行；默认跳过。

## 输入与输出

- 输入：HTTP / WebSocket mock payload、数据库状态和 outbox 事件。
- 输出：内部 DTO、错误类型、重试状态和脱敏后的审计摘要。
