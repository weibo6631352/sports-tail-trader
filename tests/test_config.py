from __future__ import annotations

from decimal import Decimal

from polymarket_trader.config import Settings


def test_settings_accepts_goalserve_live_source_when_enabled() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_leagues="nba",
        goalserve_api_key="test-api-key",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade
    assert not readiness.blocking_issues


def test_settings_accepts_sports_live_state_league_for_whole_market_coverage() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_leagues="sports",
        goalserve_api_key="test-api-key",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade
    assert not readiness.blocking_issues


def test_settings_goalserve_proxy_optional() -> None:
    """goalserve_proxy 留空时不阻断启动（生产直连）。"""
    settings = _settings(
        sports_live_state_enabled=True,
        goalserve_api_key="test-api-key",
        goalserve_proxy=None,
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade


def test_settings_goalserve_proxy_set_passes_validation() -> None:
    """goalserve_proxy 设为开发代理地址时启动验证通过。"""
    settings = _settings(
        sports_live_state_enabled=True,
        goalserve_api_key="test-api-key",
        goalserve_proxy="http://127.0.0.1:7890",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade


def test_settings_goalserve_sport_codes_default() -> None:
    """goalserve_sport_codes 默认返回包含核心运动代码的元组。"""
    settings = _settings()

    codes = settings.goalserve_sport_codes

    assert isinstance(codes, tuple)
    assert "basketball" in codes
    assert "soccer" in codes
    assert "hockey" in codes
    assert "baseball" in codes


def test_settings_rejects_missing_goalserve_api_key() -> None:
    """sports_live_state_enabled 且未提供 GOALSERVE_API_KEY 时应报 blocking issue。"""
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_leagues="nba",
        # goalserve_api_key intentionally omitted
    )

    readiness = settings.validate_startup_readiness()

    assert not readiness.ready_to_trade
    assert any(issue.field == "goalserve_api_key" for issue in readiness.blocking_issues)


def _settings(**overrides: object) -> Settings:
    base = {
        "_env_file": None,
        "extension_module": "strategies.current",
        "wallet_private_key": "0x" + "1" * 64,
        "portfolio_budget_usdc": Decimal("10"),
    }
    base.update(overrides)
    return Settings(**base)
