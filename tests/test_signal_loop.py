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
                    "positions": {"000063": "中兴"},
                    "watch_list": {"000001": "平安", "600519": "茅台"},
                },
                f,
            )

    def test_quota_global_limit(self):
        q = sl.get_daily_quota()
        self.assertEqual(len(q["tier_a"]), 1)
        self.assertGreaterEqual(q["global_limit"], 2)
        self.assertIn("lunch_blackout", q)


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
