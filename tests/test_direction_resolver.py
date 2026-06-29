"""T1.10 信号方向解析器单元测试（2026-06-29）

测试 direction_resolver 模块的决策树逻辑 + bottom_fish_score 打分。

背景：T1.10 修复 agent_desk.py:247-252 的写死 direction 判定
（rapid_drop/price_below/rolling_decline 无条件 → SELL）。
新逻辑：方向由「持仓态 × 止损态 × 抄底分 × 大盘态」三元组决定。
"""

import unittest
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

from direction_resolver import (  # noqa: E402
    resolve_direction,
    resolve_from_event,
    classify_signal_type,
    detect_t_flip,
    BF_THR,
    BO_THR,
    TP_THR,
    SIGNAL_RAPID_DROP,
    SIGNAL_ROLLING_DECLINE,
    SIGNAL_PRICE_ABOVE,
    SIGNAL_UNKNOWN,
)
from bottom_fish_score import (  # noqa: E402
    compute,
    _calc_cp,
    _calc_above_ma20,
    _score_guba,
    _score_analyst,
    _cp_score,
    W_CP,
    W_MA20,
    W_ANALYST,
    W_GUBA,
)


# ========== 决策树 8 路径测试 ==========

class TestDecisionTreePaths(unittest.TestCase):
    """T1.10 决策树 8 条核心路径（策略文档 §10.2）"""

    def test_path1_holding_stop_triggered_rapid_drop_to_SELL(self):
        """持仓 + 止损触发 + rapid_drop → SELL（截断亏损）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=True, stop_triggered=True,
            regime="sideways", risk_level="safe",
        )
        self.assertEqual(d, "SELL")

    def test_path2_holding_no_stop_bear_to_HOLD(self):
        """持仓 + 止损未触发 + 弱市(bear) + rapid_drop → HOLD（弱市不忍痛割）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=True, stop_triggered=False,
            regime="bear", risk_level="safe",
        )
        self.assertEqual(d, "HOLD")

    def test_path3_holding_no_stop_tflip_to_T_FLIP(self):
        """持仓 + 止损未触发 + 震荡 + 高开低走 → T_FLIP（做T降成本）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=True, stop_triggered=False,
            regime="sideways", risk_level="safe", t_flip=True,
        )
        self.assertEqual(d, "T_FLIP")

    def test_path4_holding_no_stop_sideways_rapid_drop_to_HOLD(self):
        """持仓 + 止损未触发 + 震荡 + rapid_drop（无做T）→ HOLD（默认忍，T1.10 核心）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=True, stop_triggered=False,
            regime="sideways", risk_level="safe", t_flip=False,
        )
        self.assertEqual(d, "HOLD")

    def test_path5_empty_high_bf_rapid_drop_to_BUY(self):
        """空仓 + 抄底分≥阈值 + rapid_drop → BUY（均值回归抄底，T1.10 核心反转）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=False, stop_triggered=False,
            bottom_fish_score=0.72, regime="sideways", risk_level="safe",
        )
        self.assertEqual(d, "BUY")

    def test_path6_empty_low_bf_rapid_drop_to_WAIT(self):
        """空仓 + 抄底分<阈值 + rapid_drop → WAIT（规避接飞刀）"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=False, stop_triggered=False,
            bottom_fish_score=0.45, regime="sideways", risk_level="safe",
        )
        self.assertEqual(d, "WAIT")

    def test_path7_empty_rolling_decline_to_WAIT(self):
        """空仓 + rolling_decline → WAIT（渐进阴跌不抄底，无论抄底分）"""
        d = resolve_direction(
            signal_type=SIGNAL_ROLLING_DECLINE, holding=False, stop_triggered=False,
            bottom_fish_score=0.95, regime="bull", risk_level="safe",
        )
        self.assertEqual(d, "WAIT")

    def test_path8_empty_price_above_breakout_to_BUY(self):
        """空仓 + price_above + 突破分≥阈值 → BUY（突破追涨）"""
        d = resolve_direction(
            signal_type=SIGNAL_PRICE_ABOVE, holding=False, stop_triggered=False,
            breakout_score=0.72, regime="bull", risk_level="safe",
        )
        self.assertEqual(d, "BUY")

    def test_holding_surge_take_profit_to_SELL(self):
        """持仓 + 急涨 + 止盈分≥阈值 → SELL（大涨中盘卖）"""
        d = resolve_direction(
            signal_type="rapid_surge", holding=True, stop_triggered=False,
            take_profit_score=0.75, regime="bull", risk_level="safe",
        )
        self.assertEqual(d, "SELL")

    def test_danger_risk_level_counts_as_weak_market(self):
        """risk_level=danger 等同弱市：持仓+未止损+danger → HOLD"""
        d = resolve_direction(
            signal_type=SIGNAL_RAPID_DROP, holding=True, stop_triggered=False,
            regime="sideways", risk_level="danger",
        )
        self.assertEqual(d, "HOLD")


# ========== 信号类型反解测试 ==========

class TestClassifySignalType(unittest.TestCase):
    """classify_signal_type contains 匹配（应对动态 signal_id）"""

    def test_rapid_drop_in_dynamic_id(self):
        self.assertEqual(classify_signal_type("002049_rapid_drop"), "rapid_drop")
        self.assertEqual(classify_signal_type("sig_20260629_rapid_drop_abc"), "rapid_drop")

    def test_rolling_decline(self):
        self.assertEqual(classify_signal_type("rolling_decline"), "rolling_decline")

    def test_price_below_with_target(self):
        self.assertEqual(classify_signal_type("002049_price_below_7820"), "price_below")

    def test_price_above(self):
        self.assertEqual(classify_signal_type("002049_price_above_8200"), "price_above")

    def test_rapid_surge_and_surge_peak(self):
        self.assertEqual(classify_signal_type("002049_rapid_surge"), "rapid_surge")
        self.assertEqual(classify_signal_type("002049_surge_peak"), "surge_peak")

    def test_unknown_signal_id(self):
        self.assertEqual(classify_signal_type("sig_xyz_12345"), "unknown")

    def test_empty_and_none(self):
        self.assertEqual(classify_signal_type(""), "unknown")
        self.assertEqual(classify_signal_type(None), "unknown")


# ========== 做T检测测试 ==========

class TestDetectTFlip(unittest.TestCase):
    """detect_t_flip 高开低走判定"""

    def test_gap_up_and_intra_decline(self):
        """高开 1.5%+ 且盘中低于开盘 → True"""
        # pre_close=80, open=81.5(+1.875%), price=80.5(<open)
        self.assertTrue(detect_t_flip(81.5, 80.0, 80.5))

    def test_small_gap_not_trigger(self):
        """高开不足 1.5% → False"""
        self.assertFalse(detect_t_flip(80.8, 80.0, 80.5))

    def test_gap_up_but_price_above_open(self):
        """高开但盘中走高（非低走）→ False"""
        self.assertFalse(detect_t_flip(81.5, 80.0, 82.0))

    def test_none_inputs(self):
        self.assertFalse(detect_t_flip(None, 80.0, 80.5))
        self.assertFalse(detect_t_flip(81.5, None, 80.5))

    def test_zero_pre_close(self):
        self.assertFalse(detect_t_flip(81.5, 0.0, 80.5))


# ========== resolve_from_event 集成测试 ==========

class TestResolveFromEvent(unittest.TestCase):
    """resolve_from_event 便捷封装（含路径返回）"""

    def test_returns_direction_and_path(self):
        d, path = resolve_from_event(
            signal_id="002049_rapid_drop", holding=True, stop_triggered=True,
        )
        self.assertEqual(d, "SELL")
        self.assertIn("→ SELL", path)
        self.assertIn("sig=rapid_drop", path)

    def test_t_flip_detection_when_holding(self):
        """持仓时传入 OHLC 自动检测做T"""
        d, path = resolve_from_event(
            signal_id="002049_rapid_drop", holding=True, stop_triggered=False,
            regime="sideways", risk_level="safe",
            open_price=81.5, pre_close=80.0, current_price=80.5,
        )
        self.assertEqual(d, "T_FLIP")
        self.assertIn("t_flip=True", path)

    def test_t_flip_not_checked_when_empty(self):
        """空仓时不检测做T"""
        d, path = resolve_from_event(
            signal_id="002049_rapid_drop", holding=False, stop_triggered=False,
            bottom_fish_score=0.72,
            open_price=81.5, pre_close=80.0, current_price=80.5,
        )
        self.assertEqual(d, "BUY")
        self.assertIn("t_flip=False", path)


# ========== bottom_fish_score 纯函数测试 ==========

class TestBottomFishHelpers(unittest.TestCase):
    """bottom_fish_score 内部纯函数"""

    def test_cp_score_thresholds(self):
        self.assertEqual(_cp_score(0.3), 1.0)
        self.assertEqual(_cp_score(0.4), 0.5)   # 边界 0.4 落中段
        self.assertEqual(_cp_score(0.5), 0.5)
        self.assertEqual(_cp_score(0.6), 0.0)   # 边界 0.6 落低段
        self.assertEqual(_cp_score(0.9), 0.0)

    def test_calc_cp_normal(self):
        self.assertAlmostEqual(_calc_cp(80, 78, 79), 0.5, places=3)

    def test_calc_cp_high_eq_low(self):
        """high==low 取 0.5（防除零，抄 backtest 口径）"""
        self.assertEqual(_calc_cp(80, 80, 80), 0.5)

    def test_calc_above_ma20_insufficient_data(self):
        """<20 根收盘返回 None（不否决）"""
        self.assertIsNone(_calc_above_ma20([1, 2, 3, 4, 5]))

    def test_calc_above_ma20_above(self):
        closes = [10 + i * 0.1 for i in range(25)]  # 上升序列，末值 > ma20
        self.assertTrue(_calc_above_ma20(closes))

    def test_calc_above_ma20_below(self):
        closes = [20 - i * 0.1 for i in range(25)]  # 下降序列，末值 < ma20
        self.assertFalse(_calc_above_ma20(closes))

    def test_score_guba_bearish(self):
        """偏空文本 → 1.0（逆向抄底信号）"""
        self.assertEqual(_score_guba("割肉止损暴雷大跌套牢"), 1.0)

    def test_score_guba_bullish(self):
        """偏多文本 → 0.3（已上车非底部）"""
        self.assertEqual(_score_guba("抄底加仓看多涨停大涨"), 0.3)

    def test_score_guba_neutral(self):
        self.assertEqual(_score_guba("今天天气不错"), 0.5)

    def test_score_guba_unavailable(self):
        """含'暂不可用' → None（跳过该维度）"""
        self.assertIsNone(_score_guba("股吧(002049): 情绪数据暂不可用"))
        self.assertIsNone(_score_guba(None))
        self.assertIsNone(_score_guba(""))

    def test_score_analyst_hold_buy(self):
        self.assertEqual(_score_analyst({"verdict": "HOLD"}), 1.0)
        self.assertEqual(_score_analyst({"verdict": "BUY"}), 1.0)

    def test_score_analyst_sell(self):
        self.assertEqual(_score_analyst({"verdict": "SELL"}), 0.0)

    def test_score_analyst_none(self):
        self.assertIsNone(_score_analyst(None))
        self.assertIsNone(_score_analyst({"verdict": "UNKNOWN"}))


# ========== bottom_fish_score.compute 集成测试（mock 数据源）==========

class TestBottomFishCompute(unittest.TestCase):
    """compute() 四维打分 + 维度缺失重归一化 + 硬否决"""

    @patch("market_data.fetch_quote")
    @patch("risk_monitor.get_price_history")
    @patch("stock_kb.StockKB")
    @patch("tradingagents_runner._fetch_eastmoney_guba")
    def test_full_score_high(self, mock_guba, mock_kb, mock_ph, mock_quote):
        """四维全可用：cp 低分位 + above MA20 + analyst HOLD + 股吧偏空 → 高分"""
        mock_quote.return_value = {"price": 78.2, "high": 80.0, "low": 77.5, "open": 79.0, "pre_close": 79.5}
        # cp = (78.2-77.5)/(80-77.5) = 0.28 → score 1.0
        mock_ph.return_value = [70 + i * 0.5 for i in range(25)]  # 上升，above ma20
        mock_kb_inst = mock_kb.return_value
        mock_kb_inst.check_cache.return_value = {"hit": True, "report": {"verdict": "HOLD"}, "reason": "ok"}
        mock_guba.return_value = "割肉止损大跌套牢"

        r = compute("002049", 78.2)
        self.assertIsNotNone(r["score"])
        self.assertGreaterEqual(r["score"], 0.8)
        self.assertEqual(set(r["dimensions_used"]), {"cp", "ma20", "analyst", "guba"})
        self.assertEqual(r["dimensions_missing"], [])

    @patch("market_data.fetch_quote")
    @patch("risk_monitor.get_price_history")
    @patch("stock_kb.StockKB")
    @patch("tradingagents_runner._fetch_eastmoney_guba")
    def test_below_ma20_veto(self, mock_guba, mock_kb, mock_ph, mock_quote):
        """close < MA20 → 硬否决，score=0.0"""
        mock_quote.return_value = {"price": 78.2, "high": 80.0, "low": 77.5, "open": 79.0, "pre_close": 79.5}
        mock_ph.return_value = [90 - i * 0.5 for i in range(25)]  # 下降，below ma20
        r = compute("002049", 78.2)
        self.assertEqual(r["score"], 0.0)
        self.assertEqual(r["reason"], "below_ma20_veto")

    @patch("market_data.fetch_quote")
    @patch("risk_monitor.get_price_history")
    @patch("stock_kb.StockKB")
    @patch("tradingagents_runner._fetch_eastmoney_guba")
    def test_ohlc_unavailable_returns_none(self, mock_guba, mock_kb, mock_ph, mock_quote):
        """fetch_quote 返回 None → cp 拿不到 → score=None"""
        mock_quote.return_value = None
        r = compute("002049", 78.2)
        self.assertIsNone(r["score"])
        self.assertEqual(r["reason"], "intraday_ohlc_unavailable")

    @patch("market_data.fetch_quote")
    @patch("risk_monitor.get_price_history")
    @patch("stock_kb.StockKB")
    @patch("tradingagents_runner._fetch_eastmoney_guba")
    def test_dimension_missing_renormalization(self, mock_guba, mock_kb, mock_ph, mock_quote):
        """analyst + guba 缺失 → 只用 cp+ma20 重归一化，分数仍有效"""
        mock_quote.return_value = {"price": 78.2, "high": 80.0, "low": 77.5, "open": 79.0, "pre_close": 79.5}
        mock_ph.return_value = [70 + i * 0.5 for i in range(25)]
        mock_kb_inst = mock_kb.return_value
        mock_kb_inst.check_cache.return_value = {"hit": False, "report": None, "reason": "miss"}  # analyst 缺
        mock_guba.return_value = "股吧暂不可用"  # guba 缺
        r = compute("002049", 78.2)
        self.assertIsNotNone(r["score"])
        self.assertEqual(set(r["dimensions_used"]), {"cp", "ma20"})
        self.assertIn("analyst", r["dimensions_missing"])
        self.assertIn("guba", r["dimensions_missing"])
        # cp=1.0 (w=0.35) + ma20=1.0 (w=0.30) → 1.0
        self.assertAlmostEqual(r["score"], 1.0, places=2)

    @patch("market_data.fetch_quote")
    @patch("risk_monitor.get_price_history")
    @patch("stock_kb.StockKB")
    @patch("tradingagents_runner._fetch_eastmoney_guba")
    def test_ma20_insufficient_skipped_not_vetoed(self, mock_guba, mock_kb, mock_ph, mock_quote):
        """MA20 数据不足(<20根) → 跳过该维度（不否决）"""
        mock_quote.return_value = {"price": 78.2, "high": 80.0, "low": 77.5, "open": 79.0, "pre_close": 79.5}
        mock_ph.return_value = [78.0, 78.5, 79.0]  # 仅 3 根，<20
        mock_kb_inst = mock_kb.return_value
        mock_kb_inst.check_cache.return_value = {"hit": False, "report": None, "reason": "miss"}
        mock_guba.return_value = "暂不可用"
        r = compute("002049", 78.2)
        self.assertIsNotNone(r["score"])
        self.assertIn("ma20", r["dimensions_missing"])


if __name__ == "__main__":
    unittest.main()
