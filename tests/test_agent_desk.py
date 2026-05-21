#!/config/quant_env/bin/python3
"""agent_desk 冒烟测试（mock 队列，无 TA/网络）。"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_desk  # noqa: E402


class TestAgentDeskEmpty(unittest.TestCase):
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=0)
    @patch("agent_desk.list_pending", return_value=[])
    def test_process_pending_silent(self, _pending, _cnt, _save):
        out = agent_desk.process_pending(max_events=3)
        self.assertFalse(out["needs_hermes"])
        self.assertEqual(out["analyze_tasks"], [])

    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=0)
    @patch("agent_desk.ack")
    @patch("agent_desk.list_pending")
    @patch("signal_loop.handle_trigger", return_value={"action": "SKIP", "reason": "test"})
    def test_skip_event_no_hermes(self, handle, list_pending, ack, _cnt, _save):
        list_pending.return_value = [
            {
                "event_id": "e1",
                "parse_ok": True,
                "code": "000063",
                "signal_id": "s1",
                "price": 38.0,
                "change_pct": 1.0,
                "volume": 100,
                "name": "中兴",
                "reason": "测试",
            }
        ]
        out = agent_desk.process_pending(max_events=1)
        self.assertFalse(out["needs_hermes"])
        self.assertEqual(len(out["skipped"]), 1)
        ack.assert_called_once()

    @patch("agent_desk._run_registry_plugins", return_value=[])
    @patch("agent_desk._load_playbook", return_value=[])
    @patch("agent_desk._stock_insights", return_value=[])
    @patch("agent_desk._fetch_quant_context", return_value={})
    @patch("agent_desk._latest_apps_snapshot", return_value={})
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=1)
    @patch("agent_desk.ack")
    @patch("agent_desk.list_pending")
    @patch(
        "signal_loop.handle_trigger",
        return_value={"action": "ANALYZE", "reason": "risk", "lineage_id": "lid1"},
    )
    @patch("trade_outbox.propose")
    def test_forced_risk_event_creates_sell_request(
        self,
        propose,
        _handle,
        list_pending,
        ack,
        _cnt,
        _save,
        _apps,
        _ctx,
        _insights,
        _playbook,
        _plugins,
    ):
        propose.return_value = {
            "ok": True,
            "request_id": "req1",
            "wechat_template": "tpl",
        }
        list_pending.return_value = [
            {
                "event_id": "e1",
                "parse_ok": True,
                "code": "002475",
                "signal_id": "rolling_decline",
                "price": 69.99,
                "change_pct": -3.33,
                "volume": 100,
                "name": "立讯",
                "reason": "7日累计-10.1%，连跌6天",
            }
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={
                "positions": [{"code": "002475", "name": "立讯", "shares": 1200}],
                "position_count": 1,
            },
        ):
            out = agent_desk.process_pending(max_events=1)

        self.assertTrue(out["needs_hermes"])
        self.assertEqual(out["analyze_tasks"], [])
        self.assertEqual(len(out["forced_trade_requests"]), 1)
        self.assertEqual(out["forced_trade_requests"][0]["request_id"], "req1")
        propose.assert_called_once()
        self.assertEqual(propose.call_args.args[1], "SELL")
        self.assertEqual(propose.call_args.kwargs["shares"], 600)
        ack.assert_called_once()
        ack_result = ack.call_args.kwargs["result"]
        self.assertIn("forced_trade_request", ack_result)

    @patch("agent_desk._run_registry_plugins", return_value=[])
    @patch("agent_desk._load_playbook", return_value=[])
    @patch("agent_desk._stock_insights", return_value=[])
    @patch("agent_desk._fetch_quant_context", return_value={})
    @patch("agent_desk._latest_apps_snapshot", return_value={})
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=1)
    @patch("agent_desk.ack")
    @patch("agent_desk.list_pending")
    @patch(
        "signal_loop.handle_trigger",
        return_value={"action": "ANALYZE", "reason": "quota ok", "lineage_id": "lid2"},
    )
    def test_non_position_risk_event_falls_back_to_analyze(
        self,
        _handle,
        list_pending,
        ack,
        _cnt,
        _save,
        _apps,
        _ctx,
        _insights,
        _playbook,
        _plugins,
    ):
        list_pending.return_value = [
            {
                "event_id": "e2",
                "parse_ok": True,
                "code": "002475",
                "signal_id": "rolling_decline",
                "price": 69.99,
                "change_pct": -3.33,
                "volume": 100,
                "name": "立讯",
                "reason": "7日累计-10.1%，连跌6天",
            }
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={"positions": [], "position_count": 0},
        ):
            out = agent_desk.process_pending(max_events=1)

        self.assertTrue(out["needs_hermes"])
        self.assertEqual(len(out["analyze_tasks"]), 1)
        self.assertEqual(out["forced_trade_requests"], [])
        ack.assert_not_called()


if __name__ == "__main__":
    unittest.main()
