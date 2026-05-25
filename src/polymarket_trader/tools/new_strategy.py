"""脚手架：``python -m polymarket_trader.tools.new_strategy NAME`` 一键生成新策略包。

生成的骨架包含：
    - ``strategies/<name>/__init__.py``
    - ``strategies/<name>/manifest.py``        框架装载入口
    - ``strategies/<name>/strategy.py``        ExtensionHooks 最小实现
    - ``strategies/<name>/config.py``          策略私有 dataclass 配置
    - ``strategies/<name>/README.md``          告知策略如何接线、扩展

骨架只提供能跑通的最小代码——不引入业务规则、不预设盘口语义、不放占位 TODO。
策略作者据此扩展 hook 实现即可。
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


_INIT_TEMPLATE = '''"""{display} 策略包入口。"""
'''


_CONFIG_TEMPLATE = '''"""{display} 策略私有配置。

只声明本策略需要的字段；framework 通过 ``load_extension_config`` 加载/校验，
策略不重复实现配置加载逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class {class_name}Config:
    enabled: bool = True


def default_config() -> {class_name}Config:
    return {class_name}Config()
'''


_STRATEGY_TEMPLATE = '''"""{display} 策略实现。

实现 ``ExtensionHooks`` 协议；framework 通过 manifest.factory 装配本类实例。
"""

from __future__ import annotations

from typing import Any

from polymarket_trader.domain.market import Market
from polymarket_trader.extension_api import (
    AccountSnapshotView,
    BusinessExtension,
    DiscoveryQuery,
    EntrySizing,
    ExtensionContext,
    ExtensionDecision,
    ExtensionHooks,
    ExtensionPorts,
    ExtensionSpec,
    LiveStateHooks,
    QuantDecision,
    UniverseDecision,
)
from polymarket_trader.domain.allocation import AllocationPlan

from strategies.{name}.config import {class_name}Config, default_config


class {class_name}Strategy:
    """框架装配入口；hook 方法由本类直接实现。"""

    # 不消费体育直播状态时保持 None，framework 会跳过 SportsLiveStateWorker 装配。
    live_state_hooks: LiveStateHooks | None = None

    def __init__(self, *, config: {class_name}Config, ports: ExtensionPorts | None = None) -> None:
        self._config = config
        self._ports = ports

    @property
    def spec(self) -> ExtensionSpec:
        return ExtensionSpec(name="{name}", version="1", description="{display} strategy")

    @property
    def hooks(self) -> ExtensionHooks:
        return self

    # --- discovery ---

    def discovery_queries(self) -> tuple[DiscoveryQuery, ...]:
        return ()

    # --- universe / sizing ---

    def select_market(self, market: Market) -> UniverseDecision:
        return UniverseDecision.exclude(reason="{name}_not_implemented")

    def size_entry(self, context: ExtensionContext) -> EntrySizing:
        return EntrySizing(
            allocation_plan=AllocationPlan(trace_id=context.trace_id, total_budget_usdc=context.portfolio_budget_usdc or 0),
            reason="{name}_not_implemented",
        )

    # --- decisions ---

    def decide_entry(self, context: ExtensionContext) -> ExtensionDecision:
        return ExtensionDecision.skip(reason="{name}_not_implemented")

    def quant_decide(self, context: ExtensionContext) -> QuantDecision:
        """量化决策器——按 context.quant_trigger_kind 分派持仓决策。"""
        return QuantDecision()

    # --- tracking ---

    def should_keep_tracking(
        self, market: Market, account_snapshot: AccountSnapshotView | None
    ) -> bool:
        return False

    def build_filtered_tracking_market(
        self,
        candidate_market: Market,
        *,
        existing_market: Market,
        reason: str,
    ) -> Market:
        return existing_market


def build_strategy(
    *,
    ports: ExtensionPorts | None = None,
    config_path: str | None = None,
) -> BusinessExtension:
    """manifest.factory 入口：framework 在启动时调一次。"""

    return {class_name}Strategy(config=default_config(), ports=ports)
'''


_MANIFEST_TEMPLATE = '''"""{display} 策略 manifest。framework 通过 ``EXTENSION_MODULE`` 找到这里。"""

from __future__ import annotations

from polymarket_trader.extension_api import ExtensionManifest

from strategies.{name}.config import {class_name}Config
from strategies.{name}.strategy import build_strategy

manifest = ExtensionManifest(
    name="{name}",
    version="1",
    module_path="strategies.{name}",
    factory=build_strategy,
    config_type={class_name}Config,
)
'''


_README_TEMPLATE = """# {display} 策略骨架

由 `polymarket_trader.tools.new_strategy` 生成。

## 装载
设置环境变量 `EXTENSION_MODULE=strategies.{name}.manifest` 后启动 framework。

## 扩展点
- `discovery_queries` / `discovery_queries_for_live_games`：远端 market 发现查询
- `match_live_state`：把外部直播比赛匹配到 framework 跟踪的 market
- `select_market`：判断 market 是否进入策略 universe
- `size_entry`：构造预算分配
- `decide_entry` / `decide_exit` / `decide_follow_up` / `decide_recovery`：决策
- `should_keep_tracking` / `build_filtered_tracking_market`：跟踪生命周期

## 工具
策略可直接 `from polymarket_trader.extension_api import toolkit` 用纯函数工具：
orderbook 计算 / Decimal 取整 / 仓位投影 / 幂等 key / 市场过滤 DSL / 时间窗。

## 复盘
framework 会通过 outbox 把每次 hook 调用以 `DECISION_RECORDED` 事件落到
`decision_records` 表；admin GET `/admin/decisions/dump` 暴露查询接口，
对比新旧决策用 `polymarket_trader.app.replay_harness.replay_records`。
"""


def _to_class_name(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_")) or "Strategy"


def _ensure_name(value: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise SystemExit(
            f"strategy name must match {_NAME_RE.pattern!r} (lowercase, digits, underscores; start with letter)"
        )
    return value


def scaffold(name: str, *, target_root: Path) -> Path:
    """生成新策略包；返回包目录绝对路径。已存在则报错而不是覆盖。"""

    name = _ensure_name(name)
    pkg_root = target_root / "src" / "strategies" / name
    if pkg_root.exists():
        raise SystemExit(f"strategy package already exists: {pkg_root}")
    pkg_root.mkdir(parents=True)
    class_name = _to_class_name(name)
    display = name.replace("_", " ").title()
    files = {
        "__init__.py": _INIT_TEMPLATE.format(display=display),
        "config.py": _CONFIG_TEMPLATE.format(display=display, class_name=class_name),
        "strategy.py": _STRATEGY_TEMPLATE.format(name=name, display=display, class_name=class_name),
        "manifest.py": _MANIFEST_TEMPLATE.format(name=name, display=display, class_name=class_name),
        "README.md": _README_TEMPLATE.format(name=name, display=display),
    }
    for filename, content in files.items():
        (pkg_root / filename).write_text(content, encoding="utf-8")
    return pkg_root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new strategy package skeleton.")
    parser.add_argument("name", help="strategy package name (lowercase, snake_case)")
    parser.add_argument(
        "--target-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="repository root containing src/strategies (default: auto-detected)",
    )
    args = parser.parse_args(argv)
    pkg = scaffold(args.name, target_root=Path(args.target_root))
    print(f"created strategy skeleton at {pkg}")
    print(f"set EXTENSION_MODULE=strategies.{args.name}.manifest to load it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
