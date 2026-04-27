# db 目录说明

该目录存放 PostgreSQL 模型、会话和仓储。数据库是审计、复盘、调试和恢复参考，不是交易状态唯一真相来源。

## 职责

- 定义 SQLAlchemy 模型。
- 创建 async session factory。
- 封装 market、order、fill、position、audit event、outbox 等仓储。
- 支持幂等写入和可追踪重试。

## 允许依赖

- SQLAlchemy / asyncpg。
- `polymarket_trader.domain` DTO。
- `polymarket_trader.config` 中的数据库配置。

## 禁止行为

- 不在仓储中调用 Polymarket API。
- 不在仓储中实现风控和策略规则。
- 不要求 Order Executor 同步等待 PostgreSQL 写入成功。
- 不把数据库状态当作订单是否发生的唯一判断。

## 输入与输出

- 仓储方法应表达明确用例，例如 `save_audit_event`、`list_open_orders_snapshot`。
- 写入操作需要幂等键或唯一约束支持。
- 查询方法需要分页或明确限制条数。
- 交易热路径不得临时执行慢查询。
