"""T1.6 动态止损规则单元测试（2026-06-26）

测试 dynamic_stop_loss 模块的修复速度分档与止损触发逻辑。
"""

import unittest
import sys
import os

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

from dynamic_stop_loss import (
    classify_recovery_speed,
    compute_stop_loss_pct,
    compute_support_stop,
    compute_vol_stop,
    compute_tightest_stop,
    is_stop_loss_triggered,
    STOP_LOSS_FAST,
    STOP_LOSS_MID,
    STOP_LOSS_SLOW,
    DEFAULT_STOP_LOSS,
    MIN_STOP_LOSS,
    MAX_STOP_LOSS,
    SUPPORT_LOOKBACK,
    VOL_STOP_K,
    ATR_LOOKBACK,
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


# ============ T1.10 二期：三件套止损测试 ============

class TestSupportStop(unittest.TestCase):
    """T1.10 二期：支撑位止损"""

    def test_low_20_below_cost_returns_negative_pct(self):
        """low_20 低于成本 → 负数止损百分比"""
        # 成本 36.385，low_20=33.0 → support=32.99 → pct=(32.99/36.385-1)*100 ≈ -9.33%
        pct, details = compute_support_stop("000063", 36.385, low_20=33.0)
        self.assertLess(pct, 0)
        self.assertEqual(details["source"], "support")
        self.assertAlmostEqual(details["support_price"], 32.99, places=2)
        self.assertEqual(details["low_20"], 33.0)

    def test_low_20_none_falls_back_to_default(self):
        """low_20=None → 回退 DEFAULT_STOP_LOSS"""
        pct, details = compute_support_stop("000063", 36.385, low_20=None)
        self.assertEqual(pct, DEFAULT_STOP_LOSS)
        self.assertIn("fallback", details["reason"])

    def test_round_number_aligns_to_5_multiple(self):
        """round_number=True → 对齐到 5 的倍数"""
        # low_20=33.0 → floor(32.99//5*5)=30 → support=30.0
        pct, details = compute_support_stop("000063", 36.385, low_20=33.0, round_number=True)
        self.assertEqual(details["support_price"], 30.0)
        self.assertTrue(details["round_number"])

    def test_clamped_to_min_stop_loss(self):
        """low_20 接近成本 → 截断到 MIN_STOP_LOSS（-3%）"""
        # 成本 36.385，low_20=36.0 → support=35.99 → pct≈-1.09% → 截断到 -3%
        pct, details = compute_support_stop("000063", 36.385, low_20=36.0)
        self.assertEqual(pct, MIN_STOP_LOSS)
        self.assertLessEqual(pct, -3.0)

    def test_clamped_to_max_stop_loss(self):
        """low_20 远低于成本 → 截断到 MAX_STOP_LOSS（-10%）"""
        # 成本 36.385，low_20=30.0 → support=29.99 → pct≈-17.6% → 截断到 -10%
        pct, details = compute_support_stop("000063", 36.385, low_20=30.0)
        self.assertEqual(pct, MAX_STOP_LOSS)
        self.assertGreaterEqual(pct, -10.0)

    def test_zero_cost_returns_default(self):
        """成本<=0 → 回退默认"""
        pct, details = compute_support_stop("000063", 0, low_20=33.0)
        self.assertEqual(pct, DEFAULT_STOP_LOSS)
        self.assertIsNone(details["stop_loss_price"])


class TestVolStop(unittest.TestCase):
    """T1.10 二期：波动率止损"""

    def test_atr_input_returns_negative_pct(self):
        """传入 ATR → 用 entry - K×ATR 算止损"""
        # 成本 36.385，ATR=2.0 → stop_price=36.385-1.5*2.0=33.385 → pct≈-8.24%
        pct, details = compute_vol_stop("000063", 36.385, atr=2.0)
        self.assertLess(pct, 0)
        self.assertEqual(details["source"], "vol")
        self.assertEqual(details["atr"], 2.0)
        self.assertEqual(details["atr_source"], "input")
        self.assertEqual(details["k"], VOL_STOP_K)

    def test_atr_none_uses_daily_returns(self):
        """ATR=None + daily_returns → std 估计"""
        returns = [0.01, -0.005, 0.015, 0.008, 0.012, -0.003, 0.01]
        pct, details = compute_vol_stop("000063", 36.385, atr=None, daily_returns=returns)
        self.assertLess(pct, 0)
        self.assertEqual(details["atr_source"], "estimated_from_returns")
        self.assertGreater(details["atr"], 0)

    def test_no_atr_no_returns_falls_back(self):
        """无 ATR 无 returns → 回退 DEFAULT_STOP_LOSS"""
        pct, details = compute_vol_stop("000063", 36.385, atr=None, daily_returns=None)
        self.assertEqual(pct, DEFAULT_STOP_LOSS)
        self.assertIn("fallback", details["reason"])

    def test_clamped_to_min_stop_loss(self):
        """小 ATR → 截断到 MIN_STOP_LOSS"""
        # 成本 36.385，ATR=0.1 → stop_price=36.385-0.15=36.235 → pct≈-0.41% → 截断到 -3%
        pct, _ = compute_vol_stop("000063", 36.385, atr=0.1)
        self.assertEqual(pct, MIN_STOP_LOSS)

    def test_clamped_to_max_stop_loss(self):
        """大 ATR → 截断到 MAX_STOP_LOSS"""
        # 成本 36.385，ATR=10 → stop_price=36.385-15=21.385 → pct≈-41% → 截断到 -10%
        pct, _ = compute_vol_stop("000063", 36.385, atr=10.0)
        self.assertEqual(pct, MAX_STOP_LOSS)


class TestTightestStop(unittest.TestCase):
    """T1.10 二期：三件套取紧"""

    def test_returns_max_of_three(self):
        """取紧 = max(动态, 支撑, 波动) — 负数 max = 最紧"""
        # 动态 -5%（default），支撑 -4%（low_20 接近成本），波动 -6%（大 ATR）
        # max(-5, -4, -6) = -4 → 支撑位最紧
        pct, details = compute_tightest_stop(
            "000063", 100.0, current_price=95.0,
            daily_returns=None,  # 动态走 default -5%
            low_20=96.0,         # support=95.99 → pct≈-4.01% → 截断到 -3%? 不，95.99/100-1=-4.01%
            atr=4.0,             # stop=100-6=94 → pct=-6%
        )
        # 重新算：low_20=96 → support=95.99 → pct=(95.99/100-1)*100=-4.01%
        # 动态 default=-5%, 支撑=-4.01%, 波动=-6% → max=-4.01% → 支撑位最紧
        self.assertAlmostEqual(pct, -4.01, places=1)
        self.assertEqual(details["final_source"], "support")
        self.assertIn("dynamic", details["components"])
        self.assertIn("support", details["components"])
        self.assertIn("vol", details["components"])

    def test_final_source_dynamic_when_tightest(self):
        """动态止损最紧时 final_source=dynamic"""
        # 动态 -8%（fast 档），支撑 -4%，波动 -6% → max=-4% → 支撑位最紧
        # 想让 dynamic 最紧：让 dynamic=-3%（slow），支撑更负，波动更负
        returns = [0.001, -0.002, 0.0005, -0.001, 0.002, -0.0015, 0.001]  # slow → -3%
        # 支撑：low_20=80, cost=100 → support=79.99 → pct=-20% → 截断到 -10%
        # 波动：atr=10, cost=100 → stop=85 → pct=-15% → 截断到 -10%
        # max(-3, -10, -10) = -3 → dynamic 最紧
        pct, details = compute_tightest_stop(
            "000063", 100.0, current_price=95.0,
            daily_returns=returns, low_20=80.0, atr=10.0,
        )
        self.assertEqual(pct, -3.0)
        self.assertEqual(details["final_source"], "dynamic")

    def test_final_source_vol_when_tightest(self):
        """波动率止损最紧时 final_source=vol"""
        # 动态 -8%（fast），支撑 -10%（low_20 远），波动 -4%（小 ATR）
        # max(-8, -10, -4) = -4 → vol 最紧
        returns = [0.01, 0.015, 0.012, 0.01, 0.008, -0.003, 0.01]  # fast → -8%
        # 支撑：low_20=80, cost=100 → -20% → 截断 -10%
        # 波动：atr=3, cost=100 → stop=100-4.5=95.5 → pct=-4.5%
        pct, details = compute_tightest_stop(
            "000063", 100.0, current_price=95.0,
            daily_returns=returns, low_20=80.0, atr=3.0,
        )
        self.assertEqual(details["final_source"], "vol")
        self.assertGreater(pct, -8.0)  # 比 -8% 更紧

    def test_backward_compat_is_stop_loss_triggered_no_new_params(self):
        """is_stop_loss_triggered 不传 low_20/atr → 一期行为（向后兼容）"""
        # 不传新参数 → 走 compute_stop_loss_pct，details 有 speed 字段无 components
        triggered, details = is_stop_loss_triggered("000063", 36.385, 33.0, None)
        self.assertTrue(triggered)
        self.assertIn("speed", details)
        self.assertNotIn("components", details)
        self.assertNotIn("final_source", details)

    def test_is_stop_loss_triggered_with_low_20_uses_tightest(self):
        """is_stop_loss_triggered 传 low_20 → 走三件套"""
        # 传 low_20 → details 应含 components / final_source
        triggered, details = is_stop_loss_triggered(
            "000063", 100.0, 95.0, None, low_20=96.0, atr=4.0
        )
        self.assertIn("components", details)
        self.assertIn("final_source", details)
        self.assertIn("triggered", details)

    def test_margin_to_stop_populated(self):
        """三件套模式 margin_to_stop 字段填充"""
        triggered, details = is_stop_loss_triggered(
            "000063", 100.0, 95.0, None, low_20=96.0, atr=4.0
        )
        self.assertIn("margin_to_stop", details)
        self.assertIn("margin_to_stop_pct", details)


if __name__ == "__main__":
    unittest.main()
