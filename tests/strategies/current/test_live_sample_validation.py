from __future__ import annotations

from pathlib import Path

from strategies.current.live_sample_validation import validate_sports_live_sample_file


def test_live_sample_validation_checks_espn_fields_and_market_match() -> None:
    fixture_path = Path("tests/fixtures/sports_live/espn_nba_knicks_celtics_live.json")

    report = validate_sports_live_sample_file(str(fixture_path))

    assert report.passed is True
    assert report.games_seen == 2
    assert report.markets_checked == 1
    assert report.matches[0]["source_event_id"] == "401705460"
    assert report.matches[0]["matched_home_alias"] == "New York Knicks"
