#!python3
"""Strategy night bridge tests."""

import json
import os
import shutil
import tempfile
import unittest

import strategy_night_bridge as snb
import strategy_registry as sr
import strategy_validation as sv


class TestStrategyNightBridge(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_data = snb.DATA
        self._orig_night_output = snb.NIGHT_OUTPUT_PATH
        self._orig_review_bundle = snb.REVIEW_BUNDLE_PATH
        self._orig_feature_snapshot = snb.FEATURE_SNAPSHOT_PATH
        self._orig_signal_audit = snb.SIGNAL_AUDIT_PATH
        self._orig_trade_log = snb.TRADE_LOG_PATH
        self._orig_strategy_review = snb.STRATEGY_REVIEW_PATH
        snb.DATA = self._tmpdir
        snb.NIGHT_OUTPUT_PATH = os.path.join(self._tmpdir, "night_output.json")
        snb.REVIEW_BUNDLE_PATH = os.path.join(self._tmpdir, "review_bundle.json")
        snb.FEATURE_SNAPSHOT_PATH = os.path.join(self._tmpdir, "feature_snapshot.json")
        snb.SIGNAL_AUDIT_PATH = os.path.join(self._tmpdir, "signal_audit.jsonl")
        snb.TRADE_LOG_PATH = os.path.join(self._tmpdir, "trade_log.db")
        snb.STRATEGY_REVIEW_PATH = os.path.join(self._tmpdir, "strategy_night_review.json")
        self._registry = {
            "generated_at": "2026-05-22T00:00:00",
            "strategy_count": 1,
            "strategies": [{
                "strategy_id": "mean-reversion-backtest",
                "title": "Mean Reversion",
                "path": "/tmp/mean-reversion.md",
                "status": "active",
                "review_mode": "nightly",
                "selector_ref": "selectors.mean_reversion_v1",
                "symbols_scope": "watchlist",
                "holding_period": "swing_1_5d",
                "risk_profile": "medium",
                "version": "v3",
                "type": "strategy",
            }],
        }
        self._orig_load_registry = sr.load_registry
        self._orig_eval_previous = sv.evaluate_previous_candidates
        self._orig_summarize = sv.summarize_validation
        sr.load_registry = lambda registry_path=sr.REGISTRY_PATH: self._registry
        sv.evaluate_previous_candidates = lambda current_trade_date=None: {
            "ok": True,
            "summary": {
                "hit_rate": 0.5,
                "avg_return_pct_open": 1.2,
                "blocked_positive_count": 2,
                "missing_feature_count": 1,
            },
        }
        sv.summarize_validation = lambda conn=None, trade_date=None: {
            "hit_rate": 0.5,
            "avg_return_pct_open": 1.2,
            "blocked_positive_count": 2,
            "missing_feature_count": 1,
        }

    def tearDown(self):
        sr.load_registry = self._orig_load_registry
        sv.evaluate_previous_candidates = self._orig_eval_previous
        sv.summarize_validation = self._orig_summarize
        snb.DATA = self._orig_data
        snb.NIGHT_OUTPUT_PATH = self._orig_night_output
        snb.REVIEW_BUNDLE_PATH = self._orig_review_bundle
        snb.FEATURE_SNAPSHOT_PATH = self._orig_feature_snapshot
        snb.SIGNAL_AUDIT_PATH = self._orig_signal_audit
        snb.TRADE_LOG_PATH = self._orig_trade_log
        snb.STRATEGY_REVIEW_PATH = self._orig_strategy_review
        shutil.rmtree(self._tmpdir)

    def test_build_strategy_night_output_includes_strategy_review(self):
        with open(snb.FEATURE_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump({"runtime_flags": {"feature_fresh": True}}, f)
        out = snb.build_strategy_night_output()
        self.assertIn("strategy_review", out)
        self.assertIn("strategy_validation", out)
        self.assertEqual(out["strategy_review"][0]["strategy_id"], "mean-reversion-backtest")
        self.assertEqual(out["strategy_review"][0]["decision"], "fix_feature_coverage")
        self.assertTrue(os.path.isfile(snb.NIGHT_OUTPUT_PATH))

    def test_strategy_review_produces_next_day_plan(self):
        with open(snb.FEATURE_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump({"runtime_flags": {"feature_fresh": True}}, f)
        out = sr.nightly_review(
            registry=self._registry,
            review_path=snb.STRATEGY_REVIEW_PATH,
            night_output_path=snb.NIGHT_OUTPUT_PATH,
            signal_audit={"entries_count": 2},
            feature_snapshot={"runtime_flags": {"feature_fresh": True}},
            strategy_validation={
                "hit_rate": 0.6,
                "avg_return_pct_open": 1.1,
                "blocked_positive_count": 1,
                "missing_feature_count": 0,
            },
        )
        self.assertEqual(out["reports"][0]["decision"], "relax_gate_candidate")
        self.assertIn("next_day_plan", out["reports"][0])
        self.assertIn("validation_summary", out["reports"][0])

    def test_load_signal_audit_reads_jsonl_entries(self):
        with open(snb.SIGNAL_AUDIT_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps({"symbol": "000001"}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"symbol": "000063"}, ensure_ascii=False) + "\n")
        out = snb._load_signal_audit(snb.SIGNAL_AUDIT_PATH)
        self.assertEqual(out["entries_count"], 2)
        self.assertEqual(out["entries"][-1]["symbol"], "000063")


if __name__ == "__main__":
    unittest.main()
