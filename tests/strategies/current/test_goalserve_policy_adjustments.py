"""Tests for _goalserve_policy_adjustments — verifies field names are correct for
dataclasses.replace(policy, **gs_policy_adj) and that all three adjustment paths
(spread conflict, multi-confirm, multi-confirm+HT) produce correct values.
"""
from __future__ import annotations

import dataclasses

from polymarket_trader.extension_api import ExtensionContext
from strategies.current.config import CurrentStrategyConfig
from strategies.current.tail import SportsMarketSide
from strategies.current.tail.types import TailPolicy
from strategies.current.trading.gates import _goalserve_policy_adjustments


def _config(**overrides: object) -> CurrentStrategyConfig:
    return dataclasses.replace(CurrentStrategyConfig(), **overrides)


def _ctx(**metadata: object) -> ExtensionContext:
    return ExtensionContext(trace_id="t", strategy_id="test", metadata=metadata)


def _base_policy(config: CurrentStrategyConfig) -> TailPolicy:
    return TailPolicy(
        min_moneyline_lead=config.tail_min_moneyline_lead,
        max_moneyline_seconds_remaining=config.tail_max_moneyline_seconds_remaining,
    )


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


def test_disabled_returns_empty() -> None:
    config = _config(goalserve_policy_adjustment_enabled=False)
    ctx = _ctx(
        goalserve_moneyline={"home_implied_prob": 0.7},
        goalserve_spread={"home_implied_prob": 0.7, "away_implied_prob": 0.3},
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert adj == {}


def test_no_signals_returns_empty() -> None:
    config = _config()
    adj = _goalserve_policy_adjustments(config, _ctx(), SportsMarketSide.HOME)
    assert adj == {}


# ---------------------------------------------------------------------------
# Spread conflict: opponent spread implied > ours + threshold → higher lead required
# ---------------------------------------------------------------------------


def test_spread_conflict_raises_min_lead() -> None:
    config = _config(
        tail_min_moneyline_lead=6,
        goalserve_spread_conflict_threshold=0.15,
        goalserve_spread_conflict_extra_lead=3,
    )
    ctx = _ctx(
        goalserve_spread={
            "home_implied_prob": 0.30,  # our side (HOME)
            "away_implied_prob": 0.70,  # opponent — conflict: 0.70-0.30=0.40 > 0.15
        }
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert adj == {"min_moneyline_lead": 9}  # 6 + 3


def test_spread_conflict_field_names_compatible_with_replace() -> None:
    """dataclasses.replace must not raise — field names must match TailPolicy."""
    config = _config(
        tail_min_moneyline_lead=6,
        goalserve_spread_conflict_extra_lead=3,
    )
    ctx = _ctx(
        goalserve_spread={"home_implied_prob": 0.20, "away_implied_prob": 0.80}
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    policy = _base_policy(config)
    adjusted = dataclasses.replace(policy, **adj)
    assert adjusted.min_moneyline_lead == 9


# ---------------------------------------------------------------------------
# Multi-confirm: ML + spread both confirm → relax lead + extend time window
# ---------------------------------------------------------------------------


def test_multi_confirm_relaxes_lead_and_extends_time() -> None:
    config = _config(
        tail_min_moneyline_lead=6,
        tail_max_moneyline_seconds_remaining=180,
        goalserve_spread_conflict_threshold=0.15,
        goalserve_multi_confirm_lead_relief=2,
        goalserve_multi_confirm_time_bonus_seconds=30,
    )
    ctx = _ctx(
        goalserve_moneyline={"home_implied_prob": 0.65},  # ≥ 0.5 → confirmed HOME
        goalserve_spread={
            "home_implied_prob": 0.60,  # our side > opponent → confirmed
            "away_implied_prob": 0.40,
        },
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert adj.get("min_moneyline_lead") == 4  # max(1, 6-2)
    assert adj.get("max_moneyline_seconds_remaining") == 210  # 180+30


def test_multi_confirm_field_names_compatible_with_replace() -> None:
    config = _config(
        tail_min_moneyline_lead=6,
        tail_max_moneyline_seconds_remaining=180,
        goalserve_multi_confirm_lead_relief=2,
        goalserve_multi_confirm_time_bonus_seconds=30,
    )
    ctx = _ctx(
        goalserve_moneyline={"home_implied_prob": 0.65},
        goalserve_spread={"home_implied_prob": 0.60, "away_implied_prob": 0.40},
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    policy = _base_policy(config)
    adjusted = dataclasses.replace(policy, **adj)
    assert adjusted.min_moneyline_lead == 4
    assert adjusted.max_moneyline_seconds_remaining == 210


# ---------------------------------------------------------------------------
# Multi-confirm + halftime: further extends time window
# ---------------------------------------------------------------------------


def test_multi_confirm_with_halftime_doubles_time_bonus() -> None:
    config = _config(
        tail_min_moneyline_lead=6,
        tail_max_moneyline_seconds_remaining=180,
        goalserve_multi_confirm_lead_relief=2,
        goalserve_multi_confirm_time_bonus_seconds=30,
    )
    ctx = _ctx(
        goalserve_moneyline={"home_implied_prob": 0.65},
        goalserve_spread={"home_implied_prob": 0.60, "away_implied_prob": 0.40},
        goalserve_halftime={"home_implied_prob": 0.60},  # ≥ 0.5 → ht confirmed
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert adj.get("max_moneyline_seconds_remaining") == 240  # 180 + 30*2


def test_halftime_alone_does_not_trigger_multi_confirm() -> None:
    config = _config()
    ctx = _ctx(
        goalserve_halftime={"home_implied_prob": 0.65},
    )
    # No ML/spread → no multi-confirm → no adjustment from HT alone
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert adj == {}


# ---------------------------------------------------------------------------
# Suspended signals are ignored
# ---------------------------------------------------------------------------


def test_suspended_ml_does_not_confirm() -> None:
    config = _config(
        tail_min_moneyline_lead=6,
        goalserve_spread_conflict_threshold=0.15,
        goalserve_multi_confirm_lead_relief=2,
        goalserve_multi_confirm_time_bonus_seconds=30,
    )
    ctx = _ctx(
        goalserve_moneyline={"suspended": True, "home_implied_prob": 0.70},
        goalserve_spread={"home_implied_prob": 0.60, "away_implied_prob": 0.40},
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    # spread confirms but ML suspended → no multi-confirm, no time extension
    assert "max_moneyline_seconds_remaining" not in adj


def test_suspended_spread_does_not_conflict() -> None:
    config = _config(goalserve_spread_conflict_extra_lead=3)
    ctx = _ctx(
        goalserve_spread={"suspended": True, "home_implied_prob": 0.10, "away_implied_prob": 0.90}
    )
    adj = _goalserve_policy_adjustments(config, ctx, SportsMarketSide.HOME)
    assert "min_moneyline_lead" not in adj
