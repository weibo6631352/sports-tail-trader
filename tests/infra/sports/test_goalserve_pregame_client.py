"""GoalservePregameOddsClient 单元测试：JSON 解析、增量 ts、多运动并发、失败隔离。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from polymarket_trader.infra.sports.goalserve_pregame_client import (
    GoalservePregameOddsClient,
    GoalservePregameSnapshot,
    PregameMatch,
    _parse_pregame_response,
)

_OBSERVED = datetime(2026, 5, 20, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# 测试 fixture（最小 JSON payload）
# ---------------------------------------------------------------------------

_SOCCER_PAYLOAD: dict = {
    "scores": {
        "sport": "soccer",
        "ts": "1474825423341",
        "category": [
            {
                "id": "1204",
                "name": "English Premier League",
                "match": [
                    {
                        "id": "12345",
                        "date": "22.05.2025",
                        "time": "15:00",
                        "localteam": {"name": "Arsenal", "id": "100"},
                        "visitorteam": {"name": "Chelsea", "id": "101"},
                        "odds": {
                            "bookmaker": [
                                {
                                    "id": "16",
                                    "name": "Bet365",
                                    "market": [
                                        {
                                            "id": "1",
                                            "name": "Match Winner",
                                            "suspended": "0",
                                            "outcome": [
                                                {"name": "Home", "odds": "2.10", "suspended": "0"},
                                                {"name": "Draw", "odds": "3.50", "suspended": "0"},
                                                {"name": "Away", "odds": "3.20", "suspended": "0"},
                                            ],
                                        },
                                        {
                                            "id": "2",
                                            "name": "Over/Under",
                                            "suspended": "0",
                                            "outcome": [
                                                {"name": "Over", "odds": "1.80", "suspended": "0"},
                                                {"name": "Under", "odds": "2.00", "suspended": "0"},
                                            ],
                                        },
                                    ],
                                }
                            ]
                        },
                    }
                ],
            }
        ],
    }
}

_EMPTY_PAYLOAD: dict = {
    "scores": {
        "sport": "basketball",
        "ts": "9999999",
        "category": [],
    }
}


# ---------------------------------------------------------------------------
# 解析单元测试（不需要 HTTP）
# ---------------------------------------------------------------------------


def test_parse_pregame_response_basic_fields() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)

    assert snapshot.sport == "soccer"
    assert snapshot.ts == "1474825423341"
    assert snapshot.fetched_at == _OBSERVED
    assert len(snapshot.matches) == 1


def test_parse_match_teams_and_league() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match: PregameMatch = snapshot.matches[0]

    assert match.match_id == "12345"
    assert match.home_team == "Arsenal"
    assert match.away_team == "Chelsea"
    assert match.league == "English Premier League"
    assert match.sport == "soccer"


def test_parse_match_start_time() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match = snapshot.matches[0]

    assert match.start_time is not None
    assert match.start_time.year == 2025
    assert match.start_time.month == 5
    assert match.start_time.day == 22
    assert match.start_time.tzinfo is not None


def test_parse_moneyline_market() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match = snapshot.matches[0]
    market = match.moneyline_market()

    assert market is not None
    assert market.name == "Match Winner"
    assert len(market.outcomes) == 3


def test_parse_totals_market() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match = snapshot.matches[0]
    market = match.totals_market()

    assert market is not None
    assert market.name == "Over/Under"
    assert len(market.outcomes) == 2


def test_implied_prob_uses_decimal() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match = snapshot.matches[0]
    moneyline = match.moneyline_market()
    assert moneyline is not None

    home = next(o for o in moneyline.outcomes if o.name == "Home")
    assert isinstance(home.value_eu, Decimal)
    assert isinstance(home.implied_prob, Decimal)
    # 1 / 2.10 ≈ 0.476...
    expected = Decimal("1") / Decimal("2.10")
    assert home.implied_prob == expected


def test_suspended_flag_parsed() -> None:
    snapshot = _parse_pregame_response("soccer", _SOCCER_PAYLOAD, _OBSERVED)
    match = snapshot.matches[0]
    moneyline = match.moneyline_market()
    assert moneyline is not None
    assert moneyline.suspended is False
    for outcome in moneyline.outcomes:
        assert outcome.suspended is False


def test_empty_categories_returns_no_matches() -> None:
    snapshot = _parse_pregame_response("basketball", _EMPTY_PAYLOAD, _OBSERVED)

    assert snapshot.sport == "basketball"
    assert snapshot.ts == "9999999"
    assert snapshot.matches == ()


def test_missing_ts_returns_none() -> None:
    payload = {"scores": {"sport": "soccer", "category": []}}
    snapshot = _parse_pregame_response("soccer", payload, _OBSERVED)

    assert snapshot.ts is None


def test_match_without_odds_still_parsed() -> None:
    payload = {
        "scores": {
            "sport": "soccer",
            "ts": "111",
            "category": [
                {
                    "name": "Test League",
                    "match": [
                        {
                            "id": "99",
                            "date": "20.05.2026",
                            "time": "10:00",
                            "localteam": {"name": "Team A"},
                            "visitorteam": {"name": "Team B"},
                        }
                    ],
                }
            ],
        }
    }
    snapshot = _parse_pregame_response("soccer", payload, _OBSERVED)
    assert len(snapshot.matches) == 1
    match = snapshot.matches[0]
    assert match.markets == ()
    assert match.moneyline_market() is None
    assert match.totals_market() is None


# ---------------------------------------------------------------------------
# 客户端 HTTP 集成测试（mock httpx）
# ---------------------------------------------------------------------------


def _make_client(sports: tuple[str, ...] = ("soccer",)) -> GoalservePregameOddsClient:
    with patch("httpx.AsyncClient"):
        return GoalservePregameOddsClient(
            api_key="test_key",
            sports=sports,
            now_provider=lambda: _OBSERVED,
        )


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_fetch_sport_returns_snapshot() -> None:
    async def run() -> None:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        snapshot = await client.fetch_sport("soccer")

        assert isinstance(snapshot, GoalservePregameSnapshot)
        assert snapshot.sport == "soccer"
        assert len(snapshot.matches) == 1
        assert snapshot.ts == "1474825423341"

    asyncio.run(run())


def test_fetch_sport_with_ts_includes_ts_in_url() -> None:
    """携带 ts 参数时 URL 中应包含 &ts=..."""

    async def run() -> None:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        await client.fetch_sport("soccer", ts="1474825423341")

        call_args = client._client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "ts=1474825423341" in url

    asyncio.run(run())


def test_fetch_sport_no_ts_url_without_ts_param() -> None:
    """首次拉取（无 ts）URL 不应包含 ts 参数。"""

    async def run() -> None:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        await client.fetch_sport("soccer")

        call_args = client._client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "ts=" not in url

    asyncio.run(run())


def test_fetch_sport_failure_returns_empty_snapshot() -> None:
    """网络错误返回空 snapshot，不抛出。"""

    async def run() -> GoalservePregameSnapshot:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        return await client.fetch_sport("soccer")

    snapshot = asyncio.run(run())
    assert snapshot.matches == ()
    assert snapshot.ts is None
    assert snapshot.sport == "soccer"


def test_fetch_sport_unsupported_sport_returns_empty() -> None:
    """不支持的运动返回空 snapshot，不抛出。"""

    async def run() -> GoalservePregameSnapshot:
        client = _make_client(sports=("soccer",))
        return await client.fetch_sport("quidditch")

    snapshot = asyncio.run(run())
    assert snapshot.matches == ()
    assert snapshot.sport == "quidditch"


def test_fetch_all_concurrent_multi_sport() -> None:
    """fetch_all 并发拉取多运动，返回 {sport: snapshot} 映射。"""

    async def run() -> dict[str, GoalservePregameSnapshot]:
        client = _make_client(sports=("soccer", "basketball"))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        return await client.fetch_all()

    result = asyncio.run(run())
    assert "soccer" in result
    assert "basketball" in result
    assert isinstance(result["soccer"], GoalservePregameSnapshot)


def test_fetch_all_one_sport_failure_does_not_block_others() -> None:
    """单运动失败不阻断其他运动，失败运动返回空 snapshot。"""

    async def run() -> dict[str, GoalservePregameSnapshot]:
        client = _make_client(sports=("soccer", "basketball"))

        async def side_effect(url: str, **kwargs: object) -> MagicMock:
            if "soccer" in url:
                raise httpx.ConnectError("timeout")
            return _mock_response(_EMPTY_PAYLOAD)

        client._client.get = AsyncMock(side_effect=side_effect)
        return await client.fetch_all()

    result = asyncio.run(run())
    assert result["soccer"].matches == ()
    assert isinstance(result["basketball"], GoalservePregameSnapshot)


def test_fetch_all_with_ts_by_sport() -> None:
    """ts_by_sport 中有 ts 时，对应运动 URL 应包含 ts 参数。"""

    async def run() -> None:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        await client.fetch_all(ts_by_sport={"soccer": "12345"})

        call_args = client._client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "ts=12345" in url

    asyncio.run(run())


def test_url_contains_api_key() -> None:
    """构造的 URL 中必须包含 API key（不能泄漏，但客户端内部需要正确嵌入）。"""

    async def run() -> None:
        client = _make_client(sports=("soccer",))
        client._client.get = AsyncMock(return_value=_mock_response(_SOCCER_PAYLOAD))
        await client.fetch_sport("soccer")

        call_args = client._client.get.call_args
        url = call_args[0][0] if call_args[0] else call_args.kwargs.get("url", "")
        assert "test_key" in url

    asyncio.run(run())


def test_proxy_parameter_accepted_without_error() -> None:
    with patch("httpx.AsyncHTTPTransport"), patch("httpx.AsyncClient"):
        client = GoalservePregameOddsClient(
            api_key="k",
            sports=("soccer",),
            proxy="http://127.0.0.1:7890",
        )
    assert client is not None
