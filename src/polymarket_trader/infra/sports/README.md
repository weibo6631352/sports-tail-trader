# sports infra

本目录放外部体育数据源适配器。适配器只负责访问协议、错误归一化和 payload -> 内部 DTO 转换。

约束：

- 不在这里判断盘口是否可交易、是否扫尾或是否需要人工确认。
- 不把外部 payload 直接传给 app / worker / strategy；必须先转成 `domain.sports_live` 内部对象。
- 多个源先由 `SportsLiveAggregateClient` 聚合、去重和降级，向上仍只暴露 `SportsLiveSnapshot`。
- 免费通用源可以作为补充 provider 接入，但必须先转成内部 DTO；例如 SofaScore 和 TheSportsDB 只按配置联赛映射到必要 sport path，并由聚合器处理同场去重和来源优先级。
- 对有明显免费限流风险的源，适配器需要内置本地限频或缓存；不能让全局 5 秒同步频率直接等价为对每个免费源的 5 秒外部请求。
- 不持有交易热状态写锁，不访问 Polymarket 下单客户端。
- 外部失败需要归一成可审计错误，不能静默吞掉。
