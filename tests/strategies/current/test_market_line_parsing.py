"""盘口线 slug 解析回归测试。

历史 bug：``totals`` 复数与 ``handicap-home-1`` 这类标记词后夹 home/away 的 slug
匹配落空后退化到通用兜底，把 slug 里的年份（如 2026）误当成盘口线。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from strategies.current.outcomes import _market_line, _normalize_text


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("wta-sramkov-carle-2026-05-22-set-totals-2pt5", Decimal("2.5")),
        ("wta-sramkov-carle-2026-05-22-set-handicap-home-1", Decimal("1")),
        ("wta-sramkov-carle-2026-05-22-set-handicap-away-2", Decimal("2")),
        ("wta-sramkov-carle-2026-05-22-match-total-21pt5", Decimal("21.5")),
        ("mlb-atl-mia-2026-05-21-total-5pt5", Decimal("5.5")),
        ("wta-x-y-2026-05-22-first-set-total-9pt5", Decimal("9.5")),
        ("nba-det-cle-2026-05-11-spread-minus-7pt5", Decimal("-7.5")),
        ("nba-x-y-2026-05-11-1h-total-109pt5", Decimal("109.5")),
    ],
)
def test_market_line_parses_slug(slug: str, expected: Decimal) -> None:
    assert _market_line(_normalize_text(slug)) == expected


def test_market_line_does_not_grab_year() -> None:
    # 仅含日期、无盘口线标记时必须返回 None，不能把年份当成线。
    assert _market_line(_normalize_text("wta-sramkov-carle-2026-05-22")) is None
