# tests 目录说明

该目录存放测试。测试不仅验证功能正确，也要保护架构边界：不能让数据库、Admin 查询、日志、全量扫描等低优先级任务慢慢渗入交易主链路。

## 测试分层

| 目录 | 目标 | 说明 |
| --- | --- | --- |
| [domain](./domain/README.md) | 单元测试 | 分类、分配、风控、策略和状态机 |
| [app](./app/README.md) | 编排测试 | FAK / GTC 流程、reconcile、Admin 操作 |
| [infra](./infra/README.md) | 适配测试 | Polymarket payload、WS 重连、DB / outbox |

## 优先测试清单

- 缺失交易字段的 market 会被拒绝。
- 只有 universe 选择通过且风控放行的 market 才能进入可交易集合。
- 非目标 universe 的 market 会被当前业务扩展排除。
- 超过策略入场价格上限的 `NO best ask` 不触发买入。
- FAK partial fill 只对成交 shares 挂 SELL。
- FAK no fill 会释放预算并重新分配。
- open BUY 异常会触发 cancel。
- 等权分配不会让早发现 market 超过 `max_market_usdc`。
- User WS 断线后恢复必须完成 reconcile 才允许继续买入。
- DB 写入失败不阻塞订单提交，但 outbox 可重试。
- Admin API 大查询、数据库慢写入、日志落盘变慢时，FAK BUY 提交路径仍不被阻塞。

## 新增测试规则

- 涉及下单、撤单、成交、持仓、资金分配、风控和 reconcile 的改动，必须补测试。
- 涉及并发、线程池、锁、队列和降级策略的改动，必须补延迟、超时或优先级测试。
- 测试中应显式模拟数据库慢、日志慢、Admin 查询慢、低优先级队列积压和 WS 重连。
- 测试不能依赖真实 Polymarket 账户、生产数据库或真实私钥。

## 运行约定

- 默认本地回归直接运行 `pytest`。
- `tests/infra/test_postgres_integration.py` 依赖真实 PostgreSQL；未设置 `TRADER_TEST_POSTGRES_DSN` 时显示 `skipped` 属于预期。
- 需要验证真实 PostgreSQL 建表和持久化链路时，设置 `TRADER_TEST_POSTGRES_DSN` 后单独运行该文件。
