#!/config/quant_env/bin/python3
"""trade_outbox 单元测试。"""

import json
import os
import sys
import tempfile
import types
import unittest

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda s: json.loads(json.dumps({}))))
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
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write("accounts: {}\n")
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state

        self._orig_load_registry = ta.load_registry
        self._orig_get_account = ta.get_account
        self._orig_resolve = ta.resolve_trading_account
        ta.load_registry = lambda path=None: {
            "version": 2,
            "accounts": {
                "paper_easyths": {
                    "enabled": True,
                    "label": "Paper",
                    "position_source": "easyths",
                    "execution": {"provider": "mock"},
                }
            },
            "initial_hermes_trading_active": ["paper_easyths"],
            "initial_desk_primary_account": "paper_easyths",
        }
        ta.get_account = lambda account_id: {
            "account_id": account_id,
            "enabled": True,
            "label": "Paper",
            "position_source": "easyths",
            "execution": {"provider": "mock"},
            "hermes_trading_active": True,
        }
        ta.resolve_trading_account = lambda explicit=None: explicit or "paper_easyths"
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def tearDown(self):
        ta.load_registry = self._orig_load_registry
        ta.get_account = self._orig_get_account
        ta.resolve_trading_account = self._orig_resolve

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

    def test_sell_proposal_binds_account_and_summary(self):
        r = to.propose(
            "002475",
            "SELL",
            name="立讯",
            price=69.99,
            shares=600,
            gate_verdict="APPROVE",
            gate_summary="风险事件强制减仓请示: 7日累计-10.1%，连跌6天",
            account_id="paper_easyths",
            signal_id="rolling_decline",
        )
        self.assertTrue(r["ok"])
        pending = to.list_pending()
        self.assertEqual(len(pending), 1)
        row = pending[0]
        self.assertEqual(row["account_id"], "paper_easyths")
        self.assertEqual(row["direction"], "SELL")
        self.assertEqual(row["shares"], 600)
        self.assertIn("风险事件强制减仓请示", row["gate_summary"])


if __name__ == "__main__":
    unittest.main()
