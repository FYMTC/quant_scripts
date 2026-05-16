#!/config/quant_env/bin/python3
"""agent_queue 单元测试（无网络）。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_queue as aq  # noqa: E402


class TestParseAgentAlert(unittest.TestCase):
    def test_parse_standard_line(self):
        line = "[AGENT_ALERT] sig1|000063|中兴|突破|现价38.50|涨+2.10%|量500万手"
        p = aq.parse_agent_alert_line(line)
        self.assertTrue(p["parse_ok"])
        self.assertEqual(p["signal_id"], "sig1")
        self.assertEqual(p["code"], "000063")
        self.assertEqual(p["name"], "中兴")
        self.assertAlmostEqual(p["price"], 38.5, places=1)

    def test_non_alert_returns_none(self):
        self.assertIsNone(aq.parse_agent_alert_line("普通异动"))


class TestQueueRoundtrip(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._queue = os.path.join(self._tmpdir, "q.jsonl")
        self._lock = os.path.join(self._tmpdir, "lock")
        aq.QUEUE_PATH = self._queue
        aq.LOCK_PATH = self._lock

    def test_enqueue_and_list(self):
        eid = aq.enqueue({"parse_ok": True, "code": "000001", "signal_id": "t1"})
        self.assertTrue(eid)
        pending = aq.list_pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["code"], "000001")

    def test_ack_removes_from_pending_view(self):
        eid = aq.enqueue({"parse_ok": True, "code": "000002", "signal_id": "t2"})
        aq.ack(eid, result={"action": "SKIP"})
        self.assertEqual(len(aq.list_pending()), 0)


if __name__ == "__main__":
    unittest.main()
