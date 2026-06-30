"""T1.10 二期 t_flip_applicability 单元测试（2026-06-30）

测试做T适用性自动判断：
  - detect_gap_up_decline 单日高开低走检测
  - compute_t_flip_frequency 统计（mock baostock）
  - is_applicable 启用/关闭/观望三态
"""

import unittest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

from t_flip_applicability import (
    detect_gap_up_decline,
    compute_t_flip_frequency,
    is_applicable,
    T_FLIP_GAP_ENABLED,
    T_FLIP_GAP_OBSERVE,
    T_FLIP_GAP_DISABLED,
    MIN_FLIP_DAYS,
    MIN_INTRADAY_RANGE,
)


class TestDetectGapUpDecline(unittest.TestCase):
    """测试单日高开低走检测"""

    def test_typical_gap_up_decline(self):
        """典型高开低走：高开1.5%+，盘中走低，收盘在低位30%分位"""
        # pre_close=10, open=10.20 (高开2%), high=10.30, low=9.80, close=9.90
        # cp = (9.90-9.80)/(10.30-9.80) = 0.10/0.50 = 0.20 < 0.3 ✓
        self.assertTrue(detect_gap_up_decline(10.20, 10.0, 10.30, 9.80, 9.90))

    def test_no_gap_up(self):
        """未高开 → False"""
        # open=10.00, pre_close=10.00 → gap=0 < 1.5%
        self.assertFalse(detect_gap_up_decline(10.00, 10.0, 10.30, 9.80, 9.90))

    def test_gap_up_but_close_high(self):
        """高开但收盘高位 → False"""
        # open=10.20, close=10.25 > open → 不满足 intraday_decline
        self.assertFalse(detect_gap_up_decline(10.20, 10.0, 10.30, 9.80, 10.25))

    def test_close_position_boundary(self):
        """close_position 边界 cp=0.3 → 不触发（严格 < 0.3）"""
        # 想让 cp=0.3: (close-low)/(high-low)=0.3
        # pre_close=10, open=10.20, high=10.50, low=9.50
        # close = 9.50 + 0.3 * (10.50-9.50) = 9.50 + 0.30 = 9.80
        # cp = (9.80-9.50)/(10.50-9.50) = 0.30/1.00 = 0.30 → 不 < 0.3 → False
        self.assertFalse(detect_gap_up_decline(10.20, 10.0, 10.50, 9.50, 9.80))

    def test_close_position_just_below_boundary(self):
        """cp 略低于 0.3 → 触发"""
        # close = 9.79 → cp = (9.79-9.50)/1.00 = 0.29 < 0.3 → True
        self.assertTrue(detect_gap_up_decline(10.20, 10.0, 10.50, 9.50, 9.79))

    def test_zero_pre_close(self):
        """pre_close<=0 → False"""
        self.assertFalse(detect_gap_up_decline(10.0, 0.0, 10.5, 9.5, 9.8))

    def test_high_equal_low(self):
        """high==low（一字板）→ cp=0.5 默认 → 不触发"""
        # ir=0 → cp=0.5 > 0.3 → False
        self.assertFalse(detect_gap_up_decline(10.20, 10.0, 10.20, 10.20, 10.10))


class TestComputeFrequency(unittest.TestCase):
    """测试频率统计（mock fetch_kline_baostock）"""

    def _make_klines(self, days):
        """构造 K 线序列，days 是 [(open, high, low, close), ...]"""
        recs = []
        # 第一天作为 pre_close 基准
        prev_close = 10.0
        recs.append({"日期": "20260301", "开盘": 10.0, "最高": 10.1, "最低": 9.9, "收盘": 10.0})
        for i, (o, h, l, c) in enumerate(days, start=2):
            recs.append({
                "日期": f"202603{i:02d}",
                "开盘": o, "最高": h, "最低": l, "收盘": c,
            })
        return recs

    @patch("t_flip_applicability.fetch_kline_baostock" if False else "data_converter.fetch_kline_baostock")
    def test_count_flip_days(self, _mock):
        """统计高开低走天数"""
        # 构造 3 个高开低走日 + 2 个普通日
        # 用 patch t_flip_applicability 内部 import 的方式
        pass  # 移到下方用正确 patch 路径

    def test_count_flip_days_correct(self):
        """正确统计高开低走天数。

        数据设计：pre_close 来自前一日实际 close（非固定基线）。
        FLIP 日：pre_close=10.0，open=10.20（gap 2%），close=9.90（cp=0.20 < 0.3）。
        RESET 日：pre_close=9.90，open=9.92（gap 0.2% < 1.5%），close=10.00 → 非高开 + close>open，双重保险非 FLIP，并把 close 重置回 10.0。
        序列 [baseline10.0, FLIP, RESET, FLIP, RESET, FLIP] → 3 flips / 5 total。
        """
        klines = self._make_klines([
            # (open, high, low, close)
            (10.20, 10.30, 9.80, 9.90),  # FLIP: pre_close=10.0, gap=2%, cp=0.20 ✓
            (9.92, 10.05, 9.85, 10.00),  # RESET: pre_close=9.90, gap=0.2% → 非 FLIP，close 回 10.0
            (10.20, 10.30, 9.80, 9.90),  # FLIP: pre_close=10.0, gap=2%, cp=0.20 ✓
            (9.92, 10.05, 9.85, 10.00),  # RESET: pre_close=9.90 → 非 FLIP
            (10.20, 10.30, 9.80, 9.90),  # FLIP: pre_close=10.0, gap=2%, cp=0.20 ✓
        ])
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            freq = compute_t_flip_frequency("002049")
        self.assertEqual(freq["flip_days"], 3)
        self.assertEqual(freq["total_days"], 5)
        self.assertGreater(freq["avg_intraday_range"], 0)
        self.assertEqual(len(freq["sample_dates"]), 3)

    def test_no_data_returns_error(self):
        """无数据 → error"""
        with patch("data_converter.fetch_kline_baostock", return_value=None):
            freq = compute_t_flip_frequency("002049")
        self.assertIn("error", freq)

    def test_flip_ratio_calculation(self):
        """flip_ratio = flip_days / total_days。

        序列 [baseline10.0, FLIP, RESET, FLIP] → 2 flips / 3 total = 2/3。
        """
        klines = self._make_klines([
            (10.20, 10.30, 9.80, 9.90),  # FLIP: pre_close=10.0, gap=2% ✓
            (9.92, 10.05, 9.85, 10.00),  # RESET: pre_close=9.90 → 非 FLIP
            (10.20, 10.30, 9.80, 9.90),  # FLIP: pre_close=10.0, gap=2% ✓
        ])
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            freq = compute_t_flip_frequency("002049")
        self.assertAlmostEqual(freq["flip_ratio"], 2 / 3, places=2)


class TestIsApplicable(unittest.TestCase):
    """测试做T适用性判断"""

    def test_enabled_when_frequent_flip_and_high_range(self):
        """flip_days>=3 + 振幅>=4% → 启用"""
        # 构造 3 个高开低走日，振幅都 > 4%
        klines = [{
            "日期": "20260301", "开盘": 10.0, "最高": 10.1, "最低": 9.9, "收盘": 10.0
        }]
        # 振幅 = (high-low)/pre_close，pre_close=10，要 >=4% 则 high-low>=0.4
        for i, (o, h, l, c) in enumerate([
            (10.20, 10.50, 9.50, 9.60),  # 振幅 10% ✓ 高开低走 ✓
            (10.30, 10.60, 9.60, 9.70),  # 振幅 10% ✓ 高开低走 ✓
            (10.25, 10.55, 9.55, 9.65),  # 振幅 10% ✓ 高开低走 ✓
            (10.00, 10.10, 9.90, 10.05), # 普通日
        ], start=2):
            klines.append({"日期": f"202603{i:02d}", "开盘": o, "最高": h, "最低": l, "收盘": c})
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            applicable, gap, reason = is_applicable("002049")
        self.assertTrue(applicable)
        self.assertEqual(gap, T_FLIP_GAP_ENABLED)
        self.assertIn("启用", reason)

    def test_disabled_when_zero_flip_days(self):
        """flip_days=0 → 关闭"""
        klines = [{
            "日期": "20260301", "开盘": 10.0, "最高": 10.1, "最低": 9.9, "收盘": 10.0
        }]
        for i, (o, h, l, c) in enumerate([
            (10.00, 10.10, 9.90, 10.05),  # 普通日
            (10.00, 10.10, 9.90, 10.02),  # 普通日
            (10.00, 10.10, 9.90, 10.08),  # 普通日
        ], start=2):
            klines.append({"日期": f"202603{i:02d}", "开盘": o, "最高": h, "最低": l, "收盘": c})
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            applicable, gap, reason = is_applicable("002049")
        self.assertFalse(applicable)
        self.assertEqual(gap, T_FLIP_GAP_DISABLED)
        self.assertIn("关闭", reason)

    def test_observe_when_few_flip_days(self):
        """flip_days 1-2 → 观望"""
        klines = [{
            "日期": "20260301", "开盘": 10.0, "最高": 10.1, "最低": 9.9, "收盘": 10.0
        }]
        for i, (o, h, l, c) in enumerate([
            (10.20, 10.50, 9.50, 9.60),  # ✓ 高开低走
            (10.00, 10.10, 9.90, 10.05), # 普通日
            (10.00, 10.10, 9.90, 10.02), # 普通日
            (10.00, 10.10, 9.90, 10.08), # 普通日
        ], start=2):
            klines.append({"日期": f"202603{i:02d}", "开盘": o, "最高": h, "最低": l, "收盘": c})
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            applicable, gap, reason = is_applicable("002049")
        self.assertFalse(applicable)
        self.assertEqual(gap, T_FLIP_GAP_OBSERVE)
        self.assertIn("观望", reason)

    def test_observe_when_low_range(self):
        """flip_days>=3 但振幅<4% → 观望"""
        # 构造 3 个高开低走日但振幅很小
        klines = [{
            "日期": "20260301", "开盘": 10.0, "最高": 10.05, "最低": 9.95, "收盘": 10.0
        }]
        # 振幅 = (high-low)/pre_close = 0.05/10 = 0.5% < 4%
        for i, (o, h, l, c) in enumerate([
            (10.15, 10.20, 9.96, 9.98),  # 高开 1.5%，振幅 2.4%，cp≈0.09 ✓ 高开低走
            (10.15, 10.20, 9.96, 9.97),  # 同上
            (10.15, 10.20, 9.96, 9.98),  # 同上
        ], start=2):
            klines.append({"日期": f"202603{i:02d}", "开盘": o, "最高": h, "最低": l, "收盘": c})
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            applicable, gap, reason = is_applicable("002049")
        self.assertFalse(applicable)
        self.assertEqual(gap, T_FLIP_GAP_OBSERVE)

    def test_data_error_returns_disabled(self):
        """数据获取失败 → 关闭（保守不启用）"""
        with patch("data_converter.fetch_kline_baostock", return_value=None):
            applicable, gap, reason = is_applicable("002049")
        self.assertFalse(applicable)
        self.assertEqual(gap, T_FLIP_GAP_DISABLED)
        self.assertIn("数据不可用", reason)


class TestCli(unittest.TestCase):
    """测试 CLI 入口"""

    def test_cli_runs_without_error(self):
        """CLI 入口可运行（mock 数据避免网络调用）"""
        klines = [{"日期": "20260301", "开盘": 10.0, "最高": 10.1, "最低": 9.9, "收盘": 10.0}]
        with patch("data_converter.fetch_kline_baostock", return_value=klines):
            import t_flip_applicability
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            old_argv = sys.argv
            sys.argv = ["t_flip_applicability.py", "002049"]
            try:
                with redirect_stdout(buf):
                    t_flip_applicability.cli()
                output = buf.getvalue()
                self.assertIn("002049", output)
                self.assertIn("applicable", output)
            finally:
                sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()
