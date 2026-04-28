from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.infra.db.models import MarketModel
from polymarket_trader.infra.polymarket.schemas import normalize_gamma_market


def test_gamma_market_maps_game_start_time_to_domain_market() -> None:
    """Gamma 的真实开赛时间进入领域模型，不能只保留 resolution endDate。"""

    market = normalize_gamma_market(
        {
            "conditionId": "condition-1",
            "slug": "mlb-mia-lad-2026-04-28",
            "question": "Marlins vs Dodgers",
            "clobTokenIds": ["mia", "lad"],
            "outcomes": ["Miami Marlins", "Los Angeles Dodgers"],
            "orderPriceMinTickSize": "0.01",
            "orderMinSize": "5",
            "endDate": "2026-05-06T02:10:00Z",
            "gameStartTime": "2026-04-29 02:10:00+00",
        }
    ).to_market()

    assert market.game_start_time is not None
    assert market.game_start_time.isoformat() == "2026-04-29T02:10:00+00:00"


def test_market_model_round_trips_game_start_time_through_raw_payload() -> None:
    """不新增 DB 列时，开赛时间仍必须随 raw_payload 恢复。"""

    market = Market(
        condition_id="condition-1",
        market_slug="mlb-mia-lad-2026-04-28",
        outcomes=(
            MarketOutcome(token_id="mia", outcome="Miami Marlins"),
            MarketOutcome(token_id="lad", outcome="Los Angeles Dodgers"),
        ),
        game_start_time=datetime(2026, 4, 29, 2, 10, tzinfo=timezone.utc),
        trading_status=TradingStatus.ELIGIBLE,
    )

    model = MarketModel.from_domain(market, raw_payload={"conditionId": "condition-1"})
    restored = model.to_domain()

    assert model.raw_payload["game_start_time"] == "2026-04-29T02:10:00+00:00"
    assert restored.game_start_time == market.game_start_time
