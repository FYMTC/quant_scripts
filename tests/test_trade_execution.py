#!/config/quant_env/bin/python3
"""trade_execution：只允许 EasyTHS 自动执行链路。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_execution as te  # noqa: E402
import trade_accounts as ta  # noqa: E402


class TestTradeExecution(unittest.TestCase):
    @patch("trade_accounts.get_account")
    def test_execution_provider_rejects_non_easyths(self, get_account):
        get_account.return_value = {
            "account_id": "paper_easyths",
            "execution": {"provider": "none"},
        }
        with self.assertRaises(ta.HermesTradingError):
            ta.execution_provider("paper_easyths")

    @patch("ths_trade_executor.execute_from_outbox")
    @patch("trade_execution.easyths_config_path", return_value=Path("/tmp/mock.yaml"))
    @patch("trade_execution.execution_provider", return_value="easyths")
    def test_execute_request_uses_easyths_executor(self, _provider, _config_path, execute_from_outbox):
        execute_from_outbox.return_value = {"ok": True, "result": {"data": {}}}
        out = te.execute_request({"request_id": "req1", "account_id": "paper_easyths"})
        self.assertTrue(out["ok"])
        execute_from_outbox.assert_called_once_with("req1", record_kb=True, config_path=Path("/tmp/mock.yaml"))


if __name__ == "__main__":
    unittest.main()
