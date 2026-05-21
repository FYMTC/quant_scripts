#!/config/quant_env/bin/python3
"""signal_loop 配额与 handle_trigger 单元测试。"""

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
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

    @patch("signal_loop._is_stale", return_value=True)
    @patch("signal_loop._build_signals_for_stock")
    @patch("signal_loop._load_profile", return_value={"effective_thresholds": {}})
    @patch("signal_loop._calc_technical_levels", return_value={"ok": True})
    @patch("signal_loop._get_stock_kb_cls")
    def test_auto_generate_keeps_new_signals_even_if_stale_checker_true(
        self, kb_cls, _tech, _profile, build, _is_stale
    ):
        kb_cls.return_value.return_value.read_portfolio_truth.return_value = {
            "positions": {"000063": {"name": "中兴", "shares": 100, "cost": 38.3}}
        }
        build.side_effect = lambda code, name, tier, tech, thresholds: [{
            "id": f"{code}_sig",
            "code": code,
            "name": name,
            "type": "price_below",
            "tier": tier,
            "params": {"target": 10},
            "auto_generated": True,
        }]

        out = sl.auto_generate()
        saved = sl._load_json(self._cfg)

        self.assertEqual(out["new"], 2)
        self.assertEqual(out["deleted"], 0)
        self.assertEqual(len(saved["signals"]), 2)


class TestCloseLoop(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._cfg = os.path.join(self._tmpdir, "guard_config.json")
        sl.CONFIG_PATH = self._cfg
        with open(self._cfg, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "signals": [
                        {
                            "id": "sig_old",
                            "code": "000063",
                            "type": "price_below",
                            "params": {"target": 38.0},
                        }
                    ]
                },
                f,
            )

    def test_wait_replaces_old_signal(self):
        result = sl.close_loop(
            "000063",
            "sig_old",
            "WAIT",
            new_signal_params=[
                {
                    "id": "sig_new",
                    "code": "000063",
                    "type": "price_above",
                    "params": {"target": 40.0},
                }
            ],
        )
        self.assertEqual(result["action"], "WAIT")
        saved = sl._load_json(self._cfg)
        ids = [s["id"] for s in saved["signals"]]
        self.assertNotIn("sig_old", ids)
        self.assertIn("sig_new", ids)

    def test_buy_removes_consumed_signal(self):
        result = sl.close_loop("000063", "sig_old", "BUY")
        self.assertEqual(result["action"], "BUY")
        saved = sl._load_json(self._cfg)
        ids = [s["id"] for s in saved["signals"]]
        self.assertNotIn("sig_old", ids)


class TestTechnicalLevels(unittest.TestCase):
    def test_calc_technical_levels_queries_history_window(self):
        rows = [
            ["2026-05-01", "10", "11", "9", "10.5", "1000"],
            ["2026-05-02", "10.4", "11.2", "10", "10.8", "1200"],
            ["2026-05-05", "10.8", "11.5", "10.6", "11.1", "1300"],
            ["2026-05-06", "11.0", "11.6", "10.7", "11.3", "1400"],
            ["2026-05-07", "11.2", "11.8", "10.9", "11.4", "1500"],
        ]

        class FakeResult:
            def __init__(self, values):
                self.values = values
                self.index = -1

            def next(self):
                self.index += 1
                return self.index < len(self.values)

            def get_row_data(self):
                return self.values[self.index]

        fake_bs = SimpleNamespace(
            login=lambda: None,
            logout=lambda: None,
            query_history_k_data_plus=lambda *args, **kwargs: FakeResult(rows),
        )

        with patch.dict(sys.modules, {"baostock": fake_bs}):
            captured = {}

            def fake_query(*args, **kwargs):
                captured.update(kwargs)
                return FakeResult(rows)

            fake_bs.query_history_k_data_plus = fake_query
            with patch("signal_loop.datetime", wraps=sl.datetime) as mock_datetime:
                mock_datetime.now.return_value = sl.datetime(2026, 5, 19, 10, 0, 0)
                tech = sl._calc_technical_levels("000063")

        self.assertIsNotNone(tech)
        self.assertEqual(captured["end_date"], "2026-05-19")
        self.assertEqual(captured["start_date"], "2026-04-19")
        self.assertEqual(tech["current"], 11.4)
        self.assertEqual(tech["low_20"], 9.0)
        self.assertEqual(tech["avg_vol_5d"], 1280.0)


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
