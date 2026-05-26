# Sports Tail Trader

跑实盘资金的 Polymarket 体育量化交易后端运行时。覆盖整个体育市场——Moneyline、Totals、Spreads、分节/分盘 prop、系列赛、outright 等所有能建立明确胜率判断和风控闭环的盘口家族。

策略入口位于 [src/polymarket_trader/workflow](./src/polymarket_trader/workflow)，策略 frozen 值由仓库根 [`strategy_config.toml`](./strategy_config.toml) 在启动期自动加载。

## 快速上手

```bash
./start_all.sh    # 启动后端 + 前端 + 必要时本地 PG
./stop_all.sh     # 停止
./build_dist.sh --archive    # 构建可运行发布包
```

仅前端开发：`npm --prefix frontend install && npm --prefix frontend run dev`
静态检查：`ruff check .`
类型检查（按需）：`mypy src`

默认地址：

- 前端：`http://127.0.0.1:5173/`
- 后端：`http://127.0.0.1:8000`
- 接口文档（Swagger）：`http://127.0.0.1:8000/docs`

## 核心约束

- `OrderExecutor` 是唯一允许创建、签名、提交、取消、替换订单的模块。
- `RiskManager` 是任何下单前的强制门禁。
- 数据库**只做审计**，不参与运行时决策（balance/positions/orders 等运行时状态只能来自 Polymarket WS / REST / 内存快照）。
- 买入侧不得保留长期 resting BUY。
- 交易主链路不被 Admin 查询、DB 写入、报表或低优先级 reconcile 阻塞。

完整开发规则见 [CLAUDE.md](./CLAUDE.md)。

## 架构分层

`src` layout，主包 [src/polymarket_trader](./src/polymarket_trader/README.md)。

| 层级 | 目录 | 主要职责 |
| --- | --- | --- |
| API | [api](./src/polymarket_trader/api/README.md) | Operator API、健康检查、SSE/WS |
| Application | [app](./src/polymarket_trader/app/README.md) | 用例编排，组合 domain 与 infra |
| Domain | [domain](./src/polymarket_trader/domain/README.md) | 决策模型、风控、分配、Kelly、订单/持仓规则 |
| Workflow | [workflow](./src/polymarket_trader/workflow/README.md) | TradingWorkflow、QuantDecider、各 market family 子策略 |
| Pipeline | pipeline/ | ingest（discovery/WS/直播/赔率）→ decision → execution |
| Recovery | recovery/ | 周期性 reconcile、settlement、orphan 修复 |
| Infrastructure | [infra](./src/polymarket_trader/infra/README.md) | Polymarket / Goalserve / DB / outbox 适配 |
| Observability | [observability](./src/polymarket_trader/observability/README.md) | 审计、trace、指标 |
| Runtime | [runtime](./src/polymarket_trader/runtime/README.md) | event bus、状态 store、调度、supervisor |
| Frontend | [frontend](./frontend/README.md) | 管理台前端 |

## 文档入口

- [CLAUDE.md](./CLAUDE.md)：开发规则、分层边界、不可违反约束
- [docs/config.md](./docs/config.md)：`.env` / Settings / strategy_config.toml 配置说明
- [docs/使用说明书.md](./docs/使用说明书.md)：启动、页面操作、人工确认、常见问题
- [docs/runbook.md](./docs/runbook.md)：故障处理与运维
- [docs/工作流.md](./docs/工作流.md)：主管道 + 兜底 reconcile 工作流
- [docs/市场发现链路.md](./docs/市场发现链路.md)：discovery / WS / 直播 / 赔率链路
- [docs/goalserve.md](./docs/goalserve.md)：Goalserve 数据接口
- HTTP API：以 `http://127.0.0.1:8000/docs` Swagger 和 `/openapi.json` 为准
