from __future__ import annotations

from pathlib import Path

import pytest

from strategies.current.live_sample_validation import validate_sports_live_sample_file


@pytest.mark.parametrize(
    "fixture_path",
    sorted(Path("tests/fixtures/sports_live").glob("*.json")),
    ids=lambda path: path.name,
)
def test_live_sample_validation_checks_espn_fields_and_market_match(fixture_path: Path) -> None:
    """所有真实直播样本都必须通过字段归一和 market 匹配校验。"""

    report = validate_sports_live_sample_file(str(fixture_path))

    assert report.passed is True
    assert report.events_seen >= 1
    assert report.markets_checked >= 1
    assert len(report.matches) >= 1
