#!python3
"""smart_guard_v3 关键盯盘逻辑回归测试。"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import smart_guard_v3 as sg  # noqa: E402


class TestAgentSignals(unittest.TestCase):
    @patch("smart_guard_v3.load_config")
    def test_check_agent_signals_reads_target_from_params(self, load_config):
        load_config.return_value = {
            "signals": [
                {
                    "id": "002475_price_below_6661",
                    "code": "002475",
                    "name": "立讯精密",
                    "type": "price_below",
                    "params": {"target": 66.61},
                }
            ]
        }
        sg.state = {"triggered_alerts": {}, "avg_volumes": {}}
        quotes = {
            "002475": {
                "最新价": 66.0,
                "涨跌幅": -3.33,
                "成交量(手)": 120000,
                "最高": 72.03,
            }
        }

        alerts = sg.check_agent_signals(quotes)

        self.assertEqual(len(alerts), 1)
        self.assertIn("跌破66.61元", alerts[0][1])


class TestRuntimeBlindness(unittest.TestCase):
    def test_evaluate_runtime_blindness_marks_empty_contract(self):
        sg.state = {}
        blindness = sg._evaluate_runtime_blindness(
            {
                "positions": {},
                "watch_list": {},
                "monitored_codes": {"000001": "测试股"},
                "signals": [],
            },
            quotes={},
            cycle_count=3,
            fetch_time=0.2,
        )
        self.assertEqual(blindness["status"], "blind")
        self.assertIn("持仓与自选同时为空", blindness["reasons"])
        self.assertIn("signals 为空", blindness["reasons"])

    def test_evaluate_runtime_blindness_marks_stale_idle_heartbeat(self):
        sg.state = {
            "last_heartbeat_status": "idle",
            "last_heartbeat_at": "2026-05-21T06:30:00+08:00",
        }
        fake_now = sg.datetime(2026, 5, 21, 6, 45, 30, tzinfo=sg.CST)
        with patch("smart_guard_v3._now_bj", return_value=fake_now):
            blindness = sg._evaluate_runtime_blindness(
                {
                    "positions": {"000001": {"name": "测试股"}},
                    "watch_list": {"000001": "测试股"},
                    "signals": [{"id": "sig1"}],
                },
                quotes={"000001": {"最新价": 10.0}},
                cycle_count=3,
                fetch_time=0.2,
            )
        self.assertEqual(blindness["status"], "degraded")
        self.assertTrue(any("heartbeat 超过 600s 未更新" in r for r in blindness["reasons"]))

    def test_emit_runtime_blindness_alert_after_three_cycles(self):
        sg.state = {"triggered_alerts": {}}
        blindness = {
            "status": "blind",
            "reasons": ["signals 为空"],
            "consecutive": 3,
        }
        alerts = sg._emit_runtime_blindness_alert(blindness)
        self.assertEqual(len(alerts), 1)
        self.assertIn("[SYSTEM_BLIND]", alerts[0][1])


class TestRollingDecline(unittest.TestCase):
    @patch("smart_guard_v3.load_config")
    def test_check_rolling_decline_triggers_on_cumulative_drop(self, load_config):
        load_config.return_value = {
            "positions": {"002475": {"name": "立讯精密"}},
            "watch_list": {},
        }
        sg.state = {
            "triggered_alerts": {},
            "price_history": {
                "002475": {
                    "2026-05-11": 77.87,
                    "2026-05-12": 76.85,
                    "2026-05-13": 76.34,
                    "2026-05-14": 75.40,
                    "2026-05-15": 74.00,
                    "2026-05-18": 72.90,
                    "2026-05-19": 72.40,
                }
            },
        }
        quotes = {"002475": {"最新价": 69.99, "涨跌幅": -3.33}}

        alerts = sg.check_rolling_decline(quotes)

        self.assertEqual(len(alerts), 1)
        self.assertIn("累计", alerts[0][1])
        self.assertIn("连跌", alerts[0][1])

    @patch("smart_guard_v3.load_config")
    def test_check_rolling_decline_deduplicates_same_day(self, load_config):
        load_config.return_value = {
            "positions": {"002475": {"name": "立讯精密"}},
            "watch_list": {},
        }
        sg.state = {
            "triggered_alerts": {},
            "price_history": {
                "002475": {
                    "2026-05-11": 77.87,
                    "2026-05-12": 76.85,
                    "2026-05-13": 76.34,
                    "2026-05-14": 75.40,
                    "2026-05-15": 74.00,
                    "2026-05-18": 72.90,
                    "2026-05-19": 72.40,
                }
            },
        }
        quotes = {"002475": {"最新价": 69.99, "涨跌幅": -3.33}}

        first = sg.check_rolling_decline(quotes)
        second = sg.check_rolling_decline(quotes)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)


class TestPushWechat(unittest.TestCase):
    @patch("smart_guard_v3.subprocess.Popen")
    def test_push_wechat_appends_signal_file_for_agent_alerts(self, _popen):
        old_alert = sg.ALERT_FILE
        old_signal = sg.SIGNAL_FILE
        old_pushlog = sg.PUSHLOG_FILE
        old_webhook = sg.WEBHOOK_URL
        try:
            with tempfile.TemporaryDirectory() as td:
                sg.ALERT_FILE = os.path.join(td, "guard_emergency.txt")
                sg.SIGNAL_FILE = os.path.join(td, "guard_emergency_signal.txt")
                sg.PUSHLOG_FILE = os.path.join(td, "guard_pushlog.txt")
                sg.WEBHOOK_URL = ""
                with patch("agent_queue.should_wake_desk", return_value=False), patch("smart_guard_v3.subprocess.Popen"):
                    sg.push_wechat("[AGENT_ALERT] sig1|000063|中兴|跌破38元|现价37.9", "🔴")
                    sg.push_wechat("[AGENT_ALERT] sig2|002475|立讯|连跌5天|现价66.0", "🔴")
                with open(sg.SIGNAL_FILE, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("sig1|000063", text)
                self.assertIn("sig2|002475", text)
                self.assertGreaterEqual(text.count("🔴 PUSH:"), 2)
        finally:
            sg.ALERT_FILE = old_alert
            sg.SIGNAL_FILE = old_signal
            sg.PUSHLOG_FILE = old_pushlog
            sg.WEBHOOK_URL = old_webhook


if __name__ == "__main__":
    unittest.main()
