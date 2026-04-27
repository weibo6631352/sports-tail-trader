# infra 目录说明

该目录存放外部系统适配和技术设施。Infra 层负责和“不受我们控制的世界”打交道，然后把外部协议转换成系统内部 DTO。

## 职责

- Polymarket Gamma / CLOB / Data / WebSocket 适配。
- PostgreSQL 模型、会话和仓储。
- outbox 可靠事件队列和持久化入口。
- 外部体育比分 / 赛况数据源适配。
- 时间、序列化、外部 I/O 辅助能力。

## 子目录

- [db](./db/README.md)：数据库模型、会话、仓储。
- [outbox](./outbox/README.md)：可靠事件队列。
- [polymarket](./polymarket/README.md)：Polymarket API / WS / order executor 适配。
- [sports](./sports/README.md)：外部体育数据源适配。

## 允许依赖

- `polymarket_trader.domain` 内部 DTO。
- 外部 SDK、HTTP client、WebSocket client、SQLAlchemy。
- `polymarket_trader.config` 中的基础配置对象。
- `polymarket_trader.observability` 的 trace id 或审计事件模型。

## 禁止行为

- 不在 infra 中实现策略判断。
- 不让 Polymarket SDK 对象泄漏到 Domain。
- 不让数据库写入阻塞交易主链路。
- 不在适配层绕过 Risk Manager 下单。
- 不把外部 API 返回的 raw JSON 原样无界写日志。

## 接口契约

- 对上返回内部 DTO、结果对象或明确的错误类型。
- 所有外部调用必须有超时、重试边界和审计信息。
- 阻塞 SDK 调用必须进入合适的线程池，不能直接跑在交易主事件循环。
- 交易主链路适配调用与后台维护 / 异步支撑调用需要使用隔离资源。
