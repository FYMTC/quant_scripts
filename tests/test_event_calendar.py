#!/config/quant_env/bin/python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.engines import event_calendar as ec  # noqa: E402


class TestEventCalendar(unittest.TestCase):
    def test_keyword_critical(self):
        text = "川习会元首会见不及预期 光模块砍单 科技泡沫 主力出逃 跌停潮"
        level, hits = ec._match_keywords(text, ec._load_yaml(ec.KEYWORDS_PATH))
        self.assertIn(level, ("HIGH", "CRITICAL"))
        self.assertTrue(len(hits) >= 2)

    def test_merge_recommendation(self):
        ev = {"recommendation_override": "BLOCKED"}
        self.assertEqual(ec.merge_recommendation("READY", ev), "BLOCKED")
        ev2 = {"recommendation_override": "CAUTION"}
        self.assertEqual(ec.merge_recommendation("READY", ev2), "CAUTION")

    def test_assess_no_news(self):
        r = ec.assess_event_risk(scan_news=False)
        self.assertIn(r["event_level"], ec.LEVEL_ORDER)
        self.assertIn("playbook", r)


if __name__ == "__main__":
    unittest.main()
