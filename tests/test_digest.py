#!/config/quant_env/bin/python3
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
        self.assertLessEqual(len(out["digest_text"]), 400)

    def test_morning_missing_file(self):
        out = dg.morning_digest()
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
