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


if __name__ == "__main__":
    unittest.main()
