from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from pydantic import SecretStr

from strategies.current.config import CurrentStrategyConfig
from strategies.current.strategy import CurrentStrategy
from strategies.current.tail.types import ExecutionPermission


def _config_auto_outright() -> CurrentStrategyConfig:
    return replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("50"),
    )


def test_validate_config_blocks_auto_outright_when_season_state_disabled() -> None:
    strategy = CurrentStrategy(config=_config_auto_outright())
    settings = SimpleNamespace(
        portfolio_budget_usdc=Decimal("100"),
        sports_season_state_enabled=False,
        sports_season_odds_api_key=SecretStr("test"),
    )

    issues = strategy.validate_config(settings)

    codes = {issue.code for issue in issues}
    assert "outright_auto_execute_requires_season_state" in codes


def test_validate_config_blocks_auto_outright_when_odds_key_missing() -> None:
    strategy = CurrentStrategy(config=_config_auto_outright())
    settings = SimpleNamespace(
        portfolio_budget_usdc=Decimal("100"),
        sports_season_state_enabled=True,
        sports_season_odds_api_key=None,
    )

    issues = strategy.validate_config(settings)

    codes = {issue.code for issue in issues}
    assert "outright_auto_execute_requires_odds_key" in codes


def test_validate_config_allows_record_only_outright_without_dependencies() -> None:
    """默认 RECORD_ONLY + budget=0 时不应触发依赖校验。"""

    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    settings = SimpleNamespace(
        portfolio_budget_usdc=Decimal("100"),
        sports_season_state_enabled=False,
        sports_season_odds_api_key=None,
    )

    issues = strategy.validate_config(settings)

    codes = {issue.code for issue in issues}
    assert "outright_auto_execute_requires_season_state" not in codes
    assert "outright_auto_execute_requires_odds_key" not in codes


def test_validate_config_passes_when_auto_outright_has_both_deps() -> None:
    strategy = CurrentStrategy(config=_config_auto_outright())
    settings = SimpleNamespace(
        portfolio_budget_usdc=Decimal("100"),
        sports_season_state_enabled=True,
        sports_season_odds_api_key=SecretStr("real-key"),
    )

    issues = strategy.validate_config(settings)
    codes = {issue.code for issue in issues}
    assert "outright_auto_execute_requires_season_state" not in codes
    assert "outright_auto_execute_requires_odds_key" not in codes


def _default_settings() -> SimpleNamespace:
    return SimpleNamespace(
        portfolio_budget_usdc=Decimal("100"),
        sports_season_state_enabled=True,
        sports_season_odds_api_key=SecretStr("k"),
    )


def test_validate_rejects_negative_outright_budget() -> None:
    cfg = replace(CurrentStrategyConfig(), tail_outright_budget_usdc=Decimal("-1"))
    strategy = CurrentStrategy(config=cfg)
    codes = {i.code for i in strategy.validate_config(_default_settings())}
    assert "negative_outright_budget" in codes


def test_validate_rejects_out_of_range_min_edge_bps() -> None:
    too_high = replace(CurrentStrategyConfig(), tail_outright_min_edge_bps=20000)
    too_low = replace(CurrentStrategyConfig(), tail_outright_min_edge_bps=-1)
    for cfg in (too_high, too_low):
        codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
        assert "invalid_outright_min_edge" in codes


def test_validate_rejects_invalid_max_entry_price() -> None:
    for bad in (Decimal("0"), Decimal("1"), Decimal("1.5"), Decimal("-0.1")):
        cfg = replace(CurrentStrategyConfig(), tail_outright_max_entry_price=bad)
        codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
        assert "invalid_outright_max_entry_price" in codes


def test_validate_rejects_non_positive_per_market_cap() -> None:
    cfg = replace(CurrentStrategyConfig(), tail_outright_max_per_market_usdc=Decimal("0"))
    codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
    assert "invalid_outright_per_market_cap" in codes


def test_validate_rejects_ttl_greater_than_max_age() -> None:
    cfg = replace(
        CurrentStrategyConfig(),
        tail_outright_season_odds_ttl_seconds=20000,
        tail_outright_max_season_odds_age_seconds=10000,
    )
    codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
    assert "ttl_exceeds_max_age" in codes


def test_validate_rejects_non_positive_horizon() -> None:
    cfg = replace(CurrentStrategyConfig(), tail_outright_max_hold_horizon_days=0)
    codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
    assert "invalid_outright_horizon" in codes


def test_validate_rejects_budget_smaller_than_per_market_cap() -> None:
    """启动期发现总额预算小于单市场上限时拒绝；AUTO_EXECUTE 路径下首单必定被拒。"""

    cfg = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.AUTO_EXECUTE,
        tail_outright_budget_usdc=Decimal("20"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
    assert "outright_budget_below_per_market_cap" in codes


def test_validate_allows_budget_below_per_market_cap_when_record_only() -> None:
    """RECORD_ONLY 模式不下单，budget 与 per_market 关系无需对齐。"""

    cfg = replace(
        CurrentStrategyConfig(),
        tail_outright_execution_permission=ExecutionPermission.RECORD_ONLY,
        tail_outright_budget_usdc=Decimal("20"),
        tail_outright_max_per_market_usdc=Decimal("25"),
    )
    codes = {i.code for i in CurrentStrategy(config=cfg).validate_config(_default_settings())}
    assert "outright_budget_below_per_market_cap" not in codes


def test_validate_no_outright_constraints_for_record_only_and_zero_budget() -> None:
    """默认配置（RECORD_ONLY + budget=0）下不应触发任何 outright-specific issue。"""

    strategy = CurrentStrategy(config=CurrentStrategyConfig())
    issues = strategy.validate_config(
        SimpleNamespace(
            portfolio_budget_usdc=Decimal("100"),
            sports_season_state_enabled=False,
            sports_season_odds_api_key=None,
        )
    )
    codes = {i.code for i in issues}
    outright_codes = {c for c in codes if "outright" in c or c == "ttl_exceeds_max_age"}
    assert outright_codes == set()
