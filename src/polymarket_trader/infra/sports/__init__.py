"""外部体育数据源适配器。"""

from polymarket_trader.infra.sports.common import (
    SportsDataClientError,
    SportsDataRateLimitError,
    SportsDataResponseError,
    SportsDataTimeoutError,
    SportsDataTransportError,
)
from polymarket_trader.infra.sports.goalserve_inplay_client import GoalserveInplayClient
from polymarket_trader.infra.sports.goalserve_inplay_parsers import (
    GoalserveMarket,
    GoalserveOdds,
    GoalserveOutcome,
    parse_goalserve_inplay,
)
from polymarket_trader.infra.sports.goalserve_livescore_client import GoalserveLivescoreClient
from polymarket_trader.infra.sports.goalserve_pregame_client import (
    GoalservePregameOddsClient,
    GoalservePregameSnapshot,
    PregameMarket,
    PregameMatch,
    PregameOutcome,
)

__all__ = [
    "GoalserveInplayClient",
    "GoalserveLivescoreClient",
    "GoalservePregameOddsClient",
    "GoalservePregameSnapshot",
    "GoalserveMarket",
    "PregameMatch",
    "PregameMarket",
    "PregameOutcome",
    "GoalserveOdds",
    "GoalserveOutcome",
    "SportsDataClientError",
    "SportsDataRateLimitError",
    "SportsDataResponseError",
    "SportsDataTimeoutError",
    "SportsDataTransportError",
    "parse_goalserve_inplay",
]
