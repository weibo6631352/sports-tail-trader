from __future__ import annotations

import logging
from decimal import Decimal
from enum import StrEnum


class PositionLifecycleStage(StrEnum):
    """仓位生命周期阶段。

    作为未来仓位状态机的领域类型先行落地。当前仅用于审计/可视化序列化，
    并不参与交易主链路判定；上层若需要据此分支必须先建模状态迁移与不变量。
    """

    ENTRY_PENDING = "entry_pending"
    HOLDING = "holding"
    EXITING = "exiting"
    CLOSED = "closed"
    UNKNOWN = "unknown"


_logger = logging.getLogger(__name__)


def classify(
    *,
    shares: Decimal,
    open_buy_shares: Decimal,
    open_sell_shares: Decimal,
    confirmed_shares: Decimal,
) -> PositionLifecycleStage:
    """根据当前持仓与挂单的份额组合推断生命周期阶段。

    UNKNOWN 表示组合不在已知分类内（例如负份额、对端挂单冲突等异常状态），
    一定要带上原始关键字段写 WARN 日志，便于补录或修正状态机。
    """

    if shares == 0 and open_buy_shares > 0:
        return PositionLifecycleStage.ENTRY_PENDING
    if shares > 0 and open_sell_shares == 0:
        return PositionLifecycleStage.HOLDING
    if shares > 0 and open_sell_shares > 0:
        return PositionLifecycleStage.EXITING
    if shares == 0 and confirmed_shares > 0 and open_buy_shares == 0:
        return PositionLifecycleStage.CLOSED
    _logger.warning(
        "position lifecycle UNKNOWN: shares=%s open_buy=%s open_sell=%s confirmed=%s",
        shares,
        open_buy_shares,
        open_sell_shares,
        confirmed_shares,
    )
    return PositionLifecycleStage.UNKNOWN
