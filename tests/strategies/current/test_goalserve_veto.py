"""Tests for _goalserve_moneyline_veto: verifies inplay-only veto, no pregame fallback.

The veto should only fire on inplay (goalserve_moneyline) odds. Using pregame odds
for the veto causes false positives in the final minutes of a game, where the
leading team's Polymarket ask has moved up significantly from pregame probability.
"""
from __future__ import annotations

import dataclasses
from decimal import Decimal
from unittest.mock import MagicMock

from polymarket_trader.domain.orderbook import OrderbookSnapshot
from polymarket_trader.extension_api import ExtensionContext
from strategies.current.config import CurrentStrategyConfig
from strategies.current.tail import SportsMarketSide
from strategies.current.trading.gates import _goalserve_moneyline_veto


def _orderbook(best_ask: float) -> OrderbookSnapshot:
    ob = MagicMock(spec=OrderbookSnapshot)
    ob.best_ask = Decimal(str(best_ask))
    return ob


def _config(**overrides: object) -> CurrentStrategyConfig:
    return dataclasses.replace(CurrentStrategyConfig(), **overrides)


def _ctx(metadata: dict, best_ask: float = 0.92) -> ExtensionContext:
    ctx = MagicMock(spec=ExtensionContext)
    ctx.metadata = metadata
    ctx.orderbook = _orderbook(best_ask)
    return ctx


# ---------------------------------------------------------------------------
# Core: inplay veto fires when inplay odds disagree
# ---------------------------------------------------------------------------


def test_inplay_veto_fires_on_large_discrepancy() -> None:
    # Goalserve inplay says 55%, Polymarket ask is 92% → discrepancy 37% > 25% threshold
    ctx = _ctx({"goalserve_moneyline": {"home_implied_prob": 0.55, "away_implied_prob": 0.45, "suspended": False}})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is not None
    assert result["goalserve_veto_discrepancy"] > 0.25


def test_inplay_veto_passes_on_small_discrepancy() -> None:
    # Goalserve inplay says 88%, Polymarket ask is 92% → discrepancy 4% < 25%
    ctx = _ctx({"goalserve_moneyline": {"home_implied_prob": 0.88, "away_implied_prob": 0.12, "suspended": False}})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


# ---------------------------------------------------------------------------
# Critical: pregame odds must NOT trigger veto
# ---------------------------------------------------------------------------


def test_pregame_only_no_veto() -> None:
    # No inplay odds; pregame shows 55% vs Polymarket 92% — must NOT veto
    ctx = _ctx({"pregame_moneyline": {"home_implied_prob": 0.55, "away_implied_prob": 0.45, "suspended": False}})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


def test_pregame_extreme_discrepancy_no_veto() -> None:
    # Extreme case: pregame 20% vs Polymarket 95% → still no veto (no inplay data)
    ctx = _ctx({"pregame_moneyline": {"home_implied_prob": 0.20, "away_implied_prob": 0.80, "suspended": False}}, best_ask=0.95)
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


def test_inplay_none_no_pregame_no_veto() -> None:
    # No metadata at all → no veto
    ctx = _ctx({})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


def test_inplay_suspended_no_veto() -> None:
    # Inplay moneyline market itself is suspended → no veto (bookmaker pulled lines)
    ctx = _ctx({"goalserve_moneyline": {"home_implied_prob": 0.55, "suspended": True}})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


def test_inplay_outcome_suspended_no_veto() -> None:
    # Inplay home outcome is suspended → no veto for home side
    ctx = _ctx({"goalserve_moneyline": {
        "home_implied_prob": 0.55, "away_implied_prob": 0.45,
        "suspended": False, "home_suspended": True,
    }})
    result = _goalserve_moneyline_veto(_config(), ctx, SportsMarketSide.HOME)
    assert result is None


# ---------------------------------------------------------------------------
# Disabled / zero margin
# ---------------------------------------------------------------------------


def test_disabled_config_no_veto() -> None:
    config = _config(goalserve_cross_validation_enabled=False)
    ctx = _ctx({"goalserve_moneyline": {"home_implied_prob": 0.10, "suspended": False}})
    result = _goalserve_moneyline_veto(config, ctx, SportsMarketSide.HOME)
    assert result is None


def test_zero_margin_no_veto() -> None:
    config = _config(goalserve_cross_validation_margin=Decimal("0"))
    ctx = _ctx({"goalserve_moneyline": {"home_implied_prob": 0.10, "suspended": False}})
    result = _goalserve_moneyline_veto(config, ctx, SportsMarketSide.HOME)
    assert result is None
