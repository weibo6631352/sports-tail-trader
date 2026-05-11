"""沙箱内存账本：模拟成交后的持仓 / 可用余额 / 累计 fee。

只服务沙箱（``app/paper`` + ``app/virtual_paper_trading``），不写库、不参与
任何真实交易决策；与真实 ``AccountStateStore`` 完全独立。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


_ZERO = Decimal("0")


@dataclass(slots=True)
class PaperVirtualLedger:
    """沙箱虚拟账本。所有金额按 net（扣 fee 后）记账。

    ``positions`` / ``cost_basis_usdc`` 同步维护：BUY 时累加 net 份额与 gross 花费，
    SELL 时按比例释放对应成本基础（避免 cost 平均价漂移）。``cost_basis_usdc[token]``
    用于同步给 ``AccountStateStore`` 的 ``Position.cost_usdc``，以驱动 worker 的
    exit overlay / scale-in 决策。
    """

    available_usdc: Decimal = _ZERO
    positions: dict[str, Decimal] = field(default_factory=dict)
    cost_basis_usdc: dict[str, Decimal] = field(default_factory=dict)
    fees_accrued_usdc: Decimal = _ZERO

    def fund(self, amount_usdc: Decimal) -> None:
        """初始化或追加可用余额。"""

        self.available_usdc += amount_usdc

    def apply_buy_fill(
        self,
        *,
        token_id: str,
        gross_spent_usdc: Decimal,
        gross_filled_shares: Decimal,
        fee_usdc: Decimal,
        fee_shares: Decimal,
    ) -> None:
        """记一次 BUY 成交：花掉 USDC（含 fee），仓位增加 net 份额。

        Polymarket BUY 的 fee 在 share 维度扣减（fee_shares），因此真实落地的
        持仓 = gross_filled_shares - fee_shares；账户花的 USDC = gross_spent。
        累计 fee 以 USDC 维度统一记录便于汇总。``cost_basis_usdc`` 累加 gross_spent
        作为该仓位的总成本，便于 ``avg_price`` 与 PnL 推算。
        """

        self.available_usdc -= gross_spent_usdc
        net_shares = gross_filled_shares - fee_shares
        if net_shares < _ZERO:
            net_shares = _ZERO
        if net_shares > _ZERO:
            self.positions[token_id] = self.positions.get(token_id, _ZERO) + net_shares
            self.cost_basis_usdc[token_id] = self.cost_basis_usdc.get(token_id, _ZERO) + gross_spent_usdc
        self.fees_accrued_usdc += fee_usdc

    def apply_sell_fill(
        self,
        *,
        token_id: str,
        gross_received_usdc: Decimal,
        gross_filled_shares: Decimal,
        fee_usdc: Decimal,
    ) -> None:
        """记一次 SELL 成交：仓位减少 gross 份额，账户收入 net USDC（扣 fee）。

        cost_basis 按比例释放：卖掉的份额占当前持仓的比例 × 当前 cost_basis。
        持仓清空时把残留 cost_basis 一并清除。
        """

        current = self.positions.get(token_id, _ZERO)
        if current > _ZERO and gross_filled_shares > _ZERO:
            current_cost = self.cost_basis_usdc.get(token_id, _ZERO)
            consumed_ratio = min(gross_filled_shares / current, Decimal("1"))
            cost_released = current_cost * consumed_ratio
            remaining_cost = current_cost - cost_released
            if remaining_cost <= _ZERO:
                self.cost_basis_usdc.pop(token_id, None)
            else:
                self.cost_basis_usdc[token_id] = remaining_cost
        new_position = current - gross_filled_shares
        if new_position <= _ZERO:
            self.positions.pop(token_id, None)
            self.cost_basis_usdc.pop(token_id, None)
        else:
            self.positions[token_id] = new_position
        self.available_usdc += gross_received_usdc - fee_usdc
        self.fees_accrued_usdc += fee_usdc

    def position_for(self, token_id: str) -> Decimal:
        return self.positions.get(token_id, _ZERO)

    def cost_for(self, token_id: str) -> Decimal:
        return self.cost_basis_usdc.get(token_id, _ZERO)
