from __future__ import annotations

from datetime import datetime, timezone

from polymarket_trader.domain.market import Market, MarketOutcome, TradingStatus
from polymarket_trader.domain.sports_live import (
    BaseballGameState,
    SportsLiveGame,
    SportsLiveGameStatus,
    SportsLiveTeam,
    TennisGameState,
)
import strategies.current.live_state as live_state_module
import strategies.current.strategy as strategy_module
from strategies.current.config import CurrentStrategyConfig
from strategies.current.live_state import match_live_game
from strategies.current.strategy import CurrentStrategy


def test_live_state_match_requires_market_date_when_both_sides_have_dates() -> None:
    market = _market("nba-phi-bos-2026-05-02")
    game = _game(start_time_utc="2026-04-28T23:30:00Z")

    assert match_live_game(market, game) is None


def test_live_state_match_accepts_same_date_team_match() -> None:
    market = _market("nba-phi-bos-2026-04-28")
    game = _game(start_time_utc="2026-04-28T23:30:00Z")

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "401869408"


def test_live_state_match_accepts_compact_soccer_team_variants() -> None:
    market = Market(
        condition_id="mar1-old-uts-condition",
        market_slug="mar1-old-uts-2026-04-30",
        market_question="Olympic Dcheira vs UnionTouargaSports",
        event_title="Olympic Dcheira vs UnionTouargaSports",
        event_slug="mar1-old-uts-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Botola Pro", "MAR1"),
        outcomes=(
            MarketOutcome(token_id="old", outcome="Olympic Dcheira"),
            MarketOutcome(token_id="uts", outcome="UnionTouargaSports"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16037147",
        league="Botola Pro",
        home=SportsLiveTeam(name="Olympique Dcheira", score=0),
        away=SportsLiveTeam(name="Union Touarga Sport", score=0, short_name="Union Touarga"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777561200,
            "tournament": "Botola Pro",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.matched_home_alias == "Olympique Dcheira"
    assert match.matched_away_alias == "Union Touarga Sport"


def test_live_state_match_accepts_accented_basketball_team_names() -> None:
    market = Market(
        condition_id="euroleague-fenerbahce-kaunas-condition",
        market_slug="euroleague-fenerbah-kaunas-2026-04-30",
        market_question="Fenerbahce vs. Zalgiris Kaunas",
        event_title="Fenerbahce vs. Zalgiris Kaunas",
        event_slug="euroleague-fenerbah-kaunas-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 16, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Basketball", "Euroleague Basketball"),
        outcomes=(
            MarketOutcome(token_id="fenerbahce", outcome="Fenerbahce"),
            MarketOutcome(token_id="zalgiris", outcome="Zalgiris Kaunas"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16000001",
        league="Euroleague",
        home=SportsLiveTeam(name="Fenerbahçe Beko", score=0, short_name="Fenerbahçe"),
        away=SportsLiveTeam(name="Žalgiris Kaunas", score=0, short_name="Žalgiris"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "basketball",
            "start_timestamp": 1777564800,
            "tournament": "Euroleague",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.matched_home_alias == "Fenerbahçe"
    assert match.matched_away_alias == "Žalgiris Kaunas"


def test_live_state_match_ignores_basketball_club_and_sponsor_tokens() -> None:
    market = Market(
        condition_id="aba-spartak-zadar-condition",
        market_slug="bkaba-spa-zad-2026-04-30",
        market_question="Spartak Subotica vs. Zadar",
        event_title="Spartak Subotica vs. Zadar",
        event_slug="bkaba-spa-zad-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 17, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Basketball", "ABA League", "bkaba"),
        outcomes=(
            MarketOutcome(token_id="spartak", outcome="Spartak Subotica"),
            MarketOutcome(token_id="zadar", outcome="Zadar"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16074050",
        league="ABA League",
        home=SportsLiveTeam(
            name="KK Spartak Office Shoes",
            score=0,
            short_name="KK Spartak",
            aliases=("kk spartak office shoes subotica",),
        ),
        away=SportsLiveTeam(
            name="KK Zadar",
            score=0,
            short_name="Zadar",
            aliases=("kk zadar",),
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "basketball",
            "start_timestamp": 1777568400,
            "tournament": "ABA League",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "16074050"
    assert match.matched_home_alias == "kk spartak office shoes subotica"
    assert match.matched_away_alias in {"KK Zadar", "Zadar"}


def test_live_state_match_ignores_basketball_team_suffix() -> None:
    market = Market(
        condition_id="euroleague-olympiacos-monaco-condition",
        market_slug="euroleague-olympiac-monaco-2026-04-30",
        market_question="Olympiacos B.C. vs. Monaco",
        event_title="Olympiacos B.C. vs. Monaco",
        event_slug="euroleague-olympiac-monaco-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 17, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Basketball", "Euroleague Basketball"),
        outcomes=(
            MarketOutcome(token_id="olympiacos", outcome="Olympiacos B.C."),
            MarketOutcome(token_id="monaco", outcome="Monaco"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="15916331",
        league="Euroleague",
        home=SportsLiveTeam(name="Olympiacos BC", score=0, short_name="Olympiacos"),
        away=SportsLiveTeam(name="Monaco Basket", score=0, short_name="Monaco Basket"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "basketball",
            "start_timestamp": 1777572000,
            "tournament": "Euroleague",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "15916331"


def test_live_state_match_accepts_chinese_soccer_translation_variants() -> None:
    market = Market(
        condition_id="chinese-super-league-tianjin-wuhan-condition",
        market_slug="chi-jin-wsz-2026-05-01",
        market_question="Tianjin Jinmen Hu FC vs. Wuhan San Zhen FC",
        event_title="Tianjin Jinmen Hu FC vs. Wuhan San Zhen FC",
        event_slug="chi-jin-wsz-2026-05-01",
        game_start_time=datetime(2026, 5, 1, 11, 35, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Chinese Super League"),
        outcomes=(
            MarketOutcome(token_id="tianjin", outcome="Tianjin Jinmen Hu FC"),
            MarketOutcome(token_id="wuhan", outcome="Wuhan San Zhen FC"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="15552514",
        league="Chinese Super League",
        home=SportsLiveTeam(name="Tianjin Jinmen Tiger", score=0, short_name="Jinmen Tiger"),
        away=SportsLiveTeam(name="Wuhan Three Towns", score=0, short_name="Three Towns"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777635300,
            "tournament": "Chinese Super League",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "15552514"


def test_live_state_match_accepts_moroccan_soccer_name_variants() -> None:
    market = Market(
        condition_id="botola-dhj-zemamra-condition",
        market_slug="mar1-dhe-rcz-2026-04-30",
        market_question="Difaâ Hassani El Jadida vs. RCA Zemamra",
        event_title="Difaâ Hassani El Jadida vs. RCA Zemamra",
        event_slug="mar1-dhe-rcz-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 15, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Botola Pro"),
        outcomes=(
            MarketOutcome(token_id="dhj", outcome="Difaâ Hassani El Jadida"),
            MarketOutcome(token_id="rcz", outcome="RCA Zemamra"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16037146",
        league="Botola Pro",
        home=SportsLiveTeam(name="Difaâ Hassani El-Jadidi", score=0, short_name="DHJ"),
        away=SportsLiveTeam(name="Renaissance Zemamra", score=0, short_name="Renaissance Zemamra"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777561200,
            "tournament": "Botola Pro",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "16037146"


def test_live_state_match_ignores_soccer_club_prefix_variants() -> None:
    market = Market(
        condition_id="ukraine-poltava-kryvbas-condition",
        market_slug="ukr1-sp-kry-2026-05-01",
        market_question="SK Poltava vs. FK Kryvbas Kryvyi Rih",
        event_title="SK Poltava vs. FK Kryvbas Kryvyi Rih",
        event_slug="ukr1-sp-kry-2026-05-01",
        game_start_time=datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Ukraine Premier Liha"),
        outcomes=(
            MarketOutcome(token_id="poltava", outcome="SK Poltava"),
            MarketOutcome(token_id="kryvbas", outcome="FK Kryvbas Kryvyi Rih"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="14090463",
        league="Ukrainian Premier League",
        home=SportsLiveTeam(name="SC Poltava", score=0),
        away=SportsLiveTeam(name="FC Kryvbas Kryvyi Rih", score=0, short_name="Kryvbas"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777638600,
            "tournament": "Ukrainian Premier League",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "14090463"


def test_live_state_match_accepts_basketball_location_suffix_variants() -> None:
    market = Market(
        condition_id="germany-bbl-fraport-bonn-condition",
        market_slug="bkbbl-fra-tel-2026-05-01",
        market_question="Fraport Skyliners vs. Telekom Baskets Bonn",
        event_title="Fraport Skyliners vs. Telekom Baskets Bonn",
        event_slug="bkbbl-fra-tel-2026-05-01",
        game_start_time=datetime(2026, 5, 1, 14, 30, tzinfo=timezone.utc),
        category="Sports",
        tags=("Basketball", "Germany BBL"),
        outcomes=(
            MarketOutcome(token_id="fraport", outcome="Fraport Skyliners"),
            MarketOutcome(token_id="bonn", outcome="Telekom Baskets Bonn"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="14381848",
        league="Germany BBL",
        home=SportsLiveTeam(name="Fraport Skyliners Frankfurt", score=0, short_name="Frankfurt"),
        away=SportsLiveTeam(name="Telekom Baskets Bonn", score=0, short_name="Bonn"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "basketball",
            "start_timestamp": 1777645800,
            "tournament": "Germany BBL",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "14381848"


def test_live_state_match_accepts_russian_soccer_transliteration_variants() -> None:
    market = Market(
        condition_id="russian-premier-lokomotiv-dinamo-condition",
        market_slug="rus-lok-din-2026-05-01",
        market_question="FK Lokomotiv Moskva vs. FK Dinamo Moskva",
        event_title="FK Lokomotiv Moskva vs. FK Dinamo Moskva",
        event_slug="rus-lok-din-2026-05-01",
        game_start_time=datetime(2026, 5, 1, 16, 30, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Russian Premier League"),
        outcomes=(
            MarketOutcome(token_id="lokomotiv", outcome="FK Lokomotiv Moskva"),
            MarketOutcome(token_id="dinamo", outcome="FK Dinamo Moskva"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="14036734",
        league="Russian Premier League",
        home=SportsLiveTeam(name="Lokomotiv Moscow", score=0, short_name="Lokomotiv"),
        away=SportsLiveTeam(name="Dynamo Moscow", score=0, short_name="Dynamo"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777653000,
            "tournament": "Russian Premier League",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "14036734"


def test_live_state_match_ignores_latin_american_club_prefix_variants() -> None:
    market = Market(
        condition_id="guatemala-guastatoya-municipal-condition",
        market_slug="gtm-cdg-mun-2026-04-30",
        market_question="CD Guastatoya vs. CSD Municipal",
        event_title="CD Guastatoya vs. CSD Municipal",
        event_slug="gtm-cdg-mun-2026-04-30",
        game_start_time=datetime(2026, 5, 1, 1, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Soccer", "Guatemala Liga Nacional"),
        outcomes=(
            MarketOutcome(token_id="guastatoya", outcome="CD Guastatoya"),
            MarketOutcome(token_id="municipal", outcome="CSD Municipal"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16083435",
        league="Liga Nacional de Fútbol de Guatemala, Clausura",
        home=SportsLiveTeam(name="Deportivo Guastatoya", score=0, short_name="Dep. Guastatoya"),
        away=SportsLiveTeam(name="CSD Municipal", score=0, short_name="Municipal"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "football",
            "start_timestamp": 1777597200,
            "tournament": "Liga Nacional de Fútbol de Guatemala, Clausura",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "16083435"


def test_live_state_match_accepts_french_polynesia_table_tennis_alias() -> None:
    market = Market(
        condition_id="wtt-chile-french-polynesia-condition",
        market_slug="wttmen-chile-polynes-2026-04-30",
        market_question="WTT - Men's Singles: Chile vs French Polynesia",
        event_title="WTT - Men's Singles: Chile vs French Polynesia",
        event_slug="wttmen-chile-polynes-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 18, 30, tzinfo=timezone.utc),
        category="Sports",
        tags=("Table Tennis", "WTT"),
        outcomes=(
            MarketOutcome(token_id="chile", outcome="Chile"),
            MarketOutcome(token_id="polynesia", outcome="French Polynesia"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16094584",
        league="World Team Championships Finals",
        home=SportsLiveTeam(name="Chile", score=0),
        away=SportsLiveTeam(name="Tahiti", score=0),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "table-tennis",
            "start_timestamp": 1777573800,
            "tournament": "World Team Championships Finals",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.game.source_event_id == "16094584"


def test_best_live_state_match_reuses_market_text_across_many_games(monkeypatch) -> None:
    market = _market("nba-phi-bos-2026-04-28")
    games = tuple(
        _game(start_time_utc=f"2026-04-28T{hour:02d}:30:00Z")
        for hour in range(4)
    )
    calls = 0
    original_market_text = live_state_module._market_text

    def counted_market_text(candidate: Market) -> str:
        nonlocal calls
        calls += 1
        return original_market_text(candidate)

    monkeypatch.setattr(live_state_module, "_market_text", counted_market_text)

    match = live_state_module.best_live_match(market, games)

    assert match is not None
    assert calls == 1


def test_current_strategy_prefilters_live_games_before_text_matching(monkeypatch) -> None:
    market = Market(
        condition_id="mlb-market",
        market_slug="mlb-kc-oak-2026-04-30",
        market_question="Kansas City Royals vs. Athletics",
        event_title="Kansas City Royals vs. Athletics",
        event_slug="mlb-kc-oak-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 19, 5, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB", "Baseball"),
        outcomes=(
            MarketOutcome(token_id="kc", outcome="Kansas City Royals"),
            MarketOutcome(token_id="oak", outcome="Athletics"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    games = tuple(
        [
            SportsLiveGame(
                source="sofascore",
                source_event_id=f"basketball-{index}",
                league="Liga ACB",
                home=SportsLiveTeam(name=f"Home {index}", score=0),
                away=SportsLiveTeam(name=f"Away {index}", score=0),
                status=SportsLiveGameStatus.SCHEDULED,
                period="Not started",
                source_payload={
                    "sport": "basketball",
                    "start_timestamp": 1777566000,
                },
            )
            for index in range(50)
        ]
        + [
            SportsLiveGame(
                source="sofascore",
                source_event_id="baseball-match",
                league="MLB",
                home=SportsLiveTeam(name="Athletics", score=0),
                away=SportsLiveTeam(name="Kansas City Royals", score=0),
                status=SportsLiveGameStatus.SCHEDULED,
                period="Not started",
                source_payload={
                    "sport": "baseball",
                    "start_timestamp": 1777575900,
                },
            )
        ]
    )
    seen_game_count = 0

    def counted_match(candidate_market, candidate_games, **_kwargs):
        nonlocal seen_game_count
        seen_game_count = len(candidate_games)
        return None

    monkeypatch.setattr(strategy_module, "build_live_state_match", counted_match)

    CurrentStrategy(config=CurrentStrategyConfig()).match_live_state(market, games)

    assert seen_game_count == 1


def test_current_strategy_reuses_live_game_prefilter_for_same_event(monkeypatch) -> None:
    first_market = Market(
        condition_id="mlb-moneyline",
        market_slug="mlb-kc-oak-2026-04-30",
        market_question="Kansas City Royals vs. Athletics",
        event_title="Kansas City Royals vs. Athletics",
        event_slug="mlb-kc-oak-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 19, 5, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB", "Baseball"),
        outcomes=(
            MarketOutcome(token_id="kc", outcome="Kansas City Royals"),
            MarketOutcome(token_id="oak", outcome="Athletics"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    second_market = Market(
        condition_id="mlb-total",
        market_slug="mlb-kc-oak-2026-04-30-total-9pt5",
        market_question="Kansas City Royals vs. Athletics: O/U 9.5",
        event_title="Kansas City Royals vs. Athletics",
        event_slug="mlb-kc-oak-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 19, 5, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB", "Baseball"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    games = (
        SportsLiveGame(
            source="sofascore",
            source_event_id="baseball-match",
            league="MLB",
            home=SportsLiveTeam(name="Athletics", score=0),
            away=SportsLiveTeam(name="Kansas City Royals", score=0),
            status=SportsLiveGameStatus.SCHEDULED,
            period="Not started",
            source_payload={
                "sport": "baseball",
                "start_timestamp": 1777575900,
            },
        ),
    )
    start_parse_calls = 0
    original_game_start_time = strategy_module._game_start_time

    def counted_game_start_time(game):
        nonlocal start_parse_calls
        start_parse_calls += 1
        return original_game_start_time(game)

    monkeypatch.setattr(strategy_module, "_game_start_time", counted_game_start_time)
    monkeypatch.setattr(strategy_module, "build_live_state_match", lambda _market, _games, **_kwargs: None)
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    strategy.match_live_state(first_market, games)
    strategy.match_live_state(second_market, games)

    assert start_parse_calls == 1


def test_current_strategy_matches_wtt_table_tennis_live_state() -> None:
    market = Market(
        condition_id="wtt-condition",
        market_slug="wttmen-austria-italy-2026-04-30",
        market_question="WTT - Men's Singles: Austria vs Italy",
        event_title="WTT - Men's Singles: Austria vs Italy",
        event_slug="wttmen-austria-italy-2026-04-30",
        game_start_time=datetime(2026, 4, 30, 9, 0, tzinfo=timezone.utc),
        category="Sports",
        tags=("Table Tennis", "WTT"),
        outcomes=(
            MarketOutcome(token_id="austria", outcome="Austria"),
            MarketOutcome(token_id="italy", outcome="Italy"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16094559",
        league="World Team Championships Finals",
        home=SportsLiveTeam(name="Austria", score=0),
        away=SportsLiveTeam(name="Italy", score=0),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, 5, 0, tzinfo=timezone.utc),
        source_payload={
            "sport": "table-tennis",
            "start_timestamp": 1777539600,
        },
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.game.source_event_id == "16094559"
    assert match.payload["live_match"]["matched_home_alias"] == "Austria"
    assert match.payload["live_match"]["matched_away_alias"] == "Italy"
    assert match.signal_allowed is False
    assert match.signal_reason == "sports_live_state_scheduled"


def test_current_strategy_marks_far_live_match_as_non_entry_signal() -> None:
    market = _market("nba-phi-bos-2026-04-28").with_metadata(
        end_date=datetime(2026, 4, 28, 2, 1, tzinfo=timezone.utc)
    )
    game = _live_game()
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.market.condition_id == market.condition_id
    assert match.game.source_event_id == game.source_event_id
    assert match.signal_allowed is False
    assert match.signal_reason == "market_end_too_far"


def test_current_strategy_allows_far_mlb_signal_when_structured_tail_state_reached() -> None:
    market = Market(
        condition_id="mlb-tail-condition",
        market_slug="mlb-stl-pit-2026-04-29",
        market_question="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_title="St. Louis Cardinals vs. Pittsburgh Pirates",
        event_slug="mlb-stl-pit-2026-04-29",
        end_date=datetime(2026, 5, 6, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB", "Baseball"),
        outcomes=(
            MarketOutcome(token_id="stl", outcome="St. Louis Cardinals"),
            MarketOutcome(token_id="pit", outcome="Pittsburgh Pirates"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="mlb",
        source_event_id="823392",
        league="MLB",
        home=SportsLiveTeam(name="Pittsburgh Pirates", score=1),
        away=SportsLiveTeam(name="St. Louis Cardinals", score=5),
        status=SportsLiveGameStatus.LIVE,
        period="B9",
        observed_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        baseball_state=BaseballGameState(
            current_inning=9,
            inning_half="bottom",
            outs=2,
            offense_team="Pittsburgh Pirates",
            defense_team="St. Louis Cardinals",
            occupied_bases=(),
        ),
        source_payload={"start_time_utc": "2026-04-29T22:40:00Z"},
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.signal_allowed is True
    assert match.signal_reason == "live_tail_state_candidate"


def test_current_strategy_allows_far_tennis_set_winner_signal_after_set_completed() -> None:
    market = Market(
        condition_id="tennis-set-condition",
        market_slug="atp-tsitsip-ruud-2026-04-28-first-set-winner-Tsitsipas-vs-Ruud",
        market_question="First set winner: Tsitsipas vs Ruud",
        event_title="Stefanos Tsitsipas vs Casper Ruud",
        event_slug="atp-tsitsip-ruud-2026-04-28",
        end_date=datetime(2026, 5, 5, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="tsitsipas", outcome="Tsitsipas"),
            MarketOutcome(token_id="ruud", outcome="Ruud"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16013091",
        league="ATP Madrid Masters",
        home=SportsLiveTeam(name="Stefanos Tsitsipas", score=1),
        away=SportsLiveTeam(name="Casper Ruud", score=0),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=4,
            away_current_set_games=4,
            home_total_games=11,
            away_total_games=10,
            set_scores=((7, 6), (4, 4)),
        ),
        source_payload={
            "sport": "tennis",
            "start_time_utc": "2026-04-28T00:00:00Z",
        },
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.signal_allowed is True
    assert match.signal_reason == "live_outcome_lock_candidate"


def test_current_strategy_allows_far_tennis_match_total_when_minimum_final_games_crosses_line() -> None:
    market = Market(
        condition_id="tennis-total-condition",
        market_slug="atp-erhard-nedic-2026-04-29-match-total-23pt5",
        market_question="Mathys Erhard vs Andrej Nedic match total 23.5",
        event_title="Shymkent 2: Mathys Erhard vs Andrej Nedic",
        event_slug="atp-erhard-nedic-2026-04-29",
        end_date=datetime(2026, 5, 6, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16050302",
        league="Shymkent 2, Kazakhstan",
        home=SportsLiveTeam(name="Mathys Erhard", score=1),
        away=SportsLiveTeam(name="Andrej Nedic", score=1),
        status=SportsLiveGameStatus.LIVE,
        period="S3",
        observed_at=datetime(2026, 4, 29, 8, 25, tzinfo=timezone.utc),
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=1,
            current_set=3,
            home_current_set_games=0,
            away_current_set_games=1,
            home_total_games=10,
            away_total_games=9,
            set_scores=((4, 6), (6, 2), (0, 1)),
        ),
        source_payload={
            "sport": "tennis",
            "start_time_utc": "2026-04-29T07:00:00Z",
        },
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.signal_allowed is True
    assert match.signal_reason == "live_outcome_lock_candidate"


def test_current_strategy_allows_far_tennis_moneyline_signal_when_tail_state_reached() -> None:
    market = Market(
        condition_id="tennis-moneyline-condition",
        market_slug="atp-omarkha-yevseye-2026-04-28",
        market_question="Amir Omarkhanov vs Denis Yevseyev",
        event_title="Amir Omarkhanov vs Denis Yevseyev",
        event_slug="atp-omarkha-yevseye-2026-04-28",
        end_date=datetime(2026, 5, 5, tzinfo=timezone.utc),
        category="Sports",
        tags=("ATP", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="home", outcome="Amir Omarkhanov"),
            MarketOutcome(token_id="away", outcome="Denis Yevseyev"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16013092",
        league="ATP Challenger",
        home=SportsLiveTeam(name="Amir Omarkhanov", score=0),
        away=SportsLiveTeam(name="Denis Yevseyev", score=0),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        tennis_state=TennisGameState(
            home_sets_won=1,
            away_sets_won=0,
            current_set=2,
            home_current_set_games=5,
            away_current_set_games=3,
            home_total_games=11,
            away_total_games=6,
            set_scores=((6, 3), (5, 3)),
        ),
        source_payload={
            "sport": "tennis",
            "start_time_utc": "2026-04-28T00:00:00Z",
        },
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is not None
    assert match.signal_allowed is True
    assert match.signal_reason == "live_tail_state_candidate"


def test_current_strategy_does_not_apply_single_game_live_source_to_series_market() -> None:
    market = Market(
        condition_id="series-condition",
        market_slug="nba-playoffs-who-will-win-series-suns-vs-thunder",
        market_question="NBA Playoffs: Who Will Win Series? - Suns vs. Thunder",
        event_title="NBA Playoffs: Who Will Win Series? - Suns vs. Thunder",
        event_slug="nba-playoffs-who-will-win-series-suns-vs-thunder",
        category="Sports",
        tags=("NBA", "2026 NBA Playoffs"),
        outcomes=(
            MarketOutcome(token_id="suns", outcome="Suns"),
            MarketOutcome(token_id="thunder", outcome="Thunder"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="nba",
        source_event_id="0042500144",
        league="NBA",
        home=SportsLiveTeam(name="Suns", score=122, display_name="Phoenix Suns"),
        away=SportsLiveTeam(name="Thunder", score=131, display_name="Oklahoma City Thunder"),
        status=SportsLiveGameStatus.ENDED,
        period="Final",
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is None


def test_current_strategy_does_not_apply_single_game_live_source_to_league_winner_market() -> None:
    market = Market(
        condition_id="league-winner-condition",
        market_slug="2026-basketball-japan-b-league-winner-akita-northern-happinets",
        market_question="Will Akita Northern Happinets win Japan B League?",
        event_title="Japan B League: Winner",
        event_slug="2026-basketball-japan-b-league-winner",
        category="Sports",
        tags=("Sports", "Japan B League", "Basketball"),
        outcomes=(
            MarketOutcome(token_id="yes", outcome="Yes"),
            MarketOutcome(token_id="no", outcome="No"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16093356",
        league="ITF M25 Santa Margherita di Pula 5 Men Doubles",
        home=SportsLiveTeam(name="Massucco S / Mencaglia S", score=0),
        away=SportsLiveTeam(name="Cocola S / Rahmani K", score=0),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
    )
    strategy = CurrentStrategy(config=CurrentStrategyConfig())

    match = strategy.match_live_state(market, (game,))

    assert match is None


def test_live_state_match_rejects_same_teams_on_different_start_times() -> None:
    market = Market(
        condition_id="mlb-market",
        market_slug="mlb-mia-lad-2026-04-28",
        event_title="Miami Marlins vs. Los Angeles Dodgers",
        event_slug="mlb-mia-lad-2026-04-28",
        game_start_time=datetime(2026, 4, 29, 2, 10, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="mia", outcome="Miami Marlins"),
            MarketOutcome(token_id="lad", outcome="Los Angeles Dodgers"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    previous_game = SportsLiveGame(
        source="sofascore",
        source_event_id="15508565",
        league="MLB",
        home=SportsLiveTeam(name="Los Angeles Dodgers", score=5, abbreviation="LAD"),
        away=SportsLiveTeam(name="Miami Marlins", score=4, abbreviation="MIA"),
        status=SportsLiveGameStatus.ENDED,
        period="Ended",
        observed_at=datetime(2026, 4, 28, 11, 4, tzinfo=timezone.utc),
        raw_status="Ended",
        source_payload={
            "sport": "baseball",
            "start_time_utc": "2026-04-28T02:10:00Z",
        },
    )

    assert match_live_game(market, previous_game) is None


def test_live_state_match_accepts_same_teams_on_matching_start_times() -> None:
    market = Market(
        condition_id="mlb-market",
        market_slug="mlb-mia-lad-2026-04-28",
        event_title="Miami Marlins vs. Los Angeles Dodgers",
        event_slug="mlb-mia-lad-2026-04-28",
        game_start_time=datetime(2026, 4, 29, 2, 10, tzinfo=timezone.utc),
        category="Sports",
        tags=("MLB",),
        outcomes=(
            MarketOutcome(token_id="mia", outcome="Miami Marlins"),
            MarketOutcome(token_id="lad", outcome="Los Angeles Dodgers"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    scheduled_game = SportsLiveGame(
        source="sofascore",
        source_event_id="upcoming",
        league="MLB",
        home=SportsLiveTeam(name="Los Angeles Dodgers", score=0, abbreviation="LAD"),
        away=SportsLiveTeam(name="Miami Marlins", score=0, abbreviation="MIA"),
        status=SportsLiveGameStatus.SCHEDULED,
        period="Not started",
        observed_at=datetime(2026, 4, 28, 11, 4, tzinfo=timezone.utc),
        raw_status="Not started",
        source_payload={
            "sport": "baseball",
            "start_time_utc": "2026-04-29T02:10:00Z",
        },
    )

    assert match_live_game(market, scheduled_game) is not None


def test_live_state_metadata_preserves_tennis_state() -> None:
    market = Market(
        condition_id="tennis-condition",
        market_slug="wta-zolotar-papamic-2026-04-28-total-21pt5",
        market_question="Rada Zolotareva vs Despina Papamichail total games 21.5",
        event_title="Rada Zolotareva vs Despina Papamichail",
        event_slug="wta-zolotar-papamic-2026-04-28",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16078142",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0, short_name="R. Zolotareva"),
        away=SportsLiveTeam(name="Despina Papamichail", score=1, short_name="D. Papamichail"),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, 7, 0, tzinfo=timezone.utc),
        tennis_state=TennisGameState(
            home_sets_won=0,
            away_sets_won=1,
            current_set=2,
            home_current_set_games=0,
            away_current_set_games=0,
            home_total_games=4,
            away_total_games=6,
            first_to_serve="away",
            serving_side="away",
        ),
        source_payload={
            "start_time_utc": "2026-04-28T06:25:00Z",
        },
    )

    match = match_live_game(market, game)

    assert match is not None
    assert match.metadata()["live_game"]["tennis_state"]["total_games"] == 10
    assert match.metadata()["live_game"]["tennis_state"]["serving_side"] == "away"


def test_live_state_match_allows_tennis_adjacent_utc_date() -> None:
    market = Market(
        condition_id="tennis-condition",
        market_slug="wta-zolotar-papamic-2026-04-27-total-21pt5",
        market_question="Rada Zolotareva vs Despina Papamichail total games 21.5",
        event_title="Rada Zolotareva vs Despina Papamichail",
        event_slug="wta-zolotar-papamic-2026-04-27",
        category="Sports",
        tags=("WTA", "Tennis"),
        outcomes=(
            MarketOutcome(token_id="over", outcome="Over"),
            MarketOutcome(token_id="under", outcome="Under"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )
    game = SportsLiveGame(
        source="sofascore",
        source_event_id="16078142",
        league="WTA 125K Huzhou, China Women Singles",
        home=SportsLiveTeam(name="Rada Zolotareva", score=0, short_name="R. Zolotareva"),
        away=SportsLiveTeam(name="Despina Papamichail", score=1, short_name="D. Papamichail"),
        status=SportsLiveGameStatus.LIVE,
        period="S2",
        observed_at=datetime(2026, 4, 28, 7, 0, tzinfo=timezone.utc),
        tennis_state=TennisGameState(home_total_games=4, away_total_games=6),
        source_payload={
            "sport": "tennis",
            "start_time_utc": "2026-04-28T06:25:00Z",
        },
    )

    assert match_live_game(market, game) is not None


def _market(slug: str) -> Market:
    return Market(
        condition_id=f"{slug}-condition",
        market_slug=slug,
        market_question="76ers vs Celtics",
        event_title="76ers vs Celtics",
        event_slug=slug,
        category="Sports",
        tags=("NBA",),
        outcomes=(
            MarketOutcome(token_id="home", outcome="76ers"),
            MarketOutcome(token_id="away", outcome="Celtics"),
        ),
        trading_status=TradingStatus.ELIGIBLE,
    )


def _game(*, start_time_utc: str) -> SportsLiveGame:
    return SportsLiveGame(
        source="espn",
        source_event_id="401869408",
        league="NBA",
        home=SportsLiveTeam(
            name="Celtics",
            score=0,
            display_name="Boston Celtics",
            abbreviation="BOS",
            short_name="Celtics",
        ),
        away=SportsLiveTeam(
            name="76ers",
            score=0,
            display_name="Philadelphia 76ers",
            abbreviation="PHI",
            short_name="76ers",
        ),
        status=SportsLiveGameStatus.SCHEDULED,
        period="STATUS_SCHEDULED",
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        source_payload={"start_time_utc": start_time_utc},
    )


def _live_game() -> SportsLiveGame:
    return SportsLiveGame(
        source="espn",
        source_event_id="401869408",
        league="NBA",
        home=SportsLiveTeam(
            name="Celtics",
            score=96,
            display_name="Boston Celtics",
            abbreviation="BOS",
            short_name="Celtics",
        ),
        away=SportsLiveTeam(
            name="76ers",
            score=94,
            display_name="Philadelphia 76ers",
            abbreviation="PHI",
            short_name="76ers",
        ),
        status=SportsLiveGameStatus.LIVE,
        period="Q4",
        seconds_remaining=600,
        observed_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        source_payload={"start_time_utc": "2026-04-28T00:00:00Z"},
    )
