"""联赛识别工具：判定 LiveGameState 属于哪个体育大类。"""

from __future__ import annotations

from .types import LiveGameState


def is_mlb_game(game: LiveGameState) -> bool:
    """识别 MLB / KBO 等棒球类联赛。"""

    league = game.league.strip().lower()
    return league in {"mlb", "kbo", "baseball", "korean baseball", "korea baseball organization"}


def is_nfl_game(game: LiveGameState) -> bool:
    """识别 NFL / 美式橄榄球。"""

    return game.league.strip().lower() in {"nfl", "american football"}


def is_tennis_game(game: LiveGameState) -> bool:
    """识别网球类联赛（ATP / WTA / 通用 tennis 字样或带 tennis_state）。"""

    league = game.league.strip().lower()
    return game.tennis_state is not None or "tennis" in league or league in {"atp", "wta"}


def is_soccer_game(game: LiveGameState) -> bool:
    """识别足球类赛事（sport=soccer 或 soccer_state 存在）。"""

    return game.sport.strip().lower() == "soccer" or game.soccer_state is not None


def is_hockey_game(game: LiveGameState) -> bool:
    """识别冰球类赛事（sport=ice-hockey）。"""

    return game.sport.strip().lower() in {"ice-hockey", "hockey"}
