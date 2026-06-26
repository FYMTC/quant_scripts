"""T1.6 动态止损规则单元测试（2026-06-26）

测试 dynamic_stop_loss 模块的修复速度分档与止损触发逻辑。
"""

import unittest
import sys
import os

sys.path.insert(0, "/config/quant_scripts")
os.chdir("/config/quant_scripts")

from dynamic_stop_loss import (
    classify_recovery_speed,
    compute_stop_loss_pct,
    is_stop_loss_triggered,
    STOP_LOSS_FAST,
    STOP_LOSS_MID,
    STOP_LOSS_SLOW,
    DEFAULT_STOP_LOSS,
)


class TestClassifyRecoverySpeed(unittest.TestCase):
    """测试修复速度分档"""

    def test_fast_recovery_high_daily_return(self):
        """高日均收益 → 快速修复档"""
        returns = [0.01, -0.005, 0.015, 0.008, 0.012, -0.003, 0.01]  # 日均约 0.67%
        speed, mean_ret = classify_recovery_speed(returns)
        self.assertEqual(speed, "fast")
        self.assertGreater(mean_ret, 0.001)

    def test_slow_recovery_low_daily_return(self):
        """低日均收益 → 慢速修复档"""
        returns = [0.001, -0.002, 0.0005, -0.001, 0.002, -0.0015, 0.001]  # 日均约 0
        speed, mean_ret = classify_recovery_speed(returns)
        self.assertEqual(speed, "slow")
        self.assertLessEqual(mean_ret, 0.0005)

    def test_mid_recovery_moderate_daily_return(self):
        """中等日均收益 → 中速修复档"""
        # 日均约 0.15%，5%亏损需约33天回本 → 中速档（15-60天）
        returns = [0.002, 0.001, -0.0005, 0.002, 0.0015, -0.001, 0.002]
        speed, mean_ret = classify_recovery_speed(returns)
        self.assertEqual(speed, "mid")
        # 验证回本天数在 15-60 范围
        if mean_ret > 0:
            days = 0.05 / mean_ret
            self.assertGreaterEqual(days, 15)
            self.assertLessEqual(days, 60)

    def test_empty_returns_defaults_to_mid(self):
        """空列表 → 默认中速"""
        speed, mean_ret = classify_recovery_speed([])
        self.assertEqual(speed, "mid")
        self.assertEqual(mean_ret, 0.0)

    def test_too_few_returns_defaults_to_mid(self):
        """少于3个数据点 → 默认中速"""
        speed, _ = classify_recovery_speed([0.01, 0.02])
        self.assertEqual(speed, "mid")


class TestComputeStopLossPct(unittest.TestCase):
    """测试止损百分比计算"""

    def test_fast_speed_returns_fast_stop_loss(self):
        """快速档 → -8% 止损"""
        returns = [0.01, 0.015, 0.012, 0.01, 0.008, -0.003, 0.01]
        pct, details = compute_stop_loss_pct("000063", 36.385, 35.0, returns)
        self.assertEqual(pct, STOP_LOSS_FAST)
        self.assertEqual(details["speed"], "fast")
        self.assertIsNotNone(details["stop_loss_price"])
        # 36.385 * (1 - 0.08) = 33.4742
        self.assertAlmostEqual(details["stop_loss_price"], 33.474, places=2)

    def test_slow_speed_returns_slow_stop_loss(self):
        """慢速档 → -3% 止损"""
        returns = [0.001, -0.002, 0.0005, -0.001, 0.002, -0.0015, 0.001]
        pct, details = compute_stop_loss_pct("002236", 16.695, 16.0, returns)
        self.assertEqual(pct, STOP_LOSS_SLOW)
        self.assertEqual(details["speed"], "slow")

    def test_no_returns_uses_default(self):
        """无价格历史 → 默认 -5% 止损"""
        pct, details = compute_stop_loss_pct("600487", 107.31, 110.0)
        self.assertEqual(pct, DEFAULT_STOP_LOSS)
        self.assertEqual(details["speed"], "default")

    def test_stop_loss_price_calculation(self):
        """止损价 = 成本 * (1 + stop_pct/100)"""
        pct, details = compute_stop_loss_pct("000063", 100.0, 95.0, None)
        # 默认 -5% → 100 * 0.95 = 95.0
        self.assertEqual(details["stop_loss_price"], 95.0)

    def test_zero_cost_returns_none_price(self):
        """成本为0 → 止损价为 None"""
        pct, details = compute_stop_loss_pct("000063", 0, 10.0)
        self.assertIsNone(details["stop_loss_price"])


class TestIsStopLossTriggered(unittest.TestCase):
    """测试止损触发判断"""

    def test_triggered_when_price_below_stop(self):
        """价格低于止损价 → 触发"""
        # 默认 -5%，成本36.385 → 止损价 34.566
        triggered, details = is_stop_loss_triggered("000063", 36.385, 33.0, None)
        self.assertTrue(triggered)
        self.assertEqual(details["speed"], "default")
        self.assertLess(details["margin_to_stop"], 0)

    def test_not_triggered_when_price_above_stop(self):
        """价格高于止损价 → 不触发"""
        triggered, details = is_stop_loss_triggered("000063", 36.385, 36.0, None)
        self.assertFalse(triggered)
        self.assertGreater(details["margin_to_stop"], 0)

    def test_fast_speed_wider_stop_avoids_trigger(self):
        """快速档止损更宽，避免在正常波动中误触发"""
        returns = [0.01, 0.015, 0.012, 0.01, 0.008, -0.003, 0.01]  # 快速档
        # 成本36.385，-8%止损价=33.474，价格34.0 → 不触发
        triggered, details = is_stop_loss_triggered("000063", 36.385, 34.0, returns)
        self.assertFalse(triggered)
        self.assertEqual(details["speed"], "fast")

    def test_slow_speed_tight_stop_triggers_earlier(self):
        """慢速档止损更紧，更早触发"""
        returns = [0.001, -0.002, 0.0005, -0.001, 0.002, -0.0015, 0.001]  # 慢速档
        # 成本16.695，-3%止损价=16.194，价格16.0 → 触发
        triggered, details = is_stop_loss_triggered("002236", 16.695, 16.0, returns)
        self.assertTrue(triggered)
        self.assertEqual(details["speed"], "slow")

    def test_zero_price_not_triggered(self):
        """价格为0 → 不触发"""
        triggered, details = is_stop_loss_triggered("000063", 36.385, 0, None)
        self.assertFalse(triggered)


class TestStopLossBoundaries(unittest.TestCase):
    """测试止损边界约束"""

    def test_stop_loss_clamped_to_max(self):
        """止损不超过 MAX_STOP_LOSS（-10%）"""
        # 极端高动量可能算出更宽止损，但应被截断到 -10%
        returns = [0.05, 0.04, 0.06, 0.05, 0.04]  # 极端高动量
        pct, _ = compute_stop_loss_pct("000063", 36.385, 35.0, returns)
        self.assertGreaterEqual(pct, -10.0)  # pct 是负数，-8 > -10

    def test_stop_loss_clamped_to_min(self):
        """止损不紧于 MIN_STOP_LOSS（-3%）"""
        returns = [0.001, -0.002, 0.0005, -0.001, 0.002, -0.0015, 0.001]  # 慢速档
        pct, _ = compute_stop_loss_pct("000063", 36.385, 35.0, returns)
        self.assertLessEqual(pct, -3.0)  # pct 是负数，-3 是最紧


if __name__ == "__main__":
    unittest.main()
