# Claude Rules

Sports Tail Trader 是一个跑实盘资金的 Polymarket 体育扫尾交易后端运行时。错误的改动会直接导致下错单、爆仓、资金错配或绕开风控，所以本文先列**不可违反的硬约束**，再讲结构、改动落点与协作。

跨模块、可长期复用的规则写在这里；策略阈值、关键词、仓位参数、市场过滤等易变细节以 `src/strategies/current/` 和 [docs/](./docs/) 为准。

## 0. 部署环境约束（**第一性原理，所有规则的根**）

**部署位置：中国 → 代理 → 美国 Polymarket 服务器**
**RTT：150–400ms（高且抖动）**
**出口带宽：小（受代理限速）**

### 0.0 核心矛盾：WS 是决策核心链路，所有其他网络 IO 都在和它抢带宽

**Polymarket market WS 推送是整个交易系统的决策驱动源**——每条 `book / price_change / best_bid_ask` 都可能触发一笔下单。WS 决策链路的延迟 = 我们对市场反应的速度 = 抢扫尾机会的能力。

但是：
- **带宽小**——同一根管子里跑的所有 HTTP/WS payload 互相抢
- **延迟高**——一个 200ms 的 gamma 请求期间，WS 上几条 price_change 可能还在路上
- **asyncio 单线程**——cooperative，但下载 100KB gzip payload 期间事件循环没空读 WS 队列

所以原则不是"省钱"，是 **任何非必要的网络 IO 都在 ms 级窃取 WS 决策反应速度**。这条要刻在脑子里。

### 0.1 这意味着什么

- **每多一次外部 REST 调用 = WS 上几条消息延迟解析**——尤其在 active market 推送密集时
- **`asyncio.gather` 并发 N 个 REST 也没救**——总下载字节数是带宽瓶颈，N 路并发只是把 N 份 payload 挤同一根管子
- **决策→下单链路必须独占一段干净时间**：在 risk → executor → clob.submit 这条 ~250ms RTT 路径上，**绝不允许任何后台 worker 同时发其他 REST**（否则 submit 排队等带宽）
- **"更新鲜的数据"和"更少网络 IO"不冲突**——靠换形态实现：**推 > 拉、内存 store > 重复 fetch、demand-driven > broadcast、batch endpoint > N 次单调**

### 0.1 设计每条链路时先问的 3 个问题

1. **这条数据能不能换成 WS push？** WS 长连开 1 次，之后 0 RTT 拿增量。能用 WS 就用 WS。
2. **这条数据能不能在一个 store 里被多个 consumer 共享？** 比如 gamma `/events` 嵌套的 `markets` 已经有完整 metadata，所有读者都从 `GammaMarketSnapshotStore` 读，不再每 condition 单独拉。
3. **这条数据真的需要这么频繁吗？** 0.5s vs 2s 对决策几乎没影响，但 RTT 200ms 的链路上 0.5s 周期意味着 40% 时间在等网络。

### 0.2 当前已上的优化（按这条约束做的）

- discovery `/events` 周期 0.5s → **2s**（−80% 调用）
- gamma `/markets/{slug}` 周期性 fetch → **从 `gamma_snapshot_store` 读**（命中即 0 RTT）
- reconcile `/book` 周期性"trust but verify" → **完全删除**，WS 是盘口唯一真相源
- user-side `/positions` + `/balance-allowance` 同步在 reconcile 主路径 → 拆到独立 `UserAccountPoller`（不阻塞主循环）
- 无 tracked sport 时仍轮询 PBP / standings → **demand-driven**，无需求 0 调用
- 启动期读 DB 预热 → **删除**，等 Polymarket 反正要拉一遍，DB 预热浪费 5 秒

实测累计：稳态出站 **5 req/s → 0.7 req/s（−85%）**。

### 0.3 还能继续做的（按 ROI 排）

1. **机房迁移到 Polymarket 同区**（AWS us-east-1 / Cloudflare Workers 边缘）——RTT **250ms → 30ms** 是数量级提升，所有现有 worker 自动受益。这是单笔最大杠杆。
2. **HTTP/2 multiplexing + connection keepalive 检查**：确保 httpx 复用 TCP，避免每次 fetch 走完整 TLS 握手（~100ms 节省）。
3. **响应 gzip / brotli**：Polymarket REST 默认应该支持 `Accept-Encoding: gzip`，验证客户端开了；Goalserve inplay 已经是 .gz。
4. **ETag / If-Modified-Since**：对 metadata（gamma /events）加条件请求头——服务端没变就 304，省 payload。Polymarket 是否支持需要试。
5. **POST batch endpoints**（如 `/markets?condition_ids=cid1,cid2,...`）：一次拉多个 condition 而不是 N 次单调。Polymarket gamma 已支持，重构成批量。

### 0.4 任何加重网络负担的改动需要明确权衡

新增"每 N 秒调外部 API"或"每事件触发 REST"前，必须答完 §19.10 的 7 个问题。如果无法在 0 RTT 路径（WS / 内存 / cache）上解决问题，**说明白为什么这条新增 RTT 是必要的、值得多少 ms 决策延迟**。不要"为了完整性"或"为了双保险"加。

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
3. PostgreSQL **仅审计**

数据库**不参与运行时**——包括程序启动管道、所有 worker、所有 scheduler job 都不得读 DB 拉运行时数据（balance / positions / orders / markets 等）。运行时数据**只能**来自 Polymarket 官方 API 或内存快照。**启动期不允许任何 DB 预热**——`balance/positions` 等空白窗口由 readiness gate（`reconcile_fresh`）兜住，不会下错单。DB 读取**只允许出现在 audit/admin API 端点**（操作员主动查历史）。详细规则见 §19。

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

## 7.1 操盘和审计统一走后端 API（不绕过后端）

**所有运行时观测、操盘动作、审计查询、账户审计都必须通过后端 admin API**，不允许 agent / 操盘脚本 / 前端绕过后端直接调链上 RPC、Polymarket data-api、Goalserve 等外部源。

- **理由**：后端是统一的数据入口和审计源，绕过后端 → 状态不一致 + 审计断链 + 直接调用外部源没有 audit + 没有 retry/降级 + 鉴权脱管。
- **缺接口怎么办**：不允许"临时绕过"——**直接在后端扩展接口或新增 endpoint**，把功能落到 `src/polymarket_trader/api/routes/` + `src/polymarket_trader/app/admin_*`，并保证：
  - 涉及账户/持仓/资金状态：从 reconcile worker + account_state_store 的内存快照取，落 audit
  - 涉及链上余额（USDC balance/allowance、合约 redeem 状态）：从 `clob_client.get_balance_allowance` 或 `data_client` 取，结果回写 account snapshot 并落 audit
  - 涉及第三方外部数据（Goalserve odds/score、Polymarket orderbook）：通过对应 infra client 取，结果回写运行时 store
  - 任何查询动作都要落对应 audit_event（如 `orderbook_direction_queried`、`account_balance_queried` 等），事后可复盘"agent 为什么这么做"
- **agent / 操盘脚本**：调用统一前缀的 admin endpoints（`/runtime`, `/portfolio/*`, `/markets/*`, `/positions/*`, `/orders/*`, `/audit-events`, `/candidates/*`），不直接 import 内部 module 或调外部 URL
- **例外**：仅在初始 bootstrap / lifecycle hook 调用框架自身实现的 client，业务路径不绕过

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
| Inplay 实时赔率+比分（HTTP GZIP feed）| `inplay.goalserve.com/inplay-{sport}.gz` | keyless（IP 白名单，经 `GOALSERVE_PROXY` 出口）| `infra/sports/goalserve_inplay_client.py` |
| Livescore 实时比分（getfeed）| `getfeed/{key}/{sport}/home?json=1` | API key | `infra/sports/goalserve_livescore_client.py` |
| 赛前赔率（Pregame Odds, GZIP）| `getfeed/{key}/getodds/soccer?cat={sport}_10` | API key | `infra/sports/goalserve_pregame_client.py` |

### 已覆盖的运动

**Inplay feed**：soccer、basket、tennis、volleyball、amfootball、esports、hockey、baseball — Goalserve inplay 覆盖的 8 个运动全部启用，由 `GoalserveInplayClient` demand-driven 轮询（无独立配置项，按 tracked market 需求自动决定轮询哪些 sport）。

**Livescore getfeed（`goalserve_livescore_sports` 配置）**：cricket、handball、rugby、boxing、mma、golf\_pga/dp/liv/lpga、horse\_racing\_us/uk/au/hk、f1、motogp。

**Pregame odds（`goalserve_pregame_sports` 配置）**：soccer、basketball、tennis、hockey、baseball、amfootball、esports、mma、cricket、rugby、volleyball、handball、boxing、darts、table\_tennis、futsal、rugbyleague。

### 扩展原则

- Goalserve 支持的运动和接口以 `goalserve/full_package_feed.txt` 为准；发现文件里有但代码里未接入的接口，视为缺口，应补实现或记录明确的跳过原因。
- Livescore parsers 基于格式推断实现，首次真实调用后需对照实际 response 校准字段——尤其是 golf、horse\_racing、f1 这三类。校准后更新 parser，不保留"推断注释"。
- 新增运动 parser 必须：① domain 加对应 GameState；② parsers 文件加 sport parser；③ `_SPORT_PARSERS` / `_SPORT_PATHS` 注册；④ config 默认值加入；⑤ 补测试。缺任何一步均为未完成。
- API key 只存 `.env` 的 `GOALSERVE_API_KEY`，不进代码仓库和文档。
- Pregame 数据量极大（>100MB），默认关闭（`goalserve_pregame_enabled=false`）；启用时必须用 `ts` 增量拉取，不允许无限循环全量请求。
- Inplay GZIP feed 每个运动维护独立后台轮询 Task（同 sport ~1 请求/秒硬限流，超速 HTTP 429 → 该 sport 单独退避）；feed 完整结构与字段以 `goalserve/inplay-feed-new.txt` 为准；`core.removed="1"` 的赛事立即标记 CANCELLED；`events_seen=0` 且 `health=success_empty` 表示 feed 连通但无 inplay 数据——此时以 livescore getfeed 为主要比分源，不影响交易。
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
2. **认证/网络**：inplay GZIP feed 出口 IP 是否在白名单（`GOALSERVE_PROXY` 是否可达，HTTP 429 = 同 sport 轮询超速、属正常退避不是故障）？livescore API key 是否有效？
3. **解析问题**：parser 是否正确处理了该运动的 XML/JSON 格式？是否静默丢弃了数据？
4. **名称匹配**：团队名拼写/格式是否导致市场文本匹配失败？
5. **时区/日期**：市场 slug 日期是否与事件实际 UTC 日期不一致（如午夜场次）？
6. **配置未启用**：该运动是否被加入了 `GOALSERVE_LIVESCORE_SPORTS`（或对应 `.env` 变量）？

确认非 Goalserve 数据问题后，才考虑使用其他数据源作为补充。

## 17. 量化交易策略方向

本系统的战略目标不限于"扫尾"（tail），而是整个体育市场的**安全量化交易**。扫尾盘是当前已实现的策略之一，但以下方向同样在目标范围内：

**核心原则：不放过任何可盈利市场。** Polymarket 一场赛事下挂的所有盘口都是潜在盈利来源——不止整场 Moneyline/Totals/Spreads，还包括：分节/分盘/分局市场（上半场、第一节、第一局、首盘）、系列赛第 N 场、NRFI 之类的单局 prop、让分、特殊事件等。任何一个有明确胜率判断、能定价、能风控的盘口都应纳入候选与执行，不能因为"目前只做扫尾"或"缺专用模型"就长期静默忽略。缺直播字段或定价模型时，应补数据源 / 建模 / 给出可审计的 record-only 原因（参照 §10），而不是直接丢弃。每个被拒市场都要能回答"为什么这个可盈利机会我们不做"。

**limit / 分页 / 截断绝不能卡掉机会。** 任何 list / 分页 / 容量上限都不得把"正在直播的赛事、入场机会、套利机会"排除在外。具体要求：① 候选、live-states、机会类端点的分页 limit 必须足够大（默认上限按可能的真实条数设，不能用 100 这种小值）；② 当存在截断风险时，列表必须把"正在直播 / ready_to_trade / 有 edge"的条目排到最前，保证即使截断也永不丢机会；③ 任何"只取前 N 个市场评估"的逻辑都要确认直播中的赛事不会被挤出。发现任何 limit 可能截掉直播/入场/套利机会，视为 bug 立即修。

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

## 19. 外部数据源与状态真相源（**反复犯过的错都记这里**）

所有以下规则都是从实盘事故 + 性能审计踩出来的，**重新设计或新增数据源前先读完**。

### 19.1 数据库只做审计，不做运行时

- **启动期**：不读 DB 预热任何运行时状态（balance / positions / orders / markets / account_snapshot 都不读）。Polymarket 第一轮 reconcile（秒级）会拉回所有权威值；readiness gate 阻挡空窗期下单。
- **运行时**：所有 workers / schedulers / supervisor 路径都不允许 `db_session_factory()` 读取（dead_records_purge 这种 DB 自身 retention 维护是例外）。
- **唯一允许**：admin/audit API 端点同步读 DB 历史（操作员主动查询），不参与决策回路。
- 违规典型：曾经的 `_load_reference_state` 启动期读 `account_snapshots`、`settlement_scanner` 用 audit_events 查幂等——都已删。

### 19.2 WS 是盘口唯一真相源，REST `/book` 只剩 sequence_gap 兜底

- **不要**为盘口数据加任何"周期性 REST 校准"——Polymarket WS 订阅后 ~1s 自动推完整 `book` 消息，之后 `price_change`/`book` 持续推送。
- **不要**在 ws_loops 订阅时 `refresh_rest_snapshots` 预取——决策 gate 本来就要等 reconcile_fresh，"头几秒数据饥饿"不是真问题。
- **不要**在 reconcile authority refresh 里调 `get_orderbook`——`AuthoritativeMarketRefresh` 已不带 `orderbook_snapshots` 字段。
- **唯一允许 REST 调 /book 的路径**：`market_ws_worker._handle_sequence_gap`（WS 真漏推消息时重建一致状态）。正常稳态出现 0 次/小时。
- 违规典型：曾经 reconcile 每 N 秒对所有 tracked tokens 调 /book "trust but verify"——实测占总出站流量 65%，已全删。

### 19.3 paper_mode 与 live_mode：account_state_store 必须只有一个 writer

- **paper_mode**：`paper_balance_syncer` 每秒从 `paper_ledger` 投影回 `AccountStateStore`。reconcile authority `refresh_account` 必须早退（已通过 `paper_mode=True` 守住）。
- **live_mode**：`UserAccountPoller` 是唯一 writer，reconcile 主循环（`refresh()`）通过 `refresh_account_inline=False` 跳过用户态拉取。
- **绝对不允许两个 writer 同时写同一份状态**——之前 paper_balance_syncer 和 reconcile authority 抢同一份 store 导致"24 个幽灵持仓时有时无"。每次新增写者必须问"我会和谁抢？"。
- 违规典型：在 paper 模式下 reconcile authority 还调 `data_client.list_positions()` 写 store——已修。

### 19.4 gamma `/events` 已含 nested markets，不要双调 `/markets`

- `GammaEventDTO.markets` 是 `tuple[GammaMarketDTO, ...]`，**完整字段**（tick_size / fee / outcomes / closed / token_ids）。
- discovery 每 N 秒拉 `/events?live=true` 后顺手把 `event.markets` 写进 `GammaMarketSnapshotStore`。
- 所有 reconcile/settlement/enrich 路径**先读 store**（`gamma_snapshot_store.get_dto_if_fresh(cid, max_age_s=30)`），命中直接用；只有 cache miss/stale 才 fallback 走 `/markets?condition_ids=` 反查。
- **不要**给每个 tracked market 都单调一次 `/markets?slug=...`——之前 200 markets × 串行 = 30s 阻塞，全是浪费。
- 唯一需要 `/markets?condition_ids=` 直查的场景：已 settled 市场（`closed=true` 不在 `live=true` 范围）+ 孤儿 condition 反查。

### 19.5 gamma `/markets/{id}` 只接受 Polymarket 内部数值 id

- **不要**给这个端点传 condition_id 或 slug——会 422。
- 用 condition_id 反查市场：`gamma_client.get_market_by_condition_id(cid)` （内部走 `/markets?condition_ids=cid&limit=1`）。
- 这个坑封装在 `GammaClient` 里，调用方禁止重新手拼 condition_ids 参数。

### 19.6 仓位状态 ≠ 市场状态，前后端都不能混

- **仓位 payload 字段**（PortfolioExposureItem）只承载仓位生命周期：`shares / avg_price / cur_price / cash_pnl / redeemable / settled_zero_value`。**`paused` 字段只反映 `MarketPauseSource.MANUAL`**（人工 click 触发），后台 reconcile/risk/strategy 自动 pause 不进仓位 payload。
- **市场状态**（`trading_status`、自动 quarantine 等）只从 `/markets` 端点返回，不挂在仓位上。
- 前端 badge 三/四态：`归零 / 暂停(人工) / 可赎回 / 持有`——不掺杂市场状态。
- 违规典型：之前 `portfolio_exposure` 用 `snapshot.is_market_paused(cid)` 给仓位行打"暂停"标签，把自动 quarantine 显示成仓位被暂停——已修。

### 19.7 redeemable 持仓必须用 gamma outcomePrices 派定胜负

- Polymarket data API 返回的 `redeemable=True` 仅说明"市场关了、可领"，**不说赢方是谁**。
- `Position.cur_price` 默认从 data API drop（避免 stale 数据骗 worker MTM），所以 redeemable 仓位刚拉到时 cur_price=None。
- `_enrich_redeemable_positions` 在 reconcile 拉到 redeemable 持仓时立即并发调 gamma `get_market_by_condition_id` → 读 outcomePrices → 用 `apply_outcome_to_position` 派定胜方 token_id → cur_price=1（赢）/ 0（输）。
- 输方 cur_price=0 + redeemable=True → `settled_zero_value` 自动为 True → UI 默认隐藏（对齐 Polymarket portfolio）。

### 19.8 不订阅就不轮询（demand-driven）

- 所有 sport-specific 数据源（goalserve inplay / livescore / pregame）必须按"tracked market 实际涉及的 sport"启停。
- **不要**起一个全运动 polling worker 在所有 sport 上跑，无视 tracked market 是否在那个 sport 上有头寸。
- 违规典型：删除前 MLB/NBA PBP 客户端 paper 模式下 2s 周期跑，可当时 0 个 MLB/NBA tracked market；ESPN standings/series-state 同理——全删。新增数据源前先想"什么 condition 会触发轮询"。

### 19.9 周期 cadence 校准原则

- **discovery `/events`**：2s（曾经 0.5s 浪费 80%）。新市场上线 2s 内捕获仍快于散户。
- **gamma snapshot store 新鲜度阈值**：30s（discovery 跑 2s 一次，30s 内一定有写入）。
- **WS orderbook 新鲜度阈值**：30s（WS 正常每秒至少推一条；30s 没动静才视为"WS 真死了"触发 REST 兜底）。
- **user_account_poll**：和 `market_sync_interval_seconds` 同 cadence（默认 20s），独立 scheduler job。
- **settlement_scanner**：5 分钟（结算事件低频，足够）。
- 改 cadence 前先回答："这个数据真的会在新 cadence 周期内变化吗？变化但没及时拉到，损失是什么？" 大部分情况是过频。

### 19.10 任何新增外部 API 客户端前的强制问题清单

1. 这个数据有没有 WS 推送版本？有就别用 REST 轮询。
2. 加这条调用预计每小时调多少次？是否 demand-driven（无 tracked 时不跑）？
3. 写入哪个 in-memory store？同一 store 是否已有别的 writer（race 风险）？
4. 读取方是 P0 决策路径还是后台 worker？P0 上不允许同步等网络。
5. 数据是否进 DB？如果"是"——确认是 audit/persistence 单向写入，不在运行时回读。
6. 同一数据如果已经在某个内存 store 里，能否走 store 命中后再 fallback 网络？
7. 失败兜底是什么？stale 多久后该降级 / 报警 / quarantine？
