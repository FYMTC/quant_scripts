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
            "wechat_notify": {"ok": True},
            "wechat_sent": True,
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
        self.assertIn("decision_gate", ack_result)
        self.assertIn("counterfactual", ack_result["decision_gate"])
        self.assertIn(ack_result["decision_gate"]["verdict"], ("APPROVE", "MODIFY", "REJECT"))
        self.assertEqual(ack_result["decision_gate"]["direction"], "SELL")

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
        self.assertIn(out["analyze_tasks"][0]["decision_gate"]["verdict"], ("APPROVE", "MODIFY", "REJECT"))
        self.assertEqual(out["analyze_tasks"][0]["decision_gate"]["direction"], "SELL")
        self.assertIsNone(out["analyze_tasks"][0]["trade_request"])
        self.assertEqual(out["forced_trade_requests"], [])
        ack.assert_not_called()

    @patch("agent_desk._run_registry_plugins", return_value=[])
    @patch("agent_desk._load_playbook", return_value=[])
    @patch("agent_desk._stock_insights", return_value=[])
    @patch("agent_desk._fetch_quant_context", return_value={"analyst_scores": {"technical": 1.5, "news": 1.2, "sentiment": 0.8, "fundamental": 0.6}})
    @patch("agent_desk._latest_apps_snapshot", return_value={})
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=1)
    @patch("agent_desk.ack")
    @patch("agent_desk.list_pending")
    @patch("signal_loop.handle_trigger", return_value={"action": "ANALYZE", "reason": "buy ok", "lineage_id": "lid3"})
    @patch("trade_outbox.propose_and_notify")
    def test_approved_buy_event_creates_visible_trade_request(
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
            "request_id": "req-buy-1",
            "wechat_template": "buy tpl",
            "wechat_notify": {"ok": True},
            "wechat_sent": True,
        }
        list_pending.return_value = [
            {
                "event_id": "e3",
                "parse_ok": True,
                "code": "000063",
                "signal_id": "sig_buy",
                "price": 38.0,
                "change_pct": 2.1,
                "volume": 100,
                "name": "中兴",
                "reason": "突破触发",
            }
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={"positions": [], "position_count": 0},
        ), patch("agent_desk._run_decision_gate_for_event", return_value={
            "verdict": "APPROVE",
            "direction": "BUY",
            "mapped_action": "BUY",
            "reasons": [],
            "suggested_shares": 200,
            "counterfactual": {"summary": "条件满足"},
        }):
            out = agent_desk.process_pending(max_events=1)

        self.assertTrue(out["needs_hermes"])
        self.assertEqual(len(out["analyze_tasks"]), 1)
        task = out["analyze_tasks"][0]
        self.assertEqual(task["trade_request"]["request_id"], "req-buy-1")
        self.assertTrue(task["trade_request"]["wechat_sent"])
        propose.assert_called_once()
        self.assertEqual(propose.call_args.args[1], "BUY")
        self.assertEqual(propose.call_args.kwargs["account_id"], "paper_easyths")
        ack.assert_not_called()
    @patch("agent_desk._run_registry_plugins", return_value=[])
    @patch("agent_desk._load_playbook", return_value=[])
    @patch("agent_desk._stock_insights", return_value=[])
    @patch("agent_desk._fetch_quant_context", return_value={})
    @patch("agent_desk._latest_apps_snapshot", return_value={})
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=0)
    @patch("agent_desk.list_pending", return_value=[])
    @patch("trade_outbox.propose_and_notify")
    @patch("agent_desk._load_json")
    def test_morning_plan_emits_visible_trade_requests(
        self,
        load_json,
        propose,
        _pending,
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
            "request_id": "req-plan-1",
            "wechat_template": "buy tpl",
            "wechat_notify": {"ok": True},
            "wechat_sent": True,
        }
        load_json.side_effect = [
            {
                "buy_proposals": [
                    {
                        "code": "300408",
                        "name": "300408",
                        "price": 115.0,
                        "shares": 100,
                        "rationale": "score ok",
                    }
                ]
            },
            {"pending_trade_requests": []},
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={"positions": [], "position_count": 0},
        ):
            out = agent_desk.process_pending(max_events=1)

        self.assertTrue(out["needs_hermes"])
        self.assertEqual(len(out["planned_trade_requests"]), 1)
        self.assertEqual(out["planned_trade_requests"][0]["request_id"], "req-plan-1")
        propose.assert_called_once()
        self.assertEqual(propose.call_args.args[0], "300408")
        self.assertEqual(propose.call_args.args[1], "BUY")
        self.assertEqual(propose.call_args.kwargs["signal_id"], "morning_plan")
        self.assertEqual(propose.call_args.kwargs["account_id"], "paper_easyths")


if __name__ == "__main__":
    unittest.main()
