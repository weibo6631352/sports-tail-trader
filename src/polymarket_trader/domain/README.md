# domain 目录说明

该目录存放纯业务规则、领域模型和内部 DTO。Domain 层必须能在没有数据库、没有 FastAPI、没有 Polymarket SDK、没有 WebSocket 的情况下独立测试。

## 职责

- Market 领域模型：表达内部 market、outcome、交易状态和拒绝原因。
- 资金分配基础模型：预算、已分配额度、释放额度、组合暴露等通用概念。
- 风控规则：单笔、单 market、组合、open orders、价格、spread、流动性和重试限制。
- 领域模型：market、orderbook、order、fill、position、allocation、events、状态机。

## 文件职责

- `allocation.py`：组合资金分配模型、预算释放记录和通用暴露计算。
- `discovery.py`：原始 market 发现事件的内部 DTO。
- `events.py`：领域事件模型。
- `market.py`：market 元数据和交易状态。
- `order.py`：订单意图、方向、类型和状态。
- `orderbook.py`：orderbook 快照、价格层级和 spread。
- `position.py`：持仓与 open SELL 覆盖状态。
- `risk.py`：下单前风控门禁。
- `state_machine.py`：market / 交易生命周期。

## 允许依赖

- Python 标准库。
- 与业务建模相关的轻量纯 Python 库。
- 同层 domain 模块。

## 禁止依赖

- FastAPI、SQLAlchemy。
- Polymarket SDK、HTTP client、WebSocket client。
- 环境变量、配置加载、日志落盘、数据库查询。
- runtime registry、worker、app service。

## 输入与输出

- Domain 输入应是内部 DTO、dataclass、枚举、Decimal 或基础类型。
- Domain 输出应是决策对象、意图对象、事件对象或错误原因。
- 拒绝原因必须可审计，不能只返回 `False`。
- 金额和价格使用 `Decimal`，不要用浮点数表示交易金额。
- 策略常量需要集中管理在策略目录，避免散落到框架层。
- 契约字段和类型名必须有明确业务语义；不要为了双路径调用、让测试临时通过或减少改动而新增别名、包装函数、重复枚举或同义字段。
- 可以保留只读派生属性，例如事件的 `name` 从 `event_type` 派生、`occurred_at` 从 `created_at` 派生；派生属性不得持有第二份状态，也不得改变统一字段名。
- 发现调用侧仍使用非目标命名时，优先修改调用侧对齐当前契约；只有在设计文档明确要求对外适配时，才允许新增双路径适配层，并需要在对应 README 中写明原因、边界和移除条件。
