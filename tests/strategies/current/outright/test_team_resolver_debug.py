"""``resolve_market_team_debug`` 返回 TeamResolutionTrace 的契约。

诊断函数；与 ``resolve_market_team`` 同算法但额外输出 normalized_text /
candidate_teams / matches，admin endpoint 用来快速定位"为什么 OUTRIGHT_TEAM_NOT_RESOLVED"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current.outright.team_resolver import (
    TeamResolutionTrace,
    resolve_market_team_debug,
)


_NOW = datetime(2026, 5, 13, tzinfo=timezone.utc)


def _snapshot(probs: dict[str, str]) -> SeasonOddsSnapshot:
    return SeasonOddsSnapshot(
        market_key="2026-nba-championship-winner",
        fair_probabilities={k: Decimal(v) for k, v in probs.items()},
        observed_at=_NOW,
        source="theoddsapi",
    )


def _market(question: str = "", title: str = "", slug: str = "") -> Market:
    return Market(
        condition_id="c",
        market_slug="m",
        market_question=question,
        event_title=title,
        event_slug=slug,
        outcomes=(MarketOutcome(token_id="t", outcome="Yes"),),
    )


def test_debug_returns_trace_with_unique_match() -> None:
    snap = _snapshot({"Boston Celtics": "0.33", "Denver Nuggets": "0.30"})
    market = _market(question="Will the Boston Celtics win the 2026 NBA championship?")
    trace = resolve_market_team_debug(market, snap)

    assert isinstance(trace, TeamResolutionTrace)
    assert trace.resolved == "Boston Celtics"
    assert trace.ambiguous is False
    assert "boston celtics" in trace.normalized_text
    assert "Boston Celtics" in trace.candidate_teams
    assert trace.matches == ("Boston Celtics",)


def test_debug_returns_zero_match_when_no_team_in_text() -> None:
    snap = _snapshot({"Boston Celtics": "0.33", "Denver Nuggets": "0.30"})
    market = _market(question="Will it rain at the finals?")
    trace = resolve_market_team_debug(market, snap)

    assert trace.resolved is None
    assert trace.ambiguous is False
    assert trace.matches == ()


def test_debug_returns_ambiguous_when_multiple_match() -> None:
    snap = _snapshot({"Boston Celtics": "0.5", "Denver Nuggets": "0.5"})
    market = _market(question="Boston Celtics vs. Denver Nuggets championship")
    trace = resolve_market_team_debug(market, snap)

    assert trace.resolved is None  # 歧义 → None
    assert trace.ambiguous is True
    assert set(trace.matches) == {"Boston Celtics", "Denver Nuggets"}


def test_debug_payload_is_jsonable_dict() -> None:
    """as_payload 返回的 dict 应只含字符串 / 列表 / bool / None：便于 admin 序列化。"""

    snap = _snapshot({"Boston Celtics": "0.33"})
    market = _market(question="Will the Boston Celtics win?")
    trace = resolve_market_team_debug(market, snap)
    payload = trace.as_payload()

    assert payload["resolved"] == "Boston Celtics"
    assert payload["ambiguous"] is False
    assert payload["candidate_teams"] == ["Boston Celtics"]
    assert payload["matches"] == ["Boston Celtics"]
    # 没有不能 JSON 序列化的值（如 Decimal / datetime）。
    import json

    json.dumps(payload)  # 不应抛 TypeError
