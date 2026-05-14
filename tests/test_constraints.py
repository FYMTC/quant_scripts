#!/config/quant_env/bin/python3
"""
单元测试：core/constraints.py（与 hermes-quant-software-spec v2.0 第五节用例对齐）

运行:
  cd /config/quant_scripts && /config/quant_env/bin/python -m unittest tests.test_constraints -v
"""

import os
import sys
import unittest

# 保证以仓库内 quant_scripts 为根导入 core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constraints import (  # noqa: E402
    ConstraintVerdict,
    check_add_position,
    check_buy,
    check_new_position,
    check_sell,
    evaluate_buy,
    evaluate_sell,
)


class TestConstraintsSpec(unittest.TestCase):
    """规格书四个用例"""

    def test_cvar_block(self):
        self.assertEqual(
            check_new_position("000063", -0.07),
            ConstraintVerdict.BLOCKED,
        )
        self.assertEqual(
            check_new_position("000063", -0.04),
            ConstraintVerdict.ALLOWED,
        )

    def test_position_limit(self):
        self.assertEqual(
            check_add_position("000063", 0.45),
            ConstraintVerdict.BLOCKED,
        )
        self.assertEqual(
            check_add_position("000063", 0.20),
            ConstraintVerdict.ALLOWED,
        )

    def test_t1_lock(self):
        self.assertEqual(
            check_sell("000063", True),
            ConstraintVerdict.BLOCKED,
        )
        self.assertEqual(
            check_sell("000063", False),
            ConstraintVerdict.ALLOWED,
        )

    def test_funds_check(self):
        self.assertEqual(
            check_buy(1000, 5000),
            ConstraintVerdict.BLOCKED,
        )
        self.assertEqual(
            check_buy(5000, 1000),
            ConstraintVerdict.ALLOWED,
        )


class TestEvaluateBuy(unittest.TestCase):
    def test_evaluate_buy_blocked_by_cvar_and_funds(self):
        res = evaluate_buy(
            "000001",
            order_value=100,
            available_cash=1000,
            cvar=-0.08,
            current_single_ratio=0.10,
        )
        self.assertTrue(res.blocked())
        self.assertIn("CVaR", res.message)

        res2 = evaluate_buy(
            "000001",
            order_value=9999,
            available_cash=1000,
            cvar=None,
        )
        self.assertTrue(res2.blocked())
        self.assertIn("资金", res2.message)


class TestEvaluateSell(unittest.TestCase):
    def test_sell_blocked_t1(self):
        r = evaluate_sell("000063", bought_today=True)
        self.assertTrue(r.blocked())


if __name__ == "__main__":
    unittest.main()
