# 配置说明

两类配置严格分开：

- **框架配置（基础设施）** → `.env` → `Settings`（[`src/polymarket_trader/config.py`](../src/polymarket_trader/config.py)）
  - DB / Polymarket 接入 / 队列容量 / 数据源 / retention / 密钥
- **策略配置（业务规则）** → 仓库根 `strategy_config.toml` → `TradingWorkflowConfig`（[`src/polymarket_trader/workflow/config.py`](../src/polymarket_trader/workflow/config.py)）
  - Kelly κ / edge 门槛 / 入场价上限 / 流动性下限 / 各 family 预算
  - 启动期由 `load_workflow_config()` 从 `strategy_config.toml` 自动加载，无 env 间接层

`.env` 里不允许出现策略参数；`Settings` 用 `extra="forbid"`，写错字段名直接启动失败。

## 模板

- [`.env.example`](../.env.example) — 含全部 `Settings` 字段的单一模板。复制为 `.env` 后按需填写。
- `Settings` 默认值以 [`config.py`](../src/polymarket_trader/config.py) 为准；模板默认值与代码保持同步。

## 字段分组速查

| 分组 | 代表字段 | 用途 |
| --- | --- | --- |
| Polymarket 接入 | `POLYMARKET_CLOB_HOST`、`POLYMARKET_MARKET_WS` | CLOB / Gamma / Data / WS 端点 |
| 钱包与签名 | `WALLET_PRIVATE_KEY`、`POLYMARKET_SIGNATURE_TYPE`、`POLYMARKET_FUNDER_ADDRESS` | 签名身份 |
| Bankroll | `PORTFOLIO_BUDGET_USDC` | bankroll 软上限；0 → 不下单 |
| Paper mode | `PAPER_TRADING_MODE` | true → submit 不上链，本地账本撮合 |
| 同步周期 | `MARKET_SYNC_INTERVAL_SECONDS` | reconcile chain balance 周期 |
| 体育直播 | `SPORTS_LIVE_STATE_*`、`GOALSERVE_*` | Goalserve inplay/livescore/pregame 配置 |
| H2H 赔率 | `SPORTS_SEASON_ODDS_*`、`SPORTS_GAME_ODDS_*` | the-odds-api 配置（驱动 series winner p_per_game） |
| 性能 / 队列 | `TRADING_EVENT_QUEUE_MAX_SIZE`、`TRADING_WORKER_THREADS` | 主链路 / 维护 / 持久化队列与线程池上限 |
| 超时 | `ORDER_SUBMIT_TIMEOUT_MS`、`CRITICAL_LOCK_TIMEOUT_MS` | 防止主链路无限等待 |
| API 面 | `EXPOSE_OPENAPI_DOCS`、`ADMIN_API_TOKEN`、`CORS_ALLOWED_ORIGINS` | Operator API 暴露面 |
| 数据库 | `DATABASE_URL` 或 `DATABASE_HOST`/`PORT`/`USER`/`PASSWORD`/`NAME` | PostgreSQL 连接 |
| 审计 retention | `AUDIT_RETENTION_DAYS`、`FILLS_RETENTION_DAYS` 等 | 各审计表保留期（天；0=关闭） |
| 密钥 | `POLYMARKET_API_KEY`、`WALLET_PRIVATE_KEY`、`GOALSERVE_API_KEY` 等 | 只通过环境变量注入 |

## 自动交易闸门

`Settings.validate_startup_readiness()` 在启动期检查，任一不满足都把 `ready_to_trade` 卡成 `false`：

- `WALLET_PRIVATE_KEY` 不能为空。
- `PORTFOLIO_BUDGET_USDC > 0`。
- `MARKET_SYNC_INTERVAL_SECONDS > 0`。
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE` 要么全填，要么全空。
- `POLYMARKET_SIGNATURE_TYPE in {0,1,2}`；1/2 时必须填 `POLYMARKET_FUNDER_ADDRESS`。
- `SPORTS_LIVE_STATE_ENABLED=true` 时 `GOALSERVE_API_KEY` 非空 + 至少一个 league。

闸门状态通过 `/ready` 端点暴露。

## 体育直播状态源

Goalserve 是当前唯一的体育直播数据源（早期 ESPN/NBA/NHL/MLB/SofaScore/TheSportsDB 已全部迁移）。

三条独立链路：

| 链路 | 端点形式 | 认证 | 实现 | 启用方式 |
| --- | --- | --- | --- | --- |
| Inplay GZIP feed | `inplay.goalserve.com/inplay-{sport}.gz` | keyless（IP 白名单，可走 `GOALSERVE_PROXY`） | [`infra/sports/goalserve_inplay_client.py`](../src/polymarket_trader/infra/sports/goalserve_inplay_client.py) | `SPORTS_LIVE_STATE_ENABLED=true` + demand-driven |
| Livescore getfeed | `getfeed/{key}/{sport}/home?json=1` | `GOALSERVE_API_KEY` | [`infra/sports/goalserve_livescore_client.py`](../src/polymarket_trader/infra/sports/goalserve_livescore_client.py) | `GOALSERVE_LIVESCORE_ENABLED=true` |
| Pregame odds | `getfeed/{key}/getodds/soccer?cat={sport}_10` | `GOALSERVE_API_KEY` | [`infra/sports/goalserve_pregame_client.py`](../src/polymarket_trader/infra/sports/goalserve_pregame_client.py) | `GOALSERVE_PREGAME_ENABLED=true`（默认关，数据 >100MB） |

Inplay feed 覆盖：soccer、basket、tennis、volleyball、amfootball、esports、hockey、baseball（按 tracked market 需求自动轮询）。

Livescore getfeed 覆盖 inplay 之外的运动：cricket、handball、rugby、boxing、mma、golf、horse_racing、f1、motogp。

Pregame 默认运动列表见 `GOALSERVE_PREGAME_SPORTS`。

## 数据库

- 开发环境先准备 PostgreSQL，再调用 `polymarket_trader.infra.db.initialize_database` 按当前 metadata 建表。
- schema 结构变更时，直接重建开发库再初始化（不写 backfill / 兼容性迁移代码，参见 [`CLAUDE.md`](../CLAUDE.md) §8）。

连接顺序：

- `DATABASE_URL` 优先级最高；填写后 `DATABASE_DRIVER` / `_HOST` / `_PORT` / `_NAME` / `_USER` / `_PASSWORD` 全部忽略。
- `DATABASE_URL` 为空时由拆分字段拼接 PostgreSQL DSN。

### 审计表 retention

各表保留期独立配置（天；0 = 关闭）：

- `AUDIT_RETENTION_DAYS`（默认 3）—— `audit_events` 主表，写入密度最高
- `DEAD_RECORDS_RETENTION_DAYS`（默认 2）—— 死记录（终态 orders + 空 positions）
- `OUTBOX_RETENTION_DAYS`（默认 1）—— transient queue
- `DECISION_RECORDS_RETENTION_DAYS`（默认 7）
- `FILLS_RETENTION_DAYS`（默认 14）
- `ACCOUNT_SNAPSHOTS_RETENTION_DAYS`（默认 14）

实现：[`app/audit_retention.py`](../src/polymarket_trader/app/audit_retention.py) + supervisor P3 worker，失败仅日志，不阻塞 P0 热路径。purge 用 CTE-based ctid 批 + `FOR UPDATE SKIP LOCKED` 避免大表锁。

## 运行时调参（`/parameters`）

启动期 `Settings` 与 `TradingWorkflowConfig` 都是 frozen；探索性临时调参走 `/parameters` 端点的 `ParameterStore` runtime override 层。

- `GET /parameters` — 列所有可调参数 + 当前 override 状态
- `PUT /parameters/{scope}/{key}` — 设置 override；落 `PARAMETER_OVERRIDE_APPLIED` 审计
- `DELETE /parameters/{scope}/{key}` — 清除 override，回落 Settings / config 默认值

**白名单**（详见 [`src/polymarket_trader/app/parameter_store.py`](../src/polymarket_trader/app/parameter_store.py)）：

```
settings.portfolio_budget_usdc
settings.audit_retention_days

strategy.entry_no_price_max
strategy.tail_outright_min_edge_bps
strategy.tail_outright_max_entry_price
strategy.tail_outright_min_orderbook_depth_usdc
strategy.tail_outright_exit_edge_target
strategy.tail_outright_min_profit_per_share
strategy.tail_outright_budget_usdc
strategy.tail_moneyline_max_entry_price
strategy.tail_spreads_max_entry_price
strategy.tail_min_liquidity_usdc
strategy.tail_implied_min_edge_bps
strategy.tail_implied_prob_confidence
strategy.tail_implied_conf_depth_baseline_usdc
strategy.tail_stale_no_live_state_seconds
strategy.tail_series_winner_budget_usdc
strategy.tail_series_winner_min_edge_bps
strategy.tail_series_winner_max_entry_price
strategy.tail_series_winner_min_orderbook_depth_usdc
strategy.tail_series_winner_execution_permission
```

注意 `tail_outright_budget_usdc` / `tail_series_winner_budget_usdc` 默认 **0** —— 整个 family 落 budget_zero 拒绝，仅审计不下单。要启用：override 设到 ≥ `*_max_per_market_usdc`（默认 25）。

策略侧消费 override 走 `ports.parameter`（[`workflow/parameter_overrides.py`](../src/polymarket_trader/workflow/parameter_overrides.py) 提供 `effective_int` / `effective_decimal` / `effective_str_enum`）。

**边界**：

- Override **重启即丢**。长期固化仍走改 `.env` 重启 / 改 `strategy_config.toml` 重启。
- Kelly 参数（`kelly_fraction` / `kelly_min_edge` / `kelly_min_stake_usdc` 等）**不在白名单** —— Kelly 引擎从 `TradingWorkflowConfig` 读 frozen 值，调参必须改 `strategy_config.toml` 后重启。
- 密钥 / SecretStr 字段、连接串不在白名单。

## 策略包

策略实现：[`src/polymarket_trader/workflow/`](../src/polymarket_trader/workflow/)

- `workflow.py` — `TradingWorkflow` 入口主类
- `config.py` — `TradingWorkflowConfig`（Kelly κ / 阈值 / 信号权重 / 各 family 预算等所有策略参数）
- `quant_decider.py` — 所有 BUY/SELL/cancel/replace 决策唯一入口
- `parameter_overrides.py` — `effective_*` helpers，从 `ports.parameter` 读 runtime override
- `tail/` `outright/` `series/` — 各 market family 子策略
- `discovery.py` / `tracking.py` — 市场发现与跟踪
- `recovery.py` — 历史仓位补救

策略 frozen 值由仓库根 [`strategy_config.toml`](../strategy_config.toml) 提供，启动期 `load_workflow_config()` 自动读取；文件不存在时退到 dataclass 默认值。

## 密钥规则

不得提交到仓库：

- Polymarket API key / secret / passphrase
- wallet private key / signer private key
- 数据库密码与生产连接串
- `GOALSERVE_API_KEY`、`SPORTS_*_ODDS_API_KEY`

日志与审计事件中不得输出：

- 完整签名 payload
- 私钥 / API secret / passphrase
- 未脱敏的 raw response 敏感字段

## 新增配置时确认

- 属于框架（`Settings`）还是策略（`TradingWorkflowConfig`）。**默认走策略 frozen + TOML**；只有 DB / Polymarket / 队列 / retention 这类基础设施才进 `Settings`。
- 默认值是什么，默认值是否安全（资金暴露 / 订单行为类必须默认保守）。
- 单位（`_MS` / `_SECONDS` / `_USDC` / `_BPS`）和取值范围明确。
- 是否进 `/parameters` 白名单（只有探索性调参才需要；长期值改文件重启）。
- 同步更新 `.env.example`（如果是 `Settings` 字段）或 `strategy_config.toml`（如果是策略字段）。
- 若只是策略业务规则，**不要新增 env 变量** —— 放进 `TradingWorkflowConfig`。
