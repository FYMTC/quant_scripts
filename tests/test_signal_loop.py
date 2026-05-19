#!/config/quant_env/bin/python3
"""signal_loop 配额与 handle_trigger 单元测试。"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import signal_loop as sl  # noqa: E402


class TestDailyQuota(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._cfg = os.path.join(self._tmpdir, "guard_config.json")
        sl.CONFIG_PATH = self._cfg
        with open(self._cfg, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "monitored_codes": {"000001": "平安", "600519": "茅台"},
                },
                f,
            )

    @patch("signal_loop._get_stock_kb_cls")
    def test_quota_global_limit(self, kb_cls):
        kb_cls.return_value.return_value.read_portfolio_truth.return_value = {
            "positions": {"000063": {"name": "中兴", "shares": 100, "cost": 38.3}}
        }
        q = sl.get_daily_quota()
        self.assertEqual(len(q["tier_a"]), 1)
        self.assertGreaterEqual(q["global_limit"], 2)
        self.assertIn("lunch_blackout", q)

    @patch("signal_loop._get_stock_kb_cls")
    def test_quota_uses_monitored_codes_when_watch_list_missing(self, kb_cls):
        kb_cls.return_value.return_value.read_portfolio_truth.return_value = {
            "positions": {"000063": {"name": "中兴", "shares": 100, "cost": 38.3}}
        }
        q = sl.get_daily_quota()
        self.assertIn("000063", q["tier_a"])
        self.assertIn("000001", q["tier_b"] + q["tier_c"])


class TestAutoGenerateScope(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._cfg = os.path.join(self._tmpdir, "guard_config.json")
        sl.CONFIG_PATH = self._cfg
        with open(self._cfg, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "monitored_codes": {"000001": "平安"},
                    "signals": [],
                },
                f,
            )

    @patch("signal_loop._build_signals_for_stock", return_value=[])
    @patch("signal_loop._load_profile", return_value={"effective_thresholds": {}})
    @patch("signal_loop._calc_technical_levels", return_value={"ok": True})
    @patch("signal_loop._get_stock_kb_cls")
    def test_auto_generate_uses_db_positions(self, kb_cls, _tech, _profile, _build):
        kb_cls.return_value.return_value.read_portfolio_truth.return_value = {
            "positions": {"000063": {"name": "中兴", "shares": 100, "cost": 38.3}}
        }
        out = sl.auto_generate()
        self.assertEqual(out["stocks_processed"], 2)
        self.assertEqual(out["errors"], [])


class TestHandleTrigger(unittest.TestCase):
    @patch("signal_loop.check_quota", return_value=(False, "配额已满"))
    @patch("signal_loop._audit_log")
    def test_skip_when_quota_full(self, _audit, _quota):
        r = sl.handle_trigger("sig1", "000063", 38.0, 2.0, 1e6)
        self.assertEqual(r["action"], "SKIP")
        self.assertIn("配额", r["reason"])

    @patch("signal_loop.check_quota", return_value=(True, "A级配额可用"))
    @patch("signal_loop._is_t1_locked", return_value=True)
    @patch("signal_loop._audit_log")
    def test_skip_when_t1_locked(self, _audit, _t1, _quota):
        r = sl.handle_trigger("sig1", "000063", 38.0, 2.0, 1e6)
        self.assertEqual(r["action"], "SKIP")
        self.assertIn("T+1", r["reason"])

    @patch("signal_loop.check_quota", return_value=(True, "ok"))
    @patch("signal_loop._is_t1_locked", return_value=False)
    @patch("signal_loop._check_analyst_cache", return_value=None)
    @patch("signal_loop._audit_log")
    def test_analyze_when_passes_filters(self, _audit, _cache, _t1, _quota):
        r = sl.handle_trigger("sig1", "000063", 38.0, 2.0, 1e6)
        self.assertEqual(r["action"], "ANALYZE")


if __name__ == "__main__":
    unittest.main()
