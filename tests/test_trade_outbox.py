#!/config/quant_env/bin/python3
"""trade_outbox 单元测试。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_accounts as ta  # noqa: E402
import trade_outbox as to  # noqa: E402


class TestTradeOutbox(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        to.STATE_PATH = os.path.join(self._tmpdir, "agent_state.json")
        to.OUTBOX_PATH = os.path.join(self._tmpdir, "trade_request_pending.json")
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        self._state = os.path.join(self._tmpdir, "trade_accounts_state.json")
        with open(
            os.path.join(os.path.dirname(__file__), "..", "trade_accounts.example.yaml"),
            encoding="utf-8",
        ) as f:
            yaml_content = f.read()
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def test_propose_and_resolve(self):
        r = to.propose("000063", "BUY", name="中兴", price=38.0, shares=100, gate_summary="ok")
        self.assertTrue(r["ok"])
        self.assertIn("wechat_template", r)
        pending = to.list_pending()
        self.assertEqual(len(pending), 1)
        rid = r["request_id"]
        ok = to.resolve(rid, "resolved", note="user confirmed")
        self.assertTrue(ok["ok"])
        self.assertEqual(len(to.list_pending()), 0)

    def test_invalid_direction(self):
        r = to.propose("000063", "HOLD")
        self.assertIn("error", r)

    def test_wechat_includes_lineage(self):
        r = to.propose(
            "000938",
            "SELL",
            name="中兴",
            price=38.0,
            shares=100,
            gate_summary="宏观CRITICAL减仓",
            lineage_stages=[
                {"stage": "MACRO_ASSESS", "source": "test", "payload": {"summary": "event=CRITICAL"}},
            ],
        )
        self.assertTrue(r["ok"])
        tpl = r.get("wechat_template") or ""
        self.assertIn("追溯ID", tpl)
        self.assertIn("流程追溯", tpl)


if __name__ == "__main__":
    unittest.main()
