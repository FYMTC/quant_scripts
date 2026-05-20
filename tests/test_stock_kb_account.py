#!/config/quant_env/bin/python3
"""stock_kb：模拟盘 audit_only 不污染标的持仓字段。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_kb import StockKB  # noqa: E402


class TestStockKbAccount(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "kb.db")
        self._guard_path = os.path.join(self._tmpdir, "guard_config.json")
        self._orig_guard_path = os.environ.get("STOCK_KB_GUARD_CONFIG_PATH")
        os.environ["STOCK_KB_GUARD_CONFIG_PATH"] = self._guard_path
        self.kb = StockKB(db_path=self._db)
        self.kb.ensure_stock("000001", "平安")

    def tearDown(self):
        if self._orig_guard_path is None:
            os.environ.pop("STOCK_KB_GUARD_CONFIG_PATH", None)
        else:
            os.environ["STOCK_KB_GUARD_CONFIG_PATH"] = self._orig_guard_path

    def test_audit_only_no_position_update(self):
        self.kb.record_trade(
            "000001", "BUY", 10.0, 100,
            account_id="paper_easyths",
            update_symbol_book=False,
        )
        row = self.kb.get_stock("000001")
        self.assertEqual(row["current_shares"], 0)

    def test_symbol_book_updates_position(self):
        self.kb.record_trade(
            "000001", "BUY", 10.0, 100,
            account_id="manual_wechat",
            update_symbol_book=True,
        )
        row = self.kb.get_stock("000001")
        self.assertEqual(row["current_shares"], 100)


if __name__ == "__main__":
    unittest.main()
