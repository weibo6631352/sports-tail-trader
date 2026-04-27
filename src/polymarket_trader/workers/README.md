# workers 目录说明

该目录存放常驻后台任务。Worker 负责从队列、WebSocket、定时器或外部事件中取数据，然后调用 App 层服务完成编排。

## 文件职责

- `market_discovery_worker.py`：后台维护链路，发现 active events / markets，触发分类。
- `market_ws_worker.py`：交易主链路，接收 target market orderbook 和 best bid / ask 更新。
- `user_ws_worker.py`：交易主链路，接收订单、成交、持仓生命周期事件。
- `trading_decision_worker.py`：交易主链路，消费交易事件并生成扩展决策和订单意图。
- `reconcile_worker.py`：后台维护链路，周期校准权威状态，必要修复动作升级到关键修复链路或交易主链路。
- `persistence_worker.py`：异步支撑链路，消费 outbox 异步落库；审计/outbox 记录保留原事件，宽表快照只物化 payload 中明确携带的结构化对象。

## 允许依赖

- `polymarket_trader.app` 应用服务。
- `polymarket_trader.runtime` 队列和调度。
- `polymarket_trader.observability` 指标和 trace。

## 禁止行为

- Worker 不直接实现业务规则。
- Worker 不直接调用 Polymarket SDK；需要通过 app / infra 边界。
- 交易主链路 worker 不等待后台维护或异步支撑 worker 释放资源。
- Persistence Worker 不反向调用 Strategy 或 Order Executor。
- Reconciler 不在批量扫描任务中长时间持有交易状态写锁。

## 输入与输出

- 输入：事件总线消息、WebSocket 消息、周期调度触发和 outbox 待消费事件。
- 输出：对 App 服务的调用、新的运行时事件，以及持久化或审计副作用。
