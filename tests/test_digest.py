#!python3
"""digest_app 摘要生成测试。"""

import json
import os
import sys
import tempfile
import unittest

HERMES_SCRIPTS = "/config/.hermes/scripts"
sys.path.insert(0, HERMES_SCRIPTS)

import digest_app as dg  # noqa: E402


class TestDigest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        dg.DATA = self._tmpdir

    def test_morning_digest_from_fixture(self):
        with open(os.path.join(self._tmpdir, "morning_output.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "recommendation": "HOLD",
                    "cash": 50000,
                    "total_assets": 120000,
                    "constraints": [{"pass": True}],
                    "candidates": [{"code": "000063"}],
                },
                f,
            )
        out = dg.morning_digest()
        self.assertIn("早计划", out["digest_text"])
        self.assertTrue(out["needs_hermes"])
        self.assertTrue(out.get("push_wechat_required"))
        self.assertIn("wechat_work_report_body", out)
        self.assertGreater(len(out["digest_text"]), 50)

    def test_morning_missing_file(self):
        out = dg.morning_digest()
        self.assertIn("error", out)


    def test_night_digest_from_fixture(self):
        with open(os.path.join(self._tmpdir, "night_output.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "recommendation": "HOLD",
                    "holdings": [{"code": "000063", "name": "中兴", "shares": 100, "price": 38}],
                    "pnl_summary": {"positions": 1, "total_pnl": 100, "market_value": 3800, "cost_basis": 3700},
                },
                f,
            )
        with open(os.path.join(self._tmpdir, "review_bundle.json"), "w", encoding="utf-8") as f:
            json.dump({
                "v5_self_check_ok": True,
                "night_summary": {"recommendation": "HOLD"},
                "account_runtime": {
                    "runtime_mode": "single_account_mode",
                    "desk_primary_account": "paper_easyths",
                    "primary_account": {"label": "EasyTHS 模拟盘（Hermes 自动执行）", "position_count": 1},
                    "special_mode": False,
                },
            }, f)
        out = dg.night_digest()
        self.assertIn("晚复盘", out["digest_text"])
        self.assertTrue(out["needs_hermes"])
        self.assertIn("wechat_work_report_body", out)


if __name__ == "__main__":
    unittest.main()
