"""外部体育数据源适配器。"""

from polymarket_trader.infra.sports.aggregate_client import SportsLiveAggregateClient
from polymarket_trader.infra.sports.common import (
    SportsDataClientError,
    SportsDataRateLimitError,
    SportsDataResponseError,
    SportsDataTimeoutError,
    SportsDataTransportError,
)
from polymarket_trader.infra.sports.espn_client import (
    EspnScoreboardClient,
    parse_espn_race_payload,
    parse_espn_scoreboard_payload,
)
from polymarket_trader.infra.sports.espn_standings_client import (
    EspnStandingsClient,
    parse_espn_standings_payload,
)
from polymarket_trader.infra.sports.mlb_client import MlbStatsApiClient, parse_mlb_schedule_payload
from polymarket_trader.infra.sports.nba_client import NbaLiveScoreboardClient, parse_nba_scoreboard_payload
from polymarket_trader.infra.sports.nhl_client import NhlScoreApiClient, parse_nhl_score_payload
from polymarket_trader.infra.sports.pandascore_client import (
    PandascoreLiveClient,
    parse_pandascore_lives_payload,
)
from polymarket_trader.infra.sports.season_odds_client import (
    SeasonOddsClient,
    TheOddsApiClient,
    parse_theoddsapi_outrights_payload,
)
from polymarket_trader.infra.sports.sofascore_client import (
    SofaScoreLiveClient,
    parse_sofascore_events_payload,
    sofascore_sports_for_leagues,
)
from polymarket_trader.infra.sports.thesportsdb_client import (
    TheSportsDbLiveClient,
    parse_thesportsdb_events_payload,
    thesportsdb_sports_for_leagues,
)
from polymarket_trader.infra.sports.tennis_live_data_client import (
    TennisLiveDataClient,
    parse_tennis_live_data_payload,
)
from polymarket_trader.infra.sports.api_football_client import (
    ApiFootballClient,
    parse_api_football_payload,
)
from polymarket_trader.infra.sports.college_football_data_client import (
    CollegeFootballDataClient,
    parse_cfbd_games_payload,
    parse_ncaa_api_scoreboard,
)

__all__ = [
    "ApiFootballClient",
    "CollegeFootballDataClient",
    "EspnScoreboardClient",
    "EspnStandingsClient",
    "MlbStatsApiClient",
    "NbaLiveScoreboardClient",
    "NhlScoreApiClient",
    "PandascoreLiveClient",
    "SeasonOddsClient",
    "SofaScoreLiveClient",
    "TennisLiveDataClient",
    "TheOddsApiClient",
    "TheSportsDbLiveClient",
    "SportsDataClientError",
    "SportsDataRateLimitError",
    "SportsDataResponseError",
    "SportsDataTimeoutError",
    "SportsDataTransportError",
    "SportsLiveAggregateClient",
    "parse_api_football_payload",
    "parse_cfbd_games_payload",
    "parse_espn_race_payload",
    "parse_espn_scoreboard_payload",
    "parse_espn_standings_payload",
    "parse_mlb_schedule_payload",
    "parse_nba_scoreboard_payload",
    "parse_ncaa_api_scoreboard",
    "parse_nhl_score_payload",
    "parse_pandascore_lives_payload",
    "parse_sofascore_events_payload",
    "parse_tennis_live_data_payload",
    "parse_theoddsapi_outrights_payload",
    "parse_thesportsdb_events_payload",
    "sofascore_sports_for_leagues",
    "thesportsdb_sports_for_leagues",
]
