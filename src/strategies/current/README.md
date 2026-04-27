# Current Strategy

当前默认策略放在顶层 `src/strategies/current/`。

- `manifest.py`：策略 manifest
- `strategy.py`：策略装配入口
- `config.py`：策略配置 dataclass
- `universe.py`：返回后本地 universe 精筛
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
4. `trading.py`
   理解资金怎么分配、什么条件下会买、什么条件下会卖。
5. `recovery.py` / `tracking.py`
   理解异常状态如何修复，以及 market 被排除后是否继续跟踪。

## 二次开发时优先改哪里

- 只想改搜索词、价格阈值、流动性门槛：
  先改 `config.py`
- 想改“哪些 market 才算命中策略”：
  改 `universe.py`
- 想改预算分配、买卖逻辑：
  改 `trading.py`
- 想改恢复策略或保留订阅规则：
  改 `recovery.py` / `tracking.py`

## 设计约束

- 这个目录只放策略自身语义，不放框架通用能力。
- 策略通过 `polymarket_trader.extension_api` 提供的契约与框架交互。
- 远端 discovery 粗筛通过 `CurrentStrategy.discovery_queries()` 暴露；当前实现从 `config.py` 的 `discovery_title_searches` 和 `discovery_tag_slugs` 生成 `DiscoveryQuery`。
- 策略不直接操作交易客户端、事件总线、数据库或 worker。
- 真正下单、撤单、改价仍然由框架统一执行。
