"""T1.10 三期 backtest_rotation 单元测试（2026-07-04）

测试覆盖：
  - recompute_weekly_rotation: 正常返回 TOP3；universe 不足 → insufficient
  - evaluate_week_hit: up>down&up>0→hit；up<down→miss；无数据→insufficient
  - run_backtest: 12 周全 hit→passed；全 miss→failed；边界 0.50→passed；weeks<8→insufficient_data；原子写
  - cli: --json --weeks 4 入口

隔离：tempfile + patch backtest_rotation.recompute_weekly_rotation / evaluate_week_hit
     （避免 baostock 真实调用）+ MagicMock 替换 backtest_rotation.cfg。
"""

import unittest
import sys
import os
import json
import tempfile
import io
from contextlib import redirect_stdout
from unittest.mock import patch, MagicMock

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

import backtest_rotation as br


def _mock_cfg_with_backtest_output(tmp_path):
    """构造 MagicMock cfg，path.backtest_rotation 指向 tmp_path。"""
    mock_cfg = MagicMock()
    mock_cfg.path.backtest_rotation = tmp_path
    mock_cfg.root = "/root/ai_trading_package/quant/quant_scripts"
    return mock_cfg


class TestRecomputeWeeklyRotation(unittest.TestCase):
    """测试 recompute_weekly_rotation。"""

    def test_returns_top3_when_universe_sufficient(self):
        """universe ≥5 → 正常返回 TOP3。"""
        ranking = {"top3_up": [{"industry": "半导体", "return_5d": 5.0}],
                   "top3_down": [{"industry": "地产", "return_5d": -4.0}]}
        with patch("rotation_scanner.fetch_universe_codes",
                   return_value=["002049", "000938", "600487", "000063", "600519"]), \
             patch("rotation_scanner.compute_industry_metrics", return_value={"半导体": {}}), \
             patch("rotation_scanner.rank_industries", return_value=ranking):
            result = br.recompute_weekly_rotation("2026-06-27")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["week"], "2026-06-27")
        self.assertEqual(result["universe_size"], 5)
        self.assertEqual(len(result["top3_up"]), 1)
        self.assertEqual(result["top3_up"][0]["industry"], "半导体")

    def test_insufficient_universe(self):
        """universe <5 → status: insufficient_universe。"""
        with patch("rotation_scanner.fetch_universe_codes", return_value=["002049", "000938"]):
            result = br.recompute_weekly_rotation("2026-06-27")
        self.assertEqual(result["status"], "insufficient_universe")
        self.assertEqual(result["top3_up"], [])
        self.assertEqual(result["top3_down"], [])


class TestEvaluateWeekHit(unittest.TestCase):
    """测试 evaluate_week_hit。"""

    def test_up_gt_down_and_up_positive_hits(self):
        """up_avg > down_avg 且 up_avg > 0 → hit=True。"""
        signal = {"status": "ok",
                  "top3_up": [{"industry": "半导体", "top_stocks": [{"code": "002049"}]}],
                  "top3_down": [{"industry": "地产", "top_stocks": [{"code": "000063"}]}]}
        # _fetch_forward_returns 返回 (entry, exit, pct)
        with patch("backtest_rotation._fetch_forward_returns",
                   side_effect=lambda code, start, days=5: (10.0, 10.5, 5.0) if code == "002049" else (10.0, 9.5, -5.0)):
            result = br.evaluate_week_hit("2026-06-27", signal)
        self.assertTrue(result["hit"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["top3_up_ret"], 5.0)
        self.assertEqual(result["top3_down_ret"], -5.0)

    def test_up_lt_down_misses(self):
        """up_avg < down_avg → hit=False。"""
        signal = {"status": "ok",
                  "top3_up": [{"industry": "半导体", "top_stocks": [{"code": "002049"}]}],
                  "top3_down": [{"industry": "地产", "top_stocks": [{"code": "000063"}]}]}
        with patch("backtest_rotation._fetch_forward_returns",
                   side_effect=lambda code, start, days=5: (10.0, 9.8, -2.0) if code == "002049" else (10.0, 10.5, 5.0)):
            result = br.evaluate_week_hit("2026-06-27", signal)
        self.assertFalse(result["hit"])
        self.assertEqual(result["top3_up_ret"], -2.0)
        self.assertEqual(result["top3_down_ret"], 5.0)

    def test_no_data_returns_insufficient(self):
        """无数据 → hit=False, status=insufficient。"""
        signal = {"status": "ok",
                  "top3_up": [{"industry": "半导体", "top_stocks": [{"code": "002049"}]}],
                  "top3_down": [{"industry": "地产", "top_stocks": [{"code": "000063"}]}]}
        # _fetch_forward_returns 全返回 None
        with patch("backtest_rotation._fetch_forward_returns", return_value=None):
            result = br.evaluate_week_hit("2026-06-27", signal)
        self.assertFalse(result["hit"])
        self.assertEqual(result["status"], "insufficient")

    def test_signal_not_ok_returns_insufficient(self):
        """signal.status != ok → insufficient。"""
        signal = {"status": "insufficient_universe", "top3_up": [], "top3_down": []}
        result = br.evaluate_week_hit("2026-06-27", signal)
        self.assertFalse(result["hit"])
        self.assertEqual(result["status"], "insufficient")


class TestRunBacktest(unittest.TestCase):
    """测试 run_backtest。"""

    def _run_with_hits(self, weeks, hit_sequence, tmp_out):
        """用受控 hit 序列跑 run_backtest。hit_sequence 按 far→near 顺序。"""
        mock_cfg = _mock_cfg_with_backtest_output(tmp_out)
        # recompute 返回 ok signal；evaluate 按 side_effect 返回 hit
        recompute_result = {"status": "ok", "week": "x", "top3_up": [], "top3_down": []}
        eval_results = [{"week": "x", "hit": h, "status": "ok",
                         "top3_up_ret": 1.0 if h else -1.0, "top3_down_ret": 0.0} for h in hit_sequence]
        with patch("backtest_rotation.cfg", mock_cfg), \
             patch("backtest_rotation.recompute_weekly_rotation", return_value=recompute_result), \
             patch("backtest_rotation.evaluate_week_hit", side_effect=eval_results):
            return br.run_backtest(weeks=weeks)

    def test_all_hits_passes(self):
        """12 周全 hit → rolling_4wk=1.0 → passed=True。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        try:
            result = self._run_with_hits(12, [True] * 12, tmp_out)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["rolling_4wk_hit_rate"], 1.0)
            self.assertTrue(result["passed"])
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_all_misses_fails(self):
        """12 周全 miss → rolling_4wk=0.0 → passed=False。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        try:
            result = self._run_with_hits(12, [False] * 12, tmp_out)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["rolling_4wk_hit_rate"], 0.0)
            self.assertFalse(result["passed"])
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_boundary_050_passes(self):
        """rolling_4wk=0.50（≥阈值）→ passed=True。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        # far→near: 前8个任意，近4个 = [T,T,F,F] → 2/4 = 0.5
        hits = [False] * 8 + [True, True, False, False]
        try:
            result = self._run_with_hits(12, hits, tmp_out)
            self.assertEqual(result["rolling_4wk_hit_rate"], 0.5)
            self.assertTrue(result["passed"], "0.50 ≥ 0.50 应通过")
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_insufficient_weeks(self):
        """weeks=7 (<8) → status=insufficient_data, passed=None。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        try:
            result = self._run_with_hits(7, [True] * 7, tmp_out)
            self.assertEqual(result["status"], "insufficient_data")
            self.assertIsNone(result["passed"])
            self.assertIn("reason", result)
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_atomic_write(self):
        """输出文件原子写入正确。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        try:
            result = self._run_with_hits(8, [True] * 8, tmp_out)
            self.assertTrue(os.path.exists(tmp_out))
            with open(tmp_out) as f:
                saved = json.load(f)
            self.assertEqual(saved["status"], "ok")
            self.assertEqual(saved["weeks_back"], 8)
            self.assertEqual(len(saved["weekly_hits"]), 8)
            self.assertIn("rolling_4wk_hit_rate", saved)
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)


class TestCli(unittest.TestCase):
    """测试 cli 入口。"""

    def test_json_weeks_flag(self):
        """`--json --weeks 4` 输出 JSON，调 run_backtest(weeks=4)。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        mock_cfg = _mock_cfg_with_backtest_output(tmp_out)
        dummy_result = {"run_at": "x", "weeks_back": 4, "weekly_hits": [],
                        "rolling_4wk_hit_rate": None, "passed": None,
                        "threshold": 0.5, "status": "insufficient_data", "reason": "test"}
        old_argv = sys.argv
        sys.argv = ["backtest_rotation.py", "--json", "--weeks", "4"]
        try:
            buf = io.StringIO()
            with redirect_stdout(buf), \
                 patch("backtest_rotation.cfg", mock_cfg), \
                 patch("backtest_rotation.run_backtest", return_value=dummy_result) as mock_run:
                br.cli()
            output = buf.getvalue()
            mock_run.assert_called_once_with(weeks=4)
            parsed = json.loads(output)
            self.assertEqual(parsed["weeks_back"], 4)
            self.assertEqual(parsed["status"], "insufficient_data")
        finally:
            sys.argv = old_argv
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)


if __name__ == "__main__":
    unittest.main()
