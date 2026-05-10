"""联赛识别工具：判定 LiveGameState 属于哪个体育大类。"""

from __future__ import annotations

from .types import LiveGameState


def _is_mlb_game(game: LiveGameState) -> bool:
    league = game.league.strip().lower()
    return league in {"mlb", "kbo", "baseball", "korean baseball", "korea baseball organization"}


def _is_nfl_game(game: LiveGameState) -> bool:
    return game.league.strip().lower() in {"nfl", "american football"}


def _is_tennis_game(game: LiveGameState) -> bool:
    league = game.league.strip().lower()
    return game.tennis_state is not None or "tennis" in league or league in {"atp", "wta"}
