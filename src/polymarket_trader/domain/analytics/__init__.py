"""【domain/analytics】纯报表算法——纯函数 + DTO，不依赖运行时。

按 docs/新架构方案.md §5 + CLAUDE.md §3：domain 是纯业务规则。

# 模块

- `calibration` —— Brier score / log-loss / 校准桶
- `edge_realization` —— 预测 edge vs 实际回报分桶
- `missed_opportunities` —— 拒绝决策事后盈利模拟
- `parameter_sweep` —— 历史决策参数 grid 回放
- `pnl_breakdown` —— 按维度（market_slug/sport/category）分组 PnL
- `risk_metrics` —— Sharpe / Sortino / max drawdown
- `trade_replay` —— 成交 + 持仓 + 审计聚合复盘视图
- `trade_timeline` —— 单市场完整时间线 build_trade_timeline
- `portfolio_history_service` —— 权益曲线 downsampling

调用方主要是 `api/aggregators/{analytics,timeline,portfolio,...}_aggregator`，
本目录内只做"输入 records / 输出 dict"，不持任何状态、不读 DB / runtime。
"""
