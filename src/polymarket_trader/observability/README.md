# observability 目录说明

该目录存放审计、指标和 trace 能力。它的目标是让每一次 market discovery、风控、订单、成交和修复动作都能被追踪，同时不阻塞交易热路径。

## 职责

- 生成并传播 `trace_id`。
- 构建结构化审计事件。
- 记录交易延迟、队列深度、锁等待、执行器等待等指标。
- 通过内存型 `MetricsRegistry` 向 Supervisor、`/ready` 和 Admin API 暴露轻量快照；指标注册表只保留运行时快照，不承载业务含义。
- 通过异步队列日志输出结构化、脱敏后的日志记录。
- 为 outbox 和 Persistence Worker 提供统一事件模型。

## 关键指标

- `orderbook_event_received_at`。
- `entry_signal_at`。
- `risk_passed_at`。
- `order_signed_at`。
- `order_submitted_at`。
- `order_ack_at`。
- `entry_signal_to_submit_ms`。
- `trading_queue_depth`。
- `trading_lock_wait_ms`。
- `executor_queue_wait_ms`。

## 允许依赖

- 标准库 logging / contextvars / uuid。
- metrics 后端适配库。
- 内部 DTO 或基础类型。

## 禁止行为

- 不在交易主链路同步写 PostgreSQL。
- 不在交易主链路同步刷磁盘日志。
- 不记录密钥、签名 payload、私钥或未脱敏 raw response。
- 不让指标聚合反向阻塞 Strategy / Order Executor。

## 接口契约

- 审计事件必须包含 `trace_id`、事件类型、时间、状态和原因。
- raw response 必须限长、脱敏，并根据优先级进入对应 outbox 队列。
- trace id 必须能贯穿 discovery、signal、risk、order、fill、sell、reconcile。
