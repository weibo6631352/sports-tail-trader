# 配置说明

先分清两类配置：

- 框架配置：写 `.env`
- 策略规则：写策略包；默认示例策略在 `src/strategies/current/`

## 示例文件

- [`.env.example`](../.env.example)：最小复制入口，只保留常用启动项和自动交易闸门。
- [`.env.full.example`](../.env.full.example)：完整覆盖项清单，字段名与 `Settings` 一一对应。
- 运行时默认值以 `src/polymarket_trader/config.py` 的 `Settings` 为准。

## 先看哪里

| 分组 | 示例 | 说明 |
| --- | --- | --- |
| Polymarket 地址 | `POLYMARKET_CLOB_HOST`、`POLYMARKET_MARKET_WS` | 外部 API / WS 地址 |
| 组合预算 | `PORTFOLIO_BUDGET_USDC`、`MAX_ORDER_USDC`、`MAX_MARKET_USDC` | 控制总预算、单笔和单 market 上限 |
| 框架风控阈值 | `MAX_OPEN_ORDERS` | 运行时公共风控门禁 |
| 同步与重试 | `MARKET_SYNC_INTERVAL_SECONDS`、`ORDER_RETRY_LIMIT` | reconcile 与失败处理 |
| 体育直播状态源 | `SPORTS_LIVE_STATE_ENABLED`、`SPORTS_LIVE_STATE_LEAGUES` | 外部比分/阶段事实输入，不承载策略阈值 |
| 性能隔离 | `TRADING_EVENT_QUEUE_MAX_SIZE`、`TRADING_WORKER_THREADS` | 交易主链路与后台维护 / 异步支撑队列及执行器隔离 |
| 超时告警 | `ORDER_SUBMIT_TIMEOUT_MS`、`CRITICAL_LOCK_TIMEOUT_MS` | 防止交易链路无限等待 |
| 数据库 | `DATABASE_URL`、`DATABASE_HOST` | PostgreSQL 连接地址；支持完整 URL 或拆分字段 |
| 密钥 | `POLYMARKET_API_KEY`、`WALLET_PRIVATE_KEY` | 只能通过安全环境注入 |
| 扩展装配 | `EXTENSION_MODULE`、`EXTENSION_CONFIG_PATH` | 选择要装配的业务扩展包和可选配置文件 |
| 策略规则 | `src/strategies/current/` | 默认示例策略的交易阈值、筛选语义、订阅规则 |

## 规则

- 框架只通过 `EXTENSION_MODULE` 选择业务扩展实现；策略业务语义仍然收口在 `src/strategies/`。
- `Settings` 只接受已声明字段；未声明字段和已淘汰字段会直接报错，不会静默忽略。
- `.env.example` 只保留常用启动项；不常改的默认值直接看 `Settings`，需要显式覆盖时再写 `.env.full.example`。
- `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE` 要么同时提供，要么全部留空并在运行时派生。
- 会影响资金风险的配置应默认保守，不能默认放大仓位。
- 队列和线程池必须有上限。
- 超时字段统一用 `_MS` 或 `_SECONDS`。

## 自动交易闸门

- `validate_startup_readiness` 会在 `WALLET_PRIVATE_KEY` 为空时阻止系统进入 `ready_to_trade=true`。
- `PORTFOLIO_BUDGET_USDC`、`MAX_ORDER_USDC`、`MAX_MARKET_USDC`、`MAX_TOTAL_USDC`、`MAX_OPEN_ORDERS` 任一不大于 `0` 时，系统不会进入自动交易态。
- `MAX_ORDER_USDC`、`MAX_MARKET_USDC` 和 `MAX_TOTAL_USDC` 需要覆盖目标市场的 `min_order_size`；Polymarket 体育市场常见最小下单金额为 `5` USDC，低于该值时即使策略发现 `auto_execute` 机会也会被风控拒绝。
- `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE` 只填部分字段时，系统不会进入自动交易态。
- `POLYMARKET_SIGNATURE_TYPE` 为 `1` 或 `2` 但未填写 `POLYMARKET_FUNDER_ADDRESS` 时，系统不会进入自动交易态。
- `SPORTS_LIVE_STATE_ENABLED=true` 时，`SPORTS_LIVE_STATE_SOURCES` 只支持 `espn,nba,nhl,mlb,sofascore,thesportsdb`，且 `SPORTS_LIVE_STATE_LEAGUES` 至少需要一个可由启用源覆盖的联赛代码；配置错误会阻止系统进入自动交易态。

## 体育直播状态源

这些配置只决定运行时从哪里读取比分、阶段和剩余时间，不属于策略参数：

- `SPORTS_LIVE_STATE_ENABLED`：是否启用外部直播状态同步 worker。
- `SPORTS_LIVE_STATE_SOURCES`：逗号分隔的直播源代码，当前支持 `espn,nba,nhl,mlb,sofascore,thesportsdb`；默认启用全部。
- `SPORTS_LIVE_STATE_ESPN_BASE_URL`：ESPN site API 基础地址。
- `SPORTS_LIVE_STATE_NBA_BASE_URL`：NBA liveData 基础地址。
- `SPORTS_LIVE_STATE_NHL_BASE_URL`：NHL score API 基础地址。
- `SPORTS_LIVE_STATE_MLB_BASE_URL`：MLB Stats API 基础地址。
- `SPORTS_LIVE_STATE_SOFASCORE_BASE_URL`：SofaScore 公开 scheduled-events API 基础地址。
- `SPORTS_LIVE_STATE_SOFASCORE_LOOKAHEAD_DAYS`：SofaScore 除当前 UTC 比赛日外额外拉取的近未来天数，默认 `3`，上限 `3`；用于提前覆盖近未来开赛的单场市场，避免等到比赛日才写入直播状态。
- `SPORTS_LIVE_STATE_THESPORTSDB_BASE_URL`：TheSportsDB 公开 eventsday API 基础地址；当前只启用已验证可用的 NHL/MLB 映射，并在适配器内做本地限频缓存。
- `SPORTS_LIVE_STATE_LEAGUES`：逗号分隔的联赛代码，例如 `nba,nhl,nfl,mlb,tennis,sports`；`sports` 是通用覆盖码，会让 SofaScore 拉取 basketball、ice-hockey、baseball、american-football、football、tennis 和 table-tennis 的 scheduled-events，用于整个体育市场的直播状态覆盖与匹配诊断。
- `SPORTS_LIVE_STATE_INTERVAL_SECONDS`：P2 同步任务间隔，默认 `5` 秒。
- `SPORTS_LIVE_STATE_TIMEOUT_S`：外部请求超时。
- `SPORTS_LIVE_STATE_PUBLISH_ENTRY_SIGNALS`：直播状态更新后是否发布 `ENTRY_SIGNAL_TRIGGERED`，用于让交易主链路基于最新 metadata 重放入场判断。

运行时行为：

- 外部 API 请求只发生在 `sports_live_state_sync` P2 scheduler job 中；ESPN、NBA、NHL、MLB、SofaScore 和 TheSportsDB 会先聚合成单个 `sports_live_aggregate` 快照再进入匹配。
- 聚合去重优先保留官方源状态；通用免费源与官方源状态冲突时，通用源只记录为 `source_conflicts`，交易侧会降级拒绝自动执行。
- `TradingDecisionWorker.entry_metadata_provider` 只读取内存 `EntryMetadataStore`，不会在 P0 路径请求外部 API。
- 同步状态通过 `/runtime`、`/workers`、`/metrics` 的 `sports_live_sync` 字段和前端候选页展示；`source_statuses.health` 区分 `success_with_live_data`、`success_empty`、`cached`、`rate_limited` 和 `failed`。

默认覆盖边界：

| 联赛 | 主要源 | 自动交易默认语义 |
| --- | --- | --- |
| NBA | ESPN、NBA liveData、SofaScore | 支持 Totals / Moneyline / Spreads 自动评估 |
| NHL | ESPN、NHL score、SofaScore、TheSportsDB | 支持 Totals / Moneyline / Spreads 自动评估 |
| MLB | ESPN、MLB Stats、SofaScore、TheSportsDB | 使用棒球局面字段评估，不使用伪造剩余秒数 |
| NFL | ESPN、SofaScore | 只生成候选和人工确认，不默认自动下单 |
| Tennis | SofaScore | 优先发现 ATP/WTA；Totals 区分整场总局数和总盘数；直播中只自动评估已锁定 Over 和整场胜负接近锁定局面，已结束未封盘时可按最终结构化盘分/局分判断 set winner、Over/Under 和 Moneyline；Spreads 暂不自动执行 |
| 其他体育市场 | SofaScore `sports` 覆盖码 | 默认只补直播状态和候选诊断；是否自动执行仍由当前策略 market family、盘口规则和风控决定 |
| Esports | 默认不在自动交易发现范围 | 需补源和策略校准后再启用 |

## 数据库

- 开发环境先准备 PostgreSQL，再调用 `polymarket_trader.infra.db.initialize_database` 按当前 metadata 建表。
- schema 结构变更时，直接重建开发库再初始化。

连接顺序：
- `DATABASE_URL` 优先级最高；一旦填写，`DATABASE_DRIVER`、`DATABASE_HOST`、`DATABASE_PORT`、`DATABASE_NAME`、`DATABASE_USER`、`DATABASE_PASSWORD` 会被忽略。
- 当 `DATABASE_URL` 为空时，运行时会用上述拆分字段拼接 PostgreSQL 连接串。

## 扩展文件

- 当 `EXTENSION_MODULE=strategies.current` 时，对应文件为：
- manifest：`src/strategies/current/manifest.py`
- 入口：`src/strategies/current/strategy.py`
- 配置：`src/strategies/current/config.py`
- 远端 discovery 粗筛输入：`src/strategies/current/config.py` 的 `discovery_title_searches` / `discovery_tag_slugs`；当前默认可以用 `sports` tag 扩大市场扫描，但本地 universe 只按已覆盖联赛 token 通过候选，官方 Gamma Events keyset 文档：<https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- 直播比赛驱动 discovery：`tail_live_discovery_max_games` 控制每轮最多取多少个直播源比赛生成高意图查询，`tail_live_discovery_max_queries` 控制追加 query 上限；默认会先按 live 状态排序，再按 Polymarket 单场盘口覆盖度优先使用 NBA/NHL/MLB/ATP/WTA，避免 ITF 等低覆盖赛事消耗扫描预算。这两个字段属于策略配置，不写入框架 `.env`。
- 受控加仓参数：`tail_scale_in_budget_fraction` 控制单次加仓预算相对首笔 BUY 成交额的比例，`tail_scale_in_max_buy_fills` 控制同 token BUY 成交次数上限；这两个字段属于策略配置，不写入框架 `.env`。
- 直播状态 freshness：`tail_max_game_state_age_seconds` 是通用 live 状态最大年龄，`tail_baseball_max_game_state_age_seconds` 只用于 MLB 官方结构化局面，避免 schedule/linescore 批量同步和市场匹配耗时把第 9 局真实尾盘误判为 stale；网球继续使用 `tail_tennis_max_game_state_age_seconds`。
- MLB 第 8 局 moneyline 早期机会：`tail_mlb_eighth_moneyline_min_lead` 默认 2，只在第 8 局、至少一出局、领先方达到该分差且二/三垒无得分威胁时放行；第 9 局仍沿用更强的 `tail_min_moneyline_lead` 终局规则。
- 资金效率参数：`tail_min_expected_profit_usdc` 和 `tail_min_expected_profit_per_hour_usdc` 控制等待权威结算时的最低预期毛利润与每小时资金效率，`tail_settlement_hold_minutes` 是保守结算占用时长估算。低于结算效率门槛的单子不会直接长期持有；一档 profit-take SELL 只要满足 `tail_profit_take_min_profit_usdc` 的绝对毛利润，或按 `tail_profit_take_hold_minutes` 折算后的每小时资金效率达到 `tail_min_expected_profit_per_hour_usdc`，就允许买入并在成交后挂卖。结算效率已经达标的单子也会在一档 profit-take 毛利润达标时附加止盈挂单，成交则提前释放资金，未成交则继续等待权威结算。
- 历史仓位补救：`tail_recovery_profit_take_enabled` 默认开启，用于恢复侧发现近端高成本价、无开放 SELL 的旧仓或漏挂止盈仓位时补一张 profit-take SELL；`tail_recovery_profit_take_min_avg_price` 限定只处理均价不低于默认 `0.90` 的仓位，预期毛利润仍沿用 `tail_profit_take_min_profit_usdc`，避免把低成本 settlement 仓位误改成主动退出。若历史市场缺少完整 outcomes 导致体育目标无法解析，恢复侧只允许 eligible 市场做这种 sell-only profit-take 补救，并继续暂停该市场新增交易。
- 候选市场减仓：本地市场仍处于 `candidate` 时，框架风控只允许已有持仓完全覆盖的 SELL 减仓退出通过；BUY 或无持仓 SELL 仍按 market gate 拒绝，避免把历史补救扩大成新增风险暴露。
- 体育扫尾模型和权限：`src/strategies/current/tail/`（types / core / evaluator / slug / mlb / tennis 子模块），通用解析与联赛工具在 `src/strategies/sports_framework/`（parsing / leagues / slug / types）。策略目标范围是整个体育市场；所有体育盘口应优先被解析成统一 market family / market type / side / line。没有专用胜率模型的单场 Yes/No prop 只做 record-only 候选诊断，不能绕过策略评估、资金效率和风控进入自动执行。
- 体育扫尾策略级风控：`src/strategies/current/risk.py`
- 体育扫尾退出计划：`src/strategies/current/exit_plan.py`
- 盘口方向解析：`src/strategies/current/outcomes.py`
- 市场筛选：`src/strategies/current/universe.py`
- 交易决策：`src/strategies/current/trading/`（拆分为 hooks/allocation/gates/matching/pricing/exit_overlay/risk_limits/helpers 子模块）
- 恢复与跟踪：`src/strategies/current/recovery.py` / `src/strategies/current/tracking.py`
- 扩展契约：`src/polymarket_trader/extension_api/`
- 扩展外部配置路径：`EXTENSION_CONFIG_PATH`

## 密钥规则

不得提交到仓库：
- Polymarket API key / secret / passphrase。
- wallet private key 或 signer 配置。
- builder attribution credentials。
- 数据库密码和生产连接串。

日志和审计事件中不得输出：
- 完整签名 payload。
- 私钥、API secret、passphrase。
- 未脱敏 raw response 中的敏感账户字段。

## 运行时调参（不进 `.env`）

启动期 `Settings` 与策略 frozen config 都是不可变的；探索性临时调参用
`/parameters` 端点，走独立的 `ParameterStore` runtime override 层。

- `GET /parameters` 列所有可调参数及当前 override 状态。
- `PUT /parameters/{scope}/{key}` 设置 override；`scope` 是 `settings` 或
  `strategy`。每次写入通过 `PARAMETER_OVERRIDE_APPLIED` 事件落 audit_events。
- `DELETE /parameters/{scope}/{key}` 清除 override，回落 Settings / config 默认值。

可调白名单（详见 `src/polymarket_trader/app/parameter_store.py`）：

- `settings.{portfolio_budget_usdc, max_order_usdc, max_market_usdc,
  max_total_usdc, max_open_orders, order_retry_limit}`
- `strategy.{tail_outright_min_edge_bps, tail_outright_max_entry_price,
  tail_outright_min_orderbook_depth_usdc, tail_outright_exit_edge_target,
  tail_outright_min_profit_per_share, entry_no_price_max,
  tail_moneyline_max_entry_price, tail_spreads_max_entry_price,
  tail_min_liquidity_usdc}`

边界：

- Override **重启即丢**。Long-term 固化仍走 `.env` 改 `Settings` 或策略
  config 文件后重启。
- 密钥 / SecretStr 字段、连接串、`EXTENSION_MODULE` 等不在白名单——不能通过
  API 改。
- 策略侧消费 override 需要策略代码主动走 `ports.parameter`；具体写法见
  [`strategy_authoring.md`](./strategy_authoring.md)。

## 新增配置时确认

- 属于交易主链路、关键修复链路、后台维护链路还是异步支撑链路。
- 默认值是什么，默认值是否安全。
- 单位是什么，取值范围是什么。
- 是否需要进白名单接入 `/parameters` 运行时热更新（只有探索性调参才需要；
  长期值仍走 `.env`）。
- 是否需要写入 [`.env.full.example`](../.env.full.example)；如果属于最常用启动项，再同步写入 [`.env.example`](../.env.example)。
- 如果只是策略规则，直接写 `EXTENSION_MODULE` 指向的策略包，不要新增框架环境变量。
- 是否会改变资金暴露、订单行为或 reconcile 行为。
