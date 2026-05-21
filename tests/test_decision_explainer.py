#!/config/quant_env/bin/python3
"""decision_explainer 单元测试。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_explainer import (  # noqa: E402
    build_counterfactual_from_constraints,
    build_counterfactual_from_gate,
)


class TestDecisionExplainer(unittest.TestCase):
    def test_gate_counterfactual_for_missing_research(self):
        out = build_counterfactual_from_gate(
            {
                "verdict": "REJECT",
                "direction": "BUY",
                "composite_score": 0.4,
                "research_gate": {"pass": False, "message": "000063 缺少 research_features"},
                "reasons": ["[RG] 000063 缺少 research_features"],
            }
        )
        self.assertEqual(out["current_verdict"], "REJECT")
        self.assertEqual(out["target_action"], "BUY")
        self.assertTrue(any(f["rule"] == "research_features" for f in out["blocking_factors"]))
        self.assertEqual(out["next_best_action"], "HOLD")

    def test_constraints_counterfactual_for_market_regime(self):
        out = build_counterfactual_from_constraints(
            [{"check": "market_regime", "pass": False, "message": "市场状态为 bear，禁止放松新开仓门槛"}]
        )
        self.assertEqual(out["current_verdict"], "BLOCKED")
        self.assertTrue(any(f["rule"] == "market_regime" for f in out["blocking_factors"]))


if __name__ == "__main__":
    unittest.main()
