from __future__ import annotations

from strategies.current._shared.team_normalize import normalize_team_name


def test_normalize_handles_none_and_empty() -> None:
    assert normalize_team_name(None) == ""
    assert normalize_team_name("") == ""
    assert normalize_team_name("   ") == ""


def test_normalize_lowercases_and_compresses_whitespace() -> None:
    assert normalize_team_name("  Boston   Celtics  ") == "boston celtics"


def test_normalize_replaces_ampersand_with_and() -> None:
    assert normalize_team_name("Texas A&M") == "texas a and m"


def test_normalize_strips_periods_so_la_lakers_equivalent() -> None:
    assert normalize_team_name("L.A. Lakers") == normalize_team_name("LA Lakers")


def test_normalize_strips_punctuation_from_questions() -> None:
    assert normalize_team_name("Will Celtics win, really?") == "will celtics win really"


def test_normalize_strips_apostrophes() -> None:
    # 撇号去掉而不是替换为空格，避免 "L'Equipe" 变成 "l equipe" 后失配
    assert normalize_team_name("L'Equipe") == "lequipe"
