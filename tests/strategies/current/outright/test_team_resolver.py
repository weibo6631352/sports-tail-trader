from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.domain.market import Market, MarketOutcome
from polymarket_trader.domain.sports_season import SeasonOddsSnapshot
from strategies.current.outright.team_resolver import resolve_market_team


def _snap(probs: dict[str, str]) -> SeasonOddsSnapshot:
    return SeasonOddsSnapshot(
        market_key="nba-championship",
        fair_probabilities={k: Decimal(v) for k, v in probs.items()},
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
        source="theoddsapi",
    )


def _market(
    *,
    question: str | None = None,
    event_title: str | None = None,
    event_slug: str | None = None,
    slug: str = "m",
    cond: str = "c",
) -> Market:
    return Market(
        condition_id=cond,
        market_slug=slug,
        outcomes=(MarketOutcome(token_id="t", outcome="Yes"),),
        market_question=question,
        event_title=event_title,
        event_slug=event_slug,
    )


def test_resolver_uniquely_matches_full_team_name() -> None:
    snap = _snap({"Boston Celtics": "0.33", "Denver Nuggets": "0.30"})
    market = _market(
        question="Will the Boston Celtics win the 2026 NBA championship?",
        event_title="2026 NBA Champion",
        event_slug="2026-nba-championship-winner",
    )
    assert resolve_market_team(market, snap) == "Boston Celtics"


def test_resolver_matches_short_last_token() -> None:
    snap = _snap({"Boston Celtics": "0.33", "Denver Nuggets": "0.30"})
    market = _market(
        question="Will Celtics win the 2026 NBA championship?",
        event_slug="2026-nba-championship-winner",
    )
    assert resolve_market_team(market, snap) == "Boston Celtics"


def test_resolver_returns_none_on_ambiguous_match() -> None:
    snap = _snap(
        {
            "Los Angeles Lakers": "0.30",
            "Los Angeles Clippers": "0.25",
        }
    )
    market = _market(
        question="Will Los Angeles win the 2026 NBA championship?",
    )
    # 全名都不命中，但 last token 都不重叠；歧义来自全名两次匹配 "los angeles"——
    # 但我们的规则要求 full-name word-boundary，"los angeles lakers" 不在文本中，
    # 因此全名都不命中，再走 last token：lakers / clippers 都不在 → 0 命中 → None。
    assert resolve_market_team(market, snap) is None


def test_resolver_returns_none_when_two_full_names_match() -> None:
    snap = _snap(
        {
            "Boston Celtics": "0.33",
            "Denver Nuggets": "0.30",
        }
    )
    # 故意构造一段同时提及两支球队的市场文本——例如错误的多球队 question。
    market = _market(
        question="Boston Celtics vs Denver Nuggets head-to-head?",
    )
    assert resolve_market_team(market, snap) is None


def test_resolver_returns_none_when_no_team_appears() -> None:
    snap = _snap({"Boston Celtics": "0.33"})
    market = _market(question="Will the Phoenix Suns win the 2026 NBA championship?")
    assert resolve_market_team(market, snap) is None


def test_resolver_handles_case_punctuation_and_ampersand() -> None:
    snap = _snap({"Texas A&M": "0.10"})
    market = _market(question="will texas a and m win the 2026 ncaa football championship?")
    assert resolve_market_team(market, snap) == "Texas A&M"


def test_resolver_falls_back_to_event_slug() -> None:
    snap = _snap({"Boston Celtics": "0.33", "Denver Nuggets": "0.30"})
    # market_question 没有球队信息（极端情况），但 event_slug 含球队短名。
    market = _market(
        question="Will this team win?",
        event_slug="will-boston-celtics-win-the-2026-nba-championship",
    )
    assert resolve_market_team(market, snap) == "Boston Celtics"
