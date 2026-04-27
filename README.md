# Sports Tail Trader

这是一个面向体育类扫尾机会的 Polymarket 后端运行时。
策略实现位于顶层 `src/strategies/`；执行、风控、恢复、审计和管理面由框架统一处理。

## 项目方向

- 聚焦体育类临近结束市场，优先研究快结束阶段胜率 95% 以上时的买入机会。
- 具体胜率来源、时间窗口、盘口过滤、仓位、滑点和退出条件应沉淀在 `src/strategies/current/`，不直接扩散到框架主链路。
- 在策略验证闭合前，不把“95% 以上胜率就买”硬编码为绕过 allocator / risk / executor 的下单旁路。

## 快速上手

先看这几个文件：

- 改 discovery 粗筛：[src/strategies/current/config.py](./src/strategies/current/config.py) 的 `discovery_title_searches` / `discovery_tag_slugs`，或 [src/strategies/current/strategy.py](./src/strategies/current/strategy.py) 的 `discovery_queries()`；官方 Gamma Events keyset 文档见 <https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- 改市场筛选：[src/strategies/current/universe.py](./src/strategies/current/universe.py)
- 改策略配置：[src/strategies/current/config.py](./src/strategies/current/config.py)
- 改分配、入场、退出：[src/strategies/current/trading.py](./src/strategies/current/trading.py)
- 改恢复和保留跟踪：[src/strategies/current/recovery.py](./src/strategies/current/recovery.py) / [src/strategies/current/tracking.py](./src/strategies/current/tracking.py)
- 看策略装配入口：[src/strategies/current/strategy.py](./src/strategies/current/strategy.py)
- 看扩展 API 契约：[src/polymarket_trader/extension_api](./src/polymarket_trader/extension_api)

常用命令：

- 跑回归：`pytest -q`
- 跑静态检查：`ruff check .`
- 验证 PostgreSQL 链路：`pytest tests/infra/test_postgres_integration.py -q`
- 启动前端管理台（开发模式）：`npm --prefix frontend install && npm --prefix frontend run dev`
- 一键启动后台和前端页面：`./start_all.sh`
- 停止后台和前端页面：`./stop_all.sh`
- 构建可运行发布包：`./build_dist.sh --archive`

## 核心规则

- 单个 runtime 通过 `EXTENSION_MODULE` 装配一个业务扩展；默认扩展位于 `src/strategies/current/`。
- 交易标的、入场规则、退出规则和 universe 选择由业务扩展定义。
- 买入侧不得保留长期 resting BUY order；一旦发现 open BUY 异常，必须立即进入 cancel / reconcile 修复流程。
- 资金分配、订单执行、恢复、审计和管理面必须经过统一服务编排。
- 数据库用于审计、复盘、调试和恢复参考，不作为交易状态唯一真相来源。
- 交易主链路优先级最高，不得被 Admin 查询、数据库写入、日志落盘、后台 discovery 扫描、报表或低优先级 reconcile 阻塞。

## 架构分层

代码采用 `src` layout，主包是 [src/polymarket_trader](./src/polymarket_trader/README.md)。

| 层级 | 目录 | 主要职责 |
| --- | --- | --- |
| Interfaces | [api](./src/polymarket_trader/api/README.md) | Admin API、健康检查 |
| Application | [app](./src/polymarket_trader/app/README.md) | 用例编排，组合 domain 与 infra |
| Domain | [domain](./src/polymarket_trader/domain/README.md) | 分类、分配、风控、策略、订单和持仓规则 |
| Infrastructure | [infra](./src/polymarket_trader/infra/README.md) | Polymarket、数据库、outbox 和外部 I/O 适配 |
| Observability | [observability](./src/polymarket_trader/observability/README.md) | 审计、trace、指标 |
| Runtime | [runtime](./src/polymarket_trader/runtime/README.md) | 事件总线、状态注册表、调度、supervisor |
| Frontend | [frontend](./frontend/README.md) | 管理台前端、通用 UI 与策略展示扩展槽 |
| Extension API | [extension_api](./src/polymarket_trader/extension_api) | 业务扩展 hooks、上下文对象、决策对象、manifest 和配置加载 |
| Strategies | [strategies](./src/strategies) | 顶层策略实现包 |
| Workers | [workers](./src/polymarket_trader/workers/README.md) | 常驻后台任务 |
| Tests | [tests](./tests/README.md) | 单元、编排和基础设施测试 |

## 文档入口

- [Codex Rules](./AGENTS.md)：仓库级开发规则、分层边界、结构改造准则和测试要求。
- [使用说明书](./docs/使用说明书.md)：日常启动、页面操作、人工确认、验收和常见问题。
- [业务需求](./docs/业务需求.md)：体育类扫尾交易的业务目标、盈利逻辑和操作规则。
- [体育扫尾策略目标设计](./docs/体育扫尾策略设计.md)：从业务需求抽象出的目标模型、统一链路和策略边界。
- [体育扫尾完整接线设计](./docs/体育扫尾完整接线设计.md)：后续实现前必须遵守的文件级接线、框架边界和管理台边界。
- [体育直播状态输入设计](./docs/体育直播状态输入设计.md)：直播比分、阶段、剩余时间等运行时输入的接线边界。
- [二次开发计划](./docs/二次开发计划.md)：围绕体育扫尾策略的后续开发里程碑、验收顺序和文档维护要求。
- [开发实施计划](./docs/开发实施计划.md)：当前可执行开发切片、任务拆分和验证命令。
- [开发进度](./docs/开发进度.md)：开发状态、验证结果、当前决策和遗留风险。
- [策略验收与风控清单](./docs/策略验收与风控清单.md)：候选、价格、流动性、风控、执行权限和退出检查项。
- [框架设计与边界](./docs/设计文档.md)：运行时装配、核心链路、业务扩展契约、前端边界和二次开发入口。
- [市场发现链路说明](./docs/市场发现链路.md)：扩展 discovery hook、分页扫描、WS 热发现和运行时状态。
- [配置文档](./docs/config.md)：`.env`、策略模块加载和策略侧配置文件。
- [API 文档](./docs/api.md)：Admin API。
- [故障处理](./docs/runbook.md)：启动检查、异常排查顺序和人工恢复路径。

## 策略与测试入口

- 策略 manifest：[src/strategies/current/manifest.py](./src/strategies/current/manifest.py)
- 运行入口：[src/strategies/current/strategy.py](./src/strategies/current/strategy.py)
- 策略配置：[src/strategies/current/config.py](./src/strategies/current/config.py)
- discovery query / universe：[src/strategies/current/strategy.py](./src/strategies/current/strategy.py) / [src/strategies/current/config.py](./src/strategies/current/config.py) / [src/strategies/current/universe.py](./src/strategies/current/universe.py)
- 交易决策：[src/strategies/current/trading.py](./src/strategies/current/trading.py)
- 恢复 / 跟踪：[src/strategies/current/recovery.py](./src/strategies/current/recovery.py) / [src/strategies/current/tracking.py](./src/strategies/current/tracking.py)
- 扩展 API 契约：[src/polymarket_trader/extension_api/hooks.py](./src/polymarket_trader/extension_api/hooks.py) / [src/polymarket_trader/extension_api/decisions.py](./src/polymarket_trader/extension_api/decisions.py)

## 模块接口原则

- Domain 层只能使用系统内部 DTO，不依赖 FastAPI、SQLAlchemy、Polymarket SDK、WebSocket client 或环境变量。
- App 层负责编排用例，不直接拼接 Polymarket payload，不绕过 Domain 的规则对象。
- Infra 层负责外部协议适配，必须把 Polymarket / DB / WS 的响应转换为内部 DTO 后再向上返回。
- Order Executor 是唯一允许创建、签名、提交、取消和替换订单的模块。
- Risk Manager 是任何下单前的强制门禁。新增下单入口必须显式经过 Risk Manager。
- Admin API 只能调用应用服务，不能直接碰交易热状态写锁，不能绕过风控。
- Persistence Worker 和数据库写入只能异步承接 outbox 事件，不能反向阻塞交易主链路。
