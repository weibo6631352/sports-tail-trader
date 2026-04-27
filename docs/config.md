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
- `POLYMARKET_API_KEY`、`POLYMARKET_API_SECRET`、`POLYMARKET_API_PASSPHRASE` 只填部分字段时，系统不会进入自动交易态。
- `POLYMARKET_SIGNATURE_TYPE` 为 `1` 或 `2` 但未填写 `POLYMARKET_FUNDER_ADDRESS` 时，系统不会进入自动交易态。
- `SPORTS_LIVE_STATE_ENABLED=true` 时，`SPORTS_LIVE_STATE_SOURCE` 当前只支持 `espn`，且 `SPORTS_LIVE_STATE_LEAGUES` 至少需要一个联赛代码；配置错误会阻止系统进入自动交易态。

## 体育直播状态源

这些配置只决定运行时从哪里读取比分、阶段和剩余时间，不属于策略参数：

- `SPORTS_LIVE_STATE_ENABLED`：是否启用外部直播状态同步 worker。
- `SPORTS_LIVE_STATE_SOURCE`：当前支持 `espn`。
- `SPORTS_LIVE_STATE_BASE_URL`：ESPN site API 基础地址。
- `SPORTS_LIVE_STATE_LEAGUES`：逗号分隔的联赛代码，例如 `nba,nhl,nfl,mlb`。
- `SPORTS_LIVE_STATE_INTERVAL_SECONDS`：P2 同步任务间隔，默认 `15` 秒。
- `SPORTS_LIVE_STATE_TIMEOUT_S`：外部请求超时。
- `SPORTS_LIVE_STATE_PUBLISH_ENTRY_SIGNALS`：直播状态更新后是否发布 `ENTRY_SIGNAL_TRIGGERED`，用于让交易主链路基于最新 metadata 重放入场判断。

运行时行为：

- 外部 API 请求只发生在 `sports_live_state_sync` P2 scheduler job 中。
- `TradingDecisionWorker.entry_metadata_provider` 只读取内存 `EntryMetadataStore`，不会在 P0 路径请求外部 API。
- 同步状态通过 `/runtime`、`/workers`、`/metrics` 的 `sports_live_sync` 字段和前端候选页展示。

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
- 远端 discovery 粗筛输入：`src/strategies/current/config.py` 的 `discovery_title_searches` / `discovery_tag_slugs`；当前默认面向体育直播相关搜索词与 sports tag，官方 Gamma Events keyset 文档：<https://docs.polymarket.com/api-reference/events/list-events-keyset-pagination>
- 体育扫尾模型和权限：`src/strategies/current/sports_tail.py`
- 盘口方向解析：`src/strategies/current/outcomes.py`
- 市场筛选：`src/strategies/current/universe.py`
- 交易决策：`src/strategies/current/trading.py`
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

## 新增配置时确认

- 属于交易主链路、关键修复链路、后台维护链路还是异步支撑链路。
- 默认值是什么，默认值是否安全。
- 单位是什么，取值范围是什么。
- 是否可以运行时热更新。
- 是否需要写入 [`.env.full.example`](../.env.full.example)；如果属于最常用启动项，再同步写入 [`.env.example`](../.env.example)。
- 如果只是策略规则，直接写 `EXTENSION_MODULE` 指向的策略包，不要新增框架环境变量。
- 是否会改变资金暴露、订单行为或 reconcile 行为。
