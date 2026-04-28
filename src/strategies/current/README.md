# Current Strategy

当前默认策略放在顶层 `src/strategies/current/`。

- `manifest.py`：策略 manifest
- `strategy.py`：策略装配入口
- `config.py`：策略配置 dataclass
- `sports_tail.py`：体育扫尾候选、执行权限、评估结果和拒绝原因
- `live_state.py`：外部直播比赛状态与当前策略 market 的匹配和 metadata 映射
- `outcomes.py`：体育盘口类型、盘口线和目标 token 方向解析
- `universe.py`：返回后本地 universe 精筛
- `risk.py`：体育扫尾策略级风控，包括单场、联赛、单日和连续亏损暂停
- `exit_plan.py`：买入后等待结算、显式自动退出和恢复动作共用的退出计划 metadata
- `trading.py`：分配、入场、退出
- `recovery.py`：恢复语义
- `tracking.py`：过滤后继续跟踪的规则

## 建议阅读顺序

如果你准备基于当前策略做二次开发，建议按下面顺序读：

1. `strategy.py`
   先看策略是怎么被组装起来的，知道各个子模块分别负责什么。
2. `config.py`
   再看有哪些业务参数可以改，哪些阈值会影响扫描、筛选和交易。
3. `universe.py`
   理解扫回来的 market 为什么会被纳入或排除。
4. `sports_tail.py` / `outcomes.py` / `live_state.py`
   理解 `Totals`、`Moneyline`、`Spreads` 如何被统一建模、解析和评估。
5. `risk.py` / `exit_plan.py` / `trading.py`
   理解资金怎么分配、什么条件下会买、什么条件下会卖，以及买入后怎么保留退出计划。
6. `recovery.py` / `tracking.py`
   理解异常状态如何修复，以及 market 被排除后是否继续跟踪。

## 二次开发时优先改哪里

- 只想改搜索词、价格阈值、流动性门槛：
  先改 `config.py`
- 想改体育盘口模型、执行权限或拒绝原因：
  改 `sports_tail.py`
- 想改 outcome / token 方向解析：
  改 `outcomes.py`
- 想改外部比分与 market 文本的匹配方式：
  改 `live_state.py`
- 想改“哪些 market 才算命中策略”：
  改 `universe.py`
- 想改预算分配、买卖逻辑：
  改 `trading.py`
- 想改单场、联赛、单日或连续亏损暂停规则：
  改 `risk.py`
- 想改买入后的目标卖出、等待结算或异常处理口径：
  改 `exit_plan.py` / `recovery.py`
- 想改恢复策略或保留订阅规则：
  改 `recovery.py` / `tracking.py`

## 设计约束

- 这个目录只放策略自身语义，不放框架通用能力。
- 策略通过 `polymarket_trader.extension_api` 提供的契约与框架交互。
- 远端 discovery 粗筛通过 `CurrentStrategy.discovery_queries()` 暴露；当前实现从 `config.py` 的 `discovery_title_searches` 和 `discovery_tag_slugs` 生成 `DiscoveryQuery`。
- 体育扫尾策略默认覆盖 `Totals`、`Moneyline`、`Spreads`，但不同盘口可以配置不同执行权限。
- 策略机会类型包括普通直播扫尾、已结束但未封盘和受控加仓；当前默认买入后等待权威结算，不自动挂 follow-up SELL。
- 策略不直接操作交易客户端、事件总线、数据库或 worker。
- 真正下单、撤单、改价仍然由框架统一执行。
