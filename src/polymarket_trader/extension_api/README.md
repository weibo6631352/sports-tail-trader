# extension_api 目录说明

该目录是策略与框架之间的契约层。框架在启动期通过 manifest 装配单一业务扩展，
扩展通过本目录暴露的 Protocol 与 dataclass 表达自己的能力（hooks）和需求（ports），
框架则通过本目录定义的输入输出对象（context / decisions / events / discovery）调用扩展。

策略类内部代码不应该出现在这里；这里只放**契约**。

## 文件职责

- `manifest.py`：扩展元数据与装配契约。`ExtensionSpec` 描述能力，`BusinessExtension`
  Protocol 是扩展实例的最小契约（`spec` + `hooks`），`ConfigValidator` Protocol 是
  扩展启动期联合校验的可选契约，`ExtensionManifest` 是 `manifest.py` 文件中暴露给
  框架 loader 的入口（`module_path` + `factory`）。
- `hooks.py`：扩展可被框架调用的方法集合 `ExtensionHooks`。当前策略实现 universe /
  sizing / entry / exit / recovery / tracking 等同步 Protocol 方法。
- `decisions.py`：策略返回给框架的决策对象。
  - `ExtensionDecision`：BUY / SELL / CANCEL / SKIP 决策（携带价格、数量、原因和 metadata）。
  - `EntrySizing`：入场预算分配结果（包含 `AllocationPlan` 与本次焦点 `Allocation`）。
  - `EntryCandidate` / `MarketTokenView`：候选市场和 token 视图。
  - `UniverseDecision`：market 是否纳入 universe 的判定。
  - `RecoveryDecision`：恢复链路的修复动作。
  - `ExtensionAction`：决策动作枚举。
- `context.py`：策略接收的运行时上下文 `ExtensionContext`，把市场快照、账户视图、
  候选列表、风控限额、metadata 与 trace_id 一次性带入。`AccountSnapshotView` 是该上下文
  暴露给策略的只读账户视图。
- `discovery.py`：`DiscoveryQuery`，策略对远端 discovery 的粗筛输入声明（标题搜索词、
  tag slug 等），框架按这些查询负责分页、限流和 cursor。
- `ports.py`：策略向框架反向读取信息的端口集合。
  - `MarketReadPort` / `OrderbookReadPort`：市场和盘口快照只读。
  - `AccountReadPort` / `HistoryReadPort`：账户与历史只读。
  - `RuntimeReadPort` / `ConfigReadPort`：运行时状态和配置只读。
  - `TelemetryPort` / `ClockPort`：观测与时钟。
  - `ExtensionPorts`：上述端口的聚合容器，由框架装配后注入。
- `events.py`：策略侧需要的 domain 事件 DTO 再导出（避免直接 import domain）。
- `errors.py`：扩展加载和校验失败抛出的 `ExtensionLoadError`。
- `config_loader.py`：扩展配置文件（json / toml）解析与 dataclass 映射，`load_extension_config`
  与 `load_mapping_file`。

## 允许依赖

- `polymarket_trader.domain` 内部 DTO（market / order / orderbook / position / allocation / events）。
- 标准库 + Pydantic / dataclass 等轻量类型工具。

## 禁止依赖

- FastAPI、SQLAlchemy、Polymarket SDK、WebSocket client。
- `polymarket_trader.app` / `infra` / `runtime` / `workers`：契约层不能反向依赖实现层。
- 环境变量、日志落盘、数据库查询。

## 硬约束

- **契约不进业务**：策略阈值、关键词、价格上限只能写在策略包内（`src/strategies/<name>/`），
  不进 hooks / decisions / ports。
- **决策对象不直接下单**：扩展只输出 `ExtensionDecision` / `EntrySizing` / `RecoveryDecision`，
  框架的 `OrderExecutor` 才允许签名、提交、取消、替换订单。
- **ports 是只读 + 受控副作用**：`MarketReadPort` 等只读，`TelemetryPort` 仅记录指标和事件，
  不允许在 ports 上加"下单 / 写持久化 / 修改 registry 状态"等动作。
- **扩展不依赖框架内部状态**：所有需要的事实通过 `ExtensionContext` 或 ports 流入；扩展不
  直接 import `polymarket_trader.runtime` 或 `polymarket_trader.app`。
- **可选契约通过 Protocol + getattr 检测**：`ConfigValidator` 等可选行为不强制写入
  `BusinessExtension`，由框架在调用点 `getattr(extension, "validate_config", None)` 决定是否
  使用，扩展不实现也不报错。

## 装配流程

```text
1. settings.extension_module 指向 manifest 模块路径
2. framework loader 导入 manifest，拿到 ExtensionManifest（含 factory）
3. framework 构造 ExtensionPorts，调用 factory(ports=..., config_path=...)
4. factory 返回实现 BusinessExtension Protocol 的实例
5. 如实例实现 ConfigValidator，build_runtime 调用 validate_config(settings) 联合校验
6. framework 从 instance.hooks 获取 ExtensionHooks 并接入主链路
```

## 输入与输出

- 输入：`ExtensionContext`（市场快照、账户视图、候选列表、风控限额、metadata、trace_id）。
- 输出：`ExtensionDecision` / `EntrySizing` / `RecoveryDecision` / `UniverseDecision`，以及
  发布到 `TelemetryPort` 的指标和审计事件。
- 拒绝原因必须可审计（写入 decision.reason 和 metadata），不能只返回空 SKIP。
