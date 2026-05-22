# sports infra

本目录放外部体育数据源适配器。适配器只负责访问协议、错误归一化和 payload -> 内部 DTO 转换。

## 客户端

- `goalserve_inplay_client.py` / `goalserve_inplay_parsers.py`：Goalserve inplay
  实时赔率+比分。数据源 `http://inplay.goalserve.com/inplay-{sport}.gz`——keyless
  （IP 白名单，经 `GOALSERVE_PROXY` 出口），gunzip 后为完整 JSON 快照，服务端
  每 ~1 秒刷新。每个 sport 维护独立后台轮询 Task（同 sport ~1 请求/秒硬限流，
  超速 HTTP 429 → 该 sport 单独退避）；支持 demand-driven 轮询。覆盖 soccer/
  basket/tennis/volleyball/amfootball/esports/hockey/baseball 共 8 个运动。
- `goalserve_livescore_client.py` / `goalserve_livescore_parsers.py`：Goalserve
  getfeed livescore，API key 认证，覆盖 inplay feed 没有的运动。
- `goalserve_pregame_client.py`：Goalserve 赛前赔率（GZIP，数据量极大，默认关闭）。
- `aggregate_client.py`：`SportsLiveAggregateClient` 聚合上述源并向上暴露统一快照。

约束：

- 不在这里判断盘口是否可交易、是否扫尾或是否需要人工确认。
- 不把外部 payload 直接传给 app / worker / strategy；必须先转成 `domain.sports_live` 内部对象。
- 多个源先由 `SportsLiveAggregateClient` 聚合、去重和降级，向上仍只暴露 `SportsLiveSnapshot`。
- 免费通用源可以作为补充 provider 接入，但必须先转成内部 DTO；例如 SofaScore 和 TheSportsDB 只按配置联赛映射到必要 sport path，并由聚合器处理同场去重和来源优先级。
- 对有明显免费限流风险的源，适配器需要内置本地限频或缓存；不能让全局 5 秒同步频率直接等价为对每个免费源的 5 秒外部请求。
- 不持有交易热状态写锁，不访问 Polymarket 下单客户端。
- 外部失败需要归一成可审计错误，不能静默吞掉。
