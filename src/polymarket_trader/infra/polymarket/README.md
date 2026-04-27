# polymarket 目录说明

该目录存放 Polymarket 外部接口适配。这里负责理解 Polymarket Gamma / CLOB / Data / WebSocket 的字段和协议差异，并转换为系统内部 DTO。

## 文件职责

- `auth.py`：API credential 和 signer 所需配置对象。
- `gamma_client.py`：Gamma API market / event 元数据读取。
- `clob_client.py`：CLOB orderbook、订单、取消、fills 等接口适配。
- `data_client.py`：Data API 用户持仓、交易数据适配。
- `ws_client.py`：Market Channel 与 User Channel 连接管理。
- `order_executor.py`：唯一允许提交、取消、替换 Polymarket 订单的适配器。
- `schemas.py`：外部响应和内部转换边界对象。

## 关键协议约束

- Gamma API 只读，用于 market / event 发现和元数据。
- Market Channel 按 asset id / token id 订阅 orderbook。
- User Channel 按 condition id 订阅订单和成交生命周期。
- FAK 会立即成交可用部分并取消剩余。
- GTC 会停留在 orderbook，直到成交、取消或 market 不可交易。
- FAK / FOK 不与 post-only 组合。
- BUY market order 的 `amount` 表示 USDC.e 花费金额。
- SELL market order 的 `amount` 表示卖出 shares。

## 允许依赖

- Polymarket SDK 或 HTTP / WebSocket client。
- `polymarket_trader.domain` 的订单意图、market、orderbook 和 position DTO。
- `polymarket_trader.config` 的 host / timeout 配置。
- `polymarket_trader.observability` 的 trace / audit 能力。

## 禁止行为

- 不在 client 中决定是否可以买入；这是 Strategy / Risk 的职责。
- 不在 SDK 适配层吞掉订单失败。
- 不把 SDK 原始对象传给 Domain。
- 不在交易主链路里执行无超时请求。
- 不把私钥、签名 payload 或未脱敏响应写日志。

## 接口契约

- 所有对外方法需要有超时和明确错误类型。
- 订单提交、取消、替换必须记录 trace id 和幂等键。
- WebSocket 重连后必须支持 REST 快照校准。
- 发现 open BUY 异常时，适配层返回足够信息让 Reconciler / Strategy 生成 cancel intent。
