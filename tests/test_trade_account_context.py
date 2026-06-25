#!python3
"""trade_account_context：只允许 EasyTHS 作为账户权威持仓源。"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_account_context as tac  # noqa: E402


class TestTradeAccountContext(unittest.TestCase):
    @patch("trade_account_context.get_account")
    def test_unsupported_position_source_returns_structured_error(self, get_account):
        get_account.return_value = {
            "account_id": "paper_easyths",
            "label": "Paper",
            "position_source": "stock_kb_guard",
        }
        snap = tac.load_account_snapshot("paper_easyths")
        self.assertEqual(snap["account_id"], "paper_easyths")
        self.assertEqual(snap["position_source"], "stock_kb_guard")
        self.assertIn("unsupported position_source", snap["error"])
        self.assertEqual(snap["positions"], [])

    @patch("trade_account_context._snapshot_from_easyths")
    @patch("trade_account_context.get_account")
    def test_easyths_position_source_uses_easyths_snapshot(self, get_account, snapshot):
        get_account.return_value = {
            "account_id": "paper_easyths",
            "label": "Paper",
            "position_source": "easyths",
        }
        snapshot.return_value = {
            "account_id": "paper_easyths",
            "account_label": "Paper",
            "position_source": "easyths",
            "positions": [{"code": "000001", "name": "平安银行", "shares": 500}],
            "position_count": 1,
        }
        snap = tac.load_account_snapshot("paper_easyths")
        self.assertEqual(snap["position_source"], "easyths")
        self.assertEqual(snap["position_count"], 1)
        snapshot.assert_called_once_with("paper_easyths", "Paper")


if __name__ == "__main__":
    unittest.main()
