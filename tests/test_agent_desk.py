#!python3
"""agent_desk 冒烟测试（mock 队列，无 TA/网络）。"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_desk  # noqa: E402


class TestAgentDeskEmpty(unittest.TestCase):
    def setUp(self):
        # T1.10 二期：隔离 TradeDB 写入，避免 test_agent_desk 污染真实 trade_log.db
        # （_build_trade_request_from_decision / _build_forced_risk_request 现在写 trading_journal）
        self._db_patch = patch("trade_db.TradeDB.log_trade_event")
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()

    @patch("agent_desk._emit_morning_plan_requests", return_value=[])
    @patch("agent_desk._emit_de_risk_requests", return_value=[])
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=0)
    @patch("agent_desk.list_pending", return_value=[])
    def test_process_pending_silent(self, _pending, _cnt, _save, _de_risk, _planned):
        out = agent_desk.process_pending(max_events=3)
        self.assertFalse(out["needs_hermes"])
        self.assertEqual(out["analyze_tasks"], [])

    @patch("agent_desk._emit_morning_plan_requests", return_value=[])
    @patch("agent_desk._emit_de_risk_requests", return_value=[])
    @patch("agent_desk._save_agent_state")
    @patch("agent_desk.pending_count", return_value=0)
    @patch("agent_desk.ack")
    @patch("agent_desk.list_pending")
    @patch("signal_loop.handle_trigger", return_value={"action": "SKIP", "reason": "test"})
    def test_skip_event_no_hermes(self, handle, list_pending, ack, _cnt, _save, _de_risk, _planned):
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

    @patch("agent_desk._resolve_signal_direction", return_value=("SELL", "forced_risk_stop_triggered"))
    @patch("agent_desk._emit_morning_plan_requests", return_value=[])
    @patch("agent_desk._emit_de_risk_requests", return_value=[])
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
        _de_risk,
        _planned,
        _resolver,  # T1.10: 避免 resolver 内 fetch_quote 网络挂起
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

    @patch("agent_desk._resolve_signal_direction", return_value=("WAIT", "empty+rolling_decline→WAIT"))
    @patch("agent_desk._emit_morning_plan_requests", return_value=[])
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
        _planned,
        _resolver,  # T1.10: 避免 resolver 内 bottom_fish_score.compute 网络挂起
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
        ), patch("agent_desk._expire_stale_pending_requests", return_value=0):
            out = agent_desk.process_pending(max_events=1)

        self.assertTrue(out["needs_hermes"])
        self.assertEqual(len(out["analyze_tasks"]), 1)
        # T1.10 行为变更：空仓 + rolling_decline 由 SELL 改为 WAIT（渐进阴跌不抄底）
        self.assertEqual(out["analyze_tasks"][0]["decision_gate"]["verdict"], "WAIT")
        self.assertEqual(out["analyze_tasks"][0]["decision_gate"]["direction"], "WAIT")
        self.assertIsNone(out["analyze_tasks"][0]["trade_request"])
        self.assertEqual(out["forced_trade_requests"], [])
        ack.assert_not_called()

    @patch("agent_desk._emit_morning_plan_requests", return_value=[])
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
        _planned,
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
    def test_morning_plan_does_not_reemit_when_same_bundle_already_resolved(
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
        load_json.side_effect = [
            {
                "generated_at": "2026-05-28T00:30:23.322653",
                "buy_proposals": [
                    {
                        "code": "300408",
                        "name": "300408",
                        "price": 122.99,
                        "shares": 100,
                        "rationale": "score ok",
                    }
                ]
            },
            {
                "pending_trade_requests": [
                    {
                        "request_id": "req-old-1",
                        "status": "resolved",
                        "account_id": "paper_easyths",
                        "code": "300408",
                        "signal_id": "morning_plan",
                        "proposal_generated_at": "2026-05-28T00:30:23.322653",
                    }
                ]
            },
            {},
            {"pending_trade_requests": []},
            {"updated_at": "2026-05-28T00:31:00"},
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={"positions": [], "position_count": 0},
        ), patch("agent_desk._expire_stale_pending_requests", return_value=0):
            out = agent_desk.process_pending(max_events=1)

        self.assertFalse(out["needs_hermes"])
        self.assertEqual(out["planned_trade_requests"], [])
        propose.assert_not_called()

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
                "generated_at": "2026-05-28T00:30:23.322653",
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
            {"pending_trade_requests": []},
            {"updated_at": "2026-05-28T00:31:00"},
        ]
        with patch("trade_accounts.resolve_trading_account", return_value="paper_easyths"), patch(
            "trade_account_context.load_account_snapshot",
            return_value={"positions": [], "position_count": 0},
        ), patch("agent_desk._expire_stale_pending_requests", return_value=0):
            out = agent_desk.process_pending(max_events=1)

        self.assertEqual(out["expired_pending_requests"], 0)
        self.assertEqual(len(out["planned_trade_requests"]), 1)
        self.assertEqual(out["planned_trade_requests"][0]["request_id"], "req-plan-1")
        propose.assert_called_once()
        self.assertEqual(propose.call_args.args[0], "300408")
        self.assertEqual(propose.call_args.args[1], "BUY")
        self.assertEqual(propose.call_args.kwargs["signal_id"], "morning_plan")
        self.assertEqual(propose.call_args.kwargs["account_id"], "paper_easyths")
        self.assertEqual(
            propose.call_args.kwargs["decision_gate"]["proposal_generated_at"],
            "2026-05-28T00:30:23.322653",
        )

    def test_process_pending_expires_stale_pending_requests(self):
        with tempfile.TemporaryDirectory() as td:
            old_state_path = agent_desk.STATE_PATH
            try:
                agent_desk.STATE_PATH = os.path.join(td, "agent_state.json")
                with open(agent_desk.STATE_PATH, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "pending_trade_requests": [
                                {
                                    "request_id": "req-old",
                                    "status": "pending",
                                    "code": "300408",
                                    "signal_id": "morning_plan",
                                    "expires_at": "2026-05-22T09:00:00",
                                }
                            ]
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                with patch("agent_desk._emit_morning_plan_requests", return_value=[]), patch(
                    "agent_desk._emit_de_risk_requests", return_value=[]
                ), patch("agent_desk._save_agent_state"), patch(
                    "agent_desk.pending_count", return_value=0
                ), patch("agent_desk.list_pending", return_value=[]), patch(
                    "trade_outbox._save_state"
                ) as save_state, patch(
                    "agent_desk.datetime"
                ) as mocked_datetime:
                    mocked_datetime.now.return_value = datetime.fromisoformat("2026-05-28T15:37:00")
                    mocked_datetime.fromisoformat.side_effect = datetime.fromisoformat
                    out = agent_desk.process_pending(max_events=1)
                self.assertEqual(out["expired_pending_requests"], 1)
                state = agent_desk._load_json(agent_desk.STATE_PATH)
                row = state["pending_trade_requests"][0]
                self.assertEqual(row["status"], "expired")
                self.assertEqual(row["note"], "auto-expired by agent_desk")
                save_state.assert_called_once()
            finally:
                agent_desk.STATE_PATH = old_state_path


class TestDeRiskExemptionRevalidation(unittest.TestCase):
    """T1.7（2026-06-26）：盘中用 snapshot 实时价格重验"长线盈利股豁免"。"""

    def setUp(self):
        # T1.10 二期：隔离 TradeDB 写入，避免污染真实 trade_log.db
        self._db_patch = patch("trade_db.TradeDB.log_trade_event")
        self._db_patch.start()

    def tearDown(self):
        self._db_patch.stop()

    def _write_morning(self, skipped):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(
            {
                "de_risk_plan": {
                    "required": True,
                    "level": "HIGH",
                    "actions": [],
                    "skipped_long_term": skipped,
                    "lineage_id": "test-lid",
                }
            },
            tmp,
            ensure_ascii=False,
        )
        tmp.close()
        return tmp.name

    def setUp(self):
        self._old_path = agent_desk.MORNING_OUTPUT_PATH
        self._tmps = []

    def tearDown(self):
        agent_desk.MORNING_OUTPUT_PATH = self._old_path
        for p in self._tmps:
            try:
                os.unlink(p)
            except OSError:
                pass

    @patch("trade_outbox.propose_and_notify")
    def test_long_term_exemption_invalidated_when_profit_drops(self, mock_propose):
        """浮盈跌破 10% → 豁免失效 → 追加 1 手 SELL request。"""
        mock_propose.return_value = {"ok": True, "request_id": "r1"}
        path = self._write_morning([
            {"code": "600487", "name": "亨通光电", "reason": "长线盈利股豁免（浮盈≥10%）"},
            {"code": "002049", "name": "紫光国微", "reason": "新仓保护期豁免（开仓<5天）"},
        ])
        self._tmps.append(path)
        agent_desk.MORNING_OUTPUT_PATH = path
        # 亨通光电 cost 119.922 → 111.0 浮盈 -7.4%（豁免失效）
        # 紫光国微 新仓豁免不重验（即使浮亏也不追加）
        snapshot = {
            "positions": [
                {"code": "600487", "name": "亨通光电", "shares": 200, "cost": 119.922, "last_price": 111.0},
                {"code": "002049", "name": "紫光国微", "shares": 100, "cost": 80.0, "last_price": 70.0},
            ]
        }
        out = agent_desk._emit_de_risk_requests(snapshot, "manual_main")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["code"], "600487")
        self.assertEqual(out[0]["shares"], 100)
        self.assertTrue(out[0]["de_risk"])
        mock_propose.assert_called_once()

    @patch("trade_outbox.propose_and_notify")
    def test_long_term_exemption_kept_when_profit_high(self, mock_propose):
        """浮盈仍 ≥10% → 豁免继续 → 不追加（actions=[] + 重验仍豁免 → 空）。"""
        mock_propose.return_value = {"ok": True, "request_id": "r1"}
        path = self._write_morning([
            {"code": "600487", "name": "亨通光电", "reason": "长线盈利股豁免（浮盈≥10%）"},
        ])
        self._tmps.append(path)
        agent_desk.MORNING_OUTPUT_PATH = path
        # 浮盈 30% ≥ 10% → 豁免继续
        snapshot = {
            "positions": [
                {"code": "600487", "name": "亨通光电", "shares": 200, "cost": 100.0, "last_price": 130.0},
            ]
        }
        out = agent_desk._emit_de_risk_requests(snapshot, "manual_main")
        self.assertEqual(out, [])
        mock_propose.assert_not_called()

    @patch("trade_outbox.propose_and_notify")
    def test_new_position_exemption_not_revalidated(self, mock_propose):
        """新仓保护期豁免不重验（即使浮亏也不追加）。"""
        mock_propose.return_value = {"ok": True, "request_id": "r1"}
        path = self._write_morning([
            {"code": "002049", "name": "紫光国微", "reason": "新仓保护期豁免（开仓<5天）"},
        ])
        self._tmps.append(path)
        agent_desk.MORNING_OUTPUT_PATH = path
        snapshot = {
            "positions": [
                {"code": "002049", "name": "紫光国微", "shares": 100, "cost": 80.0, "last_price": 70.0},
            ]
        }
        out = agent_desk._emit_de_risk_requests(snapshot, "manual_main")
        self.assertEqual(out, [])
        mock_propose.assert_not_called()

