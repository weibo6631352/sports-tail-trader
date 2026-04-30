from __future__ import annotations

from decimal import Decimal

from polymarket_trader.config import Settings


def test_settings_accepts_sofascore_live_source_for_supported_league() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="sofascore",
        sports_live_state_leagues="nba",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade
    assert not readiness.blocking_issues


def test_settings_accepts_sports_live_state_league_for_whole_market_coverage() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="sofascore",
        sports_live_state_leagues="sports",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade
    assert not readiness.blocking_issues


def test_settings_accepts_thesportsdb_live_source_for_supported_league() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="thesportsdb",
        sports_live_state_leagues="mlb",
    )

    readiness = settings.validate_startup_readiness()

    assert readiness.ready_to_trade
    assert not readiness.blocking_issues


def test_settings_rejects_sofascore_without_supported_league_overlap() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="sofascore",
        sports_live_state_leagues="cricket",
    )

    readiness = settings.validate_startup_readiness()

    assert [(issue.field, issue.code) for issue in readiness.blocking_issues] == [
        ("sports_live_state_sources", "source_league_mismatch")
    ]


def test_settings_rejects_thesportsdb_without_verified_league_overlap() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="thesportsdb",
        sports_live_state_leagues="nba",
    )

    readiness = settings.validate_startup_readiness()

    assert [(issue.field, issue.code) for issue in readiness.blocking_issues] == [
        ("sports_live_state_sources", "source_league_mismatch")
    ]


def test_settings_rejects_espn_without_supported_league_overlap() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="espn",
        sports_live_state_leagues="cricket",
    )

    readiness = settings.validate_startup_readiness()

    assert [(issue.field, issue.code) for issue in readiness.blocking_issues] == [
        ("sports_live_state_sources", "source_league_mismatch")
    ]


def test_settings_rejects_blank_sofascore_endpoint_when_enabled() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="sofascore",
        sports_live_state_leagues="nba",
        sports_live_state_sofascore_base_url="",
    )

    readiness = settings.validate_startup_readiness()

    assert ("sports_live_state_sofascore_base_url", "missing_endpoint") in {
        (issue.field, issue.code) for issue in readiness.blocking_issues
    }


def test_settings_rejects_blank_thesportsdb_endpoint_when_enabled() -> None:
    settings = _settings(
        sports_live_state_enabled=True,
        sports_live_state_sources="thesportsdb",
        sports_live_state_leagues="mlb",
        sports_live_state_thesportsdb_base_url="",
    )

    readiness = settings.validate_startup_readiness()

    assert ("sports_live_state_thesportsdb_base_url", "missing_endpoint") in {
        (issue.field, issue.code) for issue in readiness.blocking_issues
    }


def _settings(**overrides: object) -> Settings:
    base = {
        "_env_file": None,
        "extension_module": "strategies.current",
        "wallet_private_key": "0x" + "1" * 64,
        "portfolio_budget_usdc": Decimal("10"),
        "max_order_usdc": Decimal("1"),
        "max_market_usdc": Decimal("2"),
        "max_total_usdc": Decimal("10"),
        "max_open_orders": 2,
    }
    base.update(overrides)
    return Settings(**base)
