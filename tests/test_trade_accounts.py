#!/config/quant_env/bin/python3
"""多账户注册表与 resolve-and-execute 路由。"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_accounts as ta  # noqa: E402
import trade_outbox as to  # noqa: E402


class TestTradeAccounts(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        with open(
            os.path.join(os.path.dirname(__file__), "..", "trade_accounts.example.yaml"),
            encoding="utf-8",
        ) as f:
            content = f.read()
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write(content)
        ta.DEFAULT_PATH = self._accounts
        to.STATE_PATH = os.path.join(self._tmpdir, "agent_state.json")
        to.OUTBOX_PATH = os.path.join(self._tmpdir, "trade_request_pending.json")

    def test_default_paper_account_on_propose(self):
        r = to.propose("000001", "BUY", shares=100, price=10.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], "paper_easyths")
        tpl = r["wechat_template"]
        self.assertIn("模拟盘", tpl)

    def test_manual_account_no_auto_execute(self):
        r = to.propose("000001", "SELL", shares=100, account_id="manual_wechat")
        rid = r["request_id"]
        with patch("trade_execution.execute_request") as ex:
            out = to.resolve_and_execute(rid, "resolved")
            ex.assert_not_called()
        self.assertTrue(out.get("ok"))
        self.assertFalse(out.get("executed"))

    def test_paper_auto_execute_mocked(self):
        r = to.propose("000001", "BUY", shares=100, account_id="paper_easyths")
        rid = r["request_id"]
        fake = {"ok": True, "result": {"data": {"price": 10.0, "shares": 100}}}
        with patch("trade_execution.execute_request", return_value=fake):
            with patch("trade_notify.enqueue_wechat", return_value={"ok": True}) as w:
                out = to.resolve_and_execute(rid, "resolved")
        self.assertTrue(out.get("executed"))
        w.assert_called_once()


if __name__ == "__main__":
    unittest.main()
