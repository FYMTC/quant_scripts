#!python3
"""策略注册与夜间评审测试。"""

import json
import os
import shutil
import tempfile
import unittest

import strategy_registry as sr


class TestStrategyRegistry(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._strategy_dir = os.path.join(self._tmpdir, "strategies")
        os.makedirs(self._strategy_dir, exist_ok=True)
        self._registry_path = os.path.join(self._tmpdir, "strategy_registry.json")
        self._review_path = os.path.join(self._tmpdir, "strategy_night_review.json")
        self._night_output_path = os.path.join(self._tmpdir, "night_output.json")
        with open(os.path.join(self._strategy_dir, "mean-reversion.md"), "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "title: Mean Reversion\n"
                "type: strategy\n"
                "strategy_id: mean-reversion-backtest\n"
                "status: active\n"
                "review_mode: nightly\n"
                "selector_ref: selectors.mean_reversion_v1\n"
                "symbols_scope: watchlist\n"
                "holding_period: swing_1_5d\n"
                "risk_profile: medium\n"
                "version: v3\n"
                "---\n"
                "# demo\n"
            )

    def tearDown(self):
        shutil.rmtree(self._tmpdir)

    def test_scan_strategy_docs_parses_required_fields(self):
        rows = sr.scan_strategy_docs(self._strategy_dir)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy_id"], "mean-reversion-backtest")
        self.assertEqual(row["selector_ref"], "selectors.mean_reversion_v1")
        self.assertEqual(row["review_mode"], "nightly")

    def test_build_registry_writes_json_projection(self):
        out = sr.build_registry(self._strategy_dir, self._registry_path)
        self.assertEqual(out["strategy_count"], 1)
        self.assertTrue(os.path.isfile(self._registry_path))
        with open(self._registry_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["strategies"][0]["strategy_id"], "mean-reversion-backtest")

    def test_selector_returns_fixed_contract(self):
        out = sr.selector_mean_reversion_backtest(
            "000938",
            snapshot={"current_price": 30.5},
            features={"day_return": -3.8, "close_pos": 0.2, "close_above_ma20": True},
        )
        self.assertEqual(sorted(out.keys()), ["evidence", "passed", "reason", "score"])
        self.assertTrue(out["passed"])
        self.assertGreater(out["score"], 0)

    def test_route_strategy_uses_selector_ref(self):
        registry = sr.build_registry(self._strategy_dir, self._registry_path)
        strategy = registry["strategies"][0]
        out = sr.route_strategy(
            strategy,
            "000938",
            snapshot={"current_price": 30.5},
            features={"day_return": -3.8, "close_pos": 0.2, "close_above_ma20": True},
        )
        self.assertTrue(out["passed"])
        self.assertEqual(out["strategy_id"], "mean-reversion-backtest")

    def test_route_strategy_returns_rejection_reason(self):
        registry = sr.build_registry(self._strategy_dir, self._registry_path)
        strategy = registry["strategies"][0]
        out = sr.route_strategy(
            strategy,
            "000938",
            snapshot={"current_price": 30.5},
            features={"day_return": -1.0, "close_pos": 0.8, "close_above_ma20": False},
        )
        self.assertFalse(out["passed"])
        self.assertEqual(out["reason"], "criteria_not_met")
        self.assertIn("evidence", out)

    def test_nightly_review_writes_review_and_night_output(self):
        registry = sr.build_registry(self._strategy_dir, self._registry_path)
        out = sr.nightly_review(
            registry=registry,
            review_path=self._review_path,
            night_output_path=self._night_output_path,
            signal_audit={"entries_count": 3},
            feature_snapshot={"runtime_flags": {"feature_fresh": True}},
            trade_log={"ok": True},
            stock_kb={"ok": True},
        )
        self.assertEqual(out["active_strategy_count"], 1)
        self.assertEqual(out["reports"][0]["decision"], "optimize")
        self.assertTrue(os.path.isfile(self._review_path))
        self.assertTrue(os.path.isfile(self._night_output_path))
        with open(self._night_output_path, encoding="utf-8") as f:
            night = json.load(f)
        self.assertIn("strategy_review", night)
        self.assertEqual(night["strategy_review"][0]["strategy_id"], "mean-reversion-backtest")

    def test_missing_required_field_raises(self):
        bad = os.path.join(self._strategy_dir, "bad.md")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("---\ntitle: bad\ntype: strategy\nstatus: active\n---\n")
        with self.assertRaises(ValueError):
            sr.scan_strategy_docs(self._strategy_dir)


if __name__ == "__main__":
    unittest.main()
