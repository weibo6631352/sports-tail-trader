# sports infra

本目录放外部体育数据源适配器。适配器只负责访问协议、错误归一化和 payload -> 内部 DTO 转换。

约束：

- 不在这里判断盘口是否可交易、是否扫尾或是否需要人工确认。
- 不把外部 payload 直接传给 app / worker / strategy；必须先转成 `domain.sports_live` 内部对象。
- 不持有交易热状态写锁，不访问 Polymarket 下单客户端。
- 外部失败需要归一成可审计错误，不能静默吞掉。
