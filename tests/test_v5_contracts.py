#!/config/quant_env/bin/python3
"""v5 数据契约与配置完整性测试（无网络）。"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
sys.path.insert(0, ROOT)


class TestV5Artifacts(unittest.TestCase):
    def test_quant_registry_yaml_exists(self):
        p = os.path.join(DATA, "quant_registry.yaml")
        self.assertTrue(os.path.isfile(p))

    def test_agent_state_schema(self):
        p = os.path.join(DATA, "agent_state.json")
        self.assertTrue(os.path.isfile(p))
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        self.assertIn("pending_trade_requests", s)

    def test_guard_config_signals_array(self):
        p = os.environ.get("STOCK_KB_GUARD_CONFIG_PATH") or os.path.join(ROOT, "guard_config.json")
        if not os.path.isfile(p):
            self.skipTest("guard_config.json missing")
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIsInstance(cfg.get("signals", []), list)

    def test_guard_config_has_monitoring_surface(self):
        p = os.environ.get("STOCK_KB_GUARD_CONFIG_PATH") or os.path.join(ROOT, "guard_config.json")
        if not os.path.isfile(p):
            self.skipTest("guard_config.json missing")
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertTrue(cfg.get("watch_list") or cfg.get("monitored_codes"))

    def test_review_output_keys_if_present(self):
        p = os.path.join(DATA, "review_bundle.json")
        if not os.path.isfile(p):
            self.skipTest("no review_bundle yet")
        with open(p, encoding="utf-8") as f:
            r = json.load(f)
        self.assertIn("v5_self_check_ok", r)
        self.assertIn("night_summary", r)

    def test_plan_bundle_signal_auto_generate_if_present(self):
        p = os.path.join(DATA, "plan_bundle.json")
        if not os.path.isfile(p):
            self.skipTest("no plan_bundle yet")
        with open(p, encoding="utf-8") as f:
            pjson = json.load(f)
        self.assertIn("signal_auto_generate", pjson)
        self.assertIsInstance(pjson["signal_auto_generate"], dict)


class TestSignalLoopQuota(unittest.TestCase):
    def test_get_daily_quota_structure(self):
        from signal_loop import get_daily_quota
        q = get_daily_quota()
        self.assertIn("global_limit", q)
        self.assertIn("tier_a", q)


if __name__ == "__main__":
    unittest.main()
