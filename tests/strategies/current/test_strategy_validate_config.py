"""验证 CurrentStrategy.validate_config 的启动期联合校验。

这层校验补在框架 ``Settings.validate_startup_readiness`` 之后：策略侧
discovery、enabled market types 等关键配置如果被清空，应在启动时暴露而不是
上线后才发现没有候选市场。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from polymarket_trader.config import ConfigLoadError, Settings
from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy
from strategies.current.tail import ExecutionPermission


def _settings(**overrides) -> Settings:
    base = dict(
        wallet_private_key="0x" + "1" * 64,
        signer_private_key="0x" + "1" * 64,
        polymarket_funder_address="0x" + "1" * 40,
        portfolio_budget_usdc=Decimal("100"),
        # Kelly 取代旧 max_order/market/total/open_orders 静态上限；
        # 这里用 Settings 默认 kelly_* 字段，仅显式 portfolio_budget_usdc 让
        # validate_config 在 startup 阶段能区分 "未配置 bankroll" 和 "配置但=0"。
        extension_module="strategies.current.manifest:manifest",
    )
    base.update(overrides)
    # _env_file=None 隔离用户 .env：开发机 .env 可能仍残留旧字段或与测试无关的真值；
    # 测试只断言 validate_config 自身行为，不应受 .env 内容影响。
    return Settings(_env_file=None, **base)


def _strategy(config: CurrentStrategyConfig | None = None) -> CurrentStrategy:
    return CurrentStrategy(config=config or CurrentStrategyConfig())


def test_validate_config_passes_with_defaults() -> None:
    issues = _strategy().validate_config(_settings())

    assert issues == ()


def test_validate_config_flags_empty_discovery_inputs() -> None:
    config = replace(
        CurrentStrategyConfig(),
        discovery_title_searches=(),
        discovery_tag_slugs=(),
    )

    issues = _strategy(config).validate_config(_settings())

    assert any(issue.code == "empty_strategy_discovery" for issue in issues)


def test_validate_config_flags_disabled_market_types() -> None:
    config = replace(CurrentStrategyConfig(), tail_enabled_market_types=())

    issues = _strategy(config).validate_config(_settings())

    assert any(issue.code == "empty_enabled_market_types" for issue in issues)


def test_validate_config_flags_zero_portfolio_when_auto_execute() -> None:
    # PORTFOLIO_BUDGET 为 0 但仍有 auto_execute 盘口，应当阻止启动。
    config = replace(
        CurrentStrategyConfig(),
        tail_totals_execution_permission=ExecutionPermission.AUTO_EXECUTE,
    )

    issues = _strategy(config).validate_config(_settings(portfolio_budget_usdc=Decimal("0")))

    assert any(issue.code == "auto_execute_requires_portfolio_budget" for issue in issues)


def test_validate_config_flags_invalid_scale_in_budget_fraction() -> None:
    config = replace(
        CurrentStrategyConfig(),
        tail_scale_in_max_buy_fills=2,
        tail_scale_in_budget_fraction=Decimal("0"),
    )

    issues = _strategy(config).validate_config(_settings())

    assert any(issue.code == "invalid_scale_in_budget" for issue in issues)


def test_validate_config_returns_all_issues_at_once() -> None:
    config = replace(
        CurrentStrategyConfig(),
        discovery_title_searches=(),
        discovery_tag_slugs=(),
        tail_enabled_market_types=(),
    )

    issues = _strategy(config).validate_config(_settings())

    codes = {issue.code for issue in issues}
    assert "empty_strategy_discovery" in codes
    assert "empty_enabled_market_types" in codes


def test_config_load_error_carries_extension_issues() -> None:
    """ConfigLoadError 仍然能正常承载策略侧 ConfigIssue。"""

    config = replace(CurrentStrategyConfig(), tail_enabled_market_types=())
    issues = _strategy(config).validate_config(_settings())

    error = ConfigLoadError(list(issues))

    assert error.as_dict()["error"] == "config_load_failed"
    payload_codes = {item["code"] for item in error.as_dict()["issues"]}
    assert "empty_enabled_market_types" in payload_codes


def test_validate_config_returns_empty_tuple_when_callable_missing() -> None:
    """没有实现 validate_config 的扩展不应被当作失败。"""

    from polymarket_trader.main import _validate_extension_config

    class DummyExtension:
        spec = None
        hooks = None

    issues = _validate_extension_config(DummyExtension(), _settings())

    assert issues == ()


def test_validate_config_propagates_strategy_issues() -> None:
    """build_runtime 的胶水函数应能从 CurrentStrategy.validate_config 收集 issue。"""

    from polymarket_trader.main import _validate_extension_config

    config = replace(CurrentStrategyConfig(), tail_enabled_market_types=())
    strategy = _strategy(config)

    issues = _validate_extension_config(strategy, _settings())

    codes = {issue.code for issue in issues}
    assert "empty_enabled_market_types" in codes
