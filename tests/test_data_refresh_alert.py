#!python3
"""data_refresh 失败时写紧急通道。"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERMES_SCRIPTS = "/config/.hermes/scripts"
sys.path.insert(0, HERMES_SCRIPTS)

import data_refresh_app as dr  # noqa: E402


class TestDataRefreshAlert(unittest.TestCase):
  def test_push_alert_writes_files(self):
    with tempfile.TemporaryDirectory() as td:
      sig = os.path.join(td, "sig.txt")
      alert = os.path.join(td, "alert.txt")
      with patch.object(dr, "EMERGENCY_SIGNAL", sig), patch.object(dr, "EMERGENCY_FILE", alert):
        dr._push_refresh_alert("flash", "boom")
      self.assertEqual(open(sig).read(), "REFRESH_FAIL")
      self.assertIn("flash", open(alert).read())


if __name__ == "__main__":
  unittest.main()
