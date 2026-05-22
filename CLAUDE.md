# Claude Rules

Sports Tail Trader 是一个跑实盘资金的 Polymarket 体育扫尾交易后端运行时。错误的改动会直接导致下错单、爆仓、资金错配或绕开风控，所以本文先列**不可违反的硬约束**，再讲结构、改动落点与协作。

跨模块、可长期复用的规则写在这里；策略阈值、关键词、仓位参数、市场过滤等易变细节以 `src/strategies/current/` 和 [docs/](./docs/) 为准。

## 1. 项目速览

- 主包 [src/polymarket_trader](./src/polymarket_trader)：分 `api / app / domain / infra / observability / runtime / workers / extension_api`。
- 策略实现 [src/strategies/current](./src/strategies/current)：当前体育扫尾策略；`EXTENSION_MODULE` 装配单一业务扩展。
- 前端管理台 [frontend](./frontend)：Admin UI 与策略展示扩展槽。
- 长期文档 [docs/](./docs)：业务需求、设计文档、市场发现链路、runbook 等；当前状态以代码和 `git log` 为准。
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
event -> TradingDecisionWorker -> TradingDecisionService -> EntryPlanner -> RiskManager -> TradingService -> OrderExecutor -> outbox/audit
```

- `EntryPlanner`（`app/entry_planner.py`）产出 `AllocationPlan`，承担"组合预算分配 + 入场计划构造"职责。
- `RiskManager`（`domain/risk.py`）是强制门禁；`TradingService` 编排执行；`OrderExecutor` 是唯一下单入口。

App 层只编排决策与执行流程，不维护绕过 entry planner / risk / executor 的第二套交易事实。

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
- P0 交易热路径（decision → risk → executor）只允许同步执行：风控判定、内存状态读写、订单签名/提交、向 outbox `put_nowait` 投递事件。日志、DB 写入、metric 上报、payload 序列化落盘等耗时副作用必须延迟到事务结束后由 outbox / persistence worker / 后台消费者异步承接，不得在主链路任何中间步骤 `await` 完成；关键拒绝原因仅以"原因字符串 + 关键标识"随事件带出，不得在主链路同步写大 payload。关键锁（`_idempotency_lock` 等）内禁止 IO `await`、日志和大对象序列化。
- 后台任务只能异步承接副作用或做恢复参考，不能决定交易主链路是否继续执行。
- Reconciler 不在批量扫描里长时间持有交易状态写锁。
- 队列必须有容量上限、可观测性和降级路径。

## 8. 设计与改造取向

**目标永远是最优方案，不是最小改动，也不是兼容旧逻辑**。每次改动按目标形态实现，不在中间形态停留；遇到需要重构、重写、删除的旧代码或旧结构直接替换，不为了"减少改动"保留次优实现。

- 不采用"补丁式开发"：面向完整目标形态建模，不为了表面兼容堆叠旁路、特殊分支或一次性修补；即使当前只启用部分能力，也不按单一场景写死流程、接口、状态或配置。
- 结构性改造一次切到目标形态，不保留长期双路径；主路径所需接线在同一轮改动内闭合。
- 不引入只服务过渡期的配置项、状态字段或旁路逻辑。双路径适配层只在确有外部契约约束时保留，并记录原因、边界和移除条件。
- 数据库 schema 调整以新模型为准，不为旧表/旧字段做兼容性包容；旧结构直接删除后用新结构。**不写「迁移 / backfill / 向后兼容」代码**：不写一次性 backfill 脚本，不留 nullable 兼容列，不在代码层做「读旧写新」或双格式解析。旧数据保留由运维侧另行处理，不进应用层。
- 调用侧使用非目标命名时优先改调用侧，不在框架层长期保留同义字段、别名、包装函数或重复枚举。
- 发现架构边界不清或职责放错时，先提调整方案再实现，不在不合适的结构上叠加复杂度。
- 新增调度器、扫描器或后台任务时，运行时状态必须暴露为可观测快照，便于 supervisor / admin 查询。

**不留技术债务**——本次改动不留新债，遇到旧债随手清。

- 当场消除：`TODO/FIXME/HACK` 标记；`getattr/hasattr` duck-typing 兜底；同义双命名 / 别名；过渡期 fallback 字段；跨边界双口径（如前端拼字符串绕过缺字段后端）。
- 提交前自检 `# pragma: no cover` / `# type: ignore` / `as unknown` / `as Any` 等旁路检查标记，有则先清。
- P0 路径新增逻辑必须同时补契约测试；文档与代码漂移视同 bug。
- 不得已留下的中间态（shim、双路径、过渡命名）必须在 commit message 注明"何时清理 + 触发条件"。
- 任务结束债务清单 = 0；遗留项落独立 PR 计划，不口头交接。

## 9. Goalserve 数据接口规则

**`goalserve/full_package_feed.txt` 是我们订阅的 Goalserve 完整接口权威清单，任何涉及体育数据源的改动都必须先查阅它。**

### 接口分类与对应实现

| 类型 | 端点形式 | 认证 | 实现位置 |
|------|---------|------|---------|
| Inplay 实时赔率+比分（WebSocket 推送）| `live.goalserve.com/ws/{sport}?tkn={jwt}` | JWT token（`live.goalserve.com/api/v1/auth/gettoken` 换取）| `infra/sports/goalserve_client.py` |
| Livescore 实时比分（getfeed）| `getfeed/{key}/{sport}/home?json=1` | API key | `infra/sports/goalserve_livescore_client.py` |
| 赛前赔率（Pregame Odds, GZIP）| `getfeed/{key}/getodds/soccer?cat={sport}_10` | API key | `infra/sports/goalserve_pregame_client.py` |

### 已覆盖的运动

**Inplay feed（`goalserve_sports` 配置）**：basketball、soccer、hockey、baseball、tennis、esports、amfootball、volleyball — Goalserve inplay 覆盖的 8 个运动全部启用。

**Livescore getfeed（`goalserve_livescore_sports` 配置）**：cricket、handball、rugby、boxing、mma、golf\_pga/dp/liv/lpga、horse\_racing\_us/uk/au/hk、f1、motogp。

**Pregame odds（`goalserve_pregame_sports` 配置）**：soccer、basketball、tennis、hockey、baseball、amfootball、esports、mma、cricket、rugby、volleyball、handball、boxing、darts、table\_tennis、futsal、rugbyleague。

### 扩展原则

- Goalserve 支持的运动和接口以 `goalserve/full_package_feed.txt` 为准；发现文件里有但代码里未接入的接口，视为缺口，应补实现或记录明确的跳过原因。
- Livescore parsers 基于格式推断实现，首次真实调用后需对照实际 response 校准字段——尤其是 golf、horse\_racing、f1 这三类。校准后更新 parser，不保留"推断注释"。
- 新增运动 parser 必须：① domain 加对应 GameState；② parsers 文件加 sport parser；③ `_SPORT_PARSERS` / `_SPORT_PATHS` 注册；④ config 默认值加入；⑤ 补测试。缺任何一步均为未完成。
- API key 只存 `.env` 的 `GOALSERVE_API_KEY`，不进代码仓库和文档。
- Pregame 数据量极大（>100MB），默认关闭（`goalserve_pregame_enabled=false`）；启用时必须用 `ts` 增量拉取，不允许无限循环全量请求。
- Inplay WS 每个运动维护独立后台 Task；stp=99（removed）的赛事立即停止；`events_seen=0` 且 `health=success_empty` 表示 WS 连通但无 inplay 数据——此时以 livescore getfeed 为主要比分源，不影响交易。
- **无效接口处理原则**：发现代码中有 403/无数据接口，查阅 `goalserve/full_package_feed.txt` 和代码确认实际有效替代；有替代则更新接口和文档，无替代则删除死代码并在此注明原因，不保留会误导排查的旧 URL。

## 10. 实盘策略演化

- 实盘策略进化是持续闭环：跑实盘观察 → 定位瓶颈 → 改进策略或链路 → 验证测试 → 重启/继续观察 → 再次思考改进。除非用户明确要求停止/暂停/切换，agent 不应在单轮观察或单次改动后主动结束。
- 演化目标范围是**整个体育市场**，不是 `single_game`。`single_game` 只表示"可用单场直播比分源评估"的市场家族；系列赛、冠军归属、赛季归属、球员转会/奖项等长期市场也属于目标范围，应逐步补专用数据源、定价模型和风控模型。
- 新增或发现的市场不得因为缺少专用直播源/盘口模型/执行规则而长期静默忽略。Moneyline、Totals、Spreads、Yes/No prop、分节/分盘/局数/让分/特殊事件等盘口至少要统一建模、纳入候选诊断、给出可审计拒绝或 record-only 原因；只有具备明确胜率判断、资金效率和风控闭环的盘口才能进入自动执行。
- 优化优先级：发现速度、盘口热态更新速度、决策延迟、成交链路延迟。每次优化都要保留可观测指标、拒绝原因和测试验证；不为速度牺牲风控、幂等和可审计性。
- 改动若扩大交易风险或改变资金暴露模型，必须先说明影响范围与验证方式。
- 具体落点仍按 §5：实现落策略包或合适分层内，不绕过交易主链路、风控和审计。

## 11. 配置、命名与建模

- `.env` 和 `Settings` 只承载框架运行参数；策略参数不进框架必填环境变量，写在策略包内或策略自己的配置加载方式里。
- 契约字段命名单义，避免一套对外、一套对内的长期双命名层。
- Domain 内金额/价格用 `Decimal`。
- 策略常量集中管理，避免散落硬编码。
- 拒绝原因必须可审计，不能只返回 `False`；跳过、降级、恢复动作必须保留可审计原因，不静默忽略关键失败。

## 12. 测试

- 测试用于验证目标实现，不为让中间阶段通过引入临时补丁、兼容旁路或特殊分支；功能未闭合可在完整开发完成后统一验证。
- 不为让测试通过编写只覆盖该测试的最小实现。目标边界、主路径接线、长期模型未厘清时，宁可暂停测试和实现，补齐设计后再按目标形态实现。
- **必须补测试**：自动下单、风控、资金/仓位计算、订单幂等、恢复、reconcile 的行为改动，优先覆盖核心失败场景。
- **可不补测试**：纯查询、文档、日志、策略参数调整。

## 13. 注释与文档

- 注释只写非显然的 WHY：隐含约束、不变量、绕过特定 bug 的处理、会让读者意外的行为。命名能表达的 WHAT 不另加注释。
- 涉及风控、状态机、资金计算、并发边界等关键业务步骤的边界条件，应当用中文写明。
- 新增接口、配置项、运行时状态或人工操作入口时，同步更新对应文档。
- 改造计划、实施清单、进度日志等过程性文档可以在任务推进期间作为工作工具存在；任务完成后必须主动清理，不长期保留为历史档案。完成态由代码 + `git log` 表达。
- 规则类约束统一沉到本文件，不散落到其他文档。

## 14. 多 agent 协作

- 多模块、可并行或需要不同视角复核的任务，主动考虑用 Explore / Plan / general-purpose 等 subagent 拆分推进。
- 并行前先明确每个 agent 的职责边界、读写范围和预期输出；写入文件范围尽量不重叠。
- 总协调 agent 负责拆分、整合、复核关键改动；子 agent 输出不能直接当最终事实，重要结论必须本地验证。
- 紧耦合、关键路径、需要统一架构判断的工作不强行并行。

## 15. 常见误区

- 在 route 里写交易逻辑。
- 在 worker 里复制 discovery / 扩展业务判断。
- 在 framework 层加入策略专属字段或阈值。
- 在 Domain 或内部接口中保留长期同义命名以"减少改动"。
- 让数据库、持久化、日志或报表路径决定交易热路径是否能继续运行。
- 把 `single_game` 当成业务范围限制。
- 让 resting BUY 长期挂着不修复。

## 16. Goalserve 数据缺失排查原则

Goalserve 覆盖面极广，任何主要联赛/赛事在 Goalserve 上几乎必然有数据。如果发现某场比赛在 Goalserve 中找不到对应的实时直播数据（`missing_live_game_state`、源匹配失败等），**首先假定是我们这边的问题**，而不是 Goalserve 没数据，需要排查以下几点：

1. **路由问题**：该运动的 feed 路径是否正确配置（inplay vs livescore vs getfeed）？
2. **认证/网络**：inplay WS token 是否正常刷新（`live.goalserve.com/api/v1/auth/gettoken`，429 = 系统已占用 token 槽）？livescore API key 是否有效？
3. **解析问题**：parser 是否正确处理了该运动的 XML/JSON 格式？是否静默丢弃了数据？
4. **名称匹配**：团队名拼写/格式是否导致市场文本匹配失败？
5. **时区/日期**：市场 slug 日期是否与事件实际 UTC 日期不一致（如午夜场次）？
6. **配置未启用**：该运动是否被加入了 `GOALSERVE_LIVESCORE_SPORTS`（或对应 `.env` 变量）？

确认非 Goalserve 数据问题后，才考虑使用其他数据源作为补充。

## 17. 量化交易策略方向

本系统的战略目标不限于"扫尾"（tail），而是整个体育市场的**安全量化交易**。扫尾盘是当前已实现的策略之一，但以下方向同样在目标范围内：

### 买卖经验与自动化原则

**买入原则**：
- 有明确优势（比赛状态支持、胜率判断可信）才买；无明确边际不做无依据的仓位
- 入场价要合理（不追高）：已有 `max_entry_price` 门控

**卖出原则**（避免过度持有高期望资产，也避免等待结算时输掉全部）：
- **止盈（Take-profit）**：持仓浮盈达到目标倍数（如买价 × 1.6）时，应主动挂出 GTC 卖单锁定收益，而不是等待结算
- **锁定确定性**：价格超过 0.92 时（接近锁定），应挂出 GTC 卖单，避免等待结算期间出现黑天鹅
- **市场期望过高时减仓**：如果持仓后市场价格大幅高于我们的判断（可能是过度反应），考虑部分平仓降低集中度
- **止损（Stop-loss）**：直播源显示赔率突然大幅下移（持仓反向，价格跌破买入价 × 0.5 或比赛明显逆转）时，应主动卖出止损，宁可只损失部分本金，不可等待结算归零。只持仓等结算的逻辑在输的方向上是 0 回收。
- **实现路径**：通过 `auto_exit_enabled=True`、`profit_take_overlay_enabled=True`+`profit_take_target_price`、以及 recovery 侧的持仓监控实现自动化；不手动干预

**直播源赔率 vs Polymarket 价格差价**（重要机会，须认真对待）：
- Goalserve inplay/livescore 提供的实时赔率隐含了博彩市场对事件概率的真实估计；当 Goalserve 隐含概率与 Polymarket 市场价格存在显著差价时，这是一个明确的 edge 信号
- 实现路径：解析 Goalserve inplay 赔率字段（`goalserve_moneyline`/`goalserve_totals`/`goalserve_spread`），换算成隐含概率，与 Polymarket best_ask 比较；差价超过阈值（如 5 个百分点）时触发候选
- 优先级：live inplay 赔率 > pregame 赔率；inplay 赔率在比赛中动态更新，是最强信号
- 与扫尾策略的关系：扫尾策略依赖"结果已接近锁定"的确定性；赔率差价策略可更早入场，依赖"Polymarket 定价落后于博彩市场"的效率差
- 关键数据：Goalserve `goalserve_moneyline`/`goalserve_totals` 字段已通过 pregame client 拉取；inplay feed 中的 `odd` 字段也有赔率数据，需校验字段名和格式

**门禁调参原则**（拼概率，不过度保守）：
- 门禁（gate）的目的是防止明确错误，不是追求零风险。如果一个门禁在实盘中反复拦截了本应成交的机会，必须复盘并调整，不能因为"有审计"就放着不管。
- 复盘路径：查 `/audit-events` 和 `/candidates` 的 `rejection_reason`，结合当时的实际比分、赔率和市场结果，判断拒绝是否合理；不合理的拒绝（即如果成交是正期望的）视为门禁参数设定问题，需要调整阈值或逻辑。
- 能承担合理风险的地方要成交：在有统计优势的情况下，宁可偶尔在边界情况输一笔，也不能因为过于保守在系统性优势场景下全部放弃。
- 审计日志不是决策权威，而是复盘工具——看到哪些市场、什么价格、什么比赛状态被拒，然后和实际结果对照。

**买入复盘与止损原则**：
- 每次实盘买入事后必须复盘：查 `audit_events` 里 `order_created`/`fill_recorded` 找到实际成交，结合当时的直播状态（`sports_live_state_recorded`）、门禁决策（`allocation_decision_recorded`）判断买入是否由正确逻辑触发
- **Bug 买入立即止损**：如果发现某笔买入是由 bug（直播状态解析错误、价格计算误差、配置问题等）触发的，应立即通过 `/positions/force-exit` 或手动市价卖出止损，不要等待结算归零
- 判断标准：bug 触发 = 如果 bug 不存在，系统不会对这笔市场下单（例如 `"delayed"` 状态被误认为 LIVE 而触发的雨延场买入）
- 复盘工具：`/positions`（当前持仓）、`/fills`（成交记录）、`/audit-events`（完整审计链）、数据库 `audit_events` 表按 `condition_id` 过滤

**僵尸仓位（orphan position）处理原则**：
- 系统在 reconcile 时会扫描链上账户，发现系统未追踪的持仓（`account-exposure-*` slug），这些是孤儿仓位
- 孤儿仓位往往来自旧版本策略或手动操作，市场可能已关闭/结算，当前价值为 0
- 对于当前值 = 0 且无盘口的仓位，系统已修复（`decide_exit` 中加 `position_zero_value_no_orderbook` 跳过逻辑），不再重复挂无效 SELL 单
- 如果孤儿仓位对应已结算的胜利方向，使用 `/markets/{condition_id}/settlement` 手动触发结算；否则直接接受损失，无法强制卖出

**策略演化方向**：
- 赛前赔率（Pregame odds）的统计套利
- 盘中动量/逆转交易
- 多市场相关性对冲（系列赛/比赛结果联动）
- 任何可以建立明确概率模型的体育事件均可纳入

## 18. 直播源匹配与校准规则

**匹配校准**：
- 市场与直播数据源匹配后必须有校准检查，验证匹配是否正确（팀名、赛事、时间窗口三者对齐）。匹配结果可疑时（如团队名模糊匹配、时区偏移等）要记录可疑原因，不能静默接受。
- 匹配失败的市场（`missing_live_game_state`、源匹配失败）必须后台持久化记录，保留可审计路径：保存 condition_id、market_slug、gap_urgency、检测时间戳、失败原因。`/candidates/live-source-gaps` 端点已暴露实时快照；持久化日志用于追溯。

**无匹配时的排查义务**：
- 正在对局的运动市场若无匹配直播数据（`started_or_past_due` gap），必须排查是否是我们功能性缺失导致——参照 §16 流程（路由/认证/解析/名称匹配/时区/配置）。确认是 Goalserve 覆盖缺失才标记为 `unsupported_league`，不能跳过排查直接放弃。
- 如果发现系统中无任何正在对局的体育赛事（live states 全为 scheduled/ended），首先去 https://polymarket.com/zh/sports/live 人工核对是否有正在进行的赛事；如果 Polymarket 有而系统没有，必须定位原因（数据源断连、解析失败、名称匹配 bug 等）并修复，不能接受系统静默无感知。

**持续监控职责**：
- 实盘运行期间，live-source-gaps 出现大量 `started_or_past_due` 是告警信号，必须主动排查，不能只看拒绝率。
- 新修复的匹配逻辑（如 §16 的名称匹配 bug fix）需要在修复后观察 live states 数量是否恢复正常，以验证修复有效。
- **无交易机会时必须做差异核对**：每当系统当前没有任何可执行进场机会（candidates 全部被拒、`ready_to_trade` 为 0），不能直接判定"市场空窗"了事，必须人工核对 https://polymarket.com/zh/sports/live 上实时进行中的市场，与我方系统的 live states / candidates 逐一比对，定位差异：
  - Polymarket 有该直播市场、我方没发现 → discovery 缺口（参照 §16）。
  - 我方发现了市场但无 live state → 直播源映射缺口（参照 §16/§18）。
  - 有 live state 但 candidate 被拒 → 核对拒绝原因是否合理（参照 §17 门禁复盘），区分"真实无 edge"与"门禁过保守误杀"。
  - 只有逐项确认每个差异都有合理解释，才能判定确实无机会；任何无法解释的差异都按 bug 排查修复。
