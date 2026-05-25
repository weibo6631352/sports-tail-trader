# 配置说明

先分清两类配置：

- 框架配置：写 `.env`
- 策略规则：写策略包；当前策略在 `src/polymarket_trader/quant/`

## 示例文件

- [`.env.example`](../.env.example)：最小复制入口，只保留常用启动项和自动交易闸门。
- [`.env.full.example`](../.env.full.example)：完整覆盖项清单，字段名与 `Settings` 一一对应。
- 运行时默认值以 `src/polymarket_trader/config.py` 的 `Settings` 为准。

## 先看哪里

| 分组 | 示例 | 说明 |
| --- | --- | --- |
| Polymarket 地址 | `POLYMARKET_CLOB_HOST`、`POLYMARKET_MARKET_WS` | 外部 API / WS 地址 |
| Bankroll 软上限 | `PORTFOLIO_BUDGET_USDC` | bankroll = min(链上 USDC, 此值) |
| Kelly sizing | `KELLY_FRACTION`、`KELLY_MAX_POSITION_FRACTION`、`KELLY_MIN_EDGE`、`KELLY_MIN_STAKE_USDC`、`KELLY_DRAWDOWN_HALT_FRACTION` | Kelly 公式参数（替代旧 max_order/market/total_usdc + max_open_orders） |
| 同步与重试 | `MARKET_SYNC_INTERVAL_SECONDS`、`ORDER_RETRY_LIMIT` | reconcile 与失败处理 |
| 体育直播状态源 | `SPORTS_LIVE_STATE_ENABLED`、`SPORTS_LIVE_STATE_LEAGUES` | 外部比分/阶段事实输入，不承载策略阈值 |
| 性能隔离 | `TRADING_EVENT_QUEUE_MAX_SIZE`、`TRADING_WORKER_THREADS` | 交易主链路与后台维护 / 异步支撑队列及执行器隔离 |
| 超时告警 | `ORDER_SUBMIT_TIMEOUT_MS`、`CRITICAL_LOCK_TIMEOUT_MS` | 防止交易链路无限等待 |
| 数据库 | `DATABASE_URL`、`DATABASE_HOST` | PostgreSQL 连接地址；支持完整 URL 或拆分字段 |
| 密钥 | `POLYMARKET_API_KEY`、`WALLET_PRIVATE_KEY` | 只能通过安全环境注入 |
| 策略装配 | `WORKFLOW_CONFIG_PATH` | 可选 TOML/JSON 文件，覆盖 `TradingWorkflowConfig` 默认值 |
| 策略规则 | `src/polymarket_trader/quant/` | 当前量化策略的交易阈值、筛选语义、订阅规则 |

## 规则

- 当前只装配一个量化策略（`polymarket_trader.quant.TradingWorkflow`）；策略业务语义收口在 `src/polymarket_trader/quant/`。
- `Settings` 只接受已声明字段；未声明字段和已淘汰字段会直接报错，不会静默忽略。
- `.env.example` 只保留常用启动项；不常改的默认值直接看 `Settings`，需要显式覆盖时再写 `.env.full.example`。
- `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE` 要么同时提供，要么全部留空并在运行时派生。
- 会影响资金风险的配置应默认保守，不能默认放大仓位。
- 队列和线程池必须有上限。
- 超时字段统一用 `_MS` 或 `_SECONDS`。

## 自动交易闸门

- `validate_startup_readiness` 会在 `WALLET_PRIVATE_KEY` 为空时阻止系统进入 `ready_to_trade=true`。
- `PORTFOLIO_BUDGET_USDC` 不大于 `0` 时系统不会进入自动交易态——Kelly 引擎在 bankroll<=0 时拒新仓但不报错，启动期把它升级为 blocking 避免"看起来在跑但永远不下单"。
- Kelly sizing 不再用绝对 USDC 上限；`KELLY_MAX_POSITION_FRACTION` × bankroll 是单市场仓位天花板，`KELLY_MIN_STAKE_USDC` + `market.min_order_size × price` 决定每笔下单下限。详见 [`src/polymarket_trader/domain/kelly.py`](../src/polymarket_trader/domain/kelly.py) docstring。
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
- `MarketTickWorker.entry_metadata_provider` 只读取内存 `MarketMetadataStore`，不会在 P0 路径请求外部 API。
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

### `audit_events` 表 14 天 retention purge

- `audit_retention_days`（默认 14，ge=0 le=365）：早于该天数的 `audit_events` 行会被
  daily purge 删除。设为 **0** 显式禁用 retention（运维仅在调试时使用）。
- `audit_retention_interval_seconds`（默认 86400 = 24h）：purge job 的运行周期。
- `audit_retention_purge_batch_size`（默认 10000）：单批 DELETE 锁定的最大行数；
  CTE-based ctid 批 + `FOR UPDATE SKIP LOCKED` 避免大表锁。
- 实现：`app/audit_retention.py` + `supervisor.register_worker("audit_retention_purge", priority="P3")`，
  失败仅日志不抛错，不阻塞 P0 交易热路径。
- 三个字段都在 ParameterStore 白名单——运行时可改保留期（重启即丢）。

## 策略包

策略代码统一收口在 `src/polymarket_trader/quant/`：

- 入口主类：`src/polymarket_trader/quant/workflow.py`（`TradingWorkflow`）
- 量化决策中枢：`src/polymarket_trader/quant/quant_decider.py`（`QuantDecider`——所有 BUY/SELL/cancel/replace 决策的唯一入口）
- 配置：`src/polymarket_trader/quant/config.py`（`TradingWorkflowConfig`，TOML/JSON 文件覆盖入口 = `WORKFLOW_CONFIG_PATH`）
- 远端 discovery 粗筛输入：`discovery_title_searches` / `discovery_tag_slugs`；当前默认用 `sports` tag 扩大市场扫描。Gamma Events keyset 文档：<https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- 直播比赛驱动 discovery：`tail_live_discovery_max_games` 控制每轮最多取多少个直播源比赛生成高意图查询，`tail_live_discovery_max_queries` 控制追加 query 上限；默认按 live 状态优先 + Polymarket 单场盘口覆盖度（NBA/NHL/MLB/ATP/WTA 优先）。
- 受控加仓参数：`tail_scale_in_budget_fraction` 控制单次加仓预算相对首笔 BUY 成交额的比例，`tail_scale_in_max_buy_fills` 控制同 token BUY 成交次数上限。
- 直播状态 freshness：`tail_max_game_state_age_seconds` 通用，`tail_baseball_max_game_state_age_seconds` 用于 MLB 官方结构化局面，`tail_tennis_max_game_state_age_seconds` 用于网球。
- MLB 第 8 局 moneyline 早期机会：`tail_mlb_eighth_moneyline_min_lead` 默认 2；第 9 局沿用 `tail_min_moneyline_lead` 终局规则。
- 资金效率参数：`tail_min_expected_profit_usdc` / `tail_min_expected_profit_per_hour_usdc` / `tail_settlement_hold_minutes`；一档 profit-take SELL 满足 `tail_profit_take_min_profit_usdc` 绝对毛利或按 `tail_profit_take_hold_minutes` 折算的每小时资金效率即放行。
- 历史仓位补救：`tail_recovery_profit_take_enabled`（默认开），`tail_recovery_profit_take_min_avg_price`（默认 0.90）限定补 profit-take SELL 的仓位均价下限。
- 候选市场减仓：本地市场仍处于 `candidate` 时，风控只允许已有持仓完全覆盖的 SELL 减仓退出通过。
- 子模块布局（均在 `src/polymarket_trader/quant/` 下）：
  - 体育扫尾子策略：`tail/`（types / slug）
  - 通用解析与联赛工具：`src/polymarket_trader/sports/`（parsing / leagues / slug / types）
  - 系列赛 / 冠军子策略：`series/` / `outright/`
  - 市场筛选 / 跟踪：`universe.py` / `tracking.py`
  - 交易决策辅助：`trading/`（allocation / gates / matching / pricing / exit_overlay / risk_limits / helpers）
  - 恢复：`recovery.py`

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

- `settings.{portfolio_budget_usdc, kelly_fraction, kelly_max_position_fraction,
  kelly_min_edge, kelly_min_stake_usdc, kelly_allow_round_up_to_market_min,
  kelly_round_up_max_overbet_ratio, kelly_drawdown_halt_fraction,
  order_retry_limit, audit_retention_days}`
- `strategy.{tail_outright_min_edge_bps, tail_outright_max_entry_price,
  tail_outright_min_orderbook_depth_usdc, tail_outright_exit_edge_target,
  tail_outright_min_profit_per_share, entry_no_price_max,
  tail_moneyline_max_entry_price, tail_spreads_max_entry_price,
  tail_min_liquidity_usdc, tail_outright_budget_usdc,
  tail_stale_no_live_state_seconds}`

注意 `tail_outright_budget_usdc` 默认 **0**——所有 outright family 市场（赛季冠军 /
球员奖项 / 转会等长期事件）会落 `outright_budget_zero` 拒绝，仅审计不下单。
要启用 outright 自动交易：`POST /parameters/strategy/tail_outright_budget_usdc`
（值 ≥ `tail_outright_max_per_market_usdc`，默认 25）。

边界：

- Override **重启即丢**。Long-term 固化仍走 `.env` 改 `Settings` 或策略
  config 文件后重启。
- 密钥 / SecretStr 字段、连接串、`WORKFLOW_CONFIG_PATH` 等不在白名单——不能通过 API 改。
- 策略侧消费 override 走 `ports.parameter`（`src/polymarket_trader/quant/parameter_overrides.py` 提供 effective_int / effective_decimal helper）。

## 新增配置时确认

- 属于交易主链路、关键修复链路、后台维护链路还是异步支撑链路。
- 默认值是什么，默认值是否安全。
- 单位是什么，取值范围是什么。
- 是否需要进白名单接入 `/parameters` 运行时热更新（只有探索性调参才需要；
  长期值仍走 `.env`）。
- 是否需要写入 [`.env.full.example`](../.env.full.example)；如果属于最常用启动项，再同步写入 [`.env.example`](../.env.example)。
- 如果只是策略规则，直接写 `src/polymarket_trader/quant/`（或通过 `WORKFLOW_CONFIG_PATH` 指向的 TOML），不要新增框架环境变量。
- 是否会改变资金暴露、订单行为或 reconcile 行为。
