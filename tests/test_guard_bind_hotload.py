#!/config/quant_env/bin/python3
"""guard 绑定签名：操盘主账户切换触发热加载标记。"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard_account_bind as gb  # noqa: E402
import trade_accounts as ta  # noqa: E402
from unittest.mock import patch


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
        os.environ.pop("GUARD_ACCOUNT_ID", None)
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def test_signature_changes_on_primary_switch(self):
        s1 = gb.bind_signature()
        ta.start_hermes_trading("manual_wechat", set_primary=True)
        s2 = gb.bind_signature()
        self.assertNotEqual(s1[0], s2[0])
        self.assertEqual(s2[0], "manual_wechat")

    def test_load_guard_bundle_exposes_runtime_health(self):
        cfg_path = os.path.join(self._tmpdir, "guard_config.json")
        pos_path = os.path.join(self._tmpdir, "position_cache.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"monitored_codes": {"000001": "平安银行"}, "signals": []}, f)
        with open(pos_path, "w", encoding="utf-8") as f:
            json.dump({"positions": {"000063": {"name": "中兴通讯", "shares": 100}}, "cash": 1234}, f)

        with open(self._accounts, "a", encoding="utf-8") as f:
            f.write(
                "\nmanual_wechat:\n"
                "  name: Manual WeChat\n"
                f"  guard_config_path: {cfg_path}\n"
                f"  position_cache_path: {pos_path}\n"
                "  position_source: manual\n"
            )

        with patch.object(gb, "_paths_for_account", return_value={"guard_config": Path(cfg_path), "position_cache": Path(pos_path)}):
            with patch.object(ta, "get_account", return_value={"position_source": "manual"}):
                bundle = gb.load_guard_bundle("manual_wechat")
        runtime = bundle["config"]["runtime_health"]
        self.assertEqual(runtime["positions_count"], 1)
        self.assertEqual(runtime["watch_list_count"], 1)
        self.assertEqual(runtime["signals_count"], 0)
        self.assertIsNone(bundle["config"]["watch_list_original"])


if __name__ == "__main__":
    unittest.main()
