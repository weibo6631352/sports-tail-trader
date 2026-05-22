"""联赛识别工具：判定 LiveGameState 属于哪个体育大类。"""

from __future__ import annotations

from .types import LiveGameState


def is_mlb_game(game: LiveGameState) -> bool:
    """识别棒球类赛事（Goalserve inplay sport=baseball 或联赛名含 mlb/kbo/baseball）。

    livescore 接口返回的 league 是 "USA: MLB" 等复合形式，不能仅靠精确匹配。
    sport 字段由 Goalserve inplay / livescore parser 注入，是更可靠的分类依据。
    """
    sport = game.sport.strip().lower()
    if sport == "baseball":
        return True
    league = game.league.strip().lower()
    return (
        "mlb" in league
        or "kbo" in league
        or league in {"baseball", "korean baseball", "korea baseball organization"}
    )


def is_nfl_game(game: LiveGameState) -> bool:
    """识别美式橄榄球（Goalserve sport=american-football 或联赛名含 nfl）。"""

    sport = game.sport.strip().lower()
    if sport == "american-football":
        return True
    league = game.league.strip().lower()
    return "nfl" in league or league == "american football"


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
