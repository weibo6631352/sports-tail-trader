"""赔率去抽水（de-vig）—— 把含庄家 vig 的 implied 概率归一到真实概率。

庄家挂赔率含 overround（implied 概率求和 > 1，多出部分就是抽水利润）。要把
implied 与 Polymarket 价格（0~1 ≡ 真概率）对齐比较，必须先去 vig。

支持任意数量结果（2-way ML、3-way 含平局、N-way over/under 多线等），统一用
power method—— 它假设 vig 按概率非均匀分布（赢面大的结果 vig 占比更小），
比简单比例归一（assume vig 均匀）更准。

为什么不止于 2-way：足球整场胜负盘 home/away/draw 三结果，2-way devig 丢
掉 draw_implied，会把 home/away 高估约 0.20（举例 home_eu=2.0 away_eu=4.0
draw_eu=3.0：2-way 给 home_true_p=0.667，3-way 给 0.462）。任何包含 draw、
平局退款、多线总分等盘口都必须 N-way devig，否则 odds_gap 算出虚假 edge。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal


def devig_implied(raw_probs: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """De-vig：把含庄家 vig 的 implied 概率归一到 0~1 真概率。

    算法按结果数选择：
    - **N == 2 走比例归一** (raw / Σ)：行业 2-way devig 标准做法，假设 vig 在两侧
      均匀分配。N=2 时与 power method 数值差异 < 3%，但保留传统语义。
    - **N >= 3 走 power method**：找 k 使 Σ raw^k = 1。Power method 假设 vig 与
      raw^k 成比例（赢面大的 vig 占比小），3-way 含 draw 时比例归一会高估 home/
      away 0.15-0.20，必须 power method。

    输入任一 raw <= 0、求和 <= 0 时返回全 0 字典；调用方应据此判定"赔率不可信"。
    """
    if not raw_probs:
        return {}
    values = list(raw_probs.values())
    if any(v <= Decimal("0") for v in values):
        return {key: Decimal("0") for key in raw_probs}
    total = sum(values)
    if total <= Decimal("0"):
        return {key: Decimal("0") for key in raw_probs}

    # N == 2：行业 2-way 标准 = 比例归一
    if len(values) == 2:
        return {key: value / total for key, value in raw_probs.items()}

    floats = [float(v) for v in values]
    k = 1.0
    for _ in range(20):
        powered = [v**k for v in floats]
        f = sum(powered) - 1.0
        if abs(f) < 1e-8:
            break
        df = sum(p * _safe_log(v) for v, p in zip(floats, powered))
        if df == 0:
            break
        k -= f / df
    if k <= 0:
        k = 1.0
    de_vig = {key: Decimal(str(float(value) ** k)) for key, value in raw_probs.items()}
    # 最终用比例归一兜底，确保严格 Σ = 1
    s = sum(de_vig.values())
    if s <= Decimal("0"):
        return {key: Decimal("0") for key in raw_probs}
    return {key: value / s for key, value in de_vig.items()}


def overround(raw_probs: Mapping[str, Decimal]) -> Decimal:
    """庄家盘口 implied 概率求和（含 vig）= Σ raw_implied。

    无 vig 时 = 1.0；含 vig 时 > 1.0（如 1.05 = 5% 抽水）。
    抽水比例 = overround - 1。
    """
    return sum(raw_probs.values(), Decimal("0"))


def vig_fraction(raw_probs: Mapping[str, Decimal]) -> Decimal:
    """抽水占比 = Σ raw - 1。≈ 0 = 无 vig；> 0.05 = 高抽水（5%）。"""
    return overround(raw_probs) - Decimal("1")


def _safe_log(value: float) -> float:
    if value <= 0:
        return 0.0
    return math.log(value)
