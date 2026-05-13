# Runbook

先确认交易状态，再恢复自动化。
先保交易主链路，再补低优先级数据。

## 启动后仍未 ready

症状：
- `/health` 正常，但 `/ready` 返回 `ready_to_trade=false`。
- `/runtime` phase 停在 `recovering_snapshot`、`reconciling`、`workers_started` 或 `degraded`。

处置：
1. 先看 `/ready.blocking_reasons` 和 `/runtime.runtime.blocking_reasons`；配置问题再看 `/ready.blocking_issues`。
2. 若是 `db_not_ready`，先修复 PostgreSQL 连通性。
3. 若是 `trading_client_not_ready`，检查密钥、wallet signer 和运行环境。
4. 若是 `user_ws_not_connected` 或 `market_ws_not_connected`，保持自动下单关闭，先确认 WS 是否正在重连。
5. User WS 连上后系统会主动唤醒账户 reconcile；若超过一个周期仍是 `reconcile_not_fresh`，再执行 `POST /operations/reconcile`。

## Market WS 断线

症状：
- orderbook 更新停止。
- `ws_event_lag_ms` 升高。
- market channel 重连日志增加。

处置：
1. 暂停依赖该连接的新入场信号。
2. 指数退避重连 Market WS。
3. 重连后通过 CLOB REST 拉取 orderbook 快照覆盖本地状态。
4. 重新计算 best bid / ask、spread 和 eligible 状态。
5. 写入 reconcile 与 WS 恢复审计事件。

## User WS 断线

症状：
- order / fill / position 生命周期事件停止。
- 本地 open orders 与权威状态可能不一致。

处置：
1. 暂停新买入。
2. 重连 User WS。
3. 拉取 open orders、fills、positions、USDC.e balance 和 allowance。
4. 对比本地状态并生成修复动作。
5. 完成 reconcile 后再恢复新买入。

## FAK BUY 异常进入 live

症状：
- 买入订单出现在 open BUY orders。
- FAK 未按预期立即成交或取消。

处置：
1. 立即生成 cancel 修复意图，并按交易主链路优先处理。
2. 暂停该 market 新买入。
3. 记录 `resting_buy_detected` 和 cancel 审计事件。
4. 拉取订单权威状态确认是否已取消、成交或终态失败。
5. 若有成交 shares，按实际持仓补挂 GTC SELL。

## open SELL 与持仓不一致

症状：
- 持仓 shares 大于 open SELL shares。
- open SELL shares 大于实际持仓。

处置：
1. 暂停该 market 的新买入或限制新增卖出动作。
2. 拉取权威 positions 和 open orders。
3. 持仓大于 open SELL 时，按差额生成 GTC SELL intent。
4. open SELL 大于持仓时，取消多余 SELL。
5. 写入 reconcile diff 和修复结果。

## 数据库写入失败或 outbox 积压

症状：
- Persistence Worker 重试增加。
- outbox 队列深度升高。
- PostgreSQL 连接错误。

处置：
1. 保持交易主链路继续运行，除非 outbox 关键事件也无法写入。
2. 先确认市场发现、orderbook 快照、候选拒绝等非交易事件没有重新进入持久化 outbox。
3. 限速或暂停低优先级快照写入。
4. 优先保留订单、成交、cancel、risk failure 等关键审计事件。
5. 修复数据库连接后恢复 Persistence Worker。
6. 核对 outbox 幂等写入结果。

## 交易主链路延迟升高

症状：
- `entry_signal_to_submit_ms` 超阈值。
- `trading_queue_depth` 升高。
- `trading_lock_wait_ms` 或 `executor_queue_wait_ms` 升高。

处置：
1. 暂停或限速后台维护与异步支撑任务。
2. 检查是否有 Admin 大查询、数据库慢写、日志阻塞或全量扫描。
3. 检查是否有新锁进入交易热路径。
4. 保留订单和成交事件，合并或丢弃低价值快照事件。
5. 恢复后补写指标和运维记录。

## 人工 replace open order

症状：
- 某张 open order 的价格需要人工调整。
- 自动修复未满足业务预期，但目标订单和市场状态明确。

处置：
1. 调用 `POST /orders/replace`，至少传 `order_id` 和新的价格；必要时补 `market_slug`、`condition_id` 或 `token_id` 辅助定位。
2. 确认返回中的 `order` 是预期那张单。
3. 确认 `replace_order_submitted` 返回新的订单结果。
4. 若失败原因是 `price_not_aligned_to_tick_size`、`order_size_unknown` 或 `market_not_operable`，不要继续重试，先修正输入或等待状态恢复。

## 风控阈值临时调整

症状：
- `/analytics/risk-rejections/aggregate` 显示某条规则在大量拒绝中（过严）。
- 或 `/analytics/edge-realization` 显示当前 edge 阈值下样本量极少（过严）。
- 或 `/portfolio/risk-metrics` 显示 drawdown 异常扩大（过松，需收紧）。
- 或 `/analytics/missed-opportunities` 显示拒绝决策事后大量盈利（过严）。

处置：
1. 先停止扩大暴露：`POST /operations/pause-trading` 或调小 `kelly_max_position_fraction` / `portfolio_budget_usdc`。
2. 用 `POST /operations/parameter-sweep` 离线评估候选阈值，看 best_by_pnl /
   best_by_win_rate；注意响应里 `entry_price_cap_fallback_count` 非零时数字
   会偏高。
3. 经验证后用 `PUT /parameters/{scope}/{key}` 写 override，例如：
   - 收紧：`PUT /parameters/strategy/tail_outright_min_edge_bps` 提高到更高 bps
   - 放松液量：`PUT /parameters/strategy/tail_min_liquidity_usdc` 降低
   - 收紧 Kelly 单仓：`PUT /parameters/settings/kelly_max_position_fraction` 降低（如 0.05）
   - 收紧 κ：`PUT /parameters/settings/kelly_fraction` 降低（如 0.10）
   - 启用 drawdown lockout：`PUT /parameters/settings/kelly_drawdown_halt_fraction` 提高（如 0.6）
4. 观察 `/analytics/risk-rejections/aggregate` 与 `/analytics/edge-realization`
   验证阈值生效；override 重启即丢。
5. 阈值要长期固化时**改 `.env` / `src/strategies/current/config.py` 后重启**，
   并清掉 override（`DELETE /parameters/{scope}/{key}`），避免内存值与启动
   配置长期分裂。

## audit_events 表暴涨 / 查询慢

症状：
- `audit_events` 表磁盘占用持续增长（每天累计 100k–1M+ 行）。
- 包含 `audit_events` 的 admin 查询（trade timeline / parameter history / risk-rejections）
  开始变慢。

处置：
1. 默认 daily P3 worker `audit_retention_purge` 每 24h 删早于 `audit_retention_days`
   天（默认 14）的行；看 supervisor `/workers` 或日志 `audit_retention.purge_run`
   是否正常跑。
2. 紧急清理：临时调小 `PUT /parameters/settings/audit_retention_days` 为更小天数
   触发下一次 daily 时收紧（重启即丢）。
3. 长期固化：改 `.env` `AUDIT_RETENTION_DAYS` 重启。
4. **彻底关 retention**：`audit_retention_days=0`（仅调试用，会让表无界增长）。
5. 批次行为：CTE-based ctid + `FOR UPDATE SKIP LOCKED`，每批
   `audit_retention_purge_batch_size`（默认 10000）行，最多 200 批；不锁全表。
6. 失败处理：worker 内 try/except 后只日志不外抛——CLAUDE.md §7 后台不能
   阻塞 P0 主链路。看 `last_error` snapshot 字段排查 DB 异常。

## 市场结算无 ground truth

症状：
- `/analytics/calibration` 的 `with_outcome_count` 远低于 `total_samples`。
- `/markets/{condition_id}/settlement` 对已结算市场返回 404。

处置：
1. 看 scheduler `settlement_scanner` 是否正常跑（`/runtime` 或日志）。
2. 自动 scanner 拉不到时，运维手工 `POST /markets/settle` 录入；payload 含
   `condition_id` / `winning_token_id` / `winning_outcome`。事件落 audit_events
   后 calibration 端点立即可用。
3. 若 Gamma 撞 rate limit（日志含 `settlement_scanner.gamma_filter_failed`），
   降低 scheduler 频率或减少 `max_markets_per_run`。

## YES/NO 冠军市场 + 系列赛市场异常诊断

症状：
- 某条 outright 二元市场（如 `Will the Boston Celtics win 2026 NBA?`）一直未生成
  BUY 决策，`/admin/decisions/dump` 显示 `outright_reject_reason=outright_team_not_resolved`。
- 系列赛市场（NBA/NHL 季后赛）大量 `series_reject_reason=missing_series_state` 或
  `stale_series_state`。
- `/metrics` 中 `strategy_outright_reject_total{reason=...}` /
  `strategy_series_reject_total{sub_type=...,reason=...}` 某条原因激增。

新增可审计拒绝原因（出现在 ExtensionDecision.metadata + metrics 维度）：

- Outright：
  - `outright_team_not_resolved`：market 文本里没有 snapshot 球队，或命中歧义
    （≥ 2 个球队同时被命中）。
  - `season_odds_incomplete`：snapshot Σp 偏离 [0.95, 1.05]，de-vig 不完整。
- Series 通用：
  - `missing_series_state` / `stale_series_state`：``series_state_worker`` 未刷新或
    距上次刷新过久。
  - `missing_series_odds` / `stale_series_odds`：单场胜率源（TheOddsAPI）缺失或过期。
  - `insufficient_edge` / `price_above_fair`：模型 fair 高于 ask 但 edge 未达 min_edge_bps。
  - `liquidity_below_min`：ask 侧可买深度低于 ``tail_series_*_min_orderbook_depth_usdc``。
  - `source_conflict`：fusion 多源结论分歧。
- Series sub-type 专用：
  - `series_team_not_resolved`：outcome 文本无法映射到 ``SeriesState.team_a/b``。
  - `series_outcome_not_parsed`：TOTAL_GAMES `Over X.5` / `Under X.5` 或 GAME_HANDICAP
    `Team -3.5` 文本解析失败。
  - `missing_game_spreads` / `stale_game_spreads`：GAME_HANDICAP single-game scope 缺
    单场让分。
  - `subtype_unclassified`：classifier 命中 SERIES family 但子类型为 OTHER。

诊断步骤：

1. **查命中率分布**：`GET /metrics`，看 `strategy_outright_decision_total{outcome=...}` 与
   `strategy_series_decision_total{sub_type=...,outcome=...}`；accepted/rejected 比与
   top reason 决定优先排查哪条链路。
2. **outright team 解析失败**：`GET /admin/outright/team-resolution?condition_id=<id>`
   （也可 `?market_slug=<slug>`），返回字段：
   - ``trace.normalized_text``：拼 market_question + event_title + event_slug 归一后的文本。
   - ``trace.candidate_teams``：snapshot 中所有候选球队。
   - ``trace.matches``：命中集合；空 = 0 命中（snapshot 没该球队 / 文本里没球队名），
     ≥ 2 = 歧义（同一文本里出现多支 snapshot 球队）。
   - ``trace.resolved``：唯一命中时返回 key，否则 None。
   - ``snapshot_available=False`` + ``reason=missing_season_odds``：
     ``sports_season_odds_worker`` 未为该 market 写入 snapshot，先排 worker 健康度。
3. **series_state 缺失/过期**：`GET /admin/series/state`，返回所有 series_state 快照
   含 ``age_seconds``。``age_seconds`` 显著大于 ``tail_series_winner_max_state_age_seconds``
   说明 ``series_state_worker`` 抓取失败（ESPN 端点变动 / 网络层异常）；目标 market 缺失
   说明 worker 还没匹配上该 market 的 event_slug → 看 worker 日志。
4. **单场胜率源**：``missing_series_odds`` 表示 ``game_odds_worker`` 未写入或写入了不
   匹配的球队 → 直接看 ``decision_records`` 的 ``decision_input.metadata.game_odds``。
5. **调阈值**：edge / liquidity 类原因经验证后用 ``PUT /parameters/strategy/{key}``
   写 override（同上节）。

注意：admin endpoints 全部只读，不触发 discovery / 订阅 / 交易；调用频率不受
P0 路径限制。
