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
