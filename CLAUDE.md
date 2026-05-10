# Claude Rules

Sports Tail Trader 是一个跑实盘资金的 Polymarket 体育扫尾交易后端运行时。错误的改动会直接导致下错单、爆仓、资金错配或绕开风控，所以本文先列**不可违反的硬约束**，再讲结构、改动落点与协作。

跨模块、可长期复用的规则写在这里；策略阈值、关键词、仓位参数、市场过滤等易变细节以 `src/strategies/current/` 和 [docs/](./docs/) 为准。

## 1. 项目速览

- 主包 [src/polymarket_trader](./src/polymarket_trader)：分 `api / app / domain / infra / observability / runtime / workers / extension_api`。
- 策略实现 [src/strategies/current](./src/strategies/current)：当前体育扫尾策略；`EXTENSION_MODULE` 装配单一业务扩展。
- 前端管理台 [frontend](./frontend)：Admin UI 与策略展示扩展槽。
- 长期文档 [docs/](./docs)：业务需求、设计文档、市场发现链路、runbook 等；接手任务前先看 [docs/开发进度.md](./docs/开发进度.md) 了解当前状态。
- Python 3.12，`src` layout，FastAPI + asyncpg + SQLAlchemy + py-clob-client-v2 + websockets。

## 2. 常用命令

- 跑回归：`pytest -q`
- 静态检查：`ruff check .`
- 类型检查（按需）：`mypy src`
- 单跑 PG 集成：`pytest tests/infra/test_postgres_integration.py -q`
- 启动后台 + 前端：`./start_all.sh`；停止：`./stop_all.sh`
- 仅前端开发：`npm --prefix frontend install && npm --prefix frontend run dev`
- 构建发布包：`./build_dist.sh --archive`

提交前最少跑 `pytest -q` 和 `ruff check .`；改动触及交易/风控/资金/恢复路径时，必须本地跑相关测试再交付。

## 3. 不可违反的硬约束

**交易安全**：

- `OrderExecutor` 是唯一允许创建、签名、提交、取消、替换订单的模块。任何新下单入口都必须最终走它。
- `RiskManager` 是任何下单前的强制门禁。新增下单入口必须显式经过它，禁止旁路。
- 业务扩展只输出框架定义的决策对象，不直接调用交易客户端。
- 买入侧不得保留长期 resting BUY order；发现 open BUY 异常必须立即 cancel / reconcile 修复。
- Admin API 不是自动下单入口，也不是交易客户端直连入口。
- `Persistence / outbox / 审计` 只能异步承接副作用，不得反向阻塞交易主链路。

**状态真相**：

1. Polymarket 实时事件与权威快照
2. 本地内存状态
3. PostgreSQL 审计与快照

数据库只用于审计、复盘、查询和恢复参考，**不是交易状态唯一真相来源**。

**Domain 纯度**：Domain 不依赖 FastAPI / SQLAlchemy / Polymarket SDK / WebSocket client / 环境变量 / 数据库查询。Domain 内金额和价格用 `Decimal`，不用浮点。

## 4. 交易主链路

```text
event -> TradingDecisionWorker -> TradingDecisionService -> PortfolioAllocator -> RiskManager -> TradingService -> OrderExecutor -> outbox/audit
```

App 层只编排决策与执行流程，不维护绕过 allocator / risk / executor 的第二套交易事实。

## 5. 改动落点

| 改的东西 | 落到哪里 |
| --- | --- |
| 策略规则、筛选语义、定价、仓位、恢复策略 | `src/strategies/current/` |
| 通用契约、上下文对象、扩展接口 | `src/polymarket_trader/extension_api/` |
| 交易/恢复/管理操作编排 | `src/polymarket_trader/app/` |
| 外部 API、数据库、WS、outbox 适配 | `src/polymarket_trader/infra/` |
| 运行时状态、调度、队列、supervisor | `src/polymarket_trader/runtime/` 或 `workers/` |
| 人工查询和受控操作入口 | `src/polymarket_trader/api/` |
| 前端管理台、UI 组件、策略展示槽 | `frontend/` |

如果某个字段、状态或配置只服务当前策略，**留在策略包内**，不进 `domain` / `extension_api` / 全局 `Settings`。

## 6. 分层边界

```text
api / workers -> app -> domain
app -> infra / runtime / observability
infra -> domain
runtime -> domain
```

- **Domain**：纯业务规则、领域模型、内部 DTO。
- **App**：用例编排，不保存第二份业务真相，不硬编码具体策略阈值语义。
- **Infra**：外部协议适配，外部 payload 必须先转内部 DTO 再向上返回。
- **Worker**：搬运消息和调度，不直接实现业务规则，不直接调 Polymarket SDK。
- **API**：参数校验、调用应用服务、响应序列化；不直接操作交易客户端或运行时热状态写锁。

## 7. 状态与并发

- 交易主链路优先级最高，实时判断优先用内存状态；权威修正依赖 WS 和 REST。
- Admin 和 Persistence 读快照，不直接持有热状态写锁。
- 状态访问按 `condition_id` / `token_id` 分片，不用全局大锁。
- 非关键锁等待超时后跳过并告警，不能无限等待。
- 阻塞 I/O 与 CPU 密集任务必须离开交易主事件循环。
- 数据库写入、日志、全量扫描、报表和 Admin 大查询不得反向阻塞 P0 交易路径。
- 后台任务只能异步承接副作用或做恢复参考，不能决定交易主链路是否继续执行。
- Reconciler 不在批量扫描里长时间持有交易状态写锁。
- 队列必须有容量上限、可观测性和降级路径。

## 8. 设计与改造取向

- 不采用"补丁式开发"：面向完整目标形态建模，不为了表面兼容堆叠旁路、特殊分支或一次性修补；即使当前只启用部分能力，也不按单一场景写死流程、接口、状态或配置。
- 结构性改造一次切到目标形态，不保留长期双路径；主路径所需接线在同一轮改动内闭合。
- 不引入只服务过渡期的配置项、状态字段或旁路逻辑。双路径适配层只在确有外部契约约束时保留，并记录原因、边界和移除条件。
- 数据库 schema 调整以新模型为准，不为旧表/旧字段做兼容性包容；旧结构直接删除后用新结构。
- 调用侧使用非目标命名时优先改调用侧，不在框架层长期保留同义字段、别名、包装函数或重复枚举。
- 发现架构边界不清或职责放错时，先提调整方案再实现，不在不合适的结构上叠加复杂度。
- 新增调度器、扫描器或后台任务时，运行时状态必须暴露为可观测快照，便于 supervisor / admin 查询。

## 9. 实盘策略演化

- 实盘策略进化是持续闭环：跑实盘观察 → 定位瓶颈 → 改进策略或链路 → 验证测试 → 重启/继续观察 → 再次思考改进。除非用户明确要求停止/暂停/切换，agent 不应在单轮观察或单次改动后主动结束。
- 演化目标范围是**整个体育市场**，不是 `single_game`。`single_game` 只表示"可用单场直播比分源评估"的市场家族；系列赛、冠军归属、赛季归属、球员转会/奖项等长期市场也属于目标范围，应逐步补专用数据源、定价模型和风控模型。
- 新增或发现的市场不得因为缺少专用直播源/盘口模型/执行规则而长期静默忽略。Moneyline、Totals、Spreads、Yes/No prop、分节/分盘/局数/让分/特殊事件等盘口至少要统一建模、纳入候选诊断、给出可审计拒绝或 record-only 原因；只有具备明确胜率判断、资金效率和风控闭环的盘口才能进入自动执行。
- 优化优先级：发现速度、盘口热态更新速度、决策延迟、成交链路延迟。每次优化都要保留可观测指标、拒绝原因和测试验证；不为速度牺牲风控、幂等和可审计性。
- 改动若扩大交易风险或改变资金暴露模型，必须先说明影响范围与验证方式。
- 具体落点仍按 §5：实现落策略包或合适分层内，不绕过交易主链路、风控和审计。

## 10. 配置、命名与建模

- `.env` 和 `Settings` 只承载框架运行参数；策略参数不进框架必填环境变量，写在策略包内或策略自己的配置加载方式里。
- 契约字段命名单义，避免一套对外、一套对内的长期双命名层。
- Domain 内金额/价格用 `Decimal`。
- 策略常量集中管理，避免散落硬编码。
- 拒绝原因必须可审计，不能只返回 `False`；跳过、降级、恢复动作必须保留可审计原因，不静默忽略关键失败。

## 11. 测试

- 测试用于验证目标实现，不为让中间阶段通过引入临时补丁、兼容旁路或特殊分支；功能未闭合可在完整开发完成后统一验证。
- 不为让测试通过编写只覆盖该测试的最小实现。目标边界、主路径接线、长期模型未厘清时，宁可暂停测试和实现，补齐设计后再按目标形态实现。
- **必须补测试**：自动下单、风控、资金/仓位计算、订单幂等、恢复、reconcile 的行为改动，优先覆盖核心失败场景。
- **可不补测试**：纯查询、文档、日志、策略参数调整。

## 12. 注释与文档

- 注释只写非显然的 WHY：隐含约束、不变量、绕过特定 bug 的处理、会让读者意外的行为。命名能表达的 WHAT 不另加注释。
- 涉及风控、状态机、资金计算、并发边界等关键业务步骤的边界条件，应当用中文写明。
- 新增接口、配置项、运行时状态或人工操作入口时，同步更新对应文档。
- 不写一次性执行清单、改造计划或已被代码取代的历史表述；这些走 PR / 进度文档。

## 13. 多 agent 协作

- 多模块、可并行或需要不同视角复核的任务，主动考虑用 Explore / Plan / general-purpose 等 subagent 拆分推进。
- 并行前先明确每个 agent 的职责边界、读写范围和预期输出；写入文件范围尽量不重叠。
- 总协调 agent 负责拆分、整合、复核关键改动；子 agent 输出不能直接当最终事实，重要结论必须本地验证。
- 紧耦合、关键路径、需要统一架构判断的工作不强行并行。

## 14. 常见误区

- 在 route 里写交易逻辑。
- 在 worker 里复制 discovery / 扩展业务判断。
- 在 framework 层加入策略专属字段或阈值。
- 在 Domain 或内部接口中保留长期同义命名以"减少改动"。
- 让数据库、持久化、日志或报表路径决定交易热路径是否能继续运行。
- 把 `single_game` 当成业务范围限制。
- 让 resting BUY 长期挂着不修复。
