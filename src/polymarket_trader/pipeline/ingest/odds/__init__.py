"""【层 1 ingest】odds —— Goalserve / TheOddsAPI 赛前 + 赛季赔率拉取。

原架构方案 §2 主工作流图层 1 ④。三个 worker 各自周期拉权威赔率，
写入 `MarketMetadataStore` 的对应字段，策略 / risk / aggregator 后续读取。

# 与直播源 live_source 区别

- `live_source/` 是**比赛进行中**的实时数据 + match/calibrate 链路（push-like cadence）
- `odds/` 是**赛前**或**赛季**赔率（pull cadence，分钟级）

两者数据流独立，writer 都是 MarketMetadataStore 但字段不重叠。

# 模块

- `sports_season_odds_worker` —— TheOddsAPI 赛季赔率
- `game_odds_worker` —— TheOddsAPI 单场赔率
- `goalserve_pregame_worker` —— Goalserve pregame GZIP feed
"""

from .game_odds_worker import GameOddsWorker
from .goalserve_pregame_worker import GoalservePregameWorker
from .sports_season_odds_worker import SportsSeasonOddsWorker

__all__ = ["GameOddsWorker", "GoalservePregameWorker", "SportsSeasonOddsWorker"]
