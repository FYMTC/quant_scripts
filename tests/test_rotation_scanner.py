"""T1.10 三期 rotation_scanner 单元测试（2026-07-04）

测试覆盖：
  - load_rotation_scan: 文件不存在/存在
  - fetch_universe_codes: 持仓+watchlist+screener_top15 去重；ST/非6位过滤
  - compute_industry_metrics: 聚合到行业级中位数；<3 成分股 → low_confidence；end_date 历史锚定
  - rank_industries: TOP3 up/down 排序；黑名单过滤
  - consult_market_regime: bear/失败 → unknown
  - scan_and_export: bear→paused；--no-backtest→skipped；正常流程写出 JSON

隔离：tempfile + patch rotation_scanner._fetch_kline / stock_screener._fetch_industry_map /
     stock_screener.is_blacklisted / market_regime.* + MagicMock 替换 rotation_scanner.cfg。
"""

import unittest
import sys
import os
import json
import tempfile
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, "/root/ai_trading_package/quant/quant_scripts")
os.chdir("/root/ai_trading_package/quant/quant_scripts")

import rotation_scanner as rs


def _make_kline(closes, volumes=None):
    """构造 K 线 list[{date, close, volume, amount}]。"""
    base = datetime(2026, 6, 1)
    if volumes is None:
        volumes = [10000.0] * len(closes)
    return [
        {"date": (base + timedelta(days=i)).strftime("%Y-%m-%d"),
         "close": c, "volume": v, "amount": c * v}
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def _mock_cfg_with_rotation_scan(tmp_path):
    """构造 MagicMock cfg，path.rotation_scan 指向 tmp_path。"""
    mock_cfg = MagicMock()
    mock_cfg.path.rotation_scan = tmp_path
    mock_cfg.path.screener_top15 = "/nonexistent/screener_top15.json"
    mock_cfg.root = "/root/ai_trading_package/quant/quant_scripts"
    mock_cfg.python = "/root/ai_trading_package/quant_env/bin/python3"
    return mock_cfg


class TestLoadRotationScan(unittest.TestCase):
    """测试 load_rotation_scan。"""

    def test_missing_file_returns_empty(self):
        """文件不存在 → {}。"""
        with patch("rotation_scanner.cfg", _mock_cfg_with_rotation_scan("/nonexistent/rotation_scan.json")):
            self.assertEqual(rs.load_rotation_scan(), {})

    def test_existing_file_returns_dict(self):
        """文件存在 → 返回 dict。"""
        fd, tmp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        data = {"status": "ok", "top3_up": [{"industry": "半导体"}]}
        with open(tmp, "w") as f:
            json.dump(data, f)
        try:
            with patch("rotation_scanner.cfg", _mock_cfg_with_rotation_scan(tmp)):
                result = rs.load_rotation_scan()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["top3_up"][0]["industry"], "半导体")
        finally:
            os.unlink(tmp)


class TestFetchUniverseCodes(unittest.TestCase):
    """测试 fetch_universe_codes。"""

    def test_dedup_from_three_sources(self):
        """持仓 + watchlist + screener_top15 去重。"""
        # 用临时目录放真实 guard_config.json + screener_top15.json，避免 patch open
        tmp_dir = tempfile.mkdtemp(prefix="rs_test_")
        gc_path = os.path.join(tmp_dir, "guard_config.json")
        top15_path = os.path.join(tmp_dir, "screener_top15.json")
        with open(gc_path, "w") as f:
            json.dump({"watch_list": {"002049": "紫光国微", "000938": "浪潮信息"}}, f)
        with open(top15_path, "w") as f:
            json.dump([{"code": "000938"}, {"code": "600487"}, {"code": "002049"}], f)

        kb_mock = MagicMock()
        kb_mock().read_portfolio_truth.return_value = {
            "positions": {"002049": {"name": "紫光国微"}, "000063": {"name": "中兴通讯"}}
        }
        mock_cfg = _mock_cfg_with_rotation_scan("/nonexistent/rotation_scan.json")
        mock_cfg.root = tmp_dir
        mock_cfg.path.screener_top15 = top15_path
        try:
            with patch("rotation_scanner.cfg", mock_cfg), \
                 patch("stock_kb.StockKB", kb_mock):
                codes = rs.fetch_universe_codes()
            # 002049 出现三次但只算一次
            self.assertIn("002049", codes)
            self.assertIn("000063", codes)
            self.assertIn("000938", codes)
            self.assertIn("600487", codes)
            self.assertEqual(len(codes), len(set(codes)), "应去重")
            for c in codes:
                self.assertEqual(len(c), 6)
        finally:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_filters_non_six_digit(self):
        """非 6 位代码过滤。"""
        kb_mock = MagicMock()
        kb_mock().read_portfolio_truth.return_value = {
            "positions": {"002049": {"name": "紫光国微"}, "123": {"name": "短代码"}, "1234567": {"name": "长代码"}}
        }
        mock_cfg = _mock_cfg_with_rotation_scan("/nonexistent/rotation_scan.json")
        with patch("rotation_scanner.cfg", mock_cfg), \
             patch("stock_kb.StockKB", kb_mock):
            codes = rs.fetch_universe_codes()
        self.assertIn("002049", codes)
        self.assertNotIn("123", codes)
        self.assertNotIn("1234567", codes)


class TestComputeIndustryMetrics(unittest.TestCase):
    """测试 compute_industry_metrics。"""

    def test_aggregates_to_industry_median(self):
        """3 只同行业股票 → 聚合为中位数。"""
        codes = ["002049", "000938", "600487"]
        industry_map = {"002049": "半导体", "000938": "半导体", "600487": "半导体"}
        # 6 个收盘价，return_5d = (closes[-1]/closes[-6]-1)*100
        klines = {
            "002049": _make_kline([10, 10.5, 11, 10.8, 11.2, 11.5]),  # 5d=+15%
            "000938": _make_kline([20, 20.4, 20.8, 21, 21.2, 21.6]),  # 5d=+8%
            "600487": _make_kline([15, 15.3, 15.6, 15.5, 15.8, 16.2]),  # 5d=+8%
        }

        def fake_fetch_kline(code, days, end_date=None):
            return klines.get(code)

        with patch("stock_screener._fetch_industry_map", return_value=industry_map), \
             patch("rotation_scanner._fetch_kline", side_effect=fake_fetch_kline):
            metrics = rs.compute_industry_metrics(codes, lookback_days=20)
        self.assertIn("半导体", metrics)
        m = metrics["半导体"]
        self.assertFalse(m.get("low_confidence", False))
        self.assertEqual(m["constituent_count"], 3)
        # 中位数 of [15, 8, 8] = 8
        self.assertEqual(m["return_5d"], 8.0)
        # top_stocks 按 5d 涨幅降序
        self.assertEqual(m["top_stocks"][0]["code"], "002049")  # 15% 最高

    def test_low_constituents_marked_low_confidence(self):
        """<3 成分股 → low_confidence: True。"""
        codes = ["002049", "000938"]
        industry_map = {"002049": "半导体", "000938": "半导体"}
        klines = {
            "002049": _make_kline([10, 10.5, 11, 10.8, 11.2, 11.5]),
            "000938": _make_kline([20, 20.4, 20.8, 21, 21.2, 21.6]),
        }

        def fake_fetch_kline(code, days, end_date=None):
            return klines.get(code)

        with patch("stock_screener._fetch_industry_map", return_value=industry_map), \
             patch("rotation_scanner._fetch_kline", side_effect=fake_fetch_kline):
            metrics = rs.compute_industry_metrics(codes, lookback_days=20)
        self.assertTrue(metrics["半导体"].get("low_confidence"))
        self.assertEqual(metrics["半导体"]["constituent_count"], 2)

    def test_end_date_anchor_passed_through(self):
        """end_date 历史锚定参数透传给 _fetch_kline。"""
        codes = ["002049", "000938", "600487"]
        industry_map = {c: "半导体" for c in codes}
        klines = {c: _make_kline([10, 10.5, 11, 10.8, 11.2, 11.5]) for c in codes}

        calls = []

        def fake_fetch_kline(code, days, end_date=None):
            calls.append((code, end_date))
            return klines.get(code)

        with patch("stock_screener._fetch_industry_map", return_value=industry_map), \
             patch("rotation_scanner._fetch_kline", side_effect=fake_fetch_kline):
            rs.compute_industry_metrics(codes, lookback_days=20, end_date="2026-05-01")
        # 每只 code 都被调用且 end_date 透传
        for code, end_date in calls:
            self.assertEqual(end_date, "2026-05-01", f"{code} 的 end_date 未透传")


class TestRankIndustries(unittest.TestCase):
    """测试 rank_industries。"""

    def test_top3_up_down_sorting(self):
        """5 行业按 return_5d 排序 → TOP3 up + TOP3 down。"""
        metrics = {
            "半导体": {"return_5d": 5.0, "return_20d": 10.0, "volume_ratio": 1.3, "constituent_count": 5, "top_stocks": []},
            "医药": {"return_5d": 3.0, "return_20d": 6.0, "volume_ratio": 1.1, "constituent_count": 5, "top_stocks": []},
            "电子": {"return_5d": 1.0, "return_20d": 2.0, "volume_ratio": 1.0, "constituent_count": 5, "top_stocks": []},
            "地产": {"return_5d": -2.0, "return_20d": -4.0, "volume_ratio": 0.9, "constituent_count": 5, "top_stocks": []},
            "银行": {"return_5d": -4.0, "return_20d": -8.0, "volume_ratio": 0.8, "constituent_count": 5, "top_stocks": []},
        }
        result = rs.rank_industries(metrics)
        up_industries = [i["industry"] for i in result["top3_up"]]
        down_industries = [i["industry"] for i in result["top3_down"]]
        self.assertEqual(up_industries, ["半导体", "医药", "电子"])
        self.assertEqual(down_industries, ["银行", "地产"])  # 末尾2个反转（最大跌幅在前）

    def test_blacklist_filtered(self):
        """黑名单行业被过滤。"""
        metrics = {
            "半导体": {"return_5d": 5.0, "constituent_count": 5, "top_stocks": []},
            "房地产": {"return_5d": -4.0, "constituent_count": 5, "top_stocks": []},  # 黑名单
        }
        with patch("stock_screener.is_blacklisted", side_effect=lambda ind: ind == "房地产"):
            result = rs.rank_industries(metrics)
        # 房地产被过滤，只有半导体
        all_ind = [i["industry"] for i in result["top3_up"]] + [i["industry"] for i in result["top3_down"]]
        self.assertIn("半导体", all_ind)
        self.assertNotIn("房地产", all_ind)


class TestConsultMarketRegime(unittest.TestCase):
    """测试 consult_market_regime。"""

    def test_bear_regime_returned(self):
        """mock fit_hmm 返回 bear → current_state: bear。"""
        with patch("market_regime.fetch_index_data", return_value=[1.0] * 100), \
             patch("market_regime.fit_hmm", return_value={"current_state": "bear", "current_probs": [0.8, 0.15, 0.05]}):
            result = rs.consult_market_regime()
        self.assertEqual(result["current_state"], "bear")
        self.assertEqual(result["current_probs"], [0.8, 0.15, 0.05])

    def test_failure_returns_unknown(self):
        """fetch_index_data 失败 → unknown。"""
        with patch("market_regime.fetch_index_data", side_effect=Exception("boom")):
            result = rs.consult_market_regime()
        self.assertEqual(result["current_state"], "unknown")


class TestScanAndExport(unittest.TestCase):
    """测试 scan_and_export 主入口。"""

    def _full_mocks(self, tmp_out, regime_state="sideways"):
        """构造 scan_and_export 全套 mock。"""
        regime = {"current_state": regime_state, "current_probs": [0.1, 0.8, 0.1]}
        universe = ["002049", "000938", "600487", "000063", "600519"]
        metrics = {
            "半导体": {"return_5d": 5.0, "return_20d": 10.0, "volume_ratio": 1.3,
                     "constituent_count": 5, "top_stocks": [{"code": "002049", "return_5d": 6.0}]},
            "地产": {"return_5d": -4.0, "return_20d": -8.0, "volume_ratio": 0.8,
                    "constituent_count": 5, "top_stocks": []},
        }
        ranking = {"top3_up": [{"industry": "半导体", "return_5d": 5.0, "top_stocks": [{"code": "002049"}]}],
                   "top3_down": [{"industry": "地产", "return_5d": -4.0}]}
        mock_cfg = _mock_cfg_with_rotation_scan(tmp_out)
        return regime, universe, metrics, ranking, mock_cfg

    def test_bear_regime_pauses(self):
        """bear 态 → status: paused + 不出 TOP3。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        regime, universe, metrics, ranking, mock_cfg = self._full_mocks(tmp_out, regime_state="bear")
        try:
            with patch("rotation_scanner.cfg", mock_cfg), \
                 patch("rotation_scanner.consult_market_regime", return_value=regime), \
                 patch("rotation_scanner.fetch_universe_codes", return_value=universe), \
                 patch("rotation_scanner.compute_industry_metrics", return_value=metrics), \
                 patch("rotation_scanner.rank_industries", return_value=ranking), \
                 patch("rotation_scanner._save_rotation_scan") as mock_save:
                result = rs.scan_and_export(run_backtest=False)
            self.assertEqual(result["status"], "paused")
            self.assertIn("bear", result.get("reason", ""))
            self.assertEqual(result["top3_up"], [])
            mock_save.assert_called_once()
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_no_backtest_skips_gate(self):
        """run_backtest=False → backtest_gate.passed: True, status: skipped。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        regime, universe, metrics, ranking, mock_cfg = self._full_mocks(tmp_out)
        try:
            with patch("rotation_scanner.cfg", mock_cfg), \
                 patch("rotation_scanner.consult_market_regime", return_value=regime), \
                 patch("rotation_scanner.fetch_universe_codes", return_value=universe), \
                 patch("rotation_scanner.compute_industry_metrics", return_value=metrics), \
                 patch("rotation_scanner.rank_industries", return_value=ranking), \
                 patch("rotation_scanner._run_backtest_gate") as mock_gate:
                result = rs.scan_and_export(run_backtest=False)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["backtest_gate"]["passed"], True)
            self.assertEqual(result["backtest_gate"]["status"], "skipped")
            mock_gate.assert_not_called()  # run_backtest=False 不调 subprocess
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)

    def test_normal_flow_writes_json(self):
        """正常流程（回测通过）→ status: ok + JSON 写到临时路径。"""
        fd, tmp_out = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(tmp_out)
        regime, universe, metrics, ranking, mock_cfg = self._full_mocks(tmp_out)
        try:
            with patch("rotation_scanner.cfg", mock_cfg), \
                 patch("rotation_scanner.consult_market_regime", return_value=regime), \
                 patch("rotation_scanner.fetch_universe_codes", return_value=universe), \
                 patch("rotation_scanner.compute_industry_metrics", return_value=metrics), \
                 patch("rotation_scanner.rank_industries", return_value=ranking), \
                 patch("rotation_scanner._run_backtest_gate",
                       return_value={"passed": True, "rolling_4wk_hit_rate": 0.75, "status": "ok"}):
                result = rs.scan_and_export(run_backtest=True)
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["backtest_gate"]["passed"])
            # JSON 文件已写出
            self.assertTrue(os.path.exists(tmp_out))
            with open(tmp_out) as f:
                saved = json.load(f)
            self.assertEqual(saved["status"], "ok")
            self.assertEqual(saved["regime"], "sideways")
        finally:
            if os.path.exists(tmp_out):
                os.unlink(tmp_out)


if __name__ == "__main__":
    unittest.main()
