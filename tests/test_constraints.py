#!python3
"""
单元测试：core/constraints.py（与 hermes-quant-software-spec v2.0 第五节用例对齐）

运行:
  cd /root/ai_trading_package/quant/quant_scripts && /root/ai_trading_package/quant_env/bin/python3 -m unittest tests.test_constraints -v
"""

import os
import sys
import unittest
from unittest.mock import patch

# 保证以仓库内 quant_scripts 为根导入 core
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.constraints import (  # noqa: E402
    ConstraintVerdict,
    check_add_position,
    check_all,
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
            check_new_position("000063", -0.12),
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
            cvar=-0.12,
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


    def test_check_all_bear_and_high_macro_are_caution_not_block(self):
        holdings = [{"code": "000001", "price": 10.0, "shares": 100}]
        quant = {"per_stock": {"000001": {"cvar": -2.0}}}
        feature_snapshot = {
            "runtime_flags": {"feature_fresh": True},
            "portfolio": {"market_regime": {"current_state": "bear"}},
        }

        class _DummyFile:
            def __enter__(self):
                from io import StringIO
                return StringIO('{"event_level":"HIGH","playbook":{"allow_new_buy":false,"message":"系统性风险偏高"}}')

            def __exit__(self, exc_type, exc, tb):
                return False

        with patch("os.path.isfile", return_value=True), patch("builtins.open", return_value=_DummyFile()):
            rows = check_all(holdings, cash=50000, total_assets=60000, quant=quant, feature_snapshot=feature_snapshot)

        row_map = {r[0]: r for r in rows}
        self.assertTrue(row_map["market_regime"][1])
        self.assertTrue(row_map["macro_event"][1])
        self.assertIn("保守", row_map["market_regime"][2])
        self.assertIn("保守", row_map["macro_event"][2])

        holdings = [
            {"code": "000001", "price": 10.0, "shares": 1000},
        ]
        quant = {"per_stock": {"000001": {"cvar": -12.0}}}  # -12% -> block (cvar_floor=-10)
        with patch("os.path.isfile", return_value=False):
            rows = check_all(holdings, cash=5000, total_assets=15000, quant=quant)
        codes = [r[0] for r in rows if not r[1]]
        self.assertIn("position_limit", codes)
        self.assertIn("cvar_block", codes)

    def test_check_all_ok(self):
        with patch("os.path.isfile", return_value=False):
            rows = check_all(
                [{"code": "000001", "price": 1.0, "shares": 100}],
                cash=50000,
                total_assets=60000,
                quant={"per_stock": {"000001": {"cvar": -2.0}}},
            )
        self.assertTrue(any(r[0] == "all" and r[1] for r in rows))


if __name__ == "__main__":
    unittest.main()
