"""【层 3：执行】pipeline/execution/ —— OrderGateway 强制门禁。

原架构方案 §2 主工作流图层 3。OrderGateway.review_intent 是任何下单
入口的唯一通道——含 RiskManager 4 道闸（market_state / bankroll_total /
balance+allowance / buy_order_type，CLAUDE.md §3）。

# 数据流

```
TradingDecision (来自层 2 决策响应)
    ↓
OrderGateway.review_intent(intent)
    ├─ RiskManager 4 道闸
    │   失败 → 拒绝 + 落 audit
    │   通过 ↓
    └─ PolymarketOrderExecutor (infra/polymarket/) → 签名 + HTTPS POST → CLOB
```

# 硬约束（CLAUDE.md §3）

任何新下单入口都必须经 OrderGateway.review_intent。recovery 路径（修复僵尸
订单、补漏 SELL、降级 pause 等）的 ReconcileActionApplier 也必经此路径，
不允许直连 order_executor。
"""

from .order_gateway import OrderGateway, OrderGatewayReview

__all__ = ["OrderGateway", "OrderGatewayReview"]
