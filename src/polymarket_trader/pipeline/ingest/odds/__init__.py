"""【层 1 ingest】odds —— Goalserve 赛前赔率拉取。

# 与直播源 live_source 区别

- `live_source/` 是**比赛进行中**的实时数据 + match/calibrate 链路（push-like cadence）
- `odds/` 是**赛前**赔率（pull cadence，分钟级）

# 模块

- `goalserve_pregame_worker` —— Goalserve pregame GZIP feed
"""

from .goalserve_pregame_worker import GoalservePregameWorker

__all__ = ["GoalservePregameWorker"]
