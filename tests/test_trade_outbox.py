#!/config/quant_env/bin/python3
"""trade_outbox 单元测试。"""

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import patch

sys.modules.setdefault("yaml", types.SimpleNamespace(safe_load=lambda s: json.loads(json.dumps({}))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_accounts as ta  # noqa: E402
import trade_outbox as to  # noqa: E402


class TestTradeOutbox(unittest.TestCase):
    _prod_state = "/config/quant_scripts/data/agent_state.json"
    _prod_outbox = "/config/quant_scripts/data/trade_request_pending.json"

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_to_state = to.STATE_PATH
        self._orig_to_outbox = to.OUTBOX_PATH
        self._orig_ta_default = ta.DEFAULT_PATH
        self._orig_ta_state = ta.STATE_PATH
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
                    "execution": {"provider": "easyths"},
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
            "execution": {"provider": "easyths"},
            "hermes_trading_active": True,
        }
        ta.resolve_trading_account = lambda explicit=None: explicit or "paper_easyths"
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def tearDown(self):
        ta.load_registry = self._orig_load_registry
        ta.get_account = self._orig_get_account
        ta.resolve_trading_account = self._orig_resolve
        to.STATE_PATH = self._orig_to_state
        to.OUTBOX_PATH = self._orig_to_outbox
        ta.DEFAULT_PATH = self._orig_ta_default
        ta.STATE_PATH = self._orig_ta_state
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

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

    def test_propose_and_notify_uses_wechat_template(self):
        import trade_outbox as outbox

        with patch("trade_notify.enqueue_wechat", return_value={"ok": True, "queued": True}) as notify:
            r = outbox.propose_and_notify("000063", "BUY", name="中兴", price=38.0, shares=100, gate_summary="ok")

        self.assertTrue(r["ok"])
        self.assertTrue(r["wechat_sent"])
        self.assertEqual(r["wechat_notify"]["ok"], True)
        notify.assert_called_once()
        self.assertIn("【买卖请示】BUY", notify.call_args.args[0])

        r = to.propose("000063", "HOLD")
        self.assertIn("error", r)

    def test_propose_and_notify_recovers_placeholder_template(self):
        with patch("trade_notify.enqueue_wechat", return_value={"ok": True, "queued": True}) as notify:
            with patch("trade_outbox.propose", return_value={"ok": True, "request_id": "req1", "account_id": "paper_easyths", "wechat_template": "tpl"}):
                with patch("trade_outbox._load_state", return_value={"pending_trade_requests": [{
                    "request_id": "req1",
                    "account_id": "paper_easyths",
                    "account_label": "Paper",
                    "code": "000063",
                    "name": "中兴",
                    "direction": "BUY",
                    "price": 38.0,
                    "shares": 100,
                    "gate_summary": "ok",
                    "expires_at": "2026-05-23T12:00:00",
                    "lineage_id": "lid1",
                    "status": "pending",
                }]}) as load_state, patch("trade_outbox._save_state") as save_state:
                    r = to.propose_and_notify("000063", "BUY", name="中兴", price=38.0, shares=100, gate_summary="ok")

        self.assertTrue(r["ok"])
        self.assertIn("【买卖请示】BUY", r["wechat_template"])
        self.assertIn("【买卖请示】BUY", notify.call_args.args[0])
        load_state.assert_called_once()
        save_state.assert_called_once()

    def test_propose_and_notify_reports_missing_template_instead_of_sending_placeholder(self):
        with patch("trade_notify.enqueue_wechat") as notify:
            with patch(
                "trade_outbox.propose",
                return_value={"ok": True, "request_id": "req1", "account_id": "paper_easyths", "wechat_template": "tpl"},
            ):
                with patch("trade_outbox._load_state", return_value={"pending_trade_requests": []}):
                    r = to.propose_and_notify("000063", "BUY", name="中兴", price=38.0, shares=100, gate_summary="ok")

        self.assertTrue(r["ok"])
        self.assertFalse(r["wechat_sent"])
        self.assertEqual(r["wechat_notify"]["error"], "wechat_template_unavailable")
        notify.assert_not_called()

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

    def test_save_state_prunes_old_resolved_history(self):
        old = {
            "request_id": "req-old",
            "status": "resolved",
            "created_at": "2026-05-10T08:00:00",
            "resolved_at": "2026-05-10T09:00:00",
            "code": "000063",
        }
        recent = {
            "request_id": "req-recent",
            "status": "resolved",
            "created_at": "2026-05-26T08:00:00",
            "resolved_at": "2026-05-26T09:00:00",
            "code": "000001",
        }
        pending = {
            "request_id": "req-pending",
            "status": "pending",
            "created_at": "2026-05-28T08:00:00",
            "expires_at": "2026-05-28T12:00:00",
            "code": "300408",
        }
        with patch("trade_outbox.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = datetime.fromisoformat("2026-05-28T15:37:00")
            mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
            to._save_state({"version": 1, "pending_trade_requests": [old, recent, pending]})

        state = to._load_state()
        ids = [row.get("request_id") for row in state.get("pending_trade_requests") or []]
        self.assertEqual(ids, ["req-recent", "req-pending"])
        with open(to.OUTBOX_PATH, encoding="utf-8") as f:
            exported = json.load(f)
        self.assertEqual(exported["count"], 1)

    def test_save_state_archives_manual_wechat_rows(self):
        manual = {
            "request_id": "req-manual",
            "status": "expired",
            "created_at": "2026-05-27T08:00:00",
            "resolved_at": "2026-05-27T09:00:00",
            "account_id": "manual_wechat",
            "code": "002475",
        }
        paper = {
            "request_id": "req-paper",
            "status": "resolved",
            "created_at": "2026-05-27T08:00:00",
            "resolved_at": "2026-05-27T09:00:00",
            "account_id": "paper_easyths",
            "code": "300408",
        }
        old_archive = to.ARCHIVE_PATH
        to.ARCHIVE_PATH = os.path.join(self._tmpdir, "trade_request_history_archive.json")
        try:
            to._save_state({"version": 1, "pending_trade_requests": [manual, paper]})
            state = to._load_state()
            ids = [row.get("request_id") for row in state.get("pending_trade_requests") or []]
            self.assertEqual(ids, ["req-paper"])
            with open(to.ARCHIVE_PATH, encoding="utf-8") as f:
                archive = json.load(f)
            archived_ids = [row.get("request_id") for row in archive.get("archived_trade_requests") or []]
            self.assertEqual(archived_ids, ["req-manual"])
        finally:
            to.ARCHIVE_PATH = old_archive

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
        self.assertIsNone(row["decision_gate"])


if __name__ == "__main__":
    unittest.main()
