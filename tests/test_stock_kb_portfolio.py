#!/config/quant_env/bin/python3
"""stock_kb portfolio --live / trade / trade-undo 测试。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stock_kb import StockKB, build_portfolio_live  # noqa: E402


class TestBuildPortfolioLive(unittest.TestCase):
    def test_live_with_mock_quotes(self):
        kb = StockKB()
        truth = kb.read_portfolio_truth()
        if not truth.get("positions"):
            self.skipTest("no positions in DB")
        code = next(iter(truth["positions"]))
        if len(code) != 6 or not code.isdigit():
            self.skipTest("first position not equity")
        mock_q = {
            code: {
                "price": 10.0,
                "pct": 1.5,
                "_source": "test",
            }
        }
        with patch("market_data.fetch_quotes_batch", return_value=mock_q):
            out = build_portfolio_live(kb, fetch_live=True)
        self.assertIn("cash", out)
        self.assertIn("positions", out)
        self.assertEqual(out["holdings_source"], "trade_log.db / stock_kb.read_portfolio_truth")
        eq = [p for p in out["positions"] if p["code"] == code][0]
        self.assertEqual(eq["price"], 10.0)
        self.assertIn("pnl", eq)


class TestTradeRoundtrip(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._db = os.path.join(self._tmpdir, "test_kb.db")
        self.kb = StockKB(db_path=self._db)
        self._orig_sync = StockKB._sync_guard_config
        self._orig_guard_path = os.environ.get("STOCK_KB_GUARD_CONFIG_PATH")
        self._orig_db_path = os.environ.get("STOCK_KB_DB_PATH")
        self._guard_path = os.path.join(self._tmpdir, "guard_config.json")
        os.environ["STOCK_KB_DB_PATH"] = self._db
        os.environ["STOCK_KB_GUARD_CONFIG_PATH"] = self._guard_path

        def _sync_to_tmp(inner):
            config = inner.export_guard_config(inner.get_cash(), existing={})
            with open(self._guard_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)

        StockKB._sync_guard_config = _sync_to_tmp
        self.kb.ensure_stock("000001", name="测试股")
        self.kb.set_cash(100000.0)
        self.kb.record_trade("000001", "BUY", 10.0, 100, rationale="seed")
        self.kb.set_cash(99000.0)

    def tearDown(self):
        StockKB._sync_guard_config = self._orig_sync
        if self._orig_db_path is None:
            os.environ.pop("STOCK_KB_DB_PATH", None)
        else:
            os.environ["STOCK_KB_DB_PATH"] = self._orig_db_path

    def test_trade_and_undo(self):
        if not hasattr(self.kb, "undo_trade"):
            self.skipTest("undo_trade missing")
        tid = self.kb.record_trade(
            "000001", "BUY", 11.0, 50, rationale="unit_test_trade"
        )
        self.kb.set_cash(self.kb.get_cash() - 550)
        after = self.kb.read_portfolio_truth()
        self.assertEqual(after["positions"]["000001"]["shares"], 150)
        undo = self.kb.undo_trade(tid)
        self.assertTrue(undo.get("ok"))
        restored = self.kb.read_portfolio_truth()
        self.assertEqual(restored["positions"]["000001"]["shares"], 100)


if __name__ == "__main__":
    unittest.main()
