#!python3

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import strategy_validation as sv  # noqa: E402


class TestStrategyValidation(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_data = sv.DATA
        self._orig_plan = sv.PLAN_BUNDLE_PATH
        self._orig_db = sv.VALIDATION_DB_PATH
        self._orig_feature = sv.FEATURE_SNAPSHOT_PATH
        sv.DATA = self._tmpdir
        sv.PLAN_BUNDLE_PATH = os.path.join(self._tmpdir, "plan_bundle.json")
        sv.VALIDATION_DB_PATH = os.path.join(self._tmpdir, "strategy_validation.db")
        sv.FEATURE_SNAPSHOT_PATH = os.path.join(self._tmpdir, "feature_snapshot.json")

    def tearDown(self):
        sv.DATA = self._orig_data
        sv.PLAN_BUNDLE_PATH = self._orig_plan
        sv.VALIDATION_DB_PATH = self._orig_db
        sv.FEATURE_SNAPSHOT_PATH = self._orig_feature

    def test_record_plan_candidates_tracks_missing_feature_and_blocks(self):
        bundle = {
            "generated_at": "2026-05-27T08:30:00",
            "recommendation": "BLOCKED",
            "constraints": [{"pass": False, "message": "market_regime=bear"}],
            "candidates_top": [
                {"code": "000063", "name": "中兴通讯", "composite_score": 1.6, "price": 28.5},
                {"code": "300408", "name": "三环集团", "composite_score": 1.4, "price": 115.0},
            ],
        }
        feature_snapshot = {
            "portfolio": {"market_regime": {"current_state": "bear"}},
            "per_stock": {"000063": {"risk_level": "medium", "cvar": -5.6}},
        }
        out = sv.record_plan_candidates(bundle, feature_snapshot)
        self.assertTrue(out["ok"])
        summary = sv.summarize_validation(trade_date="2026-05-27")
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["missing_feature_count"], 1)

    def test_evaluate_previous_candidates_counts_blocked_positive(self):
        bundle = {
            "generated_at": "2026-05-26T08:30:00",
            "recommendation": "BLOCKED",
            "constraints": [{"pass": False, "message": "macro risk"}],
            "candidates_top": [
                {"code": "000063", "name": "中兴通讯", "composite_score": 1.6, "price": 10.0},
            ],
        }
        feature_snapshot = {"portfolio": {"market_regime": {"current_state": "bear"}}, "per_stock": {}}
        sv.record_plan_candidates(bundle, feature_snapshot)
        with patch.object(sv, "_fetch_day_bars", return_value={"000063": {"open": 10.5, "close": 10.8}}):
            out = sv.evaluate_previous_candidates(current_trade_date="2026-05-27")
        self.assertTrue(out["ok"])
        summary = out["summary"]
        self.assertEqual(summary["blocked_positive_count"], 1)
        self.assertEqual(summary["hit_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
