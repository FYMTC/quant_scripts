#!/config/quant_env/bin/python3
"""guard 绑定签名：操盘主账户切换触发热加载标记。"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard_account_bind as gb  # noqa: E402
import trade_accounts as ta  # noqa: E402


class TestGuardBindHotload(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        self._state = os.path.join(self._tmpdir, "trade_accounts_state.json")
        with open(
            os.path.join(os.path.dirname(__file__), "..", "trade_accounts.example.yaml"),
            encoding="utf-8",
        ) as f:
            yaml_content = f.read()
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write(yaml_content)
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state
        gb.STATE_PATH = self._state
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def test_signature_changes_on_primary_switch(self):
        s1 = gb.bind_signature()
        ta.start_hermes_trading("manual_wechat", set_primary=True)
        s2 = gb.bind_signature()
        self.assertNotEqual(s1[0], s2[0])
        self.assertEqual(s2[0], "manual_wechat")


if __name__ == "__main__":
    unittest.main()
