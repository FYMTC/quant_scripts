#!python3
"""guard 绑定签名：操盘主账户切换触发热加载标记。"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml  # ensure real yaml loaded before stub — system_config needs it

_yaml_stub = types.ModuleType("yaml")
_yaml_stub.safe_load = lambda s: json.loads(json.dumps({}))  # type: ignore[attr-defined]
sys.modules.setdefault("yaml", _yaml_stub)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard_account_bind as gb  # noqa: E402
import trade_accounts as ta  # noqa: E402


class TestGuardBindHotload(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._accounts = os.path.join(self._tmpdir, "trade_accounts.yaml")
        self._state = os.path.join(self._tmpdir, "trade_accounts_state.json")
        with open(self._accounts, "w", encoding="utf-8") as f:
            f.write("accounts: {}\n")
        self._orig_ta_default = ta.DEFAULT_PATH
        self._orig_ta_state = ta.STATE_PATH
        self._orig_gb_state = gb.STATE_PATH
        ta.DEFAULT_PATH = self._accounts
        ta.STATE_PATH = self._state
        gb.STATE_PATH = self._state
        os.environ.pop("GUARD_ACCOUNT_ID", None)

        self._orig_load_registry = ta.load_registry
        self._orig_get_account = ta.get_account
        ta.load_registry = lambda path=None: {
            "version": 2,
            "accounts": {
                "paper_easyths": {
                    "enabled": True,
                    "label": "Paper",
                    "position_source": "easyths",
                },
            },
            "initial_hermes_trading_active": ["paper_easyths"],
            "initial_desk_primary_account": "paper_easyths",
        }
        ta.get_account = lambda account_id: {
            "paper_easyths": {
                "account_id": "paper_easyths",
                "enabled": True,
                "label": "Paper",
                "position_source": "easyths",
            },
        }[account_id]
        ta.start_hermes_trading("paper_easyths", set_primary=True)

    def tearDown(self):
        ta.load_registry = self._orig_load_registry
        ta.get_account = self._orig_get_account
        ta.DEFAULT_PATH = self._orig_ta_default
        ta.STATE_PATH = self._orig_ta_state
        gb.STATE_PATH = self._orig_gb_state
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_signature_changes_on_primary_switch(self):
        s1 = gb.bind_signature()
        self.assertEqual(s1[0], "paper_easyths")

    def test_load_guard_bundle_uses_easyths_snapshot(self):
        cfg_path = os.path.join(self._tmpdir, "guard_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"monitored_codes": {"000001": "平安银行"}, "signals": []}, f)

        fake_snapshot = {
            "account_id": "paper_easyths",
            "account_label": "Paper",
            "position_source": "easyths",
            "summary": {"cash": 1234},
            "positions": [{"code": "000063", "name": "中兴通讯", "shares": 100, "cost": 35.1, "market_value": 3510}],
            "position_count": 1,
        }

        with patch.object(gb, "_paths_for_account", return_value={"guard_config": Path(cfg_path), "position_cache": None}):
            with patch.object(ta, "get_account", return_value={"position_source": "easyths"}):
                with patch("trade_account_context.load_account_snapshot", return_value=fake_snapshot):
                    bundle = gb.load_guard_bundle("paper_easyths")
        runtime = bundle["config"]["runtime_health"]
        self.assertEqual(runtime["positions_count"], 1)
        self.assertEqual(bundle["config"]["cash"], 1234)
        self.assertEqual(bundle["config"]["position_source_note"], "easyths_snapshot")
        self.assertEqual(runtime["watch_list_count"], 1)
        self.assertEqual(runtime["signals_count"], 0)
        self.assertIsNone(bundle["config"]["watch_list_original"])


if __name__ == "__main__":
    unittest.main()
