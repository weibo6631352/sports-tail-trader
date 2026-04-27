# polymarket_trader 包说明

该包是 Polymarket 扩展交易框架的主源码包。包内按架构分层组织，目的是让任何人接手一个目录时，都能判断自己的改动应该放在哪一层、能调用谁、不能调用谁。

## 子目录索引

| 目录 | 角色 | 说明 |
| --- | --- | --- |
| [api](./api/README.md) | Interface | FastAPI Admin API 和健康检查 |
| [app](./app/README.md) | Application | 用例编排与服务协调 |
| [domain](./domain/README.md) | Domain | 纯业务规则和内部 DTO |
| [infra](./infra/README.md) | Infrastructure | Polymarket、DB、outbox、外部 I/O |
| [observability](./observability/README.md) | Observability | 审计、指标、trace |
| [runtime](./runtime/README.md) | Runtime | 队列、registry、调度、supervisor |
| [workers](./workers/README.md) | Workers | 常驻任务 |

## 依赖方向

推荐方向：

```text
api / workers -> app -> domain
app -> infra / runtime / observability
infra -> domain
runtime -> domain
observability -> domain-friendly DTO or primitives
```

禁止方向：

```text
domain -> api / app / infra / runtime / workers
infra/db -> api
api/routes -> infra/polymarket directly
workers -> Polymarket SDK directly
```

## 全局接口原则

- 对外交易动作统一通过 Order Executor。
- 下单前统一通过 Risk Manager。
- 状态热路径由 runtime 维护，外部查询读取快照或仓储数据。
- 审计事件由 observability / outbox 统一承接，不在业务路径散落手写日志。
- Polymarket SDK 对象不得泄漏到 domain。
