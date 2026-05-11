from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from polymarket_trader.infra.sports.season_odds_client import (
    parse_theoddsapi_outrights_payload,
)


def test_parses_outrights_with_devig() -> None:
    payload = [
        {
            "id": "nba-championship-2026",
            "sport_key": "basketball_nba_championship_winner",
            "commence_time": "2026-10-01T00:00:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Boston Celtics", "price": 3.0},
                                {"name": "Denver Nuggets", "price": 5.0},
                                {"name": "Other", "price": 1.5},
                            ],
                        }
                    ],
                },
                {
                    "key": "fanduel",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Boston Celtics", "price": 3.2},
                                {"name": "Denver Nuggets", "price": 4.8},
                                {"name": "Other", "price": 1.5},
                            ],
                        }
                    ],
                },
            ],
        }
    ]

    snapshot = parse_theoddsapi_outrights_payload(
        payload,
        market_key="basketball_nba_championship_winner",
        observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )

    assert snapshot is not None
    assert snapshot.source == "theoddsapi"
    assert snapshot.market_key == "basketball_nba_championship_winner"
    probs = snapshot.fair_probabilities
    assert set(probs.keys()) == {"Boston Celtics", "Denver Nuggets", "Other"}
    total = sum(probs.values())
    # De-vig 后必须严格归一到 1（Decimal 比对要容忍少量浮点误差）。
    assert abs(total - Decimal(1)) < Decimal("0.0000001")
    # raw prob from price=3.x is roughly 0.32 (boston); after devig should
    # be in a sane range.
    assert Decimal("0.20") < probs["Boston Celtics"] < Decimal("0.45")


def test_returns_none_when_market_not_found() -> None:
    payload = [{"id": "other-market", "bookmakers": []}]
    assert (
        parse_theoddsapi_outrights_payload(
            payload, market_key="nba-championship", observed_at=datetime(2026, 5, 11, tzinfo=timezone.utc)
        )
        is None
    )


def test_returns_none_for_non_sequence_payload() -> None:
    assert parse_theoddsapi_outrights_payload({"data": "wrong shape"}, market_key="x") is None


def _two_outcome_event(p_yes: float, p_no: float, market_key: str = "k") -> list[dict]:
    """构造两个 outcome 的事件，其中 raw price 由 (1/raw_prob) 反推。"""

    return [
        {
            "id": market_key,
            "sport_key": market_key,
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Yes", "price": 1 / p_yes},
                                {"name": "No", "price": 1 / p_no},
                            ],
                        }
                    ],
                }
            ],
        }
    ]


def test_devig_handles_zero_vig_market() -> None:
    """无 vig（sum=1）时 de-vig 应该返回与 raw 一致的概率。"""

    payload = _two_outcome_event(0.5, 0.5, market_key="x")
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="x")
    assert snapshot is not None
    yes = snapshot.fair_probabilities["Yes"]
    no = snapshot.fair_probabilities["No"]
    assert abs(yes - Decimal("0.5")) < Decimal("0.001")
    assert abs(no - Decimal("0.5")) < Decimal("0.001")
    assert abs((yes + no) - Decimal(1)) < Decimal("0.0000001")


def test_devig_handles_heavy_vig() -> None:
    """raw 概率明显 > 1（重 vig）时收敛到归一分布。"""

    # raw_yes=0.7, raw_no=0.5 → sum=1.2 (20% vig)
    payload = _two_outcome_event(0.7, 0.5, market_key="k")
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="k")
    assert snapshot is not None
    total = sum(snapshot.fair_probabilities.values())
    assert abs(total - Decimal(1)) < Decimal("0.0000001")
    # Yes 应该仍然 > No（power 单调）
    assert snapshot.fair_probabilities["Yes"] > snapshot.fair_probabilities["No"]


def test_devig_handles_underround_market() -> None:
    """raw 概率明显 < 1（贴现/underround，偶见于 sharps）时也收敛到 sum=1。"""

    payload = _two_outcome_event(0.3, 0.4, market_key="u")
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="u")
    assert snapshot is not None
    total = sum(snapshot.fair_probabilities.values())
    assert abs(total - Decimal(1)) < Decimal("0.0000001")


def test_devig_handles_single_outcome_event() -> None:
    """退化场景：只有一个 outcome。归一后应该是 1.0。"""

    payload = [
        {
            "id": "s",
            "sport_key": "s",
            "bookmakers": [
                {
                    "key": "dk",
                    "markets": [
                        {"key": "outrights", "outcomes": [{"name": "Sole", "price": 2.0}]},
                    ],
                }
            ],
        }
    ]
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="s")
    assert snapshot is not None
    assert snapshot.fair_probabilities["Sole"] == Decimal(1)


def test_devig_handles_extreme_long_shot() -> None:
    """长尾候选（极小概率）和明显热门并存：power method 应稳定收敛。"""

    payload = [
        {
            "id": "lt",
            "sport_key": "lt",
            "bookmakers": [
                {
                    "key": "dk",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Favorite", "price": 1.2},      # raw ~0.83
                                {"name": "Mid", "price": 8.0},           # raw ~0.125
                                {"name": "Long shot", "price": 200.0},   # raw 0.005
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="lt")
    assert snapshot is not None
    total = sum(snapshot.fair_probabilities.values())
    assert abs(total - Decimal(1)) < Decimal("0.0000001")
    fav = snapshot.fair_probabilities["Favorite"]
    long = snapshot.fair_probabilities["Long shot"]
    assert fav > long
    assert long > Decimal(0)  # 不应该被压成 0


def test_devig_handles_invalid_prices_gracefully() -> None:
    """非正价格（0 / 负数 / 字符串）的 outcome 被丢弃，剩下的仍正确归一。"""

    payload = [
        {
            "id": "x",
            "sport_key": "x",
            "bookmakers": [
                {
                    "key": "dk",
                    "markets": [
                        {
                            "key": "outrights",
                            "outcomes": [
                                {"name": "Bad zero", "price": 0},
                                {"name": "Bad neg", "price": -1.5},
                                {"name": "Bad str", "price": "garbage"},
                                {"name": "Good", "price": 1.5},
                                {"name": "Also good", "price": 3.0},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="x")
    assert snapshot is not None
    assert set(snapshot.fair_probabilities.keys()) == {"Good", "Also good"}
    total = sum(snapshot.fair_probabilities.values())
    assert abs(total - Decimal(1)) < Decimal("0.0000001")


def test_returns_none_when_event_has_no_bookmakers() -> None:
    payload = [{"id": "x", "sport_key": "x", "bookmakers": []}]
    assert parse_theoddsapi_outrights_payload(payload, market_key="x") is None


def test_returns_none_when_no_outrights_market_among_bookmakers() -> None:
    """bookmaker 提供其它 market（如 h2h）但没有 outrights → 返回 None。"""

    payload = [
        {
            "id": "x",
            "sport_key": "x",
            "bookmakers": [
                {
                    "key": "dk",
                    "markets": [{"key": "h2h", "outcomes": [{"name": "Yes", "price": 2.0}]}],
                }
            ],
        }
    ]
    assert parse_theoddsapi_outrights_payload(payload, market_key="x") is None


def test_marks_conflicts_when_three_or_more_bookmakers() -> None:
    payload = [
        {
            "id": "k",
            "sport_key": "k",
            "bookmakers": [
                {"key": b, "markets": [{"key": "outrights", "outcomes": [{"name": "Yes", "price": 2.0}, {"name": "No", "price": 2.0}]}]}
                for b in ("dk", "fd", "betmgm")
            ],
        }
    ]
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="k")
    assert snapshot is not None
    assert snapshot.source_conflicts is True


def test_no_conflict_flag_for_single_bookmaker() -> None:
    payload = _two_outcome_event(0.5, 0.5, market_key="k")
    snapshot = parse_theoddsapi_outrights_payload(payload, market_key="k")
    assert snapshot is not None
    assert snapshot.source_conflicts is False
