from __future__ import annotations

from decimal import Decimal

import pytest

from polymarket_trader.domain.devig import devig_implied, overround


class TestDevigTwoWay:
    def test_no_vig_passthrough(self) -> None:
        # 真正 0% vig：求和 = 1 → 输入 = 输出
        out = devig_implied({"home": Decimal("0.6"), "away": Decimal("0.4")})
        assert out["home"] == pytest.approx(Decimal("0.6"), abs=1e-6)
        assert out["away"] == pytest.approx(Decimal("0.4"), abs=1e-6)

    def test_two_way_devig_normalizes_to_one(self) -> None:
        # 含 5% vig: implied 求和 = 1.05
        out = devig_implied({"home": Decimal("0.55"), "away": Decimal("0.50")})
        total = sum(out.values())
        assert total == pytest.approx(Decimal("1.0"), abs=1e-6)
        # home 应该约 0.55/(0.55+0.50)≈0.524（power method 接近比例归一）
        assert Decimal("0.50") < out["home"] < Decimal("0.55")


class TestDevigThreeWaySoccer:
    def test_soccer_three_way_unique_bias_fix(self) -> None:
        # 关键场景：home_eu=2.0 → 0.50, away_eu=4.0 → 0.25, draw_eu=3.0 → 0.333
        # 2-way devig 会算 home_true_p = 0.50/(0.50+0.25) = 0.667（高估 +0.20）
        # 3-way devig 必须给出 ≈ 0.462
        raw = {
            "home": Decimal("0.50"),
            "away": Decimal("0.25"),
            "draw": Decimal("0.333"),
        }
        out = devig_implied(raw)
        total = sum(out.values())
        assert total == pytest.approx(Decimal("1.0"), abs=1e-6)
        # home 应该在 0.45-0.48 之间（真正概率，远低于 2-way 的 0.667）
        assert Decimal("0.43") < out["home"] < Decimal("0.50")
        # 反过来：2-way devig 错误结果 0.667 必须远离
        assert out["home"] < Decimal("0.55")  # 决不能高估到 0.55+

    def test_three_way_overround_reflects_vig(self) -> None:
        raw = {
            "home": Decimal("0.50"),
            "away": Decimal("0.25"),
            "draw": Decimal("0.333"),
        }
        # overround = Σ implied = 1.083 (含 vig)；vig = 0.083 = 8.3%
        ov = overround(raw)
        assert Decimal("1.08") < ov < Decimal("1.09")


class TestDevigEdgeCases:
    def test_empty_returns_empty(self) -> None:
        assert devig_implied({}) == {}

    def test_zero_implied_returns_zeros(self) -> None:
        # 任一 implied <= 0 → 全部置 0（赔率不可信）
        out = devig_implied({"home": Decimal("0.6"), "away": Decimal("0")})
        assert out == {"home": Decimal("0"), "away": Decimal("0")}

    def test_negative_implied_returns_zeros(self) -> None:
        out = devig_implied({"home": Decimal("0.6"), "away": Decimal("-0.1")})
        assert all(v == Decimal("0") for v in out.values())

    def test_single_outcome(self) -> None:
        out = devig_implied({"home": Decimal("1.5")})
        # 单结果：归一到 1
        assert out["home"] == pytest.approx(Decimal("1.0"), abs=1e-6)
