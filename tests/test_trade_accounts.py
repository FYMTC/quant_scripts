#!/config/quant_env/bin/python3
"""Hermes 操盘账户：启用/停用 + EasyTHS 交易源隔离。"""

import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda s: json.loads(json.dumps({}))))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_accounts as ta  # noqa: E402
import trade_outbox as to  # noqa: E402


class TestHermesTradingControl(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        self._state = os.path.join(self._tmpdir, "trade_accounts_state.json")
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write("accounts: {}\n")
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state
        to.STATE_PATH = os.path.join(self._tmpdir, "agent_state.json")
        to.OUTBOX_PATH = os.path.join(self._tmpdir, "trade_request_pending.json")

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
                    "execution": {"provider": "easyths", "auto_execute_on_resolve": True, "easyths_config": "/tmp/mock.yaml"},
                    "wechat": {"on_execution_result": True},
                },
            },
            "initial_hermes_trading_active": ["paper_easyths"],
            "initial_desk_primary_account": "paper_easyths",
            "wechat": {"default_chat_id": "wx-test"},
        }
        ta.get_account = lambda account_id: {
            "paper_easyths": {
                "account_id": "paper_easyths",
                "enabled": True,
                "label": "Paper",
                "position_source": "easyths",
                "execution": {"provider": "easyths", "auto_execute_on_resolve": True, "easyths_config": "/tmp/mock.yaml"},
                "wechat": {"on_execution_result": True},
                "hermes_trading_active": account_id in (ta.hermes_trading_active() or []),
            },
        }[account_id]
        ta.resolve_trading_account = self._orig_resolve

    def tearDown(self):
        ta.load_registry = self._orig_load_registry
        ta.get_account = self._orig_get_account
        ta.resolve_trading_account = self._orig_resolve

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

    def test_start_stop_primary(self):
        ta.stop_hermes_trading("paper_easyths")
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        self.assertEqual(ta.resolve_trading_account(), "paper_easyths")
        ta.stop_hermes_trading("paper_easyths")
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

    @patch("trade_notify._send_via_native_weixin", return_value={"ok": True, "success": True})
    def test_enqueue_prefers_native_weixin(self, native_send):
        import trade_notify

        old_notify_mode = trade_notify.NOTIFY_MODE
        try:
            trade_notify.NOTIFY_MODE = ""
            out = trade_notify.enqueue_wechat("native test", kind="execution_result")
        finally:
            trade_notify.NOTIFY_MODE = old_notify_mode
        self.assertTrue(out["native_sent"])
        self.assertEqual(out["chat_id"], "wx-test")
        native_send.assert_called_once_with("native test", chat_id="wx-test")


if __name__ == "__main__":
    unittest.main()
