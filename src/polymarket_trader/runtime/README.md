# runtime 目录说明

该目录存放进程运行时组件，包括事件队列、状态注册表、调度和 supervisor。Runtime 的核心任务是保护交易主链路的优先级和状态一致性。

## 文件职责

- `account_state.py`：账户余额、持仓、open orders、fills 和买入闸门热状态快照。
- `event_bus.py`：事件总线和优先级队列。
- `registry.py`：market / token 索引和本地状态注册表。
- `status.py`：runtime phase、readiness、scheduler snapshot 和 worker health 契约。
- `scheduler.py`：任务创建与调度。
- `supervisor.py`：worker 生命周期、降级、暂停和恢复。

## 运行链路分类

- 交易主链路：Orderbook Watcher、Strategy Worker、Risk Manager、Order Executor、User WS 中的订单 / 成交状态更新。
- 关键修复链路：取消异常 open order、补挂缺失订单、replace 漂移订单等必须优先处理的修复动作。
- 后台维护链路：Market Discovery、周期 reconcile、余额和 allowance 周期检查。
- 异步支撑链路：Persistence Worker、指标聚合、Admin 普通查询、报表。

## 允许依赖

- `polymarket_trader.domain` 的状态模型和事件。
- `polymarket_trader.config` 的队列容量和超时配置。
- `polymarket_trader.observability` 的指标和 trace。

## 禁止行为

- 不在 runtime 中实现 Polymarket API 适配。
- 不在 registry 中做慢数据库查询。
- 不使用全局大锁保护所有 market / position / orderbook 状态。
- 不让后台维护或异步支撑任务持有会阻塞交易主链路写入的锁。

## 状态访问规则

- 按 `condition_id` 或 `token_id` 分片。
- Admin 和 Persistence 读取快照，不直接持有热状态写锁。
- 交易主链路写入只做短临界区更新。
- 非关键锁等待超时后跳过并告警，不能无限等待。

## 输入与输出

- 输入：来自 worker、WebSocket、discovery、reconcile 和 supervisor 的运行时事件。
- 输出：优先级队列、状态快照、调度状态和 readiness / phase 视图。
