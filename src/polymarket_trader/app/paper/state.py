"""沙箱内存账本：模拟成交后的持仓 / 可用余额 / 累计 fee。

只服务沙箱（``app/paper`` + ``app/virtual_paper_trading``），不写库、不参与
任何真实交易决策；与真实 ``AccountStateStore`` 完全独立。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime as datetime_t, timezone
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
    # 量化指标（不参与决策）：per-token 首次 BUY 时间 + 持有期内观测到的最深浮亏
    # （unrealized PnL 最小值）。SELL 平仓时把这两个写入已实现交易记录供复盘。
    first_fill_at: dict[str, datetime_t] = field(default_factory=dict)
    max_unrealized_loss: dict[str, Decimal] = field(default_factory=dict)
    realized_trades: list[dict] = field(default_factory=list)
    # 价格时序：每个持仓的 best_bid 采样轨迹 [(timestamp_iso, best_bid)]。
    # 用于事后分析"入场后价格如何走"——计算预测信号有效性的核心数据。
    # 限制每个 token 最多 120 个采样点（约 60 分钟 × 30s 间隔）。
    position_price_history: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    _last_price_sample_at: dict[str, datetime_t] = field(default_factory=dict)
    # 资金曲线时间序列，由 runtime.paper_background_tasks.equity_curve_recorder 每 30s 追加。
    # 上限由 recorder 控制（默认 2880 ≈ 24h）。供 operator /runtime/equity-curve 端点画图。
    equity_curve: list[dict] = field(default_factory=list)

    def fund(self, amount_usdc: Decimal) -> None:
        """初始化或追加可用余额。"""

        self.available_usdc += amount_usdc

    def observe_unrealized(self, token_id: str, current_best_bid: Decimal) -> None:
        """每次 orderbook 更新时调用，跟踪该持仓的最深浮亏 + 价格时序。

        workflow 层不该调（这是统计指标），由 paper_balance_syncer 后台调用。
        - 最深浮亏：每次更新 max_unrealized_loss（取 min）
        - 价格时序：30s 采样一次 best_bid（控制内存 / 频率）
        """
        shares = self.positions.get(token_id, _ZERO)
        cost = self.cost_basis_usdc.get(token_id, _ZERO)
        if shares <= _ZERO:
            return
        unrealized = shares * current_best_bid - cost
        prev = self.max_unrealized_loss.get(token_id, Decimal("999999"))
        if unrealized < prev:
            self.max_unrealized_loss[token_id] = unrealized
        # 30s 采样一次 best_bid（用于事后分析价格走势）
        now = datetime_t.now(timezone.utc)
        last = self._last_price_sample_at.get(token_id)
        # 5s 一次采样（追求最实时 CLV 信号），保留 240 点 = 20min 时序
        if last is None or (now - last).total_seconds() >= 5:
            self._last_price_sample_at[token_id] = now
            history = self.position_price_history.setdefault(token_id, [])
            history.append((now.isoformat(), str(current_best_bid)))
            if len(history) > 240:
                self.position_price_history[token_id] = history[-240:]

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
            # 首次 BUY 记录入场时间（同 token 加仓不覆盖）；最深浮亏初始化为 0
            if token_id not in self.first_fill_at:
                self.first_fill_at[token_id] = datetime_t.now(timezone.utc)
                self.max_unrealized_loss[token_id] = _ZERO
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

        # 防卖空 + 部分超额保护：实盘 SELL 超过持仓直接被 Polymarket reject，
        # paper 必须对齐。旧实现没持仓也 += available_usdc → 卖空 bug：
        # available 凭空涨（实测 100 → 312）。
        current = self.positions.get(token_id, _ZERO)
        if current <= _ZERO or gross_filled_shares <= _ZERO:
            return  # 无持仓 → 撮合失败，不动账本
        # 实际成交份额 = min(请求份额, 当前持仓)；超额部分按比例 prorate
        actual_filled = min(gross_filled_shares, current)
        ratio = actual_filled / gross_filled_shares
        actual_received = gross_received_usdc * ratio
        actual_fee = fee_usdc * ratio
        current_cost = self.cost_basis_usdc.get(token_id, _ZERO)
        consumed_ratio = min(actual_filled / current, Decimal("1"))
        cost_released = current_cost * consumed_ratio
        remaining_cost = current_cost - cost_released
        if remaining_cost <= _ZERO:
            self.cost_basis_usdc.pop(token_id, None)
        else:
            self.cost_basis_usdc[token_id] = remaining_cost
        new_position = current - actual_filled
        # 完全平仓时记录已实现交易摘要供 metrics 查询
        if new_position <= _ZERO:
            entry_at = self.first_fill_at.pop(token_id, None)
            max_loss = self.max_unrealized_loss.pop(token_id, _ZERO)
            # 取价格时序（持仓期间 30s 采样 best_bid）+ 释放内存
            price_history = self.position_price_history.pop(token_id, [])
            self._last_price_sample_at.pop(token_id, None)
            now = datetime_t.now(timezone.utc)
            realized_pnl = actual_received - actual_fee - current_cost
            self.realized_trades.append({
                "token_id": token_id,
                "entry_at": entry_at.isoformat() if entry_at else None,
                "exit_at": now.isoformat(),
                "holding_seconds": (now - entry_at).total_seconds() if entry_at else None,
                "cost": str(current_cost),
                "received_net": str(actual_received - actual_fee),
                "realized_pnl": str(realized_pnl),
                "max_unrealized_loss": str(max_loss),
                "max_drawdown_pct": (
                    str((max_loss / current_cost * Decimal("100")).quantize(Decimal("0.01")))
                    if current_cost > 0 else "0"
                ),
                "price_history": price_history,  # [(ts, best_bid), ...]
                "price_samples": len(price_history),
            })
            # 限制 1000 笔避免内存涨
            if len(self.realized_trades) > 1000:
                self.realized_trades = self.realized_trades[-1000:]
            self.positions.pop(token_id, None)
            self.cost_basis_usdc.pop(token_id, None)
        else:
            self.positions[token_id] = new_position
        self.available_usdc += actual_received - actual_fee
        self.fees_accrued_usdc += actual_fee

    def position_for(self, token_id: str) -> Decimal:
        return self.positions.get(token_id, _ZERO)

    def cost_for(self, token_id: str) -> Decimal:
        return self.cost_basis_usdc.get(token_id, _ZERO)
