#!/config/quant_env/bin/python3
"""Hermes 操盘账户：启用/停用 + 隔离 propose。"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_accounts as ta  # noqa: E402
import trade_outbox as to  # noqa: E402


class TestHermesTradingControl(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        self._state = os.path.join(self._tmpdir, "trade_accounts_state.json")
        with open(
            os.path.join(os.path.dirname(__file__), "..", "trade_accounts.example.yaml"),
            encoding="utf-8",
        ) as f:
            content = f.read()
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write(content)
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state
        to.STATE_PATH = os.path.join(self._tmpdir, "agent_state.json")
        to.OUTBOX_PATH = os.path.join(self._tmpdir, "trade_request_pending.json")

    def test_stop_blocks_propose(self):
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        ta.stop_hermes_trading("paper_easyths")
        r = to.propose("000001", "BUY", shares=100, price=10.0)
        self.assertIn("error", r)
        self.assertTrue(r.get("hermes_trading_stopped"))

    def test_propose_uses_active_primary(self):
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        r = to.propose("000001", "BUY", shares=100, price=10.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], "paper_easyths")

    def test_manual_requires_explicit_start(self):
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        r = to.propose("000001", "SELL", shares=100, account_id="manual_wechat")
        self.assertIn("error", r)

    def test_start_stop_primary(self):
        ta.stop_hermes_trading("paper_easyths")
        ta.start_hermes_trading("manual_wechat", set_primary=True)
        self.assertEqual(ta.resolve_trading_account(), "manual_wechat")
        ta.stop_hermes_trading("manual_wechat")
        with self.assertRaises(ta.HermesTradingError):
            ta.resolve_trading_account()

    @patch("trade_execution.execute_request")
    @patch("trade_notify.enqueue_wechat", return_value={"ok": True})
    def test_paper_resolve_when_active(self, _w, ex):
        ex.return_value = {"ok": True, "result": {"data": {}}}
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        r = to.propose("000001", "BUY", shares=100, account_id="paper_easyths")
        out = to.resolve_and_execute(r["request_id"], "resolved")
        self.assertTrue(out.get("executed"))
        ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
