"""外部体育数据源适配器。"""

from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient
from polymarket_trader.infra.sports.common import (
    SportsDataClientError,
    SportsDataRateLimitError,
    SportsDataResponseError,
    SportsDataTimeoutError,
    SportsDataTransportError,
)
from polymarket_trader.infra.sports.goalserve_client import GoalserveClient
from polymarket_trader.infra.sports.goalserve_livescore_client import GoalserveLivescoreClient
from polymarket_trader.infra.sports.goalserve_pregame_client import (
    GoalservePregameOddsClient,
    GoalservePregameSnapshot,
    PregameMatch,
    PregameMarket,
    PregameOutcome,
)
from polymarket_trader.infra.sports.goalserve_parsers import (
    GoalserveMarket,
    GoalserveOdds,
    GoalserveOutcome,
    parse_goalserve_ws_events,
)
from polymarket_trader.infra.sports.season_odds_client import (
    SeasonOddsClient,
    TheOddsApiClient,
    parse_theoddsapi_outrights_payload,
)

__all__ = [
    "GoalserveClient",
    "GoalserveLivescoreClient",
    "GoalservePregameOddsClient",
    "GoalservePregameSnapshot",
    "GoalserveMarket",
    "PregameMatch",
    "PregameMarket",
    "PregameOutcome",
    "GoalserveOdds",
    "GoalserveOutcome",
    "SeasonOddsClient",
    "TheOddsApiClient",
    "SportsDataClientError",
    "SportsDataRateLimitError",
    "SportsDataResponseError",
    "SportsDataTimeoutError",
    "SportsDataTransportError",
    "SportsLiveAggregateClient",
    "parse_goalserve_ws_events",
    "parse_theoddsapi_outrights_payload",
]
