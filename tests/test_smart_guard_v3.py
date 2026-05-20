#!/config/quant_env/bin/python3
"""smart_guard_v3 关键盯盘逻辑回归测试。"""

import os
import sys
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


if __name__ == "__main__":
    unittest.main()
