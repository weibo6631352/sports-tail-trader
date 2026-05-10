# workers 目录说明

该目录存放常驻后台任务。Worker 负责从队列、WebSocket、定时器或外部事件中取数据，然后调用 App 层服务完成编排。

## 子包与单文件 worker

每个 worker 主体落在一个子包内，相关 helper、payload 序列化和投影器作为子模块共置；
跨 worker 共享时通过包级 import（如 `from polymarket_trader.workers.market_ws import MarketWsWorker`）。
单文件 worker 则直接放在 `workers/` 根。

### 交易主链路

- `trading_decision/`：消费交易事件并生成扩展决策和订单意图。
  - `worker.py`：worker 主体，编排 entry plan、风控和订单结果处理。
  - `event_payloads.py`：事件 payload 序列化与反序列化（snapshot/intent/review/order_result）。
  - `order_result_processor.py`：成交/取消/替换结果处理与下一跳事件发布。
  - `result.py`：worker 返回的结构化结果 DTO。
- `market_ws/`：接收 target market orderbook 和 best bid/ask 更新。
  - `worker.py`：worker 主体，订阅管理与 best ask 推送。
  - `market_updater.py`：本地 market 状态更新逻辑。
  - `book_projector.py`：盘口快照投影器。
- `user_ws/`：接收订单、成交、持仓生命周期事件。
  - `worker.py`：worker 主体，订阅与重连管理。
  - `projection.py`：订单/成交/持仓投影器与 payload 工具。

### 后台维护与异步支撑

- `reconcile/`：周期校准权威状态。
  - `worker.py`：worker 主体，周期触发 reconcile 流程。
  - `authority_refresher.py`：从 Polymarket / DB 拉权威快照并对账。
  - `action_applier.py`：把对账输出的修复动作派发到执行链路。
- `persistence/`：消费 outbox 异步落库。
  - `worker.py`：worker 主体。
  - `records.py`：把 outbox 事件转成数据库写入计划。
- `market_discovery_worker.py`：发现 active events / markets，触发分类（单文件 worker）。
- `sports_live_state_worker.py`：周期读取外部体育直播状态，匹配本地 market 并写入入场 metadata store（单文件 worker）。

## 允许依赖

- `polymarket_trader.app` 应用服务。
- `polymarket_trader.runtime` 队列和调度。
- `polymarket_trader.observability` 指标和 trace。

## 禁止行为

- Worker 不直接实现业务规则。
- Worker 不直接调用 Polymarket SDK；需要通过 app / infra 边界。
- 体育直播状态 worker 只能同步事实和发布入场重放信号，不能判断套利、计算仓位或直接提交订单。
- 交易主链路 worker 不等待后台维护或异步支撑 worker 释放资源。
- Persistence Worker 不反向调用 Strategy 或 Order Executor。
- Reconciler 不在批量扫描任务中长时间持有交易状态写锁。

## 输入与输出

- 输入：事件总线消息、WebSocket 消息、周期调度触发和 outbox 待消费事件。
- 输出：对 App 服务的调用、新的运行时事件，以及持久化或审计副作用。
