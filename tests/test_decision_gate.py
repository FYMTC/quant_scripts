#!/config/quant_env/bin/python3
"""decision_gate Gate1/Gate2 单元测试（无 subprocess 风控）。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_gate import DEFAULT_WEIGHTS, DecisionGate  # noqa: E402


class TestGateScoreMapping(unittest.TestCase):
    def setUp(self):
        self.gate = DecisionGate()

    def test_strong_buy_mapping(self):
        g1 = self.gate._gate_score_mapping(
            {"technical": 1.5, "news": 1.2, "sentiment": 0.8, "fundamental": 0.9},
            DEFAULT_WEIGHTS,
        )
        self.assertTrue(g1["pass"])
        self.assertIn(g1["mapped_action"], ("BUY", "OVERWEIGHT"))

    def test_neutral_maps_hold(self):
        g1 = self.gate._gate_score_mapping(
            {"technical": 0.1, "news": -0.1, "sentiment": 0.0, "fundamental": 0.05},
            DEFAULT_WEIGHTS,
        )
        self.assertTrue(g1["pass"])
        self.assertEqual(g1["mapped_action"], "HOLD")

    def test_empty_scores_fail(self):
        g1 = self.gate._gate_score_mapping({}, DEFAULT_WEIGHTS)
        self.assertFalse(g1["pass"])


class TestGateT1(unittest.TestCase):
    def setUp(self):
        self.gate = DecisionGate()

    def test_buy_skips_t1(self):
        g2 = self.gate._gate_t1_check("000063", "BUY")
        self.assertTrue(g2["pass"])


class TestGateResearch(unittest.TestCase):
    def setUp(self):
        self.gate = DecisionGate()

    def test_buy_rejects_without_research_features(self):
        out = self.gate.check(
            ticker="000063",
            direction="BUY",
            analyst_scores={"technical": 1.2, "news": 1.0},
            current_price=38.0,
            research_features=None,
        )
        self.assertIn(out["verdict"], ("REJECT", "MODIFY"))
        self.assertTrue(any("[RG]" in r for r in out["reasons"]))

    def test_buy_rejects_danger_research(self):
        out = self.gate._gate_research_features(
            "000063",
            "BUY",
            {"feature_fresh": True, "risk_level": "danger", "market_regime": "sideways", "cvar": -3.0},
        )
        self.assertFalse(out["pass"])
        self.assertIn("danger", out["message"])


if __name__ == "__main__":
    unittest.main()
