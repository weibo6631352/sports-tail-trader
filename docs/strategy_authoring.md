# 策略二次开发指南

本文是写新策略的唯一入口文档。框架的硬约束以 [CLAUDE.md](../CLAUDE.md) 为准。

## 1. 五分钟脚手架

```bash
python -m polymarket_trader.tools.new_strategy my_strategy
# 在 src/strategies/my_strategy/ 生成完整骨架
export EXTENSION_MODULE=strategies.my_strategy.manifest
./start_all.sh
```

骨架包含 5 个文件：`__init__.py`、`config.py`、`strategy.py`、`manifest.py`、`README.md`。手动编辑 `strategy.py` 实现各个 hook 即可。

## 2. 必须实现的接口（`ExtensionHooks` Protocol）

```python
from polymarket_trader.extension_api import (
    ExtensionContext, ExtensionDecision, EntrySizing,
    UniverseDecision, RecoveryDecision, DiscoveryQuery, LiveStateMatch,
)

class MyStrategy:
    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]: ...
    def discovery_queries_for_live_games(self, games) -> tuple[DiscoveryQuery, ...]: ...
    def match_live_state(self, market, games) -> LiveStateMatch | None: ...
    def select_market(self, market) -> UniverseDecision: ...
    def size_entry(self, context) -> EntrySizing: ...
    def decide_entry(self, context) -> ExtensionDecision: ...
    def decide_exit(self, context) -> ExtensionDecision: ...
    def decide_follow_up(self, context) -> tuple[ExtensionDecision, ...]: ...
    def decide_recovery(self, context) -> RecoveryDecision: ...
    def should_keep_tracking(self, market, account_snapshot) -> bool: ...
    def build_filtered_tracking_market(self, candidate, *, existing, reason): ...
```

## 3. 决策对象的强类型字段

`ExtensionDecision` 携带 framework 必读的字段——填这些字段后 admin/复盘会自动展示，无需 framework 改代码：

| 字段 | 用途 |
|------|------|
| `decision_kind: DecisionKind` | `ENTRY` / `SCALE_IN` / `EXIT` / `FOLLOW_UP` / `RECOVERY` —— framework 据此判断决策语义（如 worker 加仓门控） |
| `intent_tags: frozenset[str]` | 决策标签集合，如 `{"scale_in"}`。worker 用 `intent_tags` 决定主链路分支，**不读 metadata 字符串** |
| `summary: StrategySummary` | admin / 虚拟回放 / 复盘的展示视图。字段：`action`、`reason`、`label`、`market_type`、`side`、`line`、`best_ask`、`manual_confirmed`、`extras: dict` |
| `metadata` | 策略私有透传 dict，framework 不解释字段语义（仅 audit 用） |

**红线**：framework 决策门控只读 `decision_kind` / `intent_tags` / `summary`；不要假设 framework 会读你的 `metadata` 私有 key。

## 4. 工作流干预（lifecycle 订阅）

策略在 `build_strategy(ports=...)` 时拿到 `ports.lifecycle`，可订阅 framework 事件做 PnL 跟踪、健康统计等：

```python
from polymarket_trader.extension_api import LifecycleEvent

async def on_filled(envelope):
    print(f"order filled: {envelope.condition_id} {envelope.payload['matched_shares']}")

ports.lifecycle.subscribe(LifecycleEvent.ORDER_FILLED, on_filled)
```

可订阅事件：`ORDER_SUBMITTED` / `ORDER_FILLED` / `ORDER_REJECTED` / `ORDER_CANCELLED`（来自 TradingService）、`LIVE_STATE_UPDATED`（来自 SportsLiveStateWorker）、`RECONCILE_PASSED`（来自 ReconcileWorker）。每个事件 envelope 的 payload 字段见 `extension_api/lifecycle.py` 的 docstring。

**红线**：策略只能 `subscribe` / `unsubscribe`，**不能 publish**——framework 主链路不接受策略注入事件。回调抛错被 framework 捕获 + telemetry，不阻塞主链路。

## 5. 直接可用的工具集

```python
from polymarket_trader.extension_api import toolkit

# 盘口
toolkit.midpoint(orderbook)              # Decimal | None
toolkit.spread_bps(orderbook)            # 价差 basis points
toolkit.depth_at_price(orderbook, side="ask", price=Decimal("0.55"))
toolkit.depth_weighted_price(orderbook, side="ask", target_size=Decimal("100"))

# Decimal 数学
toolkit.clamp(value, lower=..., upper=...)
toolkit.round_to_tick(price, tick_size, rounding="down")
toolkit.pct_of(numerator, denominator)

# 持仓投影
toolkit.avg_cost(position)
toolkit.unrealized_pnl(position, mark_price=...)
toolkit.exposure_usdc(position, mark_price=...)

# 幂等 key
toolkit.build_entry_key(condition_id, token_id, decision_kind, window)

# 市场过滤 DSL
filt = toolkit.MarketFilterDSL().slug_contains("nba").best_ask_at_most(Decimal("0.95"))
filt.evaluate(market, orderbook)

# 时间窗
toolkit.classify_phase(market)           # GamePhase.PRE_GAME / IN_GAME / POST_GAME / SETTLED
toolkit.is_within_tail_window(market, horizon=timedelta(minutes=10))
```

## 6. 复盘工作流

framework 默认装配 `DecisionEventRecorder`：每次 `decide_entry` / `decide_exit` / `decide_follow_up` 调用同步投到 outbox，`PersistenceWorker` 异步落到 `decision_records` 表。

- 查询历史决策：`GET /admin/decisions/dump`，支持 `trace_id` / `condition_id` / `strategy_id` / `accepted` / `since` / `until` 过滤。
- 离线 replay：在测试或调试脚本里实例化 `polymarket_trader.app.replay_harness.ReplayHarness`，灌入历史 record + 新 hooks，得到每条 record 的 `ReplayDiff`，分类有 UNCHANGED / REASON_CHANGED / ACTION_CHANGED / PRICE_CHANGED / AMOUNT_CHANGED / OTHER。

## 7. 配置加载

不重复造轮子——用 framework 提供的：

```python
from polymarket_trader.extension_api import load_extension_config
from strategies.my_strategy.config import MyStrategyConfig

config = load_extension_config(MyStrategyConfig, config_path) or MyStrategyConfig()
```

`MyStrategyConfig` 是 frozen dataclass。加新字段直接给默认值，已有调用面不破。

## 8. 单测

策略的单测可以直接用 `ExtensionContext` 构造场景，不需要起 framework 主链路：

```python
def test_my_decision():
    ctx = ExtensionContext(trace_id="t-1", market=fake_market, token_id="tok-1", orderbook=fake_book)
    decision = MyStrategy(config=MyStrategyConfig()).decide_entry(ctx)
    assert decision.decision_kind == DecisionKind.ENTRY
    assert decision.summary.action == "auto_execute"
```

## 9. 红线（不可违反）

- 策略只输出 `ExtensionDecision` / `RecoveryDecision` / `EntrySizing` / `LiveStateMatch`，**不直接调用** `OrderExecutor` / `RiskManager` / 交易客户端
- 策略**不 publish** lifecycle 事件
- 策略包**只 import** `polymarket_trader.extension_api` + `polymarket_trader.domain` 公开 DTO；不 import `polymarket_trader.app` / `infra` / `runtime` / `workers`
- 金额 / 价格用 `Decimal`，不用 float
- 拒绝原因必须可审计（`ExtensionDecision.skip(reason="...")` 的 reason 字段不能为空）

## 10. 常见落点

| 改的东西 | 落到哪里 |
|---|---|
| 决策规则、定价、仓位 | 自己策略包的 `decide_entry` / `decide_exit` |
| 配置 | 自己策略包的 `config.py` 字段 |
| 通用 orderbook / Decimal 工具 | 用 `toolkit`，不要自己写 |
| 想监听订单成交 | `ports.lifecycle.subscribe(ORDER_FILLED, ...)` |
| 想录一次决策跑回放 | 默认已开启，CLI 工具读 dump JSONL |
| 跨策略字段 / 框架契约 | **不在策略包内改**，找 framework 维护者讨论 |
