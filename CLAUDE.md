# Claude Rules

Sports Tail Trader 是跑实盘资金的 Polymarket 体育**量化交易**后端运行时，覆盖整个体育市场——Moneyline、Totals、Spreads、分节/分盘 prop、系列赛、outright 等所有能建立胜率判断与风控闭环的盘口家族。错误的改动会直接导致下错单、爆仓、资金错配或绕开风控，本文先列**不可违反的硬约束**，再讲结构、落点与协作。

策略配置只走 `TradingWorkflowConfig` dataclass 默认值，无 TOML / env 间接层，无运行时 override；调阈值改代码默认值后重启。

## 0. 部署环境约束（第一性原理）

**部署位置：中国 → 代理 → 美国 Polymarket 服务器**
**RTT：150–400ms（高且抖动）；出口带宽：小（受代理限速）**

### 0.0 核心矛盾：WS 是决策核心链路，所有其他网络 IO 都在和它抢带宽

Polymarket market WS 推送是整个交易系统的决策驱动源——每条 `book / price_change / best_bid_ask` 都可能触发一笔下单。WS 决策链路的延迟 = 我们对市场反应的速度 = 抢市场机会的能力。

但是：

- **带宽小**——同一根管子里跑的所有 HTTP/WS payload 互相抢
- **延迟高**——一个 200ms 的 gamma 请求期间，WS 上几条 price_change 可能还在路上
- **asyncio 单线程**——cooperative，但下载 100KB gzip payload 期间事件循环没空读 WS 队列

**任何非必要的网络 IO 都在 ms 级窃取 WS 决策反应速度**。

### 0.1 设计每条链路时先问 3 个问题

1. 这条数据能不能换成 WS push？能用 WS 就用 WS。
2. 这条数据能不能在一个 in-memory store 里被多个 consumer 共享？（如 gamma `/events` 已嵌套 markets，所有读者走 `GammaMarketSnapshotStore` 不再单调）。
3. 这条数据真的需要这么频繁吗？0.5s vs 2s 对决策几乎没影响，但 RTT 200ms 链路上 0.5s 周期意味着 40% 时间在等网络。

形态优先级：**推 > 拉、内存 store > 重复 fetch、demand-driven > broadcast、batch endpoint > N 次单调**。

### 0.2 加重网络负担前的必答清单

新增"每 N 秒调外部 API"或"每事件触发 REST"前，必须答完 §19.10 的 7 个问题。无法在 0 RTT 路径（WS / 内存 / cache）解决问题时，说明白为什么这条新增 RTT 是必要的、值得多少 ms 决策延迟。不要"为了完整性"或"为了双保险"加。

## 1. 项目速览

- 主包 [src/polymarket_trader](./src/polymarket_trader)：分 `api / app / domain / workflow / pipeline / recovery / infra / observability / runtime / sports`
- 策略实现 [src/polymarket_trader/workflow](./src/polymarket_trader/workflow)：`TradingWorkflow` 装配 + `QuantDecider` 决策；所有 family 走同一份 Kelly + fair_value 主路径，**无 family 专属子包**
- 量化信号入口 [workflow/fair_value.py](./src/polymarket_trader/workflow/fair_value.py)：math_lock / goalserve_implied / orderbook microprice 三层 max 融合——接入新信号源在这里扩展
- 前端管理台 [frontend](./frontend)：Operator UI
- 长期文档 [docs/](./docs/)：当前状态以代码和 `git log` 为准
- Python 3.12，`src` layout，FastAPI + asyncpg + SQLAlchemy + py-clob-client-v2 + websockets

## 2. 常用命令

- 静态检查：`ruff check .`
- 类型检查（按需）：`mypy src`
- 启动后台 + 前端：`./start_all.sh`；停止：`./stop_all.sh`
- 仅前端开发：`npm --prefix frontend install && npm --prefix frontend run dev`
- 构建发布包：`./build_dist.sh --archive`

提交前最少跑 `ruff check .`，启动后端能跑通即可。行为对错以实盘观察 + audit_events 复盘为准（参见 §12 不写测试）。

## 3. 不可违反的硬约束

**交易安全**：

- `OrderExecutor`（`infra/polymarket/order_executor.py`）是唯一允许创建、签名、提交、取消、替换订单的模块。任何新下单入口都必须最终走它。
- `RiskManager`（`domain/risk.py`）是任何下单前的强制门禁。新增下单入口必须显式经过它，禁止旁路。
- `OrderGateway`（`pipeline/execution/order_gateway.py`）是决策侧统一执行入口：`review_intent` → RiskManager → OrderExecutor → audit/lifecycle 编排。
- 策略只输出 `TradingDecision`，不直接调用交易客户端。
- 买入侧不得保留长期 resting BUY order；发现 open BUY 异常必须立即 cancel / reconcile 修复。
- Operator API 不是自动下单入口，也不是交易客户端直连入口。
- Persistence / outbox / 审计只能异步承接副作用，不得反向阻塞交易主链路。

**状态真相**：

1. Polymarket 实时事件与权威快照
2. 本地内存状态
3. PostgreSQL **仅审计**

数据库**不参与运行时**：启动管道、所有 worker、所有 scheduler job 都不得读 DB 拉运行时数据（balance / positions / orders / markets 等）。运行时数据**只能**来自 Polymarket 官方 API 或内存快照。启动期不允许任何 DB 预热——`balance/positions` 等空白窗口由 readiness gate（`reconcile_fresh`）兜住。DB 读取**只允许出现在 operator/audit API 端点**（操作员主动查历史）。详细规则见 §19。

**Domain 纯度**：Domain 不依赖 FastAPI / SQLAlchemy / Polymarket SDK / WebSocket client / 环境变量 / 数据库查询。Domain 内金额和价格用 `Decimal`，不用浮点。

## 4. 交易主链路

```text
事件 → MarketTickWorker → DecisionContextBuilder → quant_decide
       (pipeline/decision)                          (workflow/quant_decider.py)
                                                            │
                                                    TradingDecision
                                                            ▼
                                              OrderGateway.review_intent
                                              (pipeline/execution/order_gateway.py)
                                                  ├─ RiskManager 强制门禁
                                                  └─ OrderExecutor 唯一下单
                                                            │
                                                            ▼
                                                  outbox / audit / lifecycle
```

- `DecisionContextBuilder` 承担 Kelly sizing + bankroll 解析 + 入场预算分配；和 `RiskManager`、operator manual 入口共用同一份 bankroll 口径。
- `MarketTickWorker.entry_metadata_provider` 只读 in-memory store，P0 路径不发外部 API。
- App 层只编排，不维护绕过 risk / executor 的第二套交易事实。

## 5. 改动落点

| 改的东西 | 落到哪里 |
| --- | --- |
| 策略规则、阈值、定价、入场/退出 | `strategy_config.toml` + `src/polymarket_trader/workflow/` |
| 交易/恢复/管理操作编排 | `src/polymarket_trader/app/` |
| 数据入口与决策响应 | `src/polymarket_trader/pipeline/` |
| 周期 reconcile / settlement / orphan 修复 | `src/polymarket_trader/recovery/` |
| 外部 API、数据库、WS、outbox 适配 | `src/polymarket_trader/infra/` |
| 运行时状态、调度、队列、supervisor | `src/polymarket_trader/runtime/` |
| 人工查询和受控操作入口 | `src/polymarket_trader/api/` |
| 前端管理台、UI 组件 | `frontend/` |

只服务当前策略的字段/状态/配置**留在 workflow 内**，不进 `domain` / 全局 `Settings`。

## 6. 分层边界

```text
api / pipeline → app → domain
app → infra / runtime / observability
infra → domain
runtime → domain
```

- **Domain**：纯业务规则、领域模型、内部 DTO
- **App**：用例编排，不保存第二份业务真相，不硬编码具体策略阈值语义
- **Workflow**：策略实现，输出 `TradingDecision`，不直接调交易客户端
- **Pipeline**：数据入口（ingest）、决策响应（decision）、执行（execution）
- **Recovery**：周期兜底，不决定主链路是否继续
- **Infra**：外部协议适配，外部 payload 必须先转内部 DTO 再向上返回
- **API**：参数校验、调用应用服务、响应序列化；不直接操作交易客户端或运行时热状态写锁

## 7. 状态与并发

- 交易主链路优先级最高，实时判断优先用内存状态；权威修正依赖 WS 和 REST
- Operator 和 Persistence 读快照，不直接持有热状态写锁
- 状态访问按 `condition_id` / `token_id` 分片，不用全局大锁
- 非关键锁等待超时后跳过并告警，不能无限等待
- 阻塞 I/O 与 CPU 密集任务必须离开交易主事件循环
- 数据库写入、日志、全量扫描、报表和 Operator 大查询不得反向阻塞 P0 路径
- P0 热路径（decision → risk → executor）只允许同步执行：风控判定、内存状态读写、订单签名/提交、向 outbox `put_nowait`。日志、DB 写入、metric 上报、payload 序列化落盘必须延迟到事务结束后由 outbox / persistence worker 异步承接；关键锁（`_idempotency_lock` 等）内禁止 IO `await`、日志和大对象序列化
- Reconciler 不在批量扫描里长时间持有交易状态写锁
- 队列必须有容量上限、可观测性和降级路径

### 7.1 操盘和审计统一走后端 API

所有运行时观测、操盘动作、审计查询、账户审计都必须通过后端 operator API，**不允许 agent / 操盘脚本 / 前端绕过后端直接调链上 RPC、Polymarket data-api、Goalserve 等外部源**。

- **理由**：后端是统一数据入口和审计源；绕过后端 → 状态不一致 + 审计断链 + 没 retry/降级 + 鉴权脱管
- **缺接口怎么办**：不允许"临时绕过"——直接在后端扩展接口或新增 endpoint（落 `src/polymarket_trader/api/routes/` + `src/polymarket_trader/app/operator_service.py`），保证：
  - 账户/持仓/资金状态：从 reconcile worker + account_state_store 内存快照取，落 audit
  - 链上余额（USDC balance/allowance、redeem 状态）：从 `clob_client.get_balance_allowance` / `data_client` 取，回写 account snapshot 并落 audit
  - 第三方外部数据（Goalserve odds/score、Polymarket orderbook）：通过对应 infra client 取，回写运行时 store
  - 任何查询动作落对应 audit_event，事后可复盘"agent 为什么这么做"
- **agent / 操盘脚本**：调用统一前缀的 operator endpoints（`/runtime`, `/portfolio/*`, `/markets/*`, `/positions/*`, `/orders/*`, `/audit-events/*`, `/decision-context/{cid}`, `/candidates/*`），不直接 import 内部 module 或调外部 URL
- **决策上下文一次拉齐**：单 condition 完整上下文用 `GET /decision-context/{cid}`——零 DB 默认返 market+positions+live_state；复盘走 `?include_audit=true` 附加 audit 时序。批量审计按 condition 维度的多 channel 时间线走 `GET /audit-events/by-condition/{cid}`

## 8. 设计与改造取向

**目标永远是最优方案，不是最小改动，也不是兼容旧逻辑**。每次改动按目标形态实现，不在中间形态停留；旧代码/旧结构直接替换，不为了"减少改动"保留次优实现。

- 不采用补丁式开发：面向完整目标形态建模，不为了表面兼容堆叠旁路、特殊分支或一次性修补
- 结构性改造一次切到目标形态，不保留长期双路径
- 不引入只服务过渡期的配置项、状态字段或旁路逻辑
- **不写迁移 / backfill / 向后兼容代码**：不写一次性 backfill 脚本，不留 nullable 兼容列，不在代码层做"读旧写新"或双格式解析。schema 调整以新模型为准，旧数据保留由运维侧处理
- 调用侧用非目标命名时优先改调用侧，不在框架层长期保留同义字段、别名、包装函数或重复枚举
- 新增调度器、扫描器或后台任务时，运行时状态必须暴露为可观测快照

**不留技术债务**——本次改动不留新债，遇到旧债随手清。

- 当场消除：`TODO/FIXME/HACK` 标记；`getattr/hasattr` duck-typing 兜底；同义双命名 / 别名；过渡期 fallback 字段
- 提交前自检 `# pragma: no cover` / `# type: ignore` / `as unknown` / `as Any` 旁路检查标记
- 文档与代码漂移视同 bug
- 不得已留下的中间态（shim、双路径、过渡命名）必须在 commit message 注明"何时清理 + 触发条件"

## 9. Goalserve 数据接口规则

`goalserve/full_package_feed.txt` 是订阅的完整接口权威清单，任何涉及体育数据源的改动都必须先查阅它。

| 类型 | 端点形式 | 认证 | 实现 |
|------|---------|------|------|
| Inplay 实时赔率+比分（HTTP GZIP feed）| `inplay.goalserve.com/inplay-{sport}.gz` | keyless（IP 白名单，经 `GOALSERVE_PROXY` 出口）| `infra/sports/goalserve_inplay_client.py` |
| Livescore 实时比分（getfeed）| `getfeed/{key}/{sport}/home?json=1` | API key | `infra/sports/goalserve_livescore_client.py` |
| 赛前赔率（Pregame Odds, GZIP）| `getfeed/{key}/getodds/soccer?cat={sport}_10` | API key | `infra/sports/goalserve_pregame_client.py` |

**覆盖范围**：

- Inplay feed 8 sport：soccer / basket / tennis / volleyball / amfootball / esports / hockey / baseball（demand-driven 按 tracked market 自动轮询）
- Livescore getfeed 覆盖 inplay 之外：cricket / handball / rugby / boxing / mma / golf / horse_racing / f1 / motogp
- Pregame odds 默认关闭（>100MB）；启用必须用 `ts` 增量拉取，不允许无限循环全量请求

**扩展原则**：

- 发现 `full_package_feed.txt` 里有但代码未接入的接口，视为缺口，应补实现或记录跳过原因
- Livescore parsers 基于格式推断实现，首次真实调用后须对照实际 response 校准（尤其 golf / horse_racing / f1）
- API key 只存 `.env` 的 `GOALSERVE_API_KEY`
- Inplay GZIP feed 每 sport 独立后台 Task（~1 请求/秒硬限流，超速 429 → 该 sport 单独退避）；`core.removed="1"` 立即标 CANCELLED；`events_seen=0` + `health=success_empty` 表示 feed 连通但无 inplay 数据，此时以 livescore getfeed 为主比分源
- 发现 403/无数据接口先查 `full_package_feed.txt` 确认是否有替代；有替代则更新，无替代则删死代码

## 10. 配置、命名与建模

- `.env` 和 `Settings` 只承载框架运行参数；策略参数走 `strategy_config.toml`，不进 env
- 契约字段命名单义，避免一套对外、一套对内的长期双命名层
- Domain 内金额/价格用 `Decimal`
- 策略常量集中在 `TradingWorkflowConfig`，避免散落硬编码
- 拒绝原因必须可审计，不能只返回 `False`；跳过、降级、恢复动作必须保留可审计原因，不静默忽略关键失败

## 11. 测试

**默认不写测试**。现有代码已经过实盘验证，单元测试维护成本高且常常落后于真实行为，`tests/` 目录已统一删除。

- 写代码时**只为本次新写、未经任何实盘检验的关键逻辑**写局部测试，验证完毕即可删
- 不补测试覆盖率，不在 PR 里要求"补回归测试"
- 改完跑 `ruff check` + 启动后端能跑通就算通过
- 行为对错以实盘观察 + audit_events 复盘为准
- 历史"必须补测试"的强制项（自动下单 / 风控 / 资金计算 / 订单幂等 / 恢复 / reconcile）已不再要求

## 12. 注释与文档

- 注释只写非显然的 WHY：隐含约束、不变量、绕过特定 bug 的处理、会让读者意外的行为。命名能表达的 WHAT 不另加注释
- 涉及风控、状态机、资金计算、并发边界等关键业务步骤的边界条件用中文写明
- 新增接口、配置项、运行时状态或人工操作入口时同步更新对应文档
- 改造计划、实施清单、进度日志等过程性文档可在任务推进期间存在，**任务完成后必须主动清理**，不长期保留为历史档案。完成态由代码 + `git log` 表达
- 规则类约束统一沉到本文件

## 13. 多 agent 协作

- 多模块、可并行或需要不同视角复核的任务，主动用 Explore / Plan / general-purpose subagent 拆分
- 并行前先明确每个 agent 的职责边界、读写范围和预期输出；写入文件范围尽量不重叠
- 总协调 agent 负责拆分、整合、复核；子 agent 输出不能直接当最终事实，重要结论必须本地验证
- 紧耦合、关键路径、需要统一架构判断的工作不强行并行

## 14. 常见误区

- 在 route 里写交易逻辑
- 在 worker 里复制 discovery / 策略业务判断
- 在 framework 层加入策略专属字段或阈值
- 在 Domain 或内部接口中保留长期同义命名以"减少改动"
- 让数据库、持久化、日志或报表路径决定交易热路径是否能继续运行
- 把 `single_game` 当成业务范围限制——只是"可用单场比分源评估"的家族标记
- 让 resting BUY 长期挂着不修复

## 15. 量化交易策略方向

**核心原则：不放过任何可盈利市场。** Polymarket 一场赛事下挂的所有盘口都是潜在盈利来源——Moneyline/Totals/Spreads 整场盘、分节/分盘/分局市场（上半场、第一节、第一局、首盘）、系列赛第 N 场、NRFI 之类的单局 prop、让分、特殊事件等。任何有明确胜率判断、可定价、可风控的盘口都应纳入候选与执行。缺直播字段或定价模型时，补数据源 / 建模 / 给出可审计 record-only 原因，而不是直接丢弃。每个被拒市场都要能回答"为什么这个可盈利机会我们不做"。

**limit / 分页 / 截断绝不能卡掉机会**。任何 list / 分页 / 容量上限都不得把"正在直播的赛事、入场机会、套利机会"排除在外：① 候选、live-states、机会类端点分页 limit 必须足够大；② 截断风险下，"正在直播 / ready_to_trade / 有 edge"的条目排最前；③ 任何"只取前 N 个市场评估"必须确认直播中赛事不会被挤出。发现任何 limit 可能截掉机会视为 bug 立即修。

**实盘演化闭环**：跑实盘观察 → 定位瓶颈 → 改进策略或链路 → 验证 → 重启/继续观察 → 再次改进。除非用户明确要求停止/暂停，agent 不应在单轮观察或单次改动后主动结束。优化优先级：发现速度、盘口热态更新速度、决策延迟、成交链路延迟；不为速度牺牲风控、幂等、可审计性。

### 决策原则

**所有买卖决策由 `quant_decider.QuantDecider` 统一接管**——不要在其他模块（recovery / operator / api）写交易决策逻辑。

**入场**：有真概率信号（math_lock / goalserve / 用户自定义量化信号源任一）才入场；Kelly sizing 决定金额；无真信号 → `ProbView(prob_p=None)` → Kelly 拒绝。

**持仓后**：当前 `_decide_position_action` 默认 skip——所有买卖决策（SELL / replace / HOLD）由用户在量化信号入口（`workflow/fair_value.py` 或自有信号源）接入后产生。**不预设任何退出规则**（无止盈倍数 / 锁定价 / 止损阈值 / GTC tail-bid 之类的 magic number），让真量化信号驱动。

**信号接入**：要让系统对某类信号做出 BUY/SELL/HOLD 反应，在 `fair_value.math_lock_fair_value` 或 `estimate_fair_value` 里加新的信号源——三层 max 融合会自动让信号反映到 prob_p，Kelly 自然产生决策。

**直播源赔率 vs Polymarket 价格差价**（重要机会）：

- Goalserve inplay 实时赔率隐含博彩市场对事件概率的真实估计；与 Polymarket 价格存在显著差价时是明确 edge 信号
- 实现：解析 inplay 赔率字段（`goalserve_moneyline` / `goalserve_totals` / `goalserve_spread`），换算隐含概率，与 Polymarket best_ask 比较；差价超阈值（如 5pp）触发候选
- 优先级：live inplay 赔率 > pregame 赔率
- 与"末段确定性"策略互补：差价策略可更早入场，依赖"Polymarket 定价落后于博彩市场"的效率差

**门禁调参原则**（拼概率，不过度保守）：

- 门禁目的是防止明确错误，不是追求零风险。反复拦截本应成交的机会必须复盘并调整
- 复盘路径：单 condition 多 channel 一次拉齐用 `GET /audit-events/by-condition/{cid}`（默认 7 channel：order_created / allocation_decision_recorded / sports_live_state_recorded / order_matched / order_rejected / fill_recorded / risk_rejection_recorded），结合 `/candidates` 的 `rejection_reason` 判断拒绝是否合理；不合理的拒绝（成交是正期望）视为门禁设定问题
- 在统计优势场景下宁可偶尔在边界情况输一笔，不能因过度保守在系统性优势场景下全部放弃
- 审计日志是复盘工具不是决策权威

**买入复盘与 bug 止损**：

- 每次实盘买入事后必须复盘：`GET /audit-events/by-condition/{cid}` 一次拉齐 order_created / fill_recorded / sports_live_state_recorded / allocation_decision_recorded 等多 channel 时间线，判断买入是否由正确逻辑触发
- 发现 bug 触发的买入（如 `"delayed"` 状态被误认 LIVE 导致雨延场买入）立即通过 `/positions/force-exit` 或手动市价卖出止损
- 复盘工具：`/decision-context/{cid}?include_audit=true`（一次拉齐 market + positions + live_state + audit 时序）/ `/positions` / `/fills`

**僵尸仓位（orphan position）**：

- reconcile 扫描链上发现系统未追踪的持仓（`account-exposure-*` slug）
- 当前值 = 0 且无盘口的仓位：`decide_exit` 已加 `position_zero_value_no_orderbook` 跳过逻辑，不再重复挂无效 SELL
- 已结算胜利方向：用 `/markets/{condition_id}/settlement` 手动触发结算；输方接受损失，无法强制卖出

## 16. 直播源匹配与数据缺失排查

Goalserve 覆盖面极广，主要联赛/赛事几乎必然有数据。发现 `missing_live_game_state` 或源匹配失败时**首先假定是我们这边的问题**，按以下顺序排查：

1. **路由**：该运动的 feed 路径是否正确（inplay vs livescore vs getfeed）
2. **认证/网络**：inplay 出口 IP 是否白名单（`GOALSERVE_PROXY` 可达；HTTP 429 = 同 sport 轮询超速正常退避，非故障）；livescore API key 是否有效
3. **解析**：parser 是否正确处理 XML/JSON；是否静默丢弃数据
4. **名称匹配**：团队名拼写/格式
5. **时区/日期**：市场 slug 日期是否与事件实际 UTC 日期不一致（如午夜场次）
6. **配置未启用**：该运动是否加入对应 `.env` 配置

确认非 Goalserve 数据问题后，才考虑使用其他源补充。

**匹配校准**：市场与直播源匹配后必须有校准检查，验证 team名、赛事、时间窗口三者对齐；可疑（模糊匹配、时区偏移等）记录原因不静默接受。匹配失败市场后台持久化记录 condition_id / market_slug / gap_urgency / 时间戳 / 失败原因；实时快照走 `/candidates/live-source-gaps`。

**无匹配时排查义务**：

- 正在对局的运动若无匹配直播数据（`started_or_past_due` gap）必须按上述 1-6 排查，确认是 Goalserve 覆盖缺失才标 `unsupported_league`
- 系统中无任何正在对局赛事时，先去 https://polymarket.com/zh/sports/live 人工核对；Polymarket 有但系统没有，必须定位原因并修复，不能静默无感知

**无交易机会时的差异核对**：当 candidates 全被拒、`ready_to_trade=0` 时不能直接判定"市场空窗"，必须人工核对 https://polymarket.com/zh/sports/live 与我方系统逐一比对：

- Polymarket 有该直播市场、我方没发现 → discovery 缺口
- 我方有市场但无 live state → 直播源映射缺口
- 有 live state 但 candidate 被拒 → 按 §15 门禁复盘核对拒绝原因
- 逐项确认每个差异都有合理解释，才能判定确实无机会

## 17. 外部数据源与状态真相源（反复犯过的错都记这里）

所有以下规则都是从实盘事故 + 性能审计踩出来的，**重新设计或新增数据源前先读完**。

### 17.1 数据库只做审计，不做运行时

- **启动期**：不读 DB 预热任何运行时状态（balance / positions / orders / markets / account_snapshot 都不读）。Polymarket 第一轮 reconcile（秒级）拉回所有权威值；readiness gate 阻挡空窗期下单
- **运行时**：所有 workers / schedulers / supervisor 路径不允许 `db_session_factory()` 读取（dead_records_purge 这种 DB 自身 retention 维护是例外）
- **唯一允许**：operator/audit API 端点同步读 DB 历史（操作员主动查询），不参与决策回路

### 17.2 WS 是盘口唯一真相源，REST `/book` 只剩 sequence_gap 兜底

- **不要**为盘口数据加任何"周期性 REST 校准"——Polymarket WS 订阅后 ~1s 自动推完整 `book`，之后 `price_change`/`book` 持续推送
- **不要**在 ws_loops 订阅时 `refresh_rest_snapshots` 预取——决策 gate 本来就等 reconcile_fresh
- **不要**在 reconcile authority refresh 里调 `get_orderbook`
- **唯一允许 REST 调 /book**：WS 真漏推消息时 `market_ws_worker._handle_sequence_gap` 重建一致状态。正常稳态 0 次/小时

### 17.3 paper_mode 与 live_mode：account_state_store 必须只有一个 writer

- **paper_mode**：`paper_balance_syncer` 每秒从 `paper_ledger` 投影回 `AccountStateStore`；reconcile authority `refresh_account` 必须早退（`paper_mode=True` 守住）
- **live_mode**：`UserAccountPoller` 是唯一 writer，reconcile 主循环 `refresh_account_inline=False` 跳过用户态拉取
- **绝对不允许两个 writer 同时写同一份状态**。每次新增写者必须问"我会和谁抢？"

### 17.4 gamma `/events` 已含 nested markets，不要双调 `/markets`

- `GammaEventDTO.markets` 完整字段（tick_size / fee / outcomes / closed / token_ids）
- discovery 每 N 秒拉 `/events?live=true` 后把 `event.markets` 写进 `GammaMarketSnapshotStore`
- 所有 reconcile/settlement/enrich 路径**先读 store**（`gamma_snapshot_store.get_dto_if_fresh(cid, max_age_s=30)`），命中即用；只有 cache miss/stale 才走 `/markets?condition_ids=` 反查
- **不要**给每个 tracked market 单调一次 `/markets?slug=...`
- 唯一需要 `/markets?condition_ids=` 直查：已 settled 市场（`closed=true` 不在 `live=true` 范围）+ 孤儿 condition 反查

### 17.5 gamma `/markets/{id}` 只接受 Polymarket 内部数值 id

- 不要传 condition_id 或 slug（会 422）
- 用 condition_id 反查：`gamma_client.get_market_by_condition_id(cid)`（内部走 `/markets?condition_ids=cid&limit=1`）

### 17.6 仓位状态 ≠ 市场状态

- **仓位 payload** 只承载仓位生命周期：`shares / avg_price / cur_price / cash_pnl / redeemable / settled_zero_value`。`paused` 字段只反映 `MarketPauseSource.MANUAL`（人工触发），后台 reconcile/risk/strategy 自动 pause 不进仓位 payload
- **市场状态**（`trading_status`、自动 quarantine）只从 `/markets` 端点返回，不挂在仓位上
- 前端 badge 三/四态：`归零 / 暂停(人工) / 可赎回 / 持有`——不掺杂市场状态

### 17.7 redeemable 持仓必须用 gamma outcomePrices 派定胜负

- data API 返回 `redeemable=True` 仅说"市场关了可领"，不说赢方是谁
- `Position.cur_price` 默认从 data API drop（避免 stale 骗 MTM），redeemable 仓位刚拉到 cur_price=None
- `_enrich_redeemable_positions` reconcile 时并发调 `get_market_by_condition_id` → 读 outcomePrices → `apply_outcome_to_position` 派定胜方 token_id → cur_price=1（赢）/ 0（输）
- 输方 cur_price=0 + redeemable=True → `settled_zero_value=True` → UI 默认隐藏

### 17.8 不订阅就不轮询（demand-driven）

- 所有 sport-specific 数据源（goalserve inplay / livescore / pregame）按"tracked market 实际涉及的 sport"启停
- **不要**起全运动 polling worker 无视 tracked market
- 新增数据源前先想"什么 condition 会触发轮询"

### 17.9 周期 cadence 校准原则

- **discovery `/events`**：2s
- **gamma snapshot store 新鲜度阈值**：30s
- **WS orderbook 新鲜度阈值**：30s（WS 正常每秒至少推一条；30s 没动静才视为"WS 真死了"触发 REST 兜底）
- **user_account_poll**：和 `market_sync_interval_seconds` 同 cadence（默认 20s），独立 scheduler job
- **settlement_scanner**：5 分钟
- 改 cadence 前先回答："这个数据真的会在新 cadence 周期内变化吗？变化但没及时拉到，损失是什么？"

### 17.10 新增外部 API 客户端前的强制问题清单

1. 这个数据有没有 WS 推送版本？有就别用 REST 轮询
2. 加这条调用预计每小时调多少次？是否 demand-driven（无 tracked 时不跑）
3. 写入哪个 in-memory store？同一 store 是否已有别的 writer（race 风险）
4. 读取方是 P0 决策路径还是后台 worker？P0 上不允许同步等网络
5. 数据是否进 DB？如"是"——确认是 audit/persistence 单向写入，不在运行时回读
6. 同一数据如果已经在某个内存 store 里，能否走 store 命中后再 fallback 网络
7. 失败兜底是什么？stale 多久后该降级 / 报警 / quarantine
