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

    @patch("trade_execution.execute_request")
    @patch("trade_notify.enqueue_wechat", return_value={"ok": True})
    @patch("trade_outbox._trading_hours_now", return_value=True)
    def test_paper_propose_auto_executes_without_manual_approval(self, _hours, notify, ex):
        ex.return_value = {"ok": True, "result": {"data": {}}}
        ta.start_hermes_trading("paper_easyths", set_primary=True)
        out = to.propose_and_notify("000001", "BUY", shares=100, price=10.0, account_id="paper_easyths")
        self.assertTrue(out.get("auto_resolved"))
        self.assertTrue(out.get("executed"))
        self.assertEqual(out.get("resolved_status"), "resolved")
        self.assertNotIn("请回复: 同意 / 拒绝", out.get("wechat_body") or "")
        ex.assert_called_once()
        notify.assert_called_once()

    def test_enqueue_blocks_placeholder_body(self):
        import trade_notify

        out = trade_notify.enqueue_wechat("tpl", kind="trade_request")
        self.assertFalse(out["ok"])
        self.assertTrue(out["skipped"])
        self.assertEqual(out["reason"], "placeholder_body_blocked")

    @patch("trade_notify._send_via_native_weixin", return_value={"ok": True, "success": True})
    def test_webhook_primary_channel(self, native_send):
        """Native WeChat is no longer used for notifications — webhook is the only channel.

        用 record-only 模式跑：只验 jsonl 落盘 + 原生微信未被调用，
        避免真发到企业微信群打扰成员。
        """
        import trade_notify

        old_notify_mode = trade_notify.NOTIFY_MODE
        try:
            trade_notify.NOTIFY_MODE = "record-only"
            out = trade_notify.enqueue_wechat("webhook test", kind="execution_result")
        finally:
            trade_notify.NOTIFY_MODE = old_notify_mode
        # Native WeChat must NOT be called for notifications anymore
        native_send.assert_not_called()
        # jsonl audit trail still written
        self.assertTrue(out["queued"])
        self.assertEqual(out["chat_id"], "wx-test")
        self.assertTrue(out.get("record_only"))


if __name__ == "__main__":
    unittest.main()
