# app 测试目录说明

该目录存放应用服务和流程编排测试。这里验证多个 domain / infra / runtime 边界对象如何协作，但仍然应该优先使用 fake adapter，避免真实外部依赖。

## 覆盖范围

- Market discovery 到 classification 到 registry 的流程。
- `EntryPriceTouched` 到 allocation、risk、BuyOrderIntent 的流程。
- FAK full fill 后补挂 GTC SELL。
- FAK partial fill 只对成交 shares 挂 SELL，并释放未成交资金。
- FAK no fill 释放全部预算并触发再分配。
- Reconcile 发现 open BUY 后生成 cancel 修复动作。
- Reconcile 发现持仓和 open SELL 不一致后的补挂或取消。
- Admin cancel + replace 的顺序和风控约束。

## 必须保持的边界

- app service 可以使用 fake infra adapter，但不使用真实 Polymarket API。
- 订单动作必须验证经过 Risk Manager。
- 审计事件和 trace id 需要进入断言。
- 不把 HTTP request 对象传入 app service。
- 不在编排测试中依赖真实数据库，除非测试目标就是仓储集成。

## 输入与输出

- 输入：domain 事件、fake adapter 返回值、应用服务参数和运行时快照。
- 输出：订单意图、修复动作、审计事件调用以及编排结果断言。
